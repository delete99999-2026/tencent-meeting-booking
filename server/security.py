# -*- coding: utf-8 -*-
"""安全模块：PBKDF2-SHA256 密码散列、会话 Token、登录失败锁定。

锁定状态持久化在 users 表（failed_logins / locked_until），重启不丢失。
"""
import hashlib
import hmac
import os
import random
import re
import string
import time

from server import database as db

_PBKDF2_ROUNDS = 310_000
_USERNAMES_RX = re.compile(r"^[A-Za-z0-9_\-]{2,20}$")


def check_username(username: str) -> None:
    """用户名规则：2-20 位字母/数字/下划线/连字符。"""
    if not isinstance(username, str) or not _USERNAMES_RX.match(username):
        raise ValueError("用户名须为 2-20 位字母、数字、下划线或连字符")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (_PBKDF2_ROUNDS, salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def make_token() -> str:
    return os.urandom(32).hex()


def random_password(length: int = 12) -> str:
    """生成含大小写字母+数字的随机密码。"""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    pool = upper + lower + digits
    chars = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
    ]
    chars += [random.choice(pool) for _ in range(length - 3)]
    random.shuffle(chars)
    return "".join(chars)


def session_expires_at(session_days: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + session_days * 86400))


def is_locked(user_row) -> str:
    """返回当前生效的锁定截止时间（空字符串=未锁定）。"""
    until = user_row["locked_until"] if user_row else None
    if until and until > db.now_str():
        return until
    return ""


def register_fail(db_path: str, uid: int, max_fails: int, lock_minutes: int) -> str:
    """累计一次登录失败；返回新锁定截止时间（空字符串=未触发锁定）。

    连续失败达到 max_fails 次 → 锁定 lock_minutes 分钟；
    锁定期间再次失败 → 保持原锁定截止时间（不延长）。
    """
    conn = db.connect(db_path)
    row = conn.execute("SELECT failed_logins FROM users WHERE id = ?", (uid,)).fetchone()
    count = (row["failed_logins"] if row else 0) + 1
    if count >= max_fails:
        until = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(time.time() + lock_minutes * 60)
        )
        conn.execute(
            "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
            (count, until, uid),
        )
    else:
        conn.execute(
            "UPDATE users SET failed_logins = ?, locked_until = NULL WHERE id = ?",
            (count, uid),
        )
    conn.commit()
    return until if count >= max_fails else ""


def clear_fails(db_path: str, uid: int) -> None:
    db.clear_login_fail(db_path, uid)
