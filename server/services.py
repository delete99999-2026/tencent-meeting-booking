# -*- coding: utf-8 -*-
"""业务服务层：校验、权限、预订/修改/删除、统计。

异常约定（app 层统一转 HTTP 状态码）：
  ValidationError  → 400
  AuthError        → 401
  ForbiddenError   → 403
  NotFoundError    → 404
  ConflictError    → 409（带 conflicts 列表）
  LockedError      → 423
  TMDriverError    → 由 worker 处理，不直接抛给请求
"""
import time

from server import database
from server import security
from server.driver.base import TMDriverError


class ApiError(Exception):
    status = 400
    code = "error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class ValidationError(ApiError):
    status, code = 400, "invalid"


class AuthError(ApiError):
    status, code = 401, "unauthorized"


class ForbiddenError(ApiError):
    status, code = 403, "forbidden"


class NotFoundError(ApiError):
    status, code = 404, "notfound"


class ConflictError(ApiError):
    status, code = 409, "conflict"


class LockedError(ApiError):
    status, code = 423, "locked"


def _parse_dt(s: str):
    try:
        return time.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        raise ValidationError("时间格式须为 YYYY-MM-DD HH:MM，例如 2026-11-22 14:00")


def validate_booking(subject: str, start_at: str, duration_min: int,
                     lead_minutes: int = 5, skip_lead_check: bool = False) -> str:
    """R1 时长、R2 不跨天、R4 提前量。返回 end_at。
    skip_lead_check：修改会议且时间未变更时，允许开始时间已进入提前量窗口。"""
    subject = (subject or "").strip()
    if not subject:
        raise ValidationError("会议主题不能为空")
    if len(subject) > 80:
        raise ValidationError("会议主题不能超过 80 个字符")
    try:
        duration_min = int(duration_min)
    except Exception:
        raise ValidationError("时长须为整数（分钟）")
    if not 15 <= duration_min <= 240 or duration_min % 15 != 0:
        # 与腾讯会议客户端一致：15 分钟起、每 15 分钟一档、最长 4 小时。
        # 时间未变更的旧会议编辑（skip_lead_check=True）放行历史非整档值。
        if not (skip_lead_check and 1 <= duration_min <= 240):
            raise ValidationError("预订时长须为 15 分钟的倍数，且不超过 240 分钟（最长 4 小时）")
    st = _parse_dt(start_at)
    st_epoch = time.mktime((st.tm_year, st.tm_mon, st.tm_mday,
                            st.tm_hour, st.tm_min, 0, 0, 0, -1))
    end_at = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(st_epoch + duration_min * 60))
    if end_at.split(" ")[0] != start_at.split(" ")[0]:
        raise ValidationError("会议不能跨天（开始时间+时长须在同一天 24:00 内）")
    if not skip_lead_check and st_epoch <= time.time() + int(lead_minutes) * 60:
        raise ValidationError(
            f"开始时间须晚于当前时间至少 {lead_minutes} 分钟（lead_minutes）")
    return end_at


def conflicts_brief(rows) -> list:
    return [
        {
            "id": r["id"],
            "subject": r["subject"],
            "start_at": r["start_at"],
            "end_at": r["end_at"],
            "creator": (r["creator_username"] or "（已删除用户）"),
            "status": r["status"],
        }
        for r in rows
    ]


