#!/usr/bin/env python3
"""微博超话每日签到 + 自动评论（网页版接口，纯标准库）。

用法:
  python3 weibo_checkin.py run       # 签到 + 评论（cron 用这个）
  python3 weibo_checkin.py run --no-comment
配置: 同目录 config.json  (cookie / supertopics / comments)
日志: log.txt
"""
import json
import platform
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text("utf-8"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def http(url, data=None, referer="https://weibo.com/"):
    xsrf = ""
    for kv in CFG["cookie"].split("; "):
        if kv.startswith("XSRF-TOKEN="):
            xsrf = kv.split("=", 1)[1]
    headers = {
        "User-Agent": UA,
        "Cookie": CFG["cookie"],
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "X-Xsrf-Token": xsrf,
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.read().decode("utf-8", "replace")  # 4xx 也可能是业务响应
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def find_topic(name):
    """在 s.weibo.com 搜索结果里找名为 name 的超话 containerid"""
    q = urllib.parse.quote(f"{name}超话")
    body = http("https://s.weibo.com/weibo?q=" + q,
                referer="https://s.weibo.com/")
    ids = list(dict.fromkeys(re.findall(r"100808[0-9a-f]{28,}", body)))
    for cid in ids:
        i = body.find(cid)
        ctx = re.sub(r"<[^>]+>", "", body[max(0, i - 300):i])
        if f"{name}超话" in ctx or f"#{name}#" in ctx:
            return cid
    return ids[0] if ids else None


def checkin(page_id):
    data = urllib.parse.urlencode({
        "scene_id": "pc_checkin",
        "page_id": page_id,
        "timezone": "Asia/Shanghai",
        "lang": "zh-cn",
        "plat": platform.platform(),
        "ua": UA,
    }).encode()
    body = http("https://weibo.com/ajax_proxy/chaohua/page/checkin", data=data)
    d = json.loads(body)
    if d.get("code") == 100000:
        return f"签到成功({d.get('title','')},{d.get('sub_title','')})"
    if d.get("code") == 382004:
        return "今日已签"
    return f"签到失败 {d.get('code')} {d.get('msg')}"


def get_latest_post(page_id):
    """从超话页数据接口拿最新一条帖子的 mid"""
    body = http(f"https://weibo.com/ajax_proxy/chaohua/page?flowId={page_id}")
    try:
        d = json.loads(body)
    except ValueError:
        return None
    items = d.get("items") or (d.get("data") or {}).get("items") or []
    for it in items:
        fd = it.get("data") or {}
        mid = fd.get("mid") or fd.get("id")
        if mid and str(mid).isdigit() and len(str(mid)) >= 15:
            return str(mid)
    return None


def comment(mblog_id, text):
    payload = {"id": mblog_id, "comment": text, "pic_id": "",
               "is_repost": "0", "comment_ori": "0", "is_comment": "0", "fp": ""}
    data = urllib.parse.urlencode(payload).encode()
    body = http("https://weibo.com/ajax/comments/create", data=data)
    try:
        d = json.loads(body)
    except ValueError:
        return f"评论异常: {body[:80]}"
    ok = d.get("ok") == 1
    return f"评论{'成功' if ok else '失败'} {d.get('ok')} {str(d.get('msg',''))[:60]}"


def run(do_comment=True):
    results = []
    for name in CFG["supertopics"]:
        cid = find_topic(name)
        if not cid:
            results.append(f"{name}: 未找到超话")
            continue
        line = f"{name}({cid[:16]}…): {checkin(cid)}"
        if do_comment and CFG.get("comments"):
            mid = get_latest_post(cid)
            if mid:
                text = random.choice(CFG["comments"])
                time.sleep(random.uniform(3, 8))
                line += " | " + comment(mid, text)
            else:
                line += " | 未取到帖子，跳过评论"
        results.append(line)
        time.sleep(random.uniform(5, 12))
    line = f"[{date.today()}] " + " ‖ ".join(results)
    print(line)
    with (BASE / "log.txt").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    if not CFG.get("cookie"):
        print("请先把微博 Cookie 填进 config.json")
        sys.exit(1)
    run(do_comment="--no-comment" not in sys.argv)
