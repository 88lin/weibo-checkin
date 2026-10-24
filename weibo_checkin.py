#!/usr/bin/env python3
"""weibo-checkin —— 微博超话每日自动签到 + 评论。

作者：88lin
仓库：https://github.com/88lin/weibo-checkin
协议：MIT

超话签到/评论接口系从 weibo.com 网页版逆向所得，服务端改一版就可能失效——
修复都会推到上面这个仓库。顺手点个 ⭐ Star，等哪天签到莫名断了，你能一秒把它找回来。

接口契约（实测于 2026-10，均为 weibo.com 网页版，需登录态 Cookie）：

  签到  POST https://weibo.com/ajax_proxy/chaohua/page/checkin
        body(form): scene_id=pc_checkin & page_id=<超话100808id> & timezone & lang & plat & ua
        成功        {"code":100000,"title":"今日签到 第N名","sub_title":"经验值+N"}
        幂等已签    {"code":382004,"msg":"今天已签到"}   ← 按"已签"处理，不计失败
        其他 code   业务失败（403/缺参等）
  评论  POST https://weibo.com/ajax/comments/create
        body(form): id=<帖子mid> & comment=<内容> & pic_id= & is_repost=0 & comment_ori=0 & is_comment=0
        主楼成功    {"ok":1,"data":{...}}
        楼中楼     同上接口，另加 rootid=<目标评论cid> & uid=<帖主uid>
        楼中楼成功  {"ok":1,"data":{"floor_number":N,...}}  ← N 为在目标楼内的楼层号
  评论列表 GET https://weibo.com/ajax/statuses/buildComments?is_reload=1&id=<帖子mid>
        &is_show_bulletin=3&is_mix=0&count=10&fetch_level=0&locale=en-US
        返回 data[]: [{id:评论cid, text_raw, user:{id}, floor_number}]（楼中楼目标从这里取）
        失败        {"ok":0,"msg":"..."} 或 HTTP 403（缺 X-Xsrf-Token 时）
  超话解析  GET https://s.weibo.com/weibo?q=<名字>超话
        从 HTML 里 regex 100808[0-9a-f]{28,} 提取，按上下文文字匹配确认是目标超话
  最新帖  GET https://weibo.com/ajax_proxy/chaohua/page?flowId=<超话id>
        返回 {"items":[{"type":"feed","data":{"mid":...,"text":...}}, ...]}

关键风控点：
  - 所有请求必须带 X-Xsrf-Token 头（取自 Cookie 里的 XSRF-TOKEN），否则评论 403
  - Referer 必须是 weibo.com 自身
  - Cookie 里的 ALF 字段是 unix 过期时间戳，脚本据此做体检

登录态只认 Cookie，脚本不碰账号密码，任何模式下都不打印 Cookie 内容，日志可安全分享。

用法：
  python weibo_checkin.py run               # 签到 + 评论（cron/定时任务用）
  python weibo_checkin.py run --no-comment  # 只签到不评论
  python weibo_checkin.py run --dry-run     # 演练：走完整个流程但不真正签到/评论
  python weibo_checkin.py silent            # 同 run，结果写 weibo.log 不刷 stdout（静默运行）
  python weibo_checkin.py check             # 体检：Cookie 有效期 + 超话解析预览，不做任何写操作
  python weibo_checkin.py status            # 看最近 14 天成功率与全胜连签
  python weibo_checkin.py topics            # 列出已配置的超话（含 id 缓存）
  python weibo_checkin.py topics 加 小米    # 添加超话（写回 config.json）
  python weibo_checkin.py topics 删 小米    # 移除超话

配置：同目录 config.json（首次用 cp config.example.json config.json）
状态：.state.json 缓存超话 id 与自己的 uid（可随时删，删后自动重建）
通知：config.json 里配 webhook 可推 Server酱 / Bark / 企业微信机器人（可选）
"""
from __future__ import annotations

