# -*- coding: utf-8 -*-
"""Flask 应用入口：API + 静态页面 + worker 启动。

启动：  python -m server.app
配置：  config.json（根目录）
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask, g, jsonify, request  # noqa: E402

from server import database as db  # noqa: E402
from server import security  # noqa: E402
from server import services  # noqa: E402
from server.driver import get_driver  # noqa: E402
from server.worker import Worker  # noqa: E402


def load_config() -> dict:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def create_app(cfg: dict = None) -> Flask:
    cfg = cfg or load_config()
    db_path = cfg.get("db_path", os.path.join(ROOT, "data", "meeting.db"))

    app = Flask(__name__, static_folder=os.path.join(ROOT, "web"), static_url_path="/static")
    app.config["MEETING_CFG"] = cfg
    app.config["DB_PATH"] = db_path

    driver = get_driver(cfg)
    worker = Worker(db_path, driver, cfg)
    app.config["DRIVER"] = driver
    app.config["WORKER"] = worker

    db.init_db(db_path)
    _seed_admin(db_path, cfg)

    # ---------- 认证辅助 ----------

    def current_user():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        sess = db.get_session(db_path, auth[7:].strip())
        if sess is None:
            return None
        if sess["expires_at"] <= db.now_str():
            db.delete_session(db_path, sess["token"])
            return None
        return db.get_user_by_id(db_path, sess["user_id"])

    @app.before_request
    def _purge_sessions():
        # 低频清理过期 session（每次请求一次开销可忽略：单行 DELETE）
        if request.path.startswith("/api/"):
            db.purge_expired_sessions(db_path)

    # ---------- 登录 ----------

    @app.post("/api/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        user = db.get_user_by_username(db_path, username)
        if user is None:
            return jsonify({"error": "用户名或密码错误", "code": "unauthorized"}), 401
        locked = security.is_locked(user)
        if locked:
            return jsonify({"error": f"登录失败次数过多，账户已锁定，请 {locked} 之后再试",
                            "code": "locked"}), 423
        if not security.verify_password(password, user["password_hash"]):
            until = security.register_fail(db_path, user["id"],
                                           int(cfg.get("max_login_fails", 5)),
                                           int(cfg.get("lock_minutes", 10)))
            if until:
                return jsonify({"error": f"登录失败次数过多，账户已锁定，请 {until} 之后再试",
                                "code": "locked"}), 423
            return jsonify({"error": "用户名或密码错误", "code": "unauthorized"}), 401
        security.clear_fails(db_path, user["id"])
        token = security.make_token()
        db.create_session(db_path, token, user["id"], db.now_str(),
                          security.session_expires_at(int(cfg.get("session_days", 7))))
        return jsonify({"token": token, "user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
        }})

    @app.post("/api/logout")
    def api_logout():
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            db.delete_session(db_path, auth[7:].strip())
        return jsonify({"ok": True})

    @app.get("/api/me")
    def api_me():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        return jsonify({"user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
        }})

    # ---------- 会议 ----------

    @app.get("/api/meetings")
    def api_list_meetings():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        args = {
            "from": request.args.get("from"),
            "to": request.args.get("to"),
            "creator": request.args.get("creator"),
            "subject": request.args.get("subject"),
            "sort_key": request.args.get("sort_key", "created_at"),
            "sort_dir": request.args.get("sort_dir", "desc"),
        }
        return jsonify(services.list_meetings(db_path, user, args))

    @app.get("/api/meetings/<int:mid>")
    def api_get_meeting(mid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.get_meeting(db_path, user, mid))
        except services.ApiError as e:
            return _err(e)

    @app.post("/api/meetings")
    def api_create_meeting():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            item = services.create_meeting(db_path, user, driver,
                                           data.get("subject", ""),
                                           data.get("start_at", ""),
                                           data.get("duration_min", 0),
                                           int(cfg.get("lead_minutes", 5)))
        except services.ApiError as e:
            return _err(e)
        return jsonify(item), 201

    @app.put("/api/meetings/<int:mid>")
    def api_update_meeting(mid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            item = services.update_meeting(db_path, user, driver, mid, data,
                                           int(cfg.get("lead_minutes", 5)))
        except services.ApiError as e:
            return _err(e)
        return jsonify(item)

    @app.delete("/api/meetings/<int:mid>")
    def api_delete_meeting(mid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.delete_meeting(db_path, user, driver, mid))
        except services.ApiError as e:
            return _err(e)

    @app.post("/api/meetings/<int:mid>/retry")
    def api_retry_meeting(mid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.retry_meeting(db_path, user, mid))
        except services.ApiError as e:
            return _err(e)

    # ---------- 统计 ----------

    @app.get("/api/stats")
    def api_stats():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.get_stats(db_path, user, request.args.get("period", "month")))
        except services.ApiError as e:
            return _err(e)

    # ---------- 用户管理（管理员） ----------

    @app.get("/api/users")
    def api_list_users():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.list_users(db_path, user))
        except services.ApiError as e:
            return _err(e)

    @app.post("/api/users")
    def api_create_user():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            item = services.create_user(db_path, user,
                                        data.get("username", ""),
                                        data.get("password", ""),
                                        data.get("display_name", ""),
                                        data.get("role", "user"))
        except services.ApiError as e:
            return _err(e)
        return jsonify(item), 201

    @app.put("/api/users/<int:uid>")
    def api_update_user(uid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(services.update_user(
                db_path, user, uid,
                data.get("display_name"), data.get("role")))
        except services.ApiError as e:
            return _err(e)

    @app.post("/api/users/<int:uid>/reset-password")
    def api_reset_user_password(uid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(services.reset_user_password(db_path, user, uid,
                                                        data.get("password", "")))
        except services.ApiError as e:
            return _err(e)

    @app.delete("/api/users/<int:uid>")
    def api_delete_user(uid):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.delete_user(db_path, user, uid))
        except services.ApiError as e:
            return _err(e)

    # ---------- 队列（管理员） ----------

    @app.get("/api/queue")
    def api_queue():
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或登录已过期", "code": "unauthorized"}), 401
        try:
            return jsonify(services.queue_status(db_path, user))
        except services.ApiError as e:
            return _err(e)

    # ---------- 静态页面 ----------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/<path:filename>")
    def static_files(filename):
        return app.send_static_file(filename)

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "接口不存在", "code": "notfound"}), 404
        return ("Not Found", 404)

    return app


def _err(e: services.ApiError):
    body = {"error": e.message, "code": e.code}
    body.update(e.extra)
    return jsonify(body), e.status


def _seed_admin(db_path: str, cfg: dict) -> None:
    """首次启动且无管理员时，用 config 的 seed_admin 创建（默认 admin / Admin@123456）。"""
    conn = db.connect(db_path)
    has_admin = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
    if has_admin == 0:
        sa = cfg.get("seed_admin", {})
        username = sa.get("username", "admin")
        password = sa.get("password", "Admin@123456")
        display = sa.get("display_name", "超级管理员")
        db.create_user(db_path, username, security.hash_password(password), display, "admin")
        print(f"[seed] 已创建初始管理员 {username}（默认密码 {password}，登录后请立即修改）")


if __name__ == "__main__":
    cfg = load_config()
    app = create_app(cfg)
    app.config["WORKER"].start()
    print(f"腾讯会议预订平台启动: http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8000)}")
    app.run(host=cfg.get("host", "127.0.0.1"), port=cfg.get("port", 8000),
            debug=False, use_reloader=False, threaded=True)
