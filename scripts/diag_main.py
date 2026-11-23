# -*- coding: utf-8 -*-
"""dump 腾讯会议主窗口的 UIA 控件树，判断是登录页还是主页、按钮位置。

用法（项目根目录、虚拟环境）：
    python scripts\\diag_main.py

生成 scripts\\diag_main_report.txt（UTF-8），发回即可。
只读，不点击、不创建/取消会议。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPORT = os.path.join(ROOT, "scripts", "diag_main_report.txt")
_lines = []


def out(s=""):
    _lines.append(str(s))
    try:
        print(s)
    except Exception:
        pass


out("=" * 60)
out("腾讯会议主窗口控件树诊断")
out("=" * 60)

try:
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    from server.driver.pywinauto_tm import PywinautoDriver
    drv = PywinautoDriver(cfg.get("tm", {}))
    main = drv._ensure_main()
    r = main.rectangle()
    out("主窗口: title=%r rect=%s 尺寸=%dx%d" %
        (main.window_text(), r, r.right - r.left, r.bottom - r.top))

    out("\n[1] 关键字检测（判断登录页/主页）")
    all_text = []
    for el in main.descendants():
        try:
            t = (el.window_text() or "").strip()
            if t:
                all_text.append(t)
        except Exception:
            pass
    blob = " | ".join(all_text)
    for kw in ("登录", "扫码", "手机号", "预定会议", "加入会议", "快速会议", "我发起的会议", "密码"):
        out("  %-10s : %s" % (kw, "命中" if kw in blob else "无"))
    out("  文本样本(前25): %s" % all_text[:25])

    out("\n[2] 可点击元素（Button/MenuItem/Hyperlink/ListItem）")
    n = 0
    for el in main.descendants():
        try:
            ct = el.element_info.control_type
            if ct not in ("Button", "MenuItem", "Hyperlink", "ListItem", "TabItem"):
                continue
            t = (el.window_text() or "").strip()
            rr = el.rectangle()
            out("  %-10s %-24r rect=(%d,%d,%d,%d) 中心=(%d,%d)" %
                (ct, t[:22], rr.left, rr.top, rr.right, rr.bottom,
                 (rr.left + rr.right) // 2, (rr.top + rr.bottom) // 2))
            n += 1
            if n >= 80:
                out("  ...(截断)")
                break
        except Exception:
            pass

    out("\n[3] 输入类元素（Edit/Spinner/ComboBox）")
    n = 0
    for el in main.descendants():
        try:
            ct = el.element_info.control_type
            if ct not in ("Edit", "Spinner", "ComboBox"):
                continue
            t = (el.window_text() or "").strip()
            try:
                v = el.get_value()
            except Exception:
                v = ""
            rr = el.rectangle()
            out("  %-10s text=%-16r value=%-16r rect=(%d,%d,%d,%d)" %
                (ct, t[:14], str(v)[:14], rr.left, rr.top, rr.right, rr.bottom))
            n += 1
            if n >= 40:
                out("  ...(截断)")
                break
        except Exception:
            pass

    out("\n[4] 全部有文本的元素（前 100 个，含相对主窗口坐标）")
    n = 0
    for el in main.descendants():
        try:
            t = (el.window_text() or "").strip()
            if not t:
                continue
            rr = el.rectangle()
            out("  %-8s %-20r rel=(%d,%d) ctrl=%s" %
                (el.element_info.control_type, t[:18],
                 rr.left - r.left, rr.top - r.top,
                 el.element_info.control_type))
            n += 1
            if n >= 100:
                out("  ...(截断)")
                break
        except Exception:
            pass

except Exception as e:
    out("诊断失败: %s: %s" % (type(e).__name__, e))

out("\n" + "=" * 60)
out("请把 diag_main_report.txt 发回。")
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")
print("\n报告已写入: " + REPORT)