import json
import platform
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
LOG_PATH = BASE / "weibo.log"
STATE_PATH = BASE / ".state.json"   # 运行时状态缓存（自己的 uid 等），可随时删
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "cookie": "",
    "supertopics": [],
    "comments": [],
    # ---- 评论模式（可选）----
    "comment_count": 1,      # 每个超话发几条评论（1=主流楼1条；>1 会分布到不同楼层，防风控建议 ≤5）
    "reply_mode": "new",     # new=主楼评论 | reply=楼中楼回复已有评论 | mixed=主楼+楼中楼混合
    # ---- 可选项（有默认值）----
    "delay_min": 5,          # 两次请求间随机间隔下限（秒）
    "delay_max": 15,         # 上限
    "jitter_minutes": 20,    # 启动前随机延迟 0~N 分钟（错开整点防指纹）；0 关闭
    "retries": 3,            # 网络错误重试次数
    "retry_backoff": 4,      # 重试退避基数（秒），指数：4s, 16s, 64s...
    "burst_pause_chance": 0.15,  # 防风控：每次发评论有 N 概率额外"喝水"停顿（2~5s）
    "notify": {              # 通用 webhook（可选）：Server酱/Bark/企微机器人均可
        "enabled": False,
        "url": "",           # POST JSON: {"title": ..., "desp"/"content": ...}
    },
}


# ---------------------------------------------------------------- utilities

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit("找不到 config.json —— 先 `cp config.example.json config.json` 并填入 Cookie")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    if not cfg.get("cookie") or "粘贴" in cfg["cookie"]:
        sys.exit("config.json 里还没填有效 Cookie（浏览器登录 weibo.com 后 F12 复制）")
    # 配置合法性校验：坏值早报错，不带到运行中
    if cfg.get("reply_mode") not in ("new", "reply", "mixed"):
        sys.exit(f"reply_mode 只能是 new/reply/mixed，当前: {cfg.get('reply_mode')!r}")
    try:
        n = int(cfg.get("comment_count", 1))
        assert n >= 1
        cfg["comment_count"] = n
    except (TypeError, ValueError, AssertionError):
        sys.exit("comment_count 必须是 ≥1 的整数")
    if cfg.get("delay_min", 5) > cfg.get("delay_max", 15):
        sys.exit("delay_min 不能大于 delay_max")
    return cfg


CFG = load_config()


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


STATE = load_state()


class Log:
    """控制台 + 文件双写；绝不落 Cookie。silent 模式只写文件。日志超 512KB 自动轮转。"""

    MAX_BYTES = 512 * 1024

    def __init__(self, silent: bool):
        self.silent = silent
        self.lines: list[str] = []

    def __call__(self, msg: str):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        self.lines.append(line)
        if not self.silent:
            print(line, flush=True)

    def flush(self):
        if not self.lines:
            return
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > self.MAX_BYTES:
                rot = LOG_PATH.with_suffix(".log.1")
                if rot.exists():
                    rot.unlink()
                LOG_PATH.rename(rot)
        except OSError:
            pass
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")


log = Log(False)


def sleep_jitter(seconds: float):
    log(f"… 随机间隔 {seconds:.0f}s（防风控）")
    time.sleep(seconds)


# ---------------------------------------------------------------- http core

