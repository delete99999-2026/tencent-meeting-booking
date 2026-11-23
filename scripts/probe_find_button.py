# -*- coding: utf-8 -*-
"""定位主窗口"预定会议"按钮坐标（两种方式，均不依赖截图/OCR/PIL）。

方式一（默认，最快最准）：你先把鼠标移到"预定会议"按钮上，脚本读取鼠标
    当前位置，点一下验证是否弹出预定对话框，然后输出相对坐标。
    用法：python scripts\\probe_find_button.py

方式二（--grid，自动网格扫描）：在左侧导航区逐点点击检测，适合无人操作。
    用法：python scripts\\probe_find_button.py --grid

两种方式都只点击、只检测对话框，绝不填写字段或提交会议；误开的窗口自动
关闭，找到的对话框也会关闭。生成 scripts\\probe_find_button_report.txt。
"""
import json
import os
import sys
import time
import ctypes
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.driver.pywinauto_tm import PywinautoDriver  # noqa: E402
from pywinauto import Desktop  # noqa: E402

REPORT = os.path.join(ROOT, "scripts", "probe_find_button_report.txt")
GRID = "--grid" in sys.argv
_lines = []


def out(s=""):
    _lines.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def mouse_pos():
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def close_popups(pids, main_handle):
    for w in Desktop(backend="uia").windows():
        try:
            if w.process_id() in pids and w.handle != main_handle:
                w.close()
        except Exception:
            pass


def try_click(drv, main, r, dlg_re, ax, ay):
    """点一个点，返回 (是否打开, dlg)。"""
    drv._click_xy(ax, ay, pause=0.25)
    time.sleep(0.15)
    live = set(drv._client_pids())
    dlg = drv._find_window(dlg_re, timeout=0.5, exclude=main, pids=live or None)
    return dlg, live


out("=" * 60)
out("腾讯会议『预定会议』按钮定位")
out("=" * 60)
found = None
try:
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    tm = cfg.get("tm", {})
    dlg_re = tm.get("dialog_title_re", "预定会议")
    drv = PywinautoDriver(tm)
    main = drv._ensure_main()
    drv._ensure_topmost(main)
    r = main.rectangle()
    mw, mh = r.right - r.left, r.bottom - r.top
    out("主窗口 rect=%s 尺寸=%dx%d" % (r, mw, mh))

    if not GRID:
        # 方式一：读取鼠标当前位置
        mx, my = mouse_pos()
        out("\n[方式一] 当前鼠标位置 = (%d, %d)" % (mx, my))
        if not (r.left <= mx <= r.right and r.top <= my <= r.bottom):
            out("⚠ 鼠标不在主窗口内！请先用 RDP 连进虚拟机，把鼠标移到『预定会议』按钮上，再运行本脚本。")
        else:
            out("  相对坐标 = [%d, %d]，点击验证中..." % (mx - r.left, my - r.top))
            dlg, live = try_click(drv, main, r, dlg_re, mx, my)
            if dlg:
                found = (mx, my, dlg)
            else:
                close_popups(live, main.handle)
                out("  ✗ 点击后未弹出预定对话框，鼠标位置可能不是按钮。")
    else:
        # 方式二：网格扫描（聚焦左侧导航区，步长小、置顶只做一次）
        out("\n[方式二] 网格扫描左侧导航区...")
        tried = 0
        # 按钮通常在左半区的中下部；也覆盖等比缩放点 (mw*0.188, mh*0.572)
        ys = list(range(r.top + int(mh * 0.30), r.bottom - 20, 25))
        xs = list(range(r.left + 30, r.left + min(360, mw - 20), 25))
        for ay in ys:
            for ax in xs:
                if found:
                    break
                tried += 1
                dlg, live = try_click(drv, main, r, dlg_re, ax, ay)
                if dlg:
                    found = (ax, ay, dlg)
                    break
                close_popups(live, main.handle)
            if found:
                break
        out("共点击 %d 个网格点" % tried)

    if not found:
        out("\n[未找到] 未弹出预定对话框。建议：")
        out("  · 方式一：RDP 连进 VM，鼠标悬停在『预定会议』按钮上后重跑（不带 --grid）；")
        out("  · 确认腾讯会议停在主页、客户端已登录；")
        out("  · 若是 --grid 未找到，试 --grid 前先把窗口最大化。")
    else:
        ax, ay, dlg = found
        rx, ry = ax - r.left, ay - r.top
        out("\n[找到] 预定会议按钮")
        out("  ★ 请把 config.json 的 tm.book_button_rel 设为 [%d, %d]" % (rx, ry))
        out("  （绝对坐标 %d,%d；对话框 %r rect=%s）" %
            (ax, ay, dlg.window_text(), dlg.rectangle()))
        out("\n[对话框控件摘要]")
        cnt = 0
        for t, el in drv._desc_texts(dlg):
            t = (t or "").strip()
            if t:
                out("  %-22r ctrl=%s" % (t[:20], el.element_info.control_type))
                cnt += 1
                if cnt >= 25:
                    out("  ...(截断)")
                    break
        try:
            dlg.close()
        except Exception:
            pass

except Exception as e:
    out("定位失败: %s: %s" % (type(e).__name__, e))

out("\n" + "=" * 60)
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")
print("\n报告已写入: " + REPORT)
