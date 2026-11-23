# -*- coding: utf-8 -*-
"""端到端 HTTP 验证：用户资料编辑 / 群组 / 展示状态 / 状态异常 / 批量删除。

与 smoke_test.py 的区别：本脚本真正起一个 HTTP 服务（werkzeug），
用 urllib 走真实网络请求，验证前端实际会调用的接口契约（含 JSON 结构、
HTTP 状态码、静态页面可访问性）。

全程 dryrun 驱动 + 临时数据库，不会触碰真实腾讯会议，也不会改动 data/meeting.db。

用法：python scripts\\e2e_user_batch.py
"""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import database as db  # noqa: E402
from server.app import create_app, load_config  # noqa: E402

PORT = 8017
BASE = "http://127.0.0.1:%d" % PORT
OK = 0
BAD = 0


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print("[PASS] %s" % name)
    else:
        BAD += 1
        print("[FAIL] %s  %s" % (name, extra))


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


def raw_get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, r.read()


def login(username, password):
    st, r = call("POST", "/api/login", body={"username": username, "password": password})
    return (r.get("token") if st == 200 else None), r


def _status_of(dbp, mid):
    """取会议状态；行不存在返回 None（sqlite3.Row 不支持 .get）。"""
    row = db.get_meeting(dbp, mid)
    return row["status"] if row is not None else None


def insert_meeting(dbp, subject, offset_min, dur, status, tm_no="123456789", creator=1):
    """直接插库造指定时间/状态的会议（绕过 API 的 lead 与冲突校验）。"""
    t = time.localtime(time.time() + offset_min * 60)
    s = time.strftime("%Y-%m-%d %H:%M", t)
    e = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.mktime(t) + dur * 60))
    mid = db.create_meeting(dbp, subject, s, dur, e, creator)
    if status == "booked":
        db.set_meeting_booked(dbp, mid, tm_no)
    elif status == "failed":
        db.set_meeting_failed(dbp, mid, "err", 3)
    elif status == "cancelled":
        db.set_meeting_cancelled(dbp, mid)
    return mid