def http(url: str, data: dict | None = None, referer: str = "https://weibo.com/") -> tuple[int, str]:
    """带重试与退避的请求。返回 (status, body)。HTTP 4xx/5xx 不抛异常——
    微博很多业务响应挂在 4xx 里（如缺 X-Xsrf-Token 时评论 403），必须把 body 带回去解析。"""
    xsrf = ""
    for kv in CFG["cookie"].split(";"):
        k, _, v = kv.strip().partition("=")
        if k == "XSRF-TOKEN":
            xsrf = v
    headers = {
        "User-Agent": UA,
        "Cookie": CFG["cookie"],
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    if xsrf:
        headers["X-Xsrf-Token"] = xsrf
    body = urllib.parse.urlencode(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    retries = int(CFG.get("retries", 3))
    backoff = float(CFG.get("retry_backoff", 4))
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25, context=ssl.create_default_context()) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # DNS/超时/SSL 等网络层错误 → 指数退避重试
            last_err = e
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                log(f"  网络错误（{e}），{wait:.0f}s 后重试 {attempt + 2}/{retries}")
                time.sleep(wait)
    raise RuntimeError(f"网络请求连续失败 {retries} 次: {url} ({last_err})")


def cookie_field(name: str) -> str:
    for kv in CFG["cookie"].split(";"):
        k, _, v = kv.strip().partition("=")
        if k == name:
            return v
    return ""


# ---------------------------------------------------------------- weibo api

def cookie_expiry() -> datetime | None:
    """ALF cookie 是登录态过期 unix 时间戳。"""
    raw = cookie_field("ALF")     # ALF 形如 02_1794593469
    m = re.search(r"(\d{10})", raw)
    if m and int(m.group(1)) > 10 ** 9:
        return datetime.fromtimestamp(int(m.group(1)))
    return None


def check_cookie() -> bool:
    """真实调一个需登录的私有接口验证登录态。失效形态：HTTP 200 + ok:-100 + login url。"""
    status, body = http("https://weibo.com/ajax/setting/getPrivacy")
    try:
        d = json.loads(body)
    except ValueError:
        return False
    if status == 200 and d.get("ok") == -100:
        return False   # 服务端已把会话踢掉（可能早于 ALF 过期）
    return status == 200 and d.get("data", {}).get("privacy") is not None


def find_topic(name: str) -> str | None:
    """s.weibo.com 搜索解析超话 containerid；按上下文文字确认。
    结果缓存到 .state.json（超话 id 基本不变），失效时自动重查一次。"""
    cached = (STATE.get("topics") or {}).get(name)
    if cached:
        return cached
    cid = _search_topic_once(name)
    if cid:
        STATE.setdefault("topics", {})[name] = cid
        save_state(STATE)
    return cid


def _search_topic_once(name: str) -> str | None:
    q = urllib.parse.quote(f"{name}超话")
    status, body = http(f"https://s.weibo.com/weibo?q={q}", referer="https://s.weibo.com/")
    if status != 200:
        log(f"  搜索页 HTTP {status}，可能触发风控")
        return None
    if "login.sina.com.cn" in body or "retcode=6102" in body:
        log("  搜索被重定向到登录页 → Cookie 已失效")
        return None
    ids = list(dict.fromkeys(re.findall(r"100808[0-9a-f]{28,}", body)))
    if not ids:
        return None
    for cid in ids:
        i = body.find(cid)
        ctx = re.sub(r"<[^>]+>", "", body[max(0, i - 300):i])
        if f"{name}超话" in ctx or f"#{name}#" in ctx:
            return cid
    log(f"  上下文未匹配到「{name}超话」，退回第一个候选（可用 check 命令人工确认）")
    return ids[0]


def checkin(page_id: str, dry: bool = False) -> str:
    if dry:
        return "演练：将签到（未执行）"
    _, body = http("https://weibo.com/ajax_proxy/chaohua/page/checkin", data={
        "scene_id": "pc_checkin",
        "page_id": page_id,
        "timezone": "Asia/Shanghai",
        "lang": "zh-cn",
        "plat": platform.platform(),
        "ua": UA,
    })
    try:
        d = json.loads(body)
    except ValueError:
        return f"签到响应异常: {body[:80]}"
    if d.get("code") == 100000:
        return f"签到成功（{d.get('title', '')}，{d.get('sub_title', '')}）"
    if d.get("code") == 382004:
        return "今日已签"          # 幂等：不算失败
    return f"签到失败 code={d.get('code')} {d.get('msg', '')}"


def latest_post(page_id: str) -> tuple[str, str, str] | None:
    """返回 (mid, 内容摘要, 帖主uid)。取超话最新一条 feed 帖。"""
    _, body = http(f"https://weibo.com/ajax_proxy/chaohua/page?flowId={page_id}")
    try:
        d = json.loads(body)
    except ValueError:
        return None
    for it in d.get("items") or (d.get("data") or {}).get("items") or []:
        fd = it.get("data") or {}
        mid = fd.get("mid") or fd.get("id")
        if mid and str(mid).isdigit() and len(str(mid)) >= 15:
            text = re.sub(r"\s+", " ", fd.get("text_raw") or fd.get("text") or "")[:40]
            uid = str((fd.get("user") or {}).get("id") or "")
            return str(mid), text, uid
    return None


def list_comments(mid: str, count: int = 20) -> list[dict]:
    """拉帖子的评论列表（fetch_level=0 主楼）。返回 [{id, text_raw, user_id, floor}] 按接口顺序。"""
    status, body = http(
        "https://weibo.com/ajax/statuses/buildComments?is_reload=1"
        f"&id={mid}&is_show_bulletin=3&is_mix=0&count={count}&fetch_level=0&locale=en-US")
    try:
        d = json.loads(body)
    except ValueError:
        return []
    out = []
    for c in d.get("data") or []:
        if c.get("id"):
            out.append({
                "id": str(c["id"]),
                "text": (c.get("text_raw") or "")[:40],
                "user_id": str((c.get("user") or {}).get("id") or ""),
                "floor": c.get("floor_number") or 0,
            })
    return out


def comment(mid: str, text: str, dry: bool = False, reply_to: str | None = None,
            author_uid: str = "") -> str:
    """发评论；reply_to 给目标评论 cid 时为楼中楼回复（rootid 指向它）。"""
    if dry:
        mode = f"楼中楼回复 {reply_to}" if reply_to else "主楼评论"
        return f"演练：将{mode}「{text}」（未执行）"
    payload = {"id": mid, "comment": text, "pic_id": "",
               "is_repost": "0", "comment_ori": "0", "is_comment": "0", "fp": ""}
    if reply_to:
        payload["rootid"] = reply_to
    if author_uid:
        payload["uid"] = author_uid
    status, body = http("https://weibo.com/ajax/comments/create", data=payload)
    try:
        d = json.loads(body)
    except ValueError:
        return f"评论响应异常 HTTP {status}: {body[:60]}"
    if d.get("ok") == 1:
        # 首次评论成功后缓存自己的 uid（防自回复用）
        my = str((d.get("data", {}).get("user") or {}).get("id") or "")
        if my and STATE.get("my_uid") != my:
            STATE["my_uid"] = my
            save_state(STATE)
        where = f"楼中楼#{d.get('data', {}).get('floor_number', '?')}" if reply_to else "主楼"
        return f"评论成功[{where}]「{text}」"
    if d.get("ok") == -100:
        return "评论失败: 登录态失效（ok=-100），请更新 Cookie"
    return f"评论失败 ok={d.get('ok')} {str(d.get('msg', ''))[:60]}"


# ---------------------------------------------------------------- notify

def notify(title: str, content: str):
    n = CFG.get("notify") or {}
    if not n.get("enabled") or not n.get("url"):
        return
    payload = json.dumps({"title": title, "desp": content, "content": content}).encode()
    req = urllib.request.Request(n["url"], data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("通知已推送")
    except Exception as e:
        log(f"通知推送失败: {e}")


# ---------------------------------------------------------------- commands

def cmd_run(silent: bool = False, dry: bool = False, do_comment: bool | None = None) -> int:
    global log
    log = Log(silent)
    exit_code = 0

    # 1) Cookie 体检：过期直接告警退出，避免每天空跑
    expiry = cookie_expiry()
    if expiry:
        days = (expiry - datetime.now()).days
        if days < 0:
            log("✗ Cookie 已过期，请重新抓取（浏览器登录 weibo.com → F12 → 复制 Cookie）")
            notify("weibo-checkin 失败", "Cookie 已过期，需要更新")
            log.flush()
            return 2
        if days <= 7:
            log(f"⚠ Cookie 将于 {expiry:%m-%d} 过期（还剩 {days} 天），建议提前换新")

    # 2) 启动抖动：错开整点，避免每天同一秒打点
    jitter = float(CFG.get("jitter_minutes", 0))
    if jitter > 0 and not dry:
        wait = random.uniform(0, jitter * 60)
        log(f"启动抖动 {wait / 60:.1f} 分钟…")
        time.sleep(wait)

    # 3) 登录态确认（dry-run 也做——它是只读接口，无副作用；只跳过写操作）
    if not check_cookie():
        log("✗ Cookie 登录态无效（服务端已踢会话，可能早于 ALF 过期），请更新")
        notify("weibo-checkin 失败", "Cookie 登录态无效（ok=-100），请重新抓 Cookie")
        log.flush()
        return 2
    if dry:
        log("（dry-run：登录态 ✓，写操作全部跳过）")

    if do_comment is None:
        do_comment = bool(CFG.get("comments"))

    # 4) 随机顺序处理各超话，失败隔离（任何单超话的异常不中断整体）
    topics = list(CFG.get("supertopics") or [])
    random.shuffle(topics)
    summary, failures = [], 0
    d_min = float(CFG.get("delay_min", 5))
    d_max = float(CFG.get("delay_max", 15))

    for idx, name in enumerate(topics):
        if idx:
            sleep_jitter(random.uniform(d_min, d_max))
        log(f"▶ 超话「{name}」")
        try:
            cid = find_topic(name)
            if not cid:
                summary.append(f"{name}: 未解析到超话")
                failures += 1
                continue
            sline = checkin(cid, dry)
            log(f"  {sline}")
            if "失败" in sline:
                failures += 1

            cline = ""
            if do_comment and CFG.get("comments"):
                sleep_jitter(random.uniform(max(d_min, 3), max(d_max, 8)))
                post = latest_post(cid)
                if post:
                    mid, preview, author_uid = post
                    log(f"  最新帖: {preview}…")
                    texts = random.sample(CFG["comments"],
                                          min(len(CFG["comments"]), int(CFG.get("comment_count", 1))))
                    mode = CFG.get("reply_mode", "new")
                    # 楼中楼模式先拉评论列表，排除自己的评论（uid 缓存于 .state.json，首评后可用）
                    targets = None
                    if mode in ("reply", "mixed") and not dry:
                        my_uid = STATE.get("my_uid", "")
                        all_c = list_comments(mid, 20)
                        targets = [c for c in all_c if not my_uid or c["user_id"] != my_uid]
                        if not targets and all_c:
                            log("  ⚠ 无法排除自己的评论（uid 未缓存），回退主楼评论")
                    results = []
                    for i, text in enumerate(texts):
                        reply_to = None
                        if mode == "reply" or (mode == "mixed" and i % 2 == 1):
                            if targets:
                                # 随机挑一条目标评论做楼中楼（评论数比发帖数多时尽量不重复）
                                t = random.choice(targets)
                                targets.remove(t)
                                reply_to = t["id"]
                        rline = comment(mid, text, dry, reply_to=reply_to, author_uid=author_uid)
                        log(f"  {rline}")
                        results.append(rline)
                        if "失败" in rline:
                            failures += 1
                        # 多条评论时逐条拉开间隔；小概率"喝水"停顿
                        if i < len(texts) - 1:
                            sleep_jitter(random.uniform(max(d_min, 8), max(d_max, 20)))
                            if random.random() < float(CFG.get("burst_pause_chance", 0.15)):
                                pause = random.uniform(2, 5)
                                log(f"  … 随机喝水停顿 {pause:.0f}s")
                                time.sleep(pause)
                    cline = "；".join(r.split("（")[0] for r in results)
                else:
                    log("  未取到最新帖，跳过评论")
            summary.append(f"{name}: {sline.split('（')[0]}{'；' + cline if cline else ''}")
        except Exception as e:   # 网络崩/解析崩都算这个超话失败，继续下一个
            failures += 1
            summary.append(f"{name}: 异常 {type(e).__name__}: {str(e)[:80]}")
            log(f"  ✗ 异常: {str(e)[:120]}")

    ok = len(topics) - failures
    log(f"＝ 完成 {ok}/{len(topics)}，失败 {failures}")
    if failures:
        exit_code = 1
        notify("weibo-checkin 有失败", "\n".join(summary))
    log.flush()
    return exit_code


def cmd_check():
    """只读体检：Cookie 有效性 + 各超话解析预览。不签到不评论。"""
    expiry = cookie_expiry()
    if expiry:
        print(f"Cookie 过期时间: {expiry:%Y-%m-%d %H:%M}（剩 {(expiry - datetime.now()).days} 天）")
    else:
        print("Cookie 过期时间: 未知（Cookie 里没有 ALF 字段）")
    print("登录态验证…", "✓ 有效" if check_cookie() else "✗ 无效/过期，请重新抓 Cookie")
    for name in CFG.get("supertopics", []):
        cid = find_topic(name)
        print(f"超话「{name}」: {cid if cid else '✗ 未解析到'}")
        time.sleep(random.uniform(3, 6))
    print("check 完成（本次未做任何写操作）")


def cmd_status():
    """解析 weibo.log，给最近 14 天的签到成功率与全胜连签。"""
    if not LOG_PATH.exists():
        print("还没有日志")
        return
    days: dict[str, dict[str, int]] = {}
    for line in LOG_PATH.read_text("utf-8").splitlines():
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})[^\]]*\] ＝ 完成 (\d+)/(\d+)", line)
        if m:
            d, ok, total = m.group(1), int(m.group(2)), int(m.group(3))
            days.setdefault(d, {"ok": 0, "total": 0})
            days[d]["ok"] += ok
            days[d]["total"] += total
    if not days:
        print("日志里还没有统计行")
        return
    print(f"{'日期':<12}{'成功/总数'}")
    for d in sorted(days)[-14:]:
        s = days[d]
        mark = "✓" if s["ok"] == s["total"] else ("✗" if s["ok"] == 0 else "△")
        print(f"{d:<12}{s['ok']}/{s['total']} {mark}")
    streak = 0
    for d in sorted(days, reverse=True):
        if days[d]["total"] and days[d]["ok"] == days[d]["total"]:
            streak += 1
        else:
            break
    print(f"全胜连签: {streak} 天")


