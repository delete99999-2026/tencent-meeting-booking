# -*- coding: utf-8 -*-
"""初始化/修改超级管理员账号。

用法:
  python scripts/init_admin.py                 # 创建或修改 admin 为默认配置
  python scripts/init_admin.py --username superadmin --password X@123456 --display 总管理员
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import database as db  # noqa: E402
from server import security  # noqa: E402
from server.app import load_config  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="初始化超级管理员")
    p.add_argument("--username", default="admin")
    p.add_argument("--password", default="Admin@123456")
    p.add_argument("--display", default="超级管理员")
    args = p.parse_args()

    cfg = load_config()
    db_path = cfg.get("db_path", os.path.join(ROOT, "data", "meeting.db"))
    db.init_db(db_path)

    try:
        security.check_username(args.username)
    except ValueError as e:
        print("错误:", e)
        sys.exit(1)

    user = db.get_user_by_username(db_path, args.username)
    if user is None:
        db.create_user(db_path, args.username, security.hash_password(args.password),
                       args.display, "admin")
        print(f"已创建管理员 {args.username} / {args.password}（{args.display}）")
    else:
        if user["role"] != "admin":
            db.update_user_profile(db_path, user["id"], args.display, "admin")
        else:
            db.update_user_profile(db_path, user["id"], args.display, user["role"])
        db.set_password_hash(db_path, user["id"], security.hash_password(args.password))
        print(f"已更新管理员 {args.username}（密码已重置，登录失败计数与锁定已清除）")


if __name__ == "__main__":
    main()
