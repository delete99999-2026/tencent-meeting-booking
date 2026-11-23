# -*- coding: utf-8 -*-
"""真实驱动端到端测试：登录 → 建会 → 等 worker 真预订 → 校验真实会议号 → 删除（触发真取消）。

用法: python scripts\\live_test.py [主题]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import quote

BASE = "http://127.0.0.1:8000"


def req(method, path, token=None, body=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def main():
    subject = sys.argv[1] if len(sys.argv) > 1 else "自动测试会议"
    st, r = req("POST", "/api/login", body={"username": "admin", "password": "Admin@123456"})
    if st != 200:
        print("FAIL login:", st, r)
        sys.exit(1)
    token = r["token"]
    print("login ok, admin role =", r["user"]["role"])

    # 清理上一轮遗留（同主题、未取消的）
    st, r = req("GET", "/api/meetings?subject=" + quote(subject) + "&sort_key=created_at&sort_dir=desc", token=token)
    for m in r.get("items", []):
        if m["status"] in ("pending", "booked"):
            print("cleanup old meeting", m["id"], m["status"])
            req("DELETE", "/api/meetings/" + str(m["id"]), token=token)
            time.sleep(1)

    # 10 分钟后开始，30 分钟
    t0 = (datetime.now() + timedelta(minutes=10)).replace(second=0, microsecond=0)
    st, r = req("POST", "/api/meetings", token=token,
                body={"subject": subject, "start_at": t0.strftime("%Y-%m-%d %H:%M"), "duration_min": 30})
    if st != 201:
        print("FAIL create:", st, r)
        sys.exit(1)
    mid = r["id"]
    print("created meeting", mid, r["start_at"], "+30min")

    # 等 worker 预订（最长 90s）
    for i in range(45):
        time.sleep(2)
        st, r = req("GET", "/api/meetings/" + str(mid), token=token)
        if st != 200:
            continue
        m = r
        if m["status"] == "booked":
            ok_id = bool(m.get("tm_meeting_id")) and not m["tm_meeting_id"].startswith("9000")
            print(f"BOOKED tm_meeting_id={m.get('tm_meeting_id')} attempts={m['attempts']}")
            if ok_id:
                print("PASS 真实会议号")
            else:
                print("WARN 会议号仍像假号:", m.get("tm_meeting_id"))
            break
        if m["status"] == "failed":
            print("FAIL booking:", m.get("tm_error"))
            sys.exit(1)
    else:
        print("TIMEOUT waiting for booking")
        sys.exit(1)

    # 删除 → 触发真取消
    st, r = req("DELETE", "/api/meetings/" + str(mid), token=token)
    print("delete:", st, r)
    time.sleep(3)
    st, r = req("GET", "/api/meetings?subject=" + quote(subject) + "&sort_key=created_at&sort_dir=desc&limit=5", token=token)
    for m in r.get("items", []):
        if m["id"] == mid:
            print("final status =", m["status"], "tm_meeting_id =", m.get("tm_meeting_id"))


if __name__ == "__main__":
    main()