def cmd_topics():
    """列出/管理超话：无参数列出当前配置；`topics 加 数码` 添加；`topics 删 数码` 移除。"""
    import sys as _s
    args = _s.argv[2:]
    cfg_path = CONFIG_PATH
    cfg = json.loads(cfg_path.read_text("utf-8"))
    topics = cfg.get("supertopics") or []
    if not args:
        print(f"当前超话（{len(topics)} 个）：")
        for t in topics:
            cid = (STATE.get("topics") or {}).get(t, "（未缓存，运行时自动解析）")
            print(f"  - {t}: {cid}")
        return
    op, name = args[0], " ".join(args[1:])
    if op == "加" and name:
        if name in topics:
            print(f"「{name}」已在列表")
            return
        topics.append(name)
        cfg["supertopics"] = topics
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        print(f"✓ 已添加「{name}」并写回 config.json")
    elif op == "删" and name:
        if name not in topics:
            print(f"「{name}」不在列表")
            return
        topics.remove(name)
        cfg["supertopics"] = topics
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        (STATE.get("topics") or {}).pop(name, None)
        save_state(STATE)
        print(f"✓ 已移除「{name}」")
    else:
        print("用法: topics          列出\n      topics 加 超话名  添加\n      topics 删 超话名  移除")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        return cmd_run(do_comment="--no-comment" not in argv, dry="--dry-run" in argv)
    if cmd == "silent":
        return cmd_run(silent=True, do_comment="--no-comment" not in argv)
    if cmd == "check":
        cmd_check()
        return 0
    if cmd == "status":
        cmd_status()
        return 0
    if cmd == "topics":
        cmd_topics()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
