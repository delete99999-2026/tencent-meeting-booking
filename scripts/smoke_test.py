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