def main():
    tmp = tempfile.mkdtemp(prefix="mt_e2e_")
    cfg = copy.deepcopy(load_config())
    cfg["db_path"] = os.path.join(tmp, "e2e.db")
    cfg["driver_mode"] = "dryrun"
    cfg["auto_retry"] = 1
    cfg["retry_interval_sec"] = 1
    dbp = cfg["db_path"]

    app = create_app(cfg)
    worker = app.config["WORKER"]
    worker.start()

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", PORT, app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.6)

    try:
        # ---------- 静态页面 ----------
        st, html = raw_get("/")
        html_s = html.decode("utf-8")
        check("静态-首页 200", st == 200 and len(html_s) > 1000, st)
        for eid in ["f-status", "check-all", "batch-del-btn", "batch-purge-btn",
                    "user-mask", "u-group", "group-options"]:
            check("静态-含元素 #%s" % eid, ('id="%s"' % eid) in html_s)
        st, js = raw_get("/static/app.js")
        check("静态-app.js 200", st == 200 and len(js) > 5000, st)
        st, css = raw_get("/static/style.css")
        check("静态-style.css 200", st == 200 and b"badge-ongoing" in css, st)
        # 弹窗内边距必须同时覆盖 #modal-form 与 #user-form，否则用户编辑弹窗内容贴边
        check("静态-弹窗表单内边距含 #user-form",
              b"#modal-form, #user-form" in css, css[:200])

        # ---------- 登录 ----------
        tok, r = login("admin", "Admin@123456")
        check("登录-管理员", tok is not None, r)

        # ---------- 用户资料编辑 ----------
        st, u1 = call("POST", "/api/users", tok,
                      {"username": "e2e_u1", "display_name": "端到端1", "group_name": "甲组"})
        check("用户-创建带群组 201", st == 201 and u1.get("group_name") == "甲组", u1)
        uid = u1["id"]

        st, j = call("PUT", "/api/users/%d" % uid, tok,
                     {"display_name": "端到端1改", "group_name": "乙组"})
        check("用户-改显示名+群组 200",
              st == 200 and j.get("display_name") == "端到端1改" and j.get("group_name") == "乙组", j)
        check("用户-返回提示文案", j.get("message") == "用户信息已更新", j.get("message"))

        st, j = call("PUT", "/api/users/%d" % uid, tok, {"username": "e2e_u1b"})
        check("用户-改用户名 200", st == 200 and j.get("username") == "e2e_u1b", j)

        st, j = call("PUT", "/api/users/%d" % uid, tok, {"username": "admin"})
        check("用户-重名 409", st == 409, j)

        st, j = call("PUT", "/api/users/%d" % uid, tok, {"username": "no way!"})
        check("用户-非法用户名 400", st == 400, j)

        st, j = call("PUT", "/api/users/%d" % uid, tok, {"group_name": ""})
        check("用户-清空群组", st == 200 and j.get("group_name") is None, j)

        st, j = call("PUT", "/api/users/%d" % uid, tok, {"group_name": "z" * 41})
        check("用户-群组超长 400", st == 400, j)

        st, j = call("PUT", "/api/users/999999", tok, {"display_name": "x"})
        check("用户-不存在 404", st == 404, j)

        # 群组列表
        call("PUT", "/api/users/%d" % uid, tok, {"group_name": "E2E丙组"})
        st, j = call("GET", "/api/groups", tok)
        check("群组列表-200 且含新群组",
              st == 200 and "E2E丙组" in (j.get("items") or []), j)

        # 普通用户权限
        st, u2 = call("POST", "/api/users", tok,
                      {"username": "e2e_u2", "display_name": "端到端2", "password": "Pw@123456"})
        t2, _ = login("e2e_u2", "Pw@123456")
        check("普通用户-登录成功", t2 is not None)
        st, j = call("GET", "/api/groups", t2)
        check("普通用户-群组列表 403", st == 403, j)
        st, j = call("PUT", "/api/users/%d" % uid, t2, {"display_name": "hack"})
        check("普通用户-改他人资料 403", st == 403, j)
        st, j = call("POST", "/api/meetings/batch-delete", t2, {"ids": [1]})
        check("普通用户-批量删除 403", st == 403, j)

        # ---------- 展示状态 ----------
        m_up = insert_meeting(dbp, "未来会议", 120, 60, "booked")
        m_on = insert_meeting(dbp, "进行中会议", -10, 60, "booked")
        m_end = insert_meeting(dbp, "已结束会议", -120, 60, "booked")
        st, j = call("GET", "/api/meetings", tok)
        rows = {x["id"]: x for x in j["items"]}
        check("状态-未到时间=已预订",
              rows[m_up]["display_status"] == "已预订" and rows[m_up]["phase"] == "upcoming",
              rows[m_up]["display_status"])
        check("状态-进行中=会议中",
              rows[m_on]["display_status"] == "会议中" and rows[m_on]["phase"] == "ongoing",
              rows[m_on]["display_status"])
        check("状态-已过期=已结束",
              rows[m_end]["display_status"] == "已结束" and rows[m_end]["phase"] == "ended",
              rows[m_end]["display_status"])

        for phase, want in [("upcoming", m_up), ("ongoing", m_on), ("ended", m_end)]:
            st, j = call("GET", "/api/meetings?display_status=" + phase, tok)
            got = [x["id"] for x in j["items"]]
            check("状态筛选-%s" % phase, got == [want], got)

        # ---------- 状态异常 ----------
        m_bad = insert_meeting(dbp, "缺会议号", 240, 30, "booked")
        conn = db.connect(dbp)
        conn.execute("UPDATE meetings SET tm_meeting_id=NULL WHERE id=?", (m_bad,))
        conn.commit()
        m_past = insert_meeting(dbp, "过期预订中", -60, 30, "pending")
        st, j = call("GET", "/api/meetings?anomaly=1", tok)
        anom = [x["id"] for x in j["items"]]
        check("异常-检出缺会议号", m_bad in anom, anom)
        check("异常-检出过期预订中", m_past in anom, anom)
        check("异常-正常会议不入选", m_up not in anom, anom)
        st, j = call("GET", "/api/meetings", tok)
        rows = {x["id"]: x for x in j["items"]}
        check("异常-返回 status_anomaly 与原因",
              rows[m_bad]["status_anomaly"] is True and bool(rows[m_bad]["anomaly_reason"]),
              rows[m_bad].get("anomaly_reason"))

        # ---------- 批量删除 ----------
        st, j = call("POST", "/api/meetings/batch-delete", tok,
                     {"ids": [m_bad, m_past, m_bad]})  # 重复 ID 应去重
        check("批量删除-去重后 2 条",
              st == 200 and j.get("count") == 2, j)
        check("批量删除-软删除为已取消",
              _status_of(dbp, m_bad) == "cancelled")

        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": []})
        check("批量删除-空 400", st == 400, j)
        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": ["x"]})
        check("批量删除-非整数 400", st == 400, j)
        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": list(range(1, 202))})
        check("批量删除-超 200 条 400", st == 400, j)
        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": [888888]})
        check("批量删除-不存在进 skipped",
              st == 200 and j.get("count") == 0 and bool(j.get("skipped")), j)
        # 已取消的会议：批量删除会真正移除记录
        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": [m_bad]})
        check("批量删除-已取消也删除", st == 200 and j.get("count") == 1, j)
        check("批量删除-已取消记录已移除", db.get_meeting(dbp, m_bad) is None)
        check("批量删除-已取消计入 purged_ids", m_bad in (j.get("purged_ids") or []), j)

        # 彻底删除
        st, j = call("POST", "/api/meetings/batch-delete", tok,
                     {"ids": [m_end], "purge": True})
        check("彻底删除-200", st == 200 and j.get("purged") is True, j)
        check("彻底删除-行已消失", db.get_meeting(dbp, m_end) is None)

        st, j = call("DELETE", "/api/meetings/%d?purge=1" % m_on, tok)
        check("单条彻底删除", st == 200 and db.get_meeting(dbp, m_on) is None, j)

        # 已预订的会议被删除时应排队取消腾讯侧
        st, j = call("POST", "/api/meetings/batch-delete", tok, {"ids": [m_up]})
        check("批量删除-已预订会议 200", st == 200 and j.get("count") == 1, j)
        deadline = time.time() + 15
        cancelled = False
        while time.time() < deadline:
            ts = db.list_recent_tasks(dbp, 30)
            if any(t["kind"] == "cancel" and t["meeting_id"] == m_up and t["status"] == "done"
                   for t in ts):
                cancelled = True
                break
            time.sleep(0.3)
        check("批量删除-腾讯侧取消任务完成", cancelled)

        # ---------- 已取消会议可被管理员与创建者删除 ----------
        u2_id = u2["id"]
        mc1 = insert_meeting(dbp, "已取消-管理员删", 300, 30, "cancelled")
        st, j = call("DELETE", "/api/meetings/%d" % mc1, tok)
        check("已取消-管理员删除 200", st == 200, j)
        check("已取消-管理员删除后移除", db.get_meeting(dbp, mc1) is None)

        # 创建者删自己的已取消会议（e2e_u2 是普通用户）
        mc2 = insert_meeting(dbp, "已取消-创建者删", 360, 30, "cancelled", creator=u2_id)
        st, j = call("DELETE", "/api/meetings/%d" % mc2, t2)
        check("已取消-创建者删除 200", st == 200, j)
        check("已取消-创建者删除后移除", db.get_meeting(dbp, mc2) is None)

        # 非创建者的普通用户不能删别人的已取消会议
        mc3 = insert_meeting(dbp, "已取消-他人删", 420, 30, "cancelled")
        st, j = call("DELETE", "/api/meetings/%d" % mc3, t2)
        check("已取消-非创建者普通用户 403", st == 403, j)

        # ---------- 统计：管理员可看全平台 ----------
        st, j_me = call("GET", "/api/stats?period=month&scope=me", tok)
        check("统计-scope=me 200", st == 200 and j_me.get("scope") == "me", j_me)
        st, j = call("GET", "/api/stats?period=month&scope=all", t2)
        check("统计-普通用户 scope=all 403", st == 403, j)
        st, j_all = call("GET", "/api/stats?period=month&scope=all", tok)
        check("统计-管理员 scope=all 200", st == 200 and j_all.get("scope") == "all", st)
        check("统计-全平台含 by_user", isinstance(j_all.get("by_user"), list), list(j_all))
        check("统计-全平台不少于个人",
              j_all.get("total_min", 0) >= j_me.get("total_min", 0), (j_all, j_me))
        check("统计-明细含用户与周期",
              all(k in x for x in (j_all.get("by_user") or []) for k in
                  ("username", "period", "total_min", "count"))
              if j_all.get("by_user") else True)
        st, j = call("GET", "/api/stats?period=month&scope=bogus", tok)
        check("统计-非法 scope 400", st == 400, j)

        # ---------- 角色保护 ----------
        st, me = call("GET", "/api/me", tok)
        admin_id = me["user"]["id"]
        st, j = call("PUT", "/api/users/%d" % admin_id, tok, {"role": "user"})
        check("角色-不能取消自己管理员 400", st == 400, j)

    finally:
        srv.shutdown()
        worker.stop()

    print("\n===== 端到端测试: %d/%d 通过 =====" % (OK, OK + BAD))
    print("临时数据库:", dbp)
    if BAD:
        print("失败 %d 项" % BAD)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
