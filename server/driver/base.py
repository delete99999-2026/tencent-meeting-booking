# -*- coding: utf-8 -*-
"""驱动抽象基类与错误类型。

约定返回值（dict）：
  book:   {'ok': bool, 'meeting_id': str|None, 'message': str}
  cancel: {'ok': bool, 'message': str}
"""
from abc import ABC, abstractmethod


class TMDriverError(Exception):
    """带阶段信息的驱动异常。stage 用于定位失败在哪一步。"""

    def __init__(self, stage, message):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


class TMDriver(ABC):
    @abstractmethod
    def book(self, subject: str, start_at: str, end_at: str,
             password: str = None) -> dict:
        """预订会议。start_at/end_at 为 'YYYY-MM-DD HH:MM'（同一自然日）。

        password: 6 位数字入会密码；为空则由客户端自动生成。
        返回可含 'invite_link'（腾讯侧入会短链），供上层展示。
        """

    @abstractmethod
    def cancel(self, tm_meeting_id: str, subject: str, start_at: str) -> dict:
        """按腾讯会议号取消（删除）已订会议；号缺失时按主题+时间兜底。"""

    def list_scheduled(self) -> list:
        """尽力列出腾讯侧已订会议（对账用）。默认空。"""
        return []

    def inspect(self) -> str:
        """导出当前 UIA 控件树文本（供管理员校准 selector）。默认空。"""
        return "(当前驱动不支持 inspect)"
