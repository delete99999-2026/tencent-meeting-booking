# -*- coding: utf-8 -*-
"""串行任务 worker：同一时刻全局只执行一个腾讯会议 GUI 任务。

book  : 成功 → meetings.status=booked + tm_meeting_id
        失败 → attempts+1；attempts≤auto_retry → 任务回队（next_run_at=now+retry_interval）
              否则 → meetings.status=failed、任务 error
cancel: 失败 → 任务回队（60s 后重试，上限 5 次 → error）
重启  : 上次遗留 running 任务自动回队续跑。
"""
import logging
import threading
import time

from server import database as db
from server.driver.base import TMDriverError

log = logging.getLogger("tm.worker")

POLL_SEC = 1
CANCEL_RETRY_LIMIT = 5


class Worker:
    def __init__(self, db_path: str, driver, cfg: dict):
        self.db = db_path
        self.driver = driver
        self.cfg = cfg
        self.auto_retry = int(cfg.get("auto_retry", 2))
        self.retry_interval = int(cfg.get("retry_interval_sec", 60))
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()  # 双保险：轮询循环内串行

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        # 上次未正常退出遗留的 running 任务 → 回队
        for t in db.stale_running_tasks(self.db):
            db.requeue_task(self.db, t["id"], "进程重启，任务回队", db.now_str())
            log.info("遗留 running 任务 %s 回队", t["id"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tm-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # ---------------- 主循环 ----------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not db.has_running_task(self.db):
                    task = db.get_next_ready_task(self.db)
                    if task:
                        db.set_task_running(self.db, task["id"])
                        if task["kind"] == "book":
                            self._book(task)
                        else:
                            self._cancel(task)
            except Exception:
                log.exception("worker 循环异常")
            self._stop.wait(POLL_SEC)

    # ---------------- book ----------------

    def _book(self, task) -> None:
        mid = task["meeting_id"]
        meeting = db.get_meeting(self.db, mid) if mid else None
        if meeting is None or meeting["status"] in ("cancelled",):
            db.set_task_done(self.db, task["id"])
            return
        if meeting["status"] == "booked":
            db.set_task_done(self.db, task["id"])
            return
        if db.pending_cancel_for_meeting(self.db, mid):
            # 改时间场景：等旧会议取消完成后再来，避免腾讯侧新旧会议并存
            count = 0
            prev = task["error"] or ""
            if prev.startswith("WTC"):
                try:
                    count = int(prev[3:].split(":", 1)[0])
                except Exception:
                    count = 0
            count += 1
            if count >= 30:
                db.set_task_error(self.db, task["id"],
                                  f"等待旧会议取消超时（{count * 10}s），请人工检查腾讯侧后重试")
                return
            next_run = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(time.time() + 10)
            )
            db.requeue_task(self.db, task["id"], f"WTC{count}: 等待旧会议取消完成", next_run)
            return
        try:
            r = self.driver.book(task["subject"], task["start_at"], task["end_at"],
                                 meeting["meeting_password"])
        except TMDriverError as e:
            self._book_retry(task, meeting, f"[{e.stage}] {e.message}")
            return
        except Exception as e:
            # pywintypes.error / win32api 异常等非 TMDriverError 也走重试路径，避免任务卡 running
            self._book_retry(task, meeting, f"[driver] 驱动异常 {type(e).__name__}: {e}")
            return
        if not r.get("ok"):
            self._book_retry(task, meeting, r.get("message", "预订返回失败"))
            return
        db.set_meeting_booked(self.db, mid, r.get("meeting_id"))
        if r.get("invite_link"):
            db.set_meeting_invite_link(self.db, mid, r["invite_link"])
        db.set_task_done(self.db, task["id"])
        log.info("book 成功 meeting=%s tm_id=%s", mid, r.get("meeting_id"))

    def _book_retry(self, task, meeting, err: str) -> None:
        attempts = (meeting["attempts"] or 0) + 1
        if attempts <= self.auto_retry:
            next_run = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(time.time() + self.retry_interval),
            )
            db.bump_meeting_attempt(self.db, meeting["id"], attempts, err, next_run)
            db.requeue_task(self.db, task["id"], err, next_run)
            log.warning("book 失败 meeting=%s 第%d次，%s 后重试", meeting["id"], attempts, next_run)
        else:
            db.set_meeting_failed(self.db, meeting["id"], err, attempts)
            db.set_task_error(self.db, task["id"], err)
            log.error("book 最终失败 meeting=%s: %s", meeting["id"], err)

    # ---------------- cancel ----------------

    def _cancel(self, task) -> None:
        if not task["tm_meeting_id"]:
            db.set_task_error(self.db, task["id"], "无腾讯会议号且无法定位，跳过取消")
            return
        try:
            r = self.driver.cancel(task["tm_meeting_id"], task["subject"], task["start_at"])
        except TMDriverError as e:
            r = {"ok": False, "message": f"[{e.stage}] {e.message}"}
        except Exception as e:
            # 非 TMDriverError（如 pywintypes.error）也按取消失败重试，避免任务卡 running
            r = {"ok": False, "message": f"[driver] 驱动异常 {type(e).__name__}: {e}"}
        if r.get("ok"):
            db.set_task_done(self.db, task["id"])
            log.info("cancel 成功 tm_id=%s", task["tm_meeting_id"])
            return
        err = r.get("message", "取消失败")
        # 回队次数记录在 error 前缀 RETRY<n>
        count = 0
        prev = task["error"] or ""
        if prev.startswith("RETRY"):
            try:
                count = int(prev.split(":", 1)[0][5:])
            except Exception:
                count = 0
        count += 1
        if count >= CANCEL_RETRY_LIMIT:
            db.set_task_error(self.db, task["id"], f"{err}（重试{count}次超限）")
            log.error("cancel 最终失败 tm_id=%s: %s", task["tm_meeting_id"], err)
        else:
            next_run = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(time.time() + 60)
            )
            db.requeue_task(self.db, task["id"], f"RETRY{count}: {err}", next_run)
            log.warning("cancel 失败 tm_id=%s 第%d次，60s 后重试", task["tm_meeting_id"], count)
