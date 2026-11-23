# -*- coding: utf-8 -*-
"""DryRun 驱动：模拟腾讯会议客户端操作。

用于开发机/自测环境（本机未装腾讯会议客户端）。
行为：
  - book   模拟 0.2s 操作耗时，恒成功，返回递增假会议号；
  - cancel 恒成功；
  - 可通过构造参数 fail_book / fail_cancel 注入失败，验证重试/联动逻辑。
"""
import itertools
import threading
import time

from server.driver.base import TMDriver


class DryRunDriver(TMDriver):
    def __init__(self, fail_book=False, fail_cancel=False):
        self._lock = threading.Lock()
        self._ids = itertools.count(900000001)
        self.fail_book = fail_book
        self.fail_cancel = fail_cancel
        self.calls = []  # 记录调用，便于自测断言

    def book(self, subject, start_at, end_at):
        with self._lock:
            time.sleep(0.2)
            self.calls.append(("book", subject, start_at, end_at))
        if self.fail_book:
            return {"ok": False, "meeting_id": None, "message": "dry-run 注入失败：无法定位预定按钮"}
        with self._lock:
            mid = next(self._ids)
        return {"ok": True, "meeting_id": str(mid), "message": "dry-run 预订成功"}

    def cancel(self, tm_meeting_id, subject, start_at):
        with self._lock:
            time.sleep(0.1)
            self.calls.append(("cancel", tm_meeting_id, subject, start_at))
        if self.fail_cancel:
            return {"ok": False, "message": "dry-run 注入失败：无法在列表中找到会议"}
        return {"ok": True, "message": "dry-run 取消成功"}

    def list_scheduled(self):
        return []

    def inspect(self):
        return "dry-run 驱动无客户端控件树"
