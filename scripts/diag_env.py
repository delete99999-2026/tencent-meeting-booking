# -*- coding: utf-8 -*-
"""环境诊断脚本（部署到新机器后运行，把生成的诊断文件发回用于校准）。

用法（在 项目根目录 目录下）：
    python scripts\\diag_env.py

输出：
    1) 屏幕打印关键结论；
    2) 生成 scripts\\diag_env_report.txt（UTF-8），把这个文件发回。

脚本只读不改任何配置，不创建/取消会议。
"""
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPORT = os.path.join(ROOT, "scripts", "diag_env_report.txt")
_lines = []

def out(s=""):
    _lines.append(str(s))
    try:
        print(s)
    except Exception:
        pass

def flush_report():
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    out()
    out("诊断报告已写入: " + REPORT)

out("=" * 60)
out("腾讯会议预订平台 - 环境诊断报告")
out("=" * 60)

# 1) 系统 / 显示器 / DPI
out("\n[1] 系统与显示器")
try:
    import platform
    out("  OS: %s %s" % (platform.system(), platform.version()))
    out("  Python: %s (%s)" % (platform.python_version(), sys.executable))
except Exception as e:
    out("  系统信息失败: %s" % e)

try:
    u = ctypes.windll.user32
    # DPI 感知前先尝试设置，使后续坐标与 pywinauto 一致
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
    out("  屏幕分辨率: %dx%d" % (u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
    # 每个显示器的 DPI
    try:
        hmon = u.MonitorFromPoint(0, 0, 2)  # MONITOR_DEFAULTTOPRIMARY
        dx = ctypes.c_uint(); dy = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        out("  主显示器 DPI: %d x %d  (缩放约 %d%%)" % (dx.value, dy.value, round(dx.value / 96.0 * 100)))
    except Exception as e:
        out("  DPI 查询失败: %s" % e)
except Exception as e:
    out("  显示器信息失败: %s" % e)

# 2) 配置 / 依赖
out("\n[2] 配置与依赖")
cfg = {}
try:
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    out("  driver_mode: %s" % cfg.get("driver_mode"))
    tm = cfg.get("tm", {})
    out("  tm.exe: %s" % tm.get("exe"))
    out("  tm.exe 存在: %s" % os.path.exists(tm.get("exe") or ""))
    out("  main_window_re: %s" % tm.get("main_window_re"))
    out("  dialog_title_re: %s" % tm.get("dialog_title_re"))
    out("  book_button_rel: %s (未配置则用默认 [176,365])" % tm.get("book_button_rel"))
except Exception as e:
    out("  读取配置失败: %s" % e)

# 生产运行仅依赖 pywinauto / win32api / win32clipboard。
# easyocr 只是开发期探针脚本（probe_*.py）用的可选截图识别工具，不影响预订，
# 故仅在缺失时给出说明，不作为"缺失/错误"报告，避免误以为环境有问题。
for mod in ("pywinauto", "win32api", "win32clipboard"):
    try:
        __import__(mod)
        out("  %-16s OK" % mod)
    except Exception as e:
        out("  %-16s 缺失: %s" % (mod, e))
try:
    __import__("easyocr")
    out("  %-16s OK (可选诊断工具)" % "easyocr")
except Exception:
    out("  %-16s 未安装（可选，仅开发期探针脚本截图识别用，预订功能不需要）" % "easyocr")

# 3) 客户端进程
out("\n[3] 腾讯会议客户端进程")
pids = set()
try:
    from server.driver.pywinauto_tm import _process_pids, PywinautoDriver
    exe = (cfg.get("tm", {}).get("exe") or "")
    exe_name = os.path.basename(exe) or "WeMeetApp.exe"
    pids = _process_pids(exe_name)
    out("  按 exe=%s 找到 PID: %s" % (exe_name, sorted(pids) if pids else "无！"))
except Exception as e:
    out("  进程枚举失败: %s" % e)

# 4) 客户端窗口
out("\n[4] 客户端顶层窗口")
try:
    from pywinauto import Desktop
    d = Desktop(backend="uia")
    wins = []
    for w in d.windows():
        try:
            if pids and w.process_id() not in pids:
                continue
            wins.append((w.window_text(), w.rectangle(), w.element_info.class_name, w.handle))
        except Exception:
            continue
    if not wins:
        out("  未枚举到客户端窗口（客户端可能未启动/未登录，或 exe 名不匹配）")
    for title, rect, cls, hwnd in wins:
        out("  标题=%r" % title)
        out("    rect=%s  class=%s  hwnd=%s" % (rect, cls, hwnd))
        out("    宽x高=%dx%d" % (rect.right - rect.left, rect.bottom - rect.top))
except Exception as e:
    out("  窗口枚举失败: %s" % e)

# 5) 尝试打开预定对话框并定位按钮（只读点击，不填字段不提交）
out("\n[5] 尝试打开预定对话框")
try:
    from server.driver.pywinauto_tm import PywinautoDriver
    tmcfg = cfg.get("tm", {})
    drv = PywinautoDriver(tmcfg)
    main = drv._ensure_main()
    mr = main.rectangle()
    mw, mh = mr.right - mr.left, mr.bottom - mr.top
    out("  主窗口 rect=%s 宽x高=%dx%d" % (mr, mw, mh))
    dlg_re = tmcfg.get("dialog_title_re", "预定会议")
    pids_list = list(pids) if pids else None
    # 尝试文本定位
    clicked_text = drv._click_by_text(main, tmcfg.get("book_button", "预定会议"), timeout=2)
    out("  文本定位『预定会议』按钮: %s" % clicked_text)
    time.sleep(0.5)
    dlg = drv._find_window(dlg_re, timeout=2, exclude=main, pids=pids_list)
    candidates = []
    if not dlg:
        rel = tmcfg.get("book_button_rel", [176, 365])
        pts = [
            ("配置偏移", mr.left + int(rel[0]), mr.top + int(rel[1])),
            ("相对比例", mr.left + int(mw * 0.188), mr.top + int(mh * 0.572)),
        ]
        for name, cx, cy in pts:
            drv._ensure_topmost(main)
            time.sleep(0.2)
            drv._click_xy(cx, cy, pause=0.9)
            dlg = drv._find_window(dlg_re, timeout=1.5, exclude=main, pids=pids_list)
            candidates.append("%s 点击(%d,%d) -> %s" % (name, cx, cy, "成功" if dlg else "未打开"))
            out("  " + candidates[-1])
            if dlg:
                break
    out("  对话框最终: %s" % ("已打开 rect=%s" % dlg.rectangle() if dlg else "未打开"))

    # 若打开，dump 对话框控件树（帮助判断版本差异）
    if dlg:
        out("\n  [对话框控件摘要]")
        cnt = 0
        for t, el in drv._desc_texts(dlg):
            t = (t or "").strip()
            if t:
                out("    %r rect=%s ctrl=%s" % (t[:40], el.rectangle(), el.element_info.control_type))
                cnt += 1
                if cnt >= 40:
                    out("    ...(截断)")
                    break

    # 截图（若有 PIL）
    try:
        from PIL import ImageGrab
        shot = os.path.join(ROOT, "scripts", "diag_env_screen.png")
        ImageGrab.grab().save(shot)
        out("  主屏截图: " + shot)
    except Exception as e:
        out("  截图失败: %s" % e)
except Exception as e:
    out("  对话框尝试失败: %s" % e)

out("\n" + "=" * 60)
out("诊断结束。请把 diag_env_report.txt（及 diag_env_screen.png 若有）发回。")
flush_report()
