# -*- coding: utf-8 -*-
"""SQLite 数据访问层。

线程安全：SQLite 单文件数据库，每线程独立连接（sqlite3 默认 check_same_thread=True，
threading.local 保证每线程一个连接）。时间统一存 'YYYY-MM-DD HH:MM'（秒不保留，
预订粒度为分钟，冲突判断也更直观）。
"""
import os
import sqlite3
import threading
import time

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    group_name    TEXT,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subject          TEXT NOT NULL,
    start_at         TEXT NOT NULL,
    duration_min     INTEGER NOT NULL CHECK (duration_min BETWEEN 1 AND 240),
    end_at           TEXT NOT NULL,
    creator_id       INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','booked','failed','cancelled')),
    tm_meeting_id    TEXT,
    tm_error         TEXT,
    meeting_password TEXT,
    invite_link      TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_time    ON meetings (start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_meetings_status  ON meetings (status);
CREATE INDEX IF NOT EXISTS idx_meetings_creator ON meetings (creator_id);

CREATE TABLE IF NOT EXISTS tm_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('book','cancel')),
    meeting_id    INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
    tm_meeting_id TEXT,
    subject       TEXT NOT NULL DEFAULT '',
    start_at      TEXT,
    end_at        TEXT,
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','done','error')),
    error         TEXT,
    next_run_at   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tm_tasks (status, next_run_at);
"""


def now_str() -> str:
    """当前时间（分钟粒度，本地时区）。"""
    return time.strftime("%Y-%m-%d %H:%M")


def connect(db_path: str) -> sqlite3.Connection:
    """每线程一个连接；跨线程使用请重新获取。"""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "db_path", None) != db_path:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.db_path = db_path
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库升级：缺列则 ALTER TABLE ADD（幂等）。"""
    adds = [
        ("users", "group_name", "TEXT"),
        ("meetings", "meeting_password", "TEXT"),
        ("meetings", "invite_link", "TEXT"),
    ]
    for table, col, ctype in adds:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


# ---------------- users ----------------

def get_user_by_username(db: str, username: str):
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def get_user_by_id(db: str, uid: int):
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (uid,)
    ).fetchone()


def create_user(db: str, username: str, password_hash: str,
                display_name: str = "", role: str = "user",
                group_name: str = None) -> int:
    ts = now_str()
    conn = connect(db)
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, role, group_name, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (username, display_name, password_hash, role, group_name, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def update_user_profile(db: str, uid: int, display_name: str = None, role: str = None,
                        group_name: str = None) -> None:
    sets, vals = [], []
    if display_name is not None:
        sets.append("display_name = ?")
        vals.append(display_name)
    if role is not None:
        sets.append("role = ?")
        vals.append(role)
    if group_name is not None:
        sets.append("group_name = ?")
        vals.append(group_name)
    sets.append("updated_at = ?")
    vals.append(now_str())
    vals.append(uid)
    connect(db).execute(
        f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals
    )
    connect(db).commit()


def set_password_hash(db: str, uid: int, password_hash: str) -> None:
    connect(db).execute(
        "UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL, updated_at = ? "
        "WHERE id = ?",
        (password_hash, now_str(), uid),
    )
    connect(db).commit()


def record_login_fail(db: str, uid: int, locked_until: str) -> None:
    connect(db).execute(
        "UPDATE users SET failed_logins = failed_logins + 1, locked_until = ?, updated_at = ? "
        "WHERE id = ?",
        (locked_until, now_str(), uid),
    )
    connect(db).commit()


def clear_login_fail(db: str, uid: int) -> None:
    connect(db).execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
        (now_str(), uid),
    )
    connect(db).commit()


def delete_user(db: str, uid: int) -> None:
    connect(db).execute("DELETE FROM users WHERE id = ?", (uid,))
    connect(db).commit()


def list_users(db: str):
    conn = connect(db)
    return conn.execute(
        "SELECT id, username, display_name, role, group_name, failed_logins, locked_until, "
        "created_at, updated_at FROM users ORDER BY id"
    ).fetchall()


def count_admins(db: str) -> int:
    conn = connect(db)
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


# ---------------- sessions ----------------

def create_session(db: str, token: str, uid: int, created_at: str, expires_at: str) -> None:
    connect(db).execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, uid, created_at, expires_at),
    )
    connect(db).commit()


def get_session(db: str, token: str):
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()


def delete_session(db: str, token: str) -> None:
    connect(db).execute("DELETE FROM sessions WHERE token = ?", (token,))
    connect(db).commit()


def purge_expired_sessions(db: str) -> int:
    conn = connect(db)
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_str(),))
    conn.commit()
    return cur.rowcount


# ---------------- meetings ----------------

