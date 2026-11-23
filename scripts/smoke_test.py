# -*- coding: utf-8 -*-
"""全流程冒烟测试（dryrun 驱动 + 临时数据库，不触碰真实腾讯会议）。

运行:  python scripts/smoke_test.py
全部通过时 exit 0，否则 exit 1。
"""
import copy
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import database as db  # noqa: E402
from server import security  # noqa: E402
from server.app import create_app, load_config  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra and not cond else ""))


def future_start(minutes_ahead=10):
    """now + minutes_ahead，分钟取整，保证晚于 now+lead(5)。"""
    t = time.localtime(time.time() + minutes_ahead * 60)
    return time.strftime("%Y-%m-%d %H:%M", t)


def at(offset_min, day_offset=1):
    """稳定时间基准：明天 08:00 + offset_min。

    避免 future_start 在临近午夜/月底时 +duration 跨天、跨月，导致
    validate_booking 拒绝并使后续用例级联失败。全天白天时段（08:00 起）
    配合本脚本最大 240 分钟时长，均在当天内，不跨天。
    """
    t = time.localtime(time.time() + day_offset * 86400)
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 8, 0, 0, 0, 0, -1))
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(base + offset_min * 60))


def wait_status(db_path, mid, want, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = db.get_meeting(db_path, mid)
        if m and m["status"] in want:
            return m
        time.sleep(0.3)
    m = db.get_meeting(db_path, mid)
    return m if m and m["status"] in want else None


def main():
    tmp = tempfile.mkdtemp(prefix="mt_smoke_")
    base = load_config()
    cfg = copy.deepcopy(base)
    cfg["db_path"] = os.path.join(tmp, "test.db")
    cfg["retry_interval_sec"] = 1
    cfg["auto_retry"] = 1
    cfg["driver_mode"] = "dryrun"
    # 保留 seed_admin 默认 admin / Admin@123456

    app = create_app(cfg)
    db_path = cfg["db_path"]
    driver = app.config["DRIVER"]
    worker = app.config["WORKER"]
    worker.start()
    client = app.test_client()

    H = lambda: {"Authorization": "Bearer " + ADMIN_TOKEN}
    ADMIN_TOKEN = ""

    # ---------- 登录 ----------
    r = client.post("/api/login", json={"username": "admin", "password": "wrongpass"})
    check("登录-错误密码 401", r.status_code == 401)

    r = client.post("/api/login", json={"username": "admin", "password": "Admin@123456"})
    check("登录-管理员成功", r.status_code == 200 and "token" in r.get_json())
    ADMIN_TOKEN = r.get_json()["token"]
    admin = client.get("/api/me", headers=H()).get_json()["user"]
    check("me-管理员角色", admin["role"] == "admin")

    # ---------- 用户管理 ----------
    r = client.post("/api/users", headers=H(), json={
        "username": "alice", "display_name": "爱丽丝", "role": "user"})
    j = r.get_json()
    check("创建用户-alice 201", r.status_code == 201 and "password" in j)
    alice_pwd = j["password"]
    alice_id = j["id"]

    r = client.post("/api/users", headers=H(), json={"username": "alice", "display_name": "重复"})
    check("创建用户-重名 409", r.status_code == 409)

    r = client.post("/api/users", headers=H(), json={"username": "bad name!", "display_name": "x"})
    check("创建用户-非法用户名 400", r.status_code == 400)

    r = client.get("/api/users", headers=H())
    ulist = r.get_json()["items"]
    check("用户列表-含 alice", any(u["username"] == "alice" for u in ulist))

    # 普通用户不能访问用户管理
    r = client.post("/api/login", json={"username": "alice", "password": alice_pwd})
    ALICE_TOKEN = r.get_json()["token"]
    AH = lambda: {"Authorization": "Bearer " + ALICE_TOKEN}
    r = client.get("/api/users", headers=AH())
    check("用户列表-普通用户 403", r.status_code == 403)
    r = client.get("/api/queue", headers=AH())
    check("队列-普通用户 403", r.status_code == 403)

    # ---------- 预订会议 ----------
    s1 = at(0)  # 明天 08:00，稳定不跨天
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "周例会", "start_at": s1, "duration_min": 60})
    j = r.get_json()
    check("预订-成功 201", r.status_code == 201 and j["status"] == "pending")
    m1_id = j["id"]

    m = wait_status(db_path, m1_id, {"booked"})
    check("预订-dryrun 后变已预订", m is not None, f"status={db.get_meeting(db_path, m1_id)}")
    check("预订-获得腾讯会议号", m and m["tm_meeting_id"] is not None)

    # 冲突
    r = client.post("/api/meetings", headers=H(), json={
        "subject": "冲突会议", "start_at": s1, "duration_min": 30})
    j = r.get_json()
    check("预订-完全冲突 409", r.status_code == 409 and j.get("conflicts"))
    # 部分重叠（开始后 45 分钟，与 [s1, s1+60) 重叠）
    s1b = time.strftime("%Y-%m-%d %H:%M",
                        time.localtime(time.mktime(time.strptime(s1, "%Y-%m-%d %H:%M"))
                                       + 45 * 60))
    r = client.post("/api/meetings", headers=H(), json={
        "subject": "部分重叠", "start_at": s1b, "duration_min": 30})
    check("预订-部分重叠 409", r.status_code == 409)
    # 紧邻不冲突（60 分钟后）
    nxt = time.strptime(s1, "%Y-%m-%d %H:%M")
    s2 = time.strftime("%Y-%m-%d %H:%M",
                        time.localtime(time.mktime(nxt) + 61 * 60))
    r = client.post("/api/meetings", headers=H(), json={
        "subject": "紧邻会议", "start_at": s2, "duration_min": 30})
    check("预订-紧邻不冲突 201", r.status_code == 201)
    m2_id = r.get_json()["id"]
    wait_status(db_path, m2_id, {"booked"})

    # 校验规则
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "超时长", "start_at": at(0), "duration_min": 300})
    check("校验-时长>240 400", r.status_code == 400)
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "跨天", "start_at": "2099-01-01 21:00", "duration_min": 240})
    check("校验-21:00+240min 跨天 400", r.status_code == 400)
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "时间太近", "start_at": future_start(2), "duration_min": 30})
    check("校验-早于 now+5min 400", r.status_code == 400)
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "格式错误", "start_at": "2099/01/01 9:00", "duration_min": 30})
    check("校验-格式错误 400", r.status_code == 400)

    # ---------- 修改会议 ----------
    r = client.put(f"/api/meetings/{m2_id}", headers=AH(), json={
        "subject": "紧邻会议改", "start_at": future_start(20), "duration_min": 45})
    check("修改-他人会议(普通) 403", r.status_code == 403)

    r = client.put(f"/api/meetings/{m1_id}", headers=AH(), json={"subject": "周例会v2"})
    check("修改-自己未过期(仅主题) 200", r.status_code == 200)

    s_edit2 = at(180)  # 明天 11:00 +90min = 12:30，不跨天
    r = client.put(f"/api/meetings/{m1_id}", headers=AH(), json={
        "subject": "周例会v2", "start_at": s_edit2, "duration_min": 90})
    check("修改-自己改时间 200", r.status_code == 200)
    m = wait_status(db_path, m1_id, {"booked"}, timeout=40)
    check("修改-重新预订成功", m is not None)

    # 修改到冲突时段（m2 仍占用 [s2, s2+30)）
    r = client.put(f"/api/meetings/{m1_id}", headers=AH(), json={
        "subject": "周例会v3", "start_at": s2, "duration_min": 30})
    check("修改-目标时段冲突 409", r.status_code == 409)

    # ---------- 删除会议 ----------
    r = client.delete(f"/api/meetings/{m2_id}", headers=AH())
    check("删除-他人会议(普通) 403", r.status_code == 403)
    r = client.delete(f"/api/meetings/{m2_id}", headers=H())
    check("删除-管理员 200", r.status_code == 200 and r.get_json()["status"] == "cancelled")
    deadline = time.time() + 20
    tasks_ok = False
    while time.time() < deadline:
        ts = db.list_recent_tasks(db_path, 20)
        cancels = [t for t in ts if t["kind"] == "cancel" and t["meeting_id"] == m2_id]
        if cancels and cancels[0]["status"] == "done":
            tasks_ok = True
            break
        time.sleep(0.3)
    check("删除-腾讯侧取消任务完成", tasks_ok)

    # ---------- 失败与重试 ----------
    driver.fail_book = True
    r = client.post("/api/meetings", headers=AH(), json={
        "subject": "注定失败", "start_at": at(600), "duration_min": 30})
    m3_id = r.get_json()["id"]
    m = wait_status(db_path, m3_id, {"failed"}, timeout=60)
    check("失败-自动重试后变 failed", m is not None,
          f"status={db.get_meeting(db_path, m3_id)} attempts={m and m['attempts']}")
    check("失败-记录错误信息", m and m["tm_error"])

    r = client.post(f"/api/meetings/{m3_id}/retry", headers=H())
    check("重试-failed 会议 200", r.status_code == 200)
    driver.fail_book = False
    m = wait_status(db_path, m3_id, {"booked"}, timeout=40)
    check("重试-修复后预订成功", m is not None)

    # 对 booked 的会议重试
    r = client.post(f"/api/meetings/{m1_id}/retry", headers=AH())
    check("重试-booked 会议 400", r.status_code == 400)

    # ---------- 已过期会议 ----------
    past = at(0, day_offset=-1)  # 昨天 08:00，稳定为已过期
    # 直接插库制造已过期 booked 会议（绕过 API 的 lead 校验）
    past_end = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.mktime(time.strptime(past, "%Y-%m-%d %H:%M")) + 30 * 60))
    pid = db.create_meeting(db_path, "过期会议", past, 30, past_end, alice_id)
    db.set_meeting_booked(db_path, pid, "999000111")
    r = client.put(f"/api/meetings/{pid}", headers=AH(), json={
        "subject": "过期改", "start_at": at(0), "duration_min": 30})
    check("过期-创建者修改 403", r.status_code == 403)
    r = client.delete(f"/api/meetings/{pid}", headers=AH())
    check("过期-创建者删除 403", r.status_code == 403)
    r = client.put(f"/api/meetings/{pid}", headers=H(), json={
        "subject": "过期改(管理员)", "start_at": at(120), "duration_min": 30})
    check("过期-管理员修改 200", r.status_code == 200)
    r = client.delete(f"/api/meetings/{pid}", headers=H())
    check("过期-管理员删除 200", r.status_code == 200)

    # ---------- 会议密码 / 邀请文本 / 群组 ----------
    # 群组：carol 与 dave 同组『气象局』；erin 属『其他组』；alice 无群组
    r = client.post("/api/users", headers=H(), json={
        "username": "carol", "display_name": "卡罗", "role": "user",
        "group_name": "气象局"})
    carol_pwd = r.get_json()["password"]
    carol_id = r.get_json()["id"]
    check("群组-创建带群组用户 201", r.status_code == 201 and r.get_json()["group_name"] == "气象局")

    r = client.post("/api/users", headers=H(), json={
        "username": "dave", "display_name": "戴夫", "role": "user",
        "group_name": "气象局"})
    dave_pwd = r.get_json()["password"]
    r = client.post("/api/users", headers=H(), json={
        "username": "erin", "display_name": "艾琳", "role": "user",
        "group_name": "其他组"})
    erin_pwd = r.get_json()["password"]

    r = client.get("/api/users?group=气象局", headers=H())
    gl = r.get_json()["items"]
    check("群组-按群组筛选用户",
          {u["username"] for u in gl} == {"carol", "dave"}, f"got={[u['username'] for u in gl]}")

    def token_of(name, pwd):
        rr = client.post("/api/login", json={"username": name, "password": pwd})
        return rr.get_json()["token"]

    CH = lambda: {"Authorization": "Bearer " + token_of("carol", carol_pwd)}
    DH = lambda: {"Authorization": "Bearer " + token_of("dave", dave_pwd)}
    EH = lambda: {"Authorization": "Bearer " + token_of("erin", erin_pwd)}

    # 密码校验
    r = client.post("/api/meetings", headers=CH(), json={
        "subject": "带密码会议", "start_at": at(900), "duration_min": 30,
        "meeting_password": "12345"})
    check("密码-非 6 位 400", r.status_code == 400)
    r = client.post("/api/meetings", headers=CH(), json={
        "subject": "带密码会议", "start_at": at(900), "duration_min": 30,
        "meeting_password": "abcdef"})
    check("密码-非数字 400", r.status_code == 400)

    r = client.post("/api/meetings", headers=CH(), json={
        "subject": "带密码会议", "start_at": at(900), "duration_min": 30,
        "meeting_password": "246810"})
    j = r.get_json()
    check("密码-预订成功 201", r.status_code == 201)
    check("密码-创建者可见", j.get("meeting_password") == "246810")
    pwd_mid = j["id"]
    m = wait_status(db_path, pwd_mid, {"booked"}, timeout=40)
    check("密码-已预订", m is not None)

    # 同组可见 / 非同组不可见
    r = client.get(f"/api/meetings/{pwd_mid}", headers=DH())
    check("群组-同组可见密码", r.get_json().get("meeting_password") == "246810")
    r = client.get(f"/api/meetings/{pwd_mid}", headers=EH())
    check("群组-非同组密码隐藏", r.status_code == 200 and r.get_json().get("meeting_password") is None)
    r = client.get(f"/api/meetings/{pwd_mid}", headers=H())
    check("群组-管理员可见密码", r.get_json().get("meeting_password") == "246810")

    # secret 端点
    r = client.get(f"/api/meetings/{pwd_mid}/secret", headers=DH())
    check("密码端点-同组 200", r.status_code == 200 and r.get_json()["meeting_password"] == "246810")
    r = client.get(f"/api/meetings/{pwd_mid}/secret", headers=EH())
    check("密码端点-非同组 403", r.status_code == 403)
    r = client.get(f"/api/meetings/{pwd_mid}/secret", headers=H())
    check("密码端点-管理员 200", r.status_code == 200)

    # 邀请文本
    r = client.post(f"/api/meetings/{pwd_mid}/copy-invite", headers=DH())
    txt = (r.get_json() or {}).get("text", "")
    check("邀请-同组可复制 200", r.status_code == 200 and "带密码会议" in txt)
    check("邀请-含 GMT+08 时间", "GMT+08" in txt)
    tid = (db.get_meeting(db_path, pwd_mid) or {})["tm_meeting_id"] if db.get_meeting(db_path, pwd_mid) else None
    want_no = f"{tid[0:3]}-{tid[3:6]}-{tid[6:9]}" if tid and len(tid) == 9 else None
    check("邀请-会议号 3-3-3 格式", bool(want_no) and want_no in txt, f"tid={tid} txt={txt}")
    check("邀请-含会议密码", "会议密码：246810" in txt, txt)
    check("邀请-含入会链接", "meeting.tencent.com/dm/" in txt, txt)
    r = client.post(f"/api/meetings/{pwd_mid}/copy-invite", headers=EH())
    check("邀请-非同组 403", r.status_code == 403)
    r = client.post(f"/api/meetings/{pwd_mid}/copy-invite", headers=H())
    check("邀请-管理员 200", r.status_code == 200)

    # 列表里密码按权限隐藏
    r = client.get("/api/meetings", headers=EH())
    row = [m for m in r.get_json()["items"] if m["id"] == pwd_mid]
    check("列表-非同组密码隐藏", bool(row) and row[0]["meeting_password"] is None)

    # 删除 carol（同组关系随之消失）
    r = client.delete(f"/api/users/{carol_id}", headers=H())
    check("群组-删除用户 200", r.status_code == 200)

    # ---------- 列表筛选与排序 ----------
    r = client.get("/api/meetings?subject=周例会", headers=AH())
    items = r.get_json()["items"]
    check("筛选-主题关键字", bool(items) and all("周例会" in m["subject"] for m in items))
    r = client.get("/api/meetings?creator=alice", headers=AH())
    items = r.get_json()["items"]
    check("筛选-创建者", bool(items) and all(m["creator"] == "alice" for m in items))
    d0 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    r = client.get(f"/api/meetings?from={d0}&to={d0}", headers=AH())
    check("筛选-日期范围(无数据)", r.get_json()["items"] == [])
    r = client.get("/api/meetings?sort_key=start_at&sort_dir=asc", headers=AH())
    items = r.get_json()["items"]
    check("排序-开始时间升序",
          all(items[i]["start_at"] <= items[i + 1]["start_at"] for i in range(len(items) - 1)))
    r = client.get("/api/meetings?sort_key=created_at&sort_dir=desc", headers=AH())
    items = r.get_json()["items"]
    check("默认-创建时间倒序",
          all(items[i]["created_at"] >= items[i + 1]["created_at"] for i in range(len(items) - 1)))

    # ---------- 统计 ----------
    r = client.get("/api/stats?period=month", headers=AH())
    j = r.get_json()
    check("统计-月度结构", "items" in j and "total_min" in j and "count" in j)
    check("统计-仅计已预订", j["count"] >= 1 and j["total_min"] >= 60)
    r = client.get("/api/stats?period=quarter", headers=AH())
    check("统计-季度", r.status_code == 200 and r.get_json()["period"] == "quarter")
    r = client.get("/api/stats?period=year", headers=AH())
    check("统计-年度", r.status_code == 200 and r.get_json()["period"] == "year")
    r = client.get("/api/stats?period=week", headers=AH())
    check("统计-非法 period 400", r.status_code == 400)

    # ---------- 用户资料编辑（显示名 / 用户名 / 群组 / 角色） ----------
    r = client.post("/api/users", headers=H(), json={
        "username": "frank", "display_name": "弗兰克", "group_name": "甲组"})
    j = r.get_json()
    frank_id, frank_pwd = j["id"], j["password"]
    check("用户编辑-创建带群组 201", r.status_code == 201 and j["group_name"] == "甲组")

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={
        "display_name": "弗兰克v2", "group_name": "乙组"})
    j = r.get_json()
    check("用户编辑-改显示名+群组",
          r.status_code == 200 and j["display_name"] == "弗兰克v2" and j["group_name"] == "乙组", j)

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"username": "frank2"})
    check("用户编辑-改用户名", r.status_code == 200 and r.get_json()["username"] == "frank2",
          r.get_json())

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"username": "admin"})
    check("用户编辑-用户名重名 409", r.status_code == 409, r.get_json())

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"username": "bad name!"})
    check("用户编辑-用户名非法 400", r.status_code == 400, r.get_json())

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"group_name": ""})
    check("用户编辑-清空群组", r.status_code == 200 and r.get_json()["group_name"] is None,
          r.get_json())

    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"group_name": "x" * 41})
    check("用户编辑-群组超长 400", r.status_code == 400, r.get_json())

    # 未提供 group_name 时不改动（哨兵语义）
    client.put(f"/api/users/{frank_id}", headers=H(), json={"group_name": "丙组"})
    r = client.put(f"/api/users/{frank_id}", headers=H(), json={"display_name": "弗兰克v3"})
    check("用户编辑-未传群组则保持不变",
          r.status_code == 200 and r.get_json()["group_name"] == "丙组", r.get_json())

    # 群组列表接口
    r = client.get("/api/groups", headers=H())
    groups = r.get_json()["items"]
    check("群组列表-含丙组", r.status_code == 200 and "丙组" in groups, groups)
    r = client.get("/api/groups", headers=AH())
    check("群组列表-普通用户 403", r.status_code == 403)

    # 角色保护：不能取消自己的管理员身份 / 不能降级最后一个管理员
    r = client.put(f"/api/users/{admin['id']}", headers=H(), json={"role": "user"})
    check("用户编辑-不能取消自己管理员 400", r.status_code == 400, r.get_json())

    # 普通用户不能调用用户编辑
    r = client.put(f"/api/users/{frank_id}", headers=AH(), json={"display_name": "hack"})
    check("用户编辑-普通用户 403", r.status_code == 403)

    # 改名后可用新用户名登录（旧名失效）
    r = client.post("/api/login", json={"username": "frank2", "password": frank_pwd})
    check("用户编辑-新用户名可登录", r.status_code == 200)
    r = client.post("/api/login", json={"username": "frank", "password": frank_pwd})
    check("用户编辑-旧用户名失效", r.status_code == 401)

    # ---------- 展示状态：已预订 / 会议中 / 已结束 ----------
    def insert_meeting(subject, offset_min, dur, status, tm_no="123456789", creator=alice_id):
        """直接插库造任意时间/状态的会议，绕过 API 的 lead 与冲突校验。"""
        t = time.localtime(time.time() + offset_min * 60)
        s = time.strftime("%Y-%m-%d %H:%M", t)
        e = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.mktime(t) + dur * 60))
        mid = db.create_meeting(db_path, subject, s, dur, e, creator)
        if status == "booked":
            db.set_meeting_booked(db_path, mid, tm_no)
        elif status == "failed":
            db.set_meeting_failed(db_path, mid, "err", 3)
        elif status == "cancelled":
            db.set_meeting_cancelled(db_path, mid)
        return mid

    m_up = insert_meeting("未来会议", 120, 60, "booked")
    m_on = insert_meeting("进行中会议", -10, 60, "booked")
    m_end = insert_meeting("已结束会议", -120, 60, "booked")
    rows = {x["id"]: x for x in client.get("/api/meetings", headers=H()).get_json()["items"]}
    check("状态-未到时间=已预订",
          rows[m_up]["display_status"] == "已预订" and rows[m_up]["phase"] == "upcoming",
          rows[m_up]["display_status"])
    check("状态-进行中=会议中",
          rows[m_on]["display_status"] == "会议中" and rows[m_on]["phase"] == "ongoing",
          rows[m_on]["display_status"])
    check("状态-已过期=已结束",
          rows[m_end]["display_status"] == "已结束" and rows[m_end]["phase"] == "ended",
          rows[m_end]["display_status"])

    r = client.get("/api/meetings?display_status=ongoing", headers=H())
    check("状态筛选-会议中", [x["id"] for x in r.get_json()["items"]] == [m_on],
          [x["id"] for x in r.get_json()["items"]])
    r = client.get("/api/meetings?display_status=upcoming", headers=H())
    ids_up = [x["id"] for x in r.get_json()["items"]]
    check("状态筛选-已预订", m_up in ids_up and m_end not in ids_up, ids_up)
    r = client.get("/api/meetings?display_status=ended", headers=H())
    ids_end = [x["id"] for x in r.get_json()["items"]]
    check("状态筛选-已结束", m_end in ids_end and m_up not in ids_end, ids_end)

    # ---------- 状态异常检测 ----------
    m_bad = insert_meeting("无会议号的已预订", 240, 30, "booked", tm_no=None)
    db.connect(db_path).execute("UPDATE meetings SET tm_meeting_id=NULL WHERE id=?", (m_bad,))
    db.connect(db_path).commit()
    m_past_pending = insert_meeting("过期的预订中", -60, 30, "pending")
    r = client.get("/api/meetings?anomaly=1", headers=H())
    ids_anom = [x["id"] for x in r.get_json()["items"]]
    check("异常-检出缺会议号的已预订", m_bad in ids_anom, ids_anom)
    check("异常-检出过期仍预订中", m_past_pending in ids_anom, ids_anom)
    check("异常-正常会议不在异常列表", m_up not in ids_anom, ids_anom)
    rows = {x["id"]: x for x in client.get("/api/meetings", headers=H()).get_json()["items"]}
    check("异常-返回原因文本", bool(rows[m_bad]["anomaly_reason"]) and rows[m_bad]["status_anomaly"],
          rows[m_bad])

    # A3：pending 超 30 分钟未完成；created_at 可能带秒（历史数据），两种格式都要能识别
    m_stuck = insert_meeting("卡住的预订任务", 2880, 30, "pending")
    m_stuck_sec = insert_meeting("卡住的预订任务带秒", 2880, 30, "pending")
    conn = db.connect(db_path)
    conn.execute("UPDATE meetings SET created_at=? WHERE id=?",
                 ((datetime.now() - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M"), m_stuck))
    conn.execute("UPDATE meetings SET created_at=? WHERE id=?",
                 ((datetime.now() - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S"), m_stuck_sec))
    conn.commit()
    rows = {x["id"]: x for x in client.get("/api/meetings", headers=H()).get_json()["items"]}
    check("异常-检出 pending 超 30 分钟（分钟精度）", rows[m_stuck]["status_anomaly"],
          rows[m_stuck]["anomaly_reason"])
    check("异常-检出 pending 超 30 分钟（带秒时间戳）", rows[m_stuck_sec]["status_anomaly"],
          rows[m_stuck_sec]["anomaly_reason"])
    ids_anom = [x["id"] for x in client.get("/api/meetings?anomaly=1", headers=H()).get_json()["items"]]
    check("异常-筛选含两种时间戳格式", m_stuck in ids_anom and m_stuck_sec in ids_anom, ids_anom)

    # ---------- 批量删除（管理员） ----------
    r = client.post("/api/meetings/batch-delete", headers=H(),
                    json={"ids": [m_bad, m_past_pending]})
    j = r.get_json()
    check("批量删除-2 条 200", r.status_code == 200 and j["count"] == 2, j)
    check("批量删除-软删除后为已取消",
          (db.get_meeting(db_path, m_bad) or {})["status"] == "cancelled")

    r = client.post("/api/meetings/batch-delete", headers=H(), json={"ids": []})
    check("批量删除-空列表 400", r.status_code == 400, r.get_json())
    r = client.post("/api/meetings/batch-delete", headers=H(), json={"ids": ["abc"]})
    check("批量删除-非整数 400", r.status_code == 400, r.get_json())
    r = client.post("/api/meetings/batch-delete", headers=H(), json={"ids": [999999]})
    j = r.get_json()
    check("批量删除-不存在则跳过", r.status_code == 200 and j["count"] == 0 and j["skipped"], j)

    # 已取消的会议：管理员批量删除时会真正移除记录（软删除对它是空操作）
    r = client.post("/api/meetings/batch-delete", headers=H(), json={"ids": [m_bad]})
    j = r.get_json()
    check("批量删除-已取消也删除", r.status_code == 200 and j["count"] == 1, j)
    check("批量删除-已取消记录被移除", db.get_meeting(db_path, m_bad) is None)
    check("批量删除-已取消计入 purged_ids", m_bad in (j.get("purged_ids") or []), j)

    # 普通用户即使删自己的会议也不允许批量
    r = client.post("/api/meetings/batch-delete", headers=AH(), json={"ids": [m_up]})
    check("批量删除-普通用户 403", r.status_code == 403, r.get_json())

    # 彻底删除（状态错误无法修正时使用）
    r = client.post("/api/meetings/batch-delete", headers=H(),
                    json={"ids": [m_end], "purge": True})
    check("批量删除-彻底删除", r.status_code == 200 and r.get_json()["purged"] is True, r.get_json())
    check("批量删除-彻底删除后行已消失", db.get_meeting(db_path, m_end) is None)

    # 单条彻底删除
    r = client.delete(f"/api/meetings/{m_on}?purge=1", headers=H())
    check("单条删除-彻底删除", r.status_code == 200 and db.get_meeting(db_path, m_on) is None,
          r.get_json())
    r = client.delete(f"/api/meetings/{m_up}?purge=1", headers=AH())
    check("单条删除-普通用户 purge 403", r.status_code == 403, r.get_json())

    # ---------- 已取消会议可被管理员与创建者删除 ----------
    m_c1 = insert_meeting("已取消-管理员删", 120, 30, "cancelled")
    r = client.delete(f"/api/meetings/{m_c1}", headers=H())
    check("已取消-管理员删除 200", r.status_code == 200, r.get_json())
    check("已取消-管理员删除后记录移除", db.get_meeting(db_path, m_c1) is None)

    m_c2 = insert_meeting("已取消-创建者删", 180, 30, "cancelled", creator=alice_id)
    r = client.delete(f"/api/meetings/{m_c2}", headers=AH())
    check("已取消-创建者删除 200", r.status_code == 200, r.get_json())
    check("已取消-创建者删除后记录移除", db.get_meeting(db_path, m_c2) is None)

    # 非创建者的普通用户仍不能删别人的已取消会议
    m_c3 = insert_meeting("已取消-他人删", 240, 30, "cancelled", creator=admin["id"])
    r = client.delete(f"/api/meetings/{m_c3}", headers=AH())
    check("已取消-非创建者普通用户 403", r.status_code == 403, r.get_json())

    # ---------- 统计：管理员可看全平台 ----------
    r = client.get("/api/stats?period=month&scope=me", headers=AH())
    j_me = r.get_json()
    check("统计-scope=me 结构", r.status_code == 200 and j_me["scope"] == "me"
          and "by_user" not in j_me, list(j_me.keys()))
    r = client.get("/api/stats?period=month&scope=all", headers=AH())
    check("统计-普通用户 scope=all 403", r.status_code == 403, r.get_json())

    r = client.get("/api/stats?period=month&scope=all", headers=H())
    j_all = r.get_json()
    check("统计-管理员 scope=all 200", r.status_code == 200 and j_all["scope"] == "all", r.status_code)
    check("统计-全平台含 by_user 明细", isinstance(j_all.get("by_user"), list), list(j_all.keys()))
    check("统计-全平台总量不少于个人",
          j_all["total_min"] >= j_me["total_min"] and j_all["count"] >= j_me["count"],
          (j_all["count"], j_me["count"]))
    check("统计-全平台明细含用户名",
          all("username" in x and "period" in x and "total_min" in x for x in j_all["by_user"]))
    r = client.get("/api/stats?period=month&scope=bogus", headers=H())
    check("统计-非法 scope 400", r.status_code == 400, r.get_json())

    # ---------- 用户删除保护 ----------
    r = client.delete(f"/api/users/{admin['id']}", headers=H())
    check("删除用户-自己 400", r.status_code == 400)
    r = client.delete(f"/api/users/{alice_id}", headers=H())
    check("删除用户-alice 200", r.status_code == 200)
    r = client.get("/api/meetings", headers=H())
    items = r.get_json()["items"]
    check("删除用户-会议保留", any(m["creator"] == "（已删除用户）" for m in items))

    # ---------- 登录锁定 ----------
    r = client.post("/api/users", headers=H(), json={"username": "bob", "display_name": "鲍勃"})
    bob_pwd = r.get_json()["password"]
    for i in range(4):
        r = client.post("/api/login", json={"username": "bob", "password": "nope"})
    check("锁定-前4次失败 401", r.status_code == 401)
    r = client.post("/api/login", json={"username": "bob", "password": "nope"})
    check("锁定-第5次失败 423", r.status_code == 423)
    r = client.post("/api/login", json={"username": "bob", "password": bob_pwd})
    check("锁定-锁定期间正确密码也 423", r.status_code == 423)

    # ---------- 队列接口 ----------
    r = client.get("/api/queue", headers=H())
    check("队列-管理员 200", r.status_code == 200 and "items" in r.get_json())

    # ---------- 退出登录 ----------
    r = client.post("/api/logout", headers=AH())
    check("退出-200", r.status_code == 200)
    r = client.get("/api/meetings", headers=AH())
    check("退出-旧 token 401", r.status_code == 401)

    worker.stop()
    os.path.isdir(os.path.join(tmp, "")) and None  # 临时目录保留供排查（在 %TEMP%）
    print("\n临时数据库:", db_path)

    failed = [n for n, ok in results if not ok]
    print(f"\n===== 冒烟测试: {len(results) - len(failed)}/{len(results)} 通过 =====")
    if failed:
        print("失败项:")
        for n in failed:
            print("  -", n)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
