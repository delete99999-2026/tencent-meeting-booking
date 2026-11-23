# -*- coding: utf-8 -*-
"""清理测试遗留数据：删除全部会议与任务队列（保留用户）。"""
import os
import sqlite3

db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "meeting.db")
c = sqlite3.connect(db)
c.execute("DELETE FROM tm_tasks")
c.execute("DELETE FROM meetings")
c.commit()
print("meetings:", c.execute("SELECT COUNT(*) FROM meetings").fetchone()[0])
print("tasks:", c.execute("SELECT COUNT(*) FROM tm_tasks").fetchone()[0])
print("users:", c.execute("SELECT COUNT(*) FROM users").fetchone()[0])
c.close()