def create_meeting(db: str, subject: str, start_at: str, duration_min: int,
                   end_at: str, creator_id: int,
                   meeting_password: str = None) -> int:
    ts = now_str()
    conn = connect(db)
    cur = conn.execute(
        "INSERT INTO meetings (subject, start_at, duration_min, end_at, creator_id, "
        "status, meeting_password, attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,'pending',?,0,?,?)",
        (subject, start_at, duration_min, end_at, creator_id, meeting_password, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def set_meeting_password(db: str, mid: int, password: str) -> None:
    connect(db).execute(
        "UPDATE meetings SET meeting_password=?, updated_at=? WHERE id=?",
        (password, now_str(), mid),
    )
    connect(db).commit()


def set_meeting_invite_link(db: str, mid: int, link: str) -> None:
    connect(db).execute(
        "UPDATE meetings SET invite_link=?, updated_at=? WHERE id=?",
        (link, now_str(), mid),
    )
    connect(db).commit()


def get_meeting(db: str, mid: int):
    conn = connect(db)
    return conn.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()


def get_meeting_with_creator(db: str, mid: int):
    """JOIN 出创建者信息（用户被删时 creator_* 为 NULL）。"""
    conn = connect(db)
    return conn.execute(
        "SELECT m.*, u.username AS creator_username, u.display_name AS creator_display_name, "
        "u.group_name AS creator_group "
        "FROM meetings m LEFT JOIN users u ON u.id = m.creator_id WHERE m.id = ?",
        (mid,),
    ).fetchone()


def find_conflicts(db: str, start_at: str, end_at: str, exclude_id: int = None):
    """与 [start_at, end_at) 重叠且仍占位的会议（pending/booked）。"""
    sql = (
        "SELECT m.*, u.username AS creator_username, u.display_name AS creator_display_name "
        "FROM meetings m LEFT JOIN users u ON u.id = m.creator_id "
        "WHERE m.status IN ('pending','booked') AND m.start_at < ? AND m.end_at > ?"
    )
    vals = [end_at, start_at]
    if exclude_id is not None:
        sql += " AND m.id != ?"
        vals.append(exclude_id)
    sql += " ORDER BY m.start_at"
    conn = connect(db)
    return conn.execute(sql, vals).fetchall()


def set_meeting_booked(db: str, mid: int, tm_meeting_id: str) -> None:
    connect(db).execute(
        "UPDATE meetings SET status='booked', tm_meeting_id=?, tm_error=NULL, updated_at=? WHERE id=?",
        (tm_meeting_id, now_str(), mid),
    )
    connect(db).commit()


def set_meeting_failed(db: str, mid: int, tm_error: str, attempts: int) -> None:
    connect(db).execute(
        "UPDATE meetings SET status='failed', tm_error=?, attempts=?, next_attempt_at=NULL, updated_at=? WHERE id=?",
        (tm_error, attempts, now_str(), mid),
    )
    connect(db).commit()


def bump_meeting_attempt(db: str, mid: int, attempts: int, tm_error: str, next_attempt_at: str) -> None:
    connect(db).execute(
        "UPDATE meetings SET attempts=?, tm_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
        (attempts, tm_error, next_attempt_at, now_str(), mid),
    )
    connect(db).commit()


def update_meeting_fields(db: str, mid: int, subject: str, start_at: str,
                          duration_min: int, end_at: str) -> None:
    connect(db).execute(
        "UPDATE meetings SET subject=?, start_at=?, duration_min=?, end_at=?, updated_at=? WHERE id=?",
        (subject, start_at, duration_min, end_at, now_str(), mid),
    )
    connect(db).commit()


def set_meeting_cancelled(db: str, mid: int) -> None:
    connect(db).execute(
        "UPDATE meetings SET status='cancelled', next_attempt_at=NULL, updated_at=? WHERE id=?",
        (now_str(), mid),
    )
    connect(db).commit()


def set_meeting_pending(db: str, mid: int) -> None:
    """failed -> pending（手动重试）。"""
    connect(db).execute(
        "UPDATE meetings SET status='pending', attempts=0, tm_error=NULL, next_attempt_at=NULL, updated_at=? WHERE id=?",
        (now_str(), mid),
    )
    connect(db).commit()


def set_meeting_rebooking(db: str, mid: int) -> None:
    """booked 且时间变更 -> pending 并清旧会议号（等待取消旧会议 + 重新预订）。"""
    connect(db).execute(
        "UPDATE meetings SET status='pending', tm_meeting_id=NULL, attempts=0, "
        "tm_error=NULL, next_attempt_at=NULL, updated_at=? WHERE id=?",
        (now_str(), mid),
    )
    connect(db).commit()


def list_meetings(db: str, from_date: str = None, to_date: str = None,
                  creator: str = None, subject_kw: str = None,
                  sort_key: str = "created_at", sort_dir: str = "desc",
                  limit: int = 200):
    sql = (
        "SELECT m.*, u.username AS creator_username, u.display_name AS creator_display_name, "
        "u.group_name AS creator_group "
        "FROM meetings m LEFT JOIN users u ON u.id = m.creator_id WHERE 1=1"
    )
    vals = []
    if from_date:
        sql += " AND m.start_at >= ?"
        vals.append(f"{from_date} 00:00")
    if to_date:
        sql += " AND m.start_at <= ?"
        vals.append(f"{to_date} 23:59")
    if creator:
        if str(creator).isdigit():
            sql += " AND m.creator_id = ?"
            vals.append(int(creator))
        else:
            sql += " AND u.username = ?"
            vals.append(creator)
    if subject_kw:
        sql += " AND m.subject LIKE ?"
        vals.append(f"%{subject_kw}%")
    allowed = {"created_at": "m.created_at", "subject": "m.subject",
               "creator": "u.username", "start_at": "m.start_at"}
    col = allowed.get(sort_key, "m.created_at")
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    sql += f" ORDER BY {col} {direction}, m.id DESC LIMIT ?"
    vals.append(limit)
    conn = connect(db)
    return conn.execute(sql, vals).fetchall()


# ---------------- tm_tasks ----------------

def enqueue_task(db: str, kind: str, meeting_id: int = None, tm_meeting_id: str = None,
                 subject: str = "", start_at: str = None, end_at: str = None,
                 next_run_at: str = None) -> int:
    ts = now_str()
    cur = connect(db).execute(
        "INSERT INTO tm_tasks (kind, meeting_id, tm_meeting_id, subject, start_at, end_at, "
        "status, next_run_at, created_at, updated_at) VALUES (?,?,?,?,?,?, 'queued',?,?,?)",
        (kind, meeting_id, tm_meeting_id, subject, start_at, end_at,
         next_run_at or ts, ts, ts),
    )
    connect(db).commit()
    return cur.lastrowid


def get_task(db: str, tid: int):
    conn = connect(db)
    return conn.execute("SELECT * FROM tm_tasks WHERE id = ?", (tid,)).fetchone()


def set_task_running(db: str, tid: int) -> None:
    connect(db).execute(
        "UPDATE tm_tasks SET status='running', updated_at=? WHERE id=?", (now_str(), tid)
    )
    connect(db).commit()


def set_task_done(db: str, tid: int) -> None:
    connect(db).execute(
        "UPDATE tm_tasks SET status='done', updated_at=? WHERE id=?", (now_str(), tid)
    )
    connect(db).commit()


def set_task_error(db: str, tid: int, error: str) -> None:
    connect(db).execute(
        "UPDATE tm_tasks SET status='error', error=?, updated_at=? WHERE id=?",
        (error, now_str(), tid),
    )


def requeue_task(db: str, tid: int, error: str, next_run_at: str) -> None:
    connect(db).execute(
        "UPDATE tm_tasks SET status='queued', error=?, next_run_at=?, updated_at=? WHERE id=?",
        (error, next_run_at, now_str(), tid),
    )
    connect(db).commit()


def has_running_task(db: str) -> bool:
    conn = connect(db)
    return conn.execute(
        "SELECT COUNT(*) FROM tm_tasks WHERE status='running'"
    ).fetchone()[0] > 0


def pending_cancel_for_meeting(db: str, mid: int) -> bool:
    """该会议是否还有未完成的 cancel 任务（改时间场景：须等旧会议取消完再重订）。"""
    conn = connect(db)
    return conn.execute(
        "SELECT COUNT(*) FROM tm_tasks WHERE kind='cancel' AND meeting_id=? "
        "AND status IN ('queued','running')",
        (mid,),
    ).fetchone()[0] > 0


def get_next_ready_task(db: str):
    """最老的、到期的 queued 任务（重启后自动续跑：running 遗留由调用方先清理）。"""
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM tm_tasks WHERE status='queued' AND next_run_at <= ? "
        "ORDER BY id LIMIT 1",
        (now_str(),),
    ).fetchone()


def stale_running_tasks(db: str):
    """上次进程未正常退出遗留的 running 任务。"""
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM tm_tasks WHERE status='running'"
    ).fetchall()


def list_recent_tasks(db: str, limit: int = 50):
    conn = connect(db)
    return conn.execute(
        "SELECT * FROM tm_tasks ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------------- stats ----------------

def booked_stats(db: str, period: str, uid: int):
    """某用户 booked 会议按 月/季/年 分组累计时长。返回 [(period, total_min, count), ...] 倒序。"""
    if period == "month":
        gcol = "strftime('%Y-%m', start_at)"
    elif period == "quarter":
        gcol = "strftime('%Y', start_at) || '-Q' || ((CAST(strftime('%m', start_at) AS INTEGER) - 1) / 3 + 1)"
    elif period == "year":
        gcol = "strftime('%Y', start_at)"
    else:
        raise ValueError("period must be month/quarter/year")
    conn = connect(db)
    return conn.execute(
        f"SELECT {gcol} AS period, SUM(duration_min) AS total_min, COUNT(*) AS cnt "
        f"FROM meetings WHERE status='booked' AND creator_id = ? "
        f"GROUP BY period ORDER BY period DESC",
        (uid,),
    ).fetchall()
