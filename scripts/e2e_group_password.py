# -*- coding: utf-8 -*-
"""端到端 HTTP 验证：群组 / 会议密码 / 复制邀请文本。

前置：服务已在 http://127.0.0.1:8000 运行（python -m server.app）。
用法：python scripts\e2e_group_password.py
"""
import json
import random
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8000"
OK = 0
BAD = 0


def call(method, path, token=None, body=None):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print("[PASS] %s" % name)
    else:
        BAD += 1
        print("[FAIL] %s %s" % (name, extra))


def login(username, password):
    st, r = call("POST", "/api/login", body={"username": username, "password": password})
    if st != 200:
        return None, r
    return r.get("token"), r


def main():
    tag = str(random.randint(1000, 9999))
    grp = "E2E组" + tag

    admin_tok, r = login("admin", "Admin@123456")
    check("admin 登录", admin_tok is not None, str(r))
    if not admin_tok:
        return 1

    # 建两个同组用户 + 一个不同组用户
    users = {}
    for name in ("ga" + tag, "gb" + tag, "gc" + tag):
        grp_name = grp if name != "gc" + tag else "其他组" + tag
        st, r = call("POST", "/api/users", admin_tok,
                     {"username": name, "display_name": name.upper(),
                      "role": "user", "password": "Pw@" + tag + "aa",
                      "group_name": grp_name})
        check("创建用户 %s（组=%s）" % (name, grp_name), st == 201, str(r))
        users[name] = {"password": "Pw@" + tag + "aa", "group": grp_name}

    toks = {}
    for name, u in users.items():
        t, r = login(name, u["password"])
        check("用户 %s 登录" % name, t is not None, str(r))
        toks[name] = t

    # ga 建一个带密码的会议
    st, r = call("POST", "/api/meetings", toks["ga" + tag],
                 {"subject": "密码会议" + tag, "start_at": "2027-06-01 10:00",
                  "duration_min": 60, "meeting_password": "135790"})
    check("ga 创建带密码会议 201", st == 201, str(r))
    mid = r.get("id") if st == 201 else None

    # 密码格式校验
    st, r = call("POST", "/api/meetings", toks["ga" + tag],
                 {"subject": "坏密码" + tag, "start_at": "2027-06-02 10:00",
                  "duration_min": 60, "meeting_password": "12345"})
    check("密码 5 位被拒 400", st == 400, str(r))
    st, r = call("POST", "/api/meetings", toks["ga" + tag],
                 {"subject": "坏密码2" + tag, "start_at": "2027-06-02 10:00",
                  "duration_min": 60, "meeting_password": "12a456"})
    check("密码含字母被拒 400", st == 400, str(r))

    if mid:
        # 创建者可见密码
        st, r = call("GET", "/api/meetings/%d/secret" % mid, toks["ga" + tag])
        check("创建者可读 secret 200", st == 200, str(r))
        check("secret 含密码 135790", str(r.get("meeting_password")) == "135790", str(r))

        # 同组可见
        st, r = call("GET", "/api/meetings/%d/secret" % mid, toks["gb" + tag])
        check("同组用户可读 secret 200", st == 200, str(r))

        # 非同组不可见
        st, r = call("GET", "/api/meetings/%d/secret" % mid, toks["gc" + tag])
        check("非同组读 secret 403", st == 403, str(r))

        # 管理员可见
        st, r = call("GET", "/api/meetings/%d/secret" % mid, admin_tok)
        check("管理员可读 secret 200", st == 200, str(r))

        # 列表里非同组密码应被脱敏
        st, r = call("GET", "/api/meetings?subject=" + quote("密码会议" + tag), toks["gc" + tag])
        items = r.get("items", [])
        row = next((x for x in items if x.get("id") == mid), None)
        check("非同组在列表中能看到会议", row is not None, str(items)[:200])
        if row is not None:
            check("非同组列表中密码为空", not row.get("meeting_password"),
                  "password=%r" % row.get("meeting_password"))

        # 复制邀请：创建者 / 同组 / 管理员 / 非同组
        for who, tok, want in (("创建者", toks["ga" + tag], 200),
                               ("同组", toks["gb" + tag], 200),
                               ("管理员", admin_tok, 200),
                               ("非同组", toks["gc" + tag], 403)):
            st, r = call("POST", "/api/meetings/%d/copy-invite" % mid, tok)
            check("复制邀请-%s 期望 %d" % (who, want), st == want, str(r))
            if st == 200:
                text = r.get("text", "")
                check("邀请文本含 GMT+08（%s）" % who, "GMT+08" in text, text[:120])
                check("邀请文本含密码（%s）" % who, "135790" in text, text[:200])
                check("邀请文本含微信口令 #腾讯会议：（%s）" % who, "#腾讯会议：" in text, text[:200])

    # 按群组筛选用户
    st, r = call("GET", "/api/users?group=" + quote(grp), admin_tok)
    names = [u["username"] for u in r.get("items", [])]
    check("按群组筛选用户", ("ga" + tag) in names and ("gb" + tag) in names
          and ("gc" + tag) not in names, str(names))

    # 清理：删除会议与用户
    if mid:
        call("DELETE", "/api/meetings/%d" % mid, admin_tok)
    st, r = call("GET", "/api/users", admin_tok)
    for u in r.get("items", []):
        if u["username"] in users:
            call("DELETE", "/api/users/%d" % u["id"], admin_tok)

    print("\n===== 端到端: %d/%d 通过 =====" % (OK, OK + BAD))
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