def meeting_brief(row) -> dict:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "start_at": row["start_at"],
        "duration_min": row["duration_min"],
        "end_at": row["end_at"],
        "creator_id": row["creator_id"],
        "creator": (row["creator_username"] or "（已删除用户）"),
        "creator_display_name": row["creator_display_name"],
        "status": row["status"],
        "tm_meeting_id": row["tm_meeting_id"],
        "tm_error": row["tm_error"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_meeting(db: str, mid: int):
    row = database.get_meeting_with_creator(db, mid)
    if row is None:
        raise NotFoundError("会议不存在或已被删除")
    return row


def _can_modify(row, user, is_admin: bool) -> None:
    """普通用户只能改自己的会议；已过期会议（开始时间已过）仅管理员可改删。"""
    if is_admin:
        return
    if row["creator_id"] != user["id"]:
        raise ForbiddenError("只能操作自己创建的会议")
    if row["start_at"] < database.now_str():
        raise ForbiddenError("会议已开始/已过时，普通用户无法再修改或删除（仅管理员可操作）")


def create_meeting(db: str, user, driver, subject: str, start_at: str,
                   duration_min: int, lead_minutes: int = 5) -> dict:
    end_at = validate_booking(subject, start_at, duration_min, lead_minutes)
    # R3 冲突检查（与 pending/booked 重叠即拒绝）
    conflicts = database.find_conflicts(db, start_at, end_at)
    if conflicts:
        raise ConflictError("该时间段已有其他会议，无法预订", conflicts=conflicts_brief(conflicts))
    mid = database.create_meeting(db, subject, start_at, duration_min, end_at, user["id"])
    database.enqueue_task(db, "book", meeting_id=mid, subject=subject,
                    start_at=start_at, end_at=end_at)
    return meeting_brief(database.get_meeting_with_creator(db, mid))


def get_meeting(db: str, user, mid: int) -> dict:
    """单条会议详情；任何登录用户可看所有人的会议。"""
    return meeting_brief(_require_meeting(db, mid))


def list_meetings(db: str, user, args: dict) -> dict:
    rows = database.list_meetings(
        db,
        from_date=args.get("from"),
        to_date=args.get("to"),
        creator=args.get("creator"),
        subject_kw=args.get("subject"),
        sort_key=args.get("sort_key", "created_at"),
        sort_dir=args.get("sort_dir", "desc"),
    )
    return {"items": [meeting_brief(r) for r in rows], "count": len(rows)}


def update_meeting(db: str, user, driver, mid: int, fields: dict,
                   lead_minutes: int = 5) -> dict:
    row = _require_meeting(db, mid)
    if row["status"] == "cancelled":
        raise ValidationError("会议已取消，不能再修改")
    _can_modify(row, user, user["role"] == "admin")

    subject = fields.get("subject", row["subject"])
    start_at = fields.get("start_at", row["start_at"])
    duration_min = fields.get("duration_min", row["duration_min"])
    changed_time = (start_at, duration_min) != (row["start_at"], row["duration_min"])
    end_at = validate_booking(subject, start_at, duration_min, lead_minutes,
                              skip_lead_check=not changed_time)

    if changed_time:
        conflicts = database.find_conflicts(db, start_at, end_at, exclude_id=mid)
        if conflicts:
            raise ConflictError("该时间段已有其他会议，无法修改", conflicts=conflicts_brief(conflicts))

    database.update_meeting_fields(db, mid, subject, start_at, duration_min, end_at)
    if changed_time and row["status"] == "booked" and row["tm_meeting_id"]:
        # 腾讯侧已订：旧时间取消 + 新时间重订（worker 串行执行，book 会等 cancel 完成）
        database.enqueue_task(db, "cancel", meeting_id=mid, tm_meeting_id=row["tm_meeting_id"],
                        subject=row["subject"], start_at=row["start_at"], end_at=row["end_at"])
        database.enqueue_task(db, "book", meeting_id=mid, subject=subject,
                        start_at=start_at, end_at=end_at)
        # 状态回 pending 并清旧会议号，worker 才会真正执行重订
        database.set_meeting_rebooking(db, mid)
    return meeting_brief(database.get_meeting_with_creator(db, mid))


def delete_meeting(db: str, user, driver, mid: int) -> dict:
    row = _require_meeting(db, mid)
    if row["status"] == "cancelled":
        raise ValidationError("会议已取消，无需重复删除")
    _can_modify(row, user, user["role"] == "admin")
    if row["status"] == "booked" and row["tm_meeting_id"]:
        database.enqueue_task(db, "cancel", meeting_id=mid, tm_meeting_id=row["tm_meeting_id"],
                        subject=row["subject"], start_at=row["start_at"], end_at=row["end_at"])
    database.set_meeting_cancelled(db, mid)
    return {"id": mid, "status": "cancelled"}


def retry_meeting(db: str, user, mid: int) -> dict:
    row = _require_meeting(db, mid)
    if row["status"] != "failed":
        raise ValidationError("只有预订失败（failed）的会议可以手动重试")
    if user["role"] != "admin" and row["creator_id"] != user["id"]:
        raise ForbiddenError("只能重试自己创建的会议")
    database.set_meeting_pending(db, mid)
    database.enqueue_task(db, "book", meeting_id=mid, subject=row["subject"],
                    start_at=row["start_at"], end_at=row["end_at"])
    return meeting_brief(database.get_meeting_with_creator(db, mid))


def get_stats(db: str, user, period: str) -> dict:
    period = (period or "month").lower()
    if period not in ("month", "quarter", "year"):
        raise ValidationError("period 须为 month / quarter / year")
    rows = database.booked_stats(db, period, user["id"])
    items = [
        {"period": r["period"], "total_min": r["total_min"], "count": r["cnt"]}
        for r in rows
    ]
    return {
        "period": period,
        "items": items,
        "total_min": sum(i["total_min"] for i in items),
        "count": sum(i["count"] for i in items),
    }


# ---------------- 用户管理（管理员） ----------------

def require_admin(user) -> None:
    if user["role"] != "admin":
        raise ForbiddenError("需要管理员权限")


def list_users(db: str, user) -> dict:
    require_admin(user)
    rows = database.list_users(db)
    return {"items": [
        {
            "id": r["id"],
            "username": r["username"],
            "display_name": r["display_name"],
            "role": r["role"],
            "failed_logins": r["failed_logins"],
            "locked_until": r["locked_until"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ], "count": len(rows)}


def create_user(db: str, admin_user, username: str, password: str,
                display_name: str = "", role: str = "user") -> dict:
    require_admin(admin_user)
    username = (username or "").strip()
    try:
        security.check_username(username)
    except ValueError as e:
        raise ValidationError(str(e))
    role = role if role in ("admin", "user") else "user"
    if database.get_user_by_username(db, username) is not None:
        raise ConflictError("用户名已存在")
    if not password:
        password = security.random_password()
    uid = database.create_user(db, username, security.hash_password(password), display_name, role)
    return {"id": uid, "username": username, "role": role,
            "password": password, "message": "用户已创建，请妥善保管返回的密码"}


def update_user(db: str, admin_user, uid: int, display_name: str = None, role: str = None) -> dict:
    require_admin(admin_user)
    if database.get_user_by_id(db, uid) is None:
        raise NotFoundError("用户不存在")
    if role is not None:
        role = role if role in ("admin", "user") else None
        if role is None:
            raise ValidationError("role 须为 admin / user")
        if uid == admin_user["id"] and role != "admin":
            raise ValidationError("不能取消自己的管理员身份")
    database.update_user_profile(db, uid, display_name, role)
    row = database.get_user_by_id(db, uid)
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"]}


def reset_user_password(db: str, admin_user, uid: int, password: str = None) -> dict:
    require_admin(admin_user)
    if database.get_user_by_id(db, uid) is None:
        raise NotFoundError("用户不存在")
    if not password:
        password = security.random_password()
    database.set_password_hash(db, uid, security.hash_password(password))
    return {"id": uid, "password": password, "message": "密码已重置，请妥善保管"}


def delete_user(db: str, admin_user, uid: int) -> dict:
    require_admin(admin_user)
    if database.get_user_by_id(db, uid) is None:
        raise NotFoundError("用户不存在")
    if uid == admin_user["id"]:
        raise ValidationError("不能删除自己")
    if database.get_user_by_id(db, uid)["role"] == "admin" and database.count_admins(db) <= 1:
        raise ValidationError("不能删除最后一个管理员")
    # 其会议保留（列表显示“已删除用户”）；未完成的 book 任务会被 worker 跳过
    database.delete_user(db, uid)
    return {"id": uid, "message": "用户已删除，其历史会议保留展示"}


def queue_status(db: str, user) -> dict:
    require_admin(user)
    rows = database.list_recent_tasks(db, 50)
    return {"items": [
        {
            "id": r["id"],
            "kind": r["kind"],
            "meeting_id": r["meeting_id"],
            "tm_meeting_id": r["tm_meeting_id"],
            "subject": r["subject"],
            "start_at": r["start_at"],
            "end_at": r["end_at"],
            "status": r["status"],
            "error": r["error"],
            "next_run_at": r["next_run_at"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ], "count": len(rows)}
