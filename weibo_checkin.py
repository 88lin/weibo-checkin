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
        成功        {"ok":1,"data":{...}}
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

配置：同目录 config.json（首次用 cp config.example.json config.json）
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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "cookie": "",
    "supertopics": [],
    "comments": [],
    # ---- 可选项（有默认值）----
    "delay_min": 5,          # 两次请求间随机间隔下限（秒）
    "delay_max": 15,         # 上限
    "jitter_minutes": 20,    # 启动前随机延迟 0~N 分钟（错开整点防指纹）；0 关闭
    "retries": 3,            # 网络错误重试次数
    "retry_backoff": 4,      # 重试退避基数（秒），指数：4s, 16s, 64s...
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
    return cfg


CFG = load_config()


class Log:
    """控制台 + 文件双写；绝不落 Cookie。silent 模式只写文件。"""

    def __init__(self, silent: bool):
        self.silent = silent
        self.lines: list[str] = []

    def __call__(self, msg: str):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        self.lines.append(line)
        if not self.silent:
            print(line, flush=True)

    def flush(self):
        if self.lines:
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
    """真实调一个需登录的私有接口验证登录态。"""
    status, body = http("https://weibo.com/ajax/setting/getPrivacy")
    try:
        return status == 200 and json.loads(body).get("data", {}).get("privacy") is not None
    except ValueError:
        return False


def find_topic(name: str) -> str | None:
    """s.weibo.com 搜索解析超话 containerid；按上下文文字确认是目标超话。"""
    q = urllib.parse.quote(f"{name}超话")
    status, body = http(f"https://s.weibo.com/weibo?q={q}", referer="https://s.weibo.com/")
    if status != 200:
        log(f"  搜索页 HTTP {status}，可能触发风控")
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


def latest_post(page_id: str) -> tuple[str, str] | None:
    """返回 (mid, 内容摘要)。取超话最新一条 feed 帖。"""
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
            return str(mid), text
    return None


def comment(mid: str, text: str, dry: bool = False) -> str:
    if dry:
        return f"演练：将评论「{text}」（未执行）"
    status, body = http("https://weibo.com/ajax/comments/create", data={
        "id": mid, "comment": text, "pic_id": "",
        "is_repost": "0", "comment_ori": "0", "is_comment": "0", "fp": "",
    })
    try:
        d = json.loads(body)
    except ValueError:
        return f"评论响应异常 HTTP {status}: {body[:60]}"
    if d.get("ok") == 1:
        return f"评论成功「{text}」"
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

    # 3) 登录态确认
    if not dry:
        if not check_cookie():
            log("✗ Cookie 登录态无效（接口返回未登录），请更新")
            notify("weibo-checkin 失败", "Cookie 登录态无效")
            log.flush()
            return 2
    else:
        log("（dry-run：跳过登录态验证与写操作）")

    if do_comment is None:
        do_comment = bool(CFG.get("comments"))

    # 4) 随机顺序处理各超话，失败隔离
    topics = list(CFG.get("supertopics") or [])
    random.shuffle(topics)
    summary, failures = [], 0
    d_min = float(CFG.get("delay_min", 5))
    d_max = float(CFG.get("delay_max", 15))

    for idx, name in enumerate(topics):
        if idx:
            sleep_jitter(random.uniform(d_min, d_max))
        log(f"▶ 超话「{name}」")
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
        if do_comment:
            sleep_jitter(random.uniform(max(d_min, 3), max(d_max, 8)))
            post = latest_post(cid)
            if post:
                mid, preview = post
                log(f"  最新帖: {preview}…")
                text = random.choice(CFG["comments"])
                cline = comment(mid, text, dry)
                log(f"  {cline}")
                if "失败" in cline:
                    failures += 1
            else:
                log("  未取到最新帖，跳过评论")
        summary.append(f"{name}: {sline.split('（')[0]}{'；' + cline.split('（')[0] if cline else ''}")

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
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
