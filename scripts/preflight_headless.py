# -*- coding: utf-8 -*-
"""无显示器虚拟机部署前自检脚本（只读，不改配置、不创建/取消会议）。

用途：在 Windows 11 虚拟机上运行，判断本机是否满足「虚拟显示器 + GUI 驱动」
方案的运行前提，输出每项是否通过及修复建议，并生成 UTF-8 报告文件。

用法：
    python scripts\\preflight_headless.py

输出：
    scripts\\preflight_headless_report.txt  （把这个文件发回用于排障）

判定项：
    [1] 操作系统 / 会话（是否交互桌面、session 0 会致命）
    [2] 显示器 / DPI（是否有可用屏幕；分辨率/缩放影响坐标）
    [3] Python 与依赖
    [4] 腾讯会议客户端（exe 存在、进程、窗口能否枚举）
    [5] 虚拟显示器驱动（Parsec VDD / 通用 monitor 检测）
    [6] 自动登录与锁屏策略（注册表只读检查）
    [7] 服务监听端口占用
    [8] 综合结论与修复建议
"""
import ctypes
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPORT = os.path.join(ROOT, "scripts", "preflight_headless_report.txt")
_lines = []
_PASS, _FAIL, _WARN = "通过", "失败", "警告"


def out(s=""):
    _lines.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def item(status, title, detail=""):
    tag = {"通过": "[OK]  ", "失败": "[FAIL]", "警告": "[WARN]"}[status]
    out("  %s %s" % (tag, title))
    if detail:
        for line in str(detail).splitlines():
            out("         " + line)


def flush():
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    out()
    out("报告已写入: " + REPORT)


def main():
    out("=" * 64)
    out("腾讯会议预订平台 - 无显示器虚拟机部署自检")
    out("=" * 64)
    results = {}  # key -> PASS/FAIL/WARN

    # ---------- [1] 系统 / 会话 ----------
    out("\n[1] 操作系统与桌面会话")
    out("  OS: %s %s" % (platform.system(), platform.version()))
    out("  Python: %s (%s)" % (platform.python_version(), sys.executable))
    try:
        session_id = ctypes.windll.kernel32.GetCurrentProcessId  # placeholder
        # WTSQuerySessionInformationW 获取当前会话 id
        import ctypes.wintypes as wintypes
        kernel32 = ctypes.windll.kernel32
        cur_pid = kernel32.GetCurrentProcessId()
        sid = wintypes.DWORD()
        ok = kernel32.ProcessIdToSessionId(cur_pid, ctypes.byref(sid))
        if ok:
            out("  当前进程 Session ID: %d" % sid.value)
            if sid.value == 0:
                item(_FAIL, "运行在 Session 0（无交互桌面）",
                     "pywinauto 无法看到窗口树。必须配置为自动登录到交互用户会话后在该会话启动服务。")
                results["session"] = _FAIL
            else:
                item(_PASS, "运行在交互桌面会话 (Session %d)" % sid.value)
                results["session"] = _PASS
        else:
            item(_WARN, "无法确定会话 ID")
            results["session"] = _WARN
    except Exception as e:
        item(_WARN, "会话检测失败: %s" % e)
        results["session"] = _WARN

    # ---------- [2] 显示器 / DPI ----------
    out("\n[2] 显示器与 DPI")
    try:
        u = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                u.SetProcessDPIAware()
            except Exception:
                pass
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        out("  主屏分辨率: %dx%d" % (w, h))
        if w <= 0 or h <= 0:
            item(_FAIL, "无有效显示器",
                 "需要安装虚拟显示器驱动（如 Parsec VDD）并保持一块屏幕在线。")
            results["display"] = _FAIL
        else:
            item(_PASS, "检测到有效桌面 %dx%d" % (w, h))
            results["display"] = _PASS
        # 枚举显示器数量
        try:
            import ctypes.wintypes as wintypes
            mi = wintypes.RECT()
            # EnumDisplayMonitors 回调计数
            MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR,
                                                 wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                                 wintypes.LPARAM)
            count = [0]

            def _cb(hmon, hdc, lprc, data):
                count[0] += 1
                return 1

            u.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
            out("  显示器数量: %d" % count[0])
            if count[0] == 0:
                item(_FAIL, "未枚举到任何显示器",
                     "安装 Parsec VDD 后会新增一块虚拟显示器；驱动必须处于已连接状态。")
                results["monitor_count"] = _FAIL
            else:
                item(_PASS, "枚举到 %d 块显示器" % count[0])
                results["monitor_count"] = _PASS
        except Exception as e:
            item(_WARN, "显示器数量检测失败: %s" % e)
            results["monitor_count"] = _WARN
        # DPI
        try:
            hmon = u.MonitorFromPoint(0, 0, 2)
            dx, dy = ctypes.c_uint(), ctypes.c_uint()
            ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
            scale = round(dx.value / 96.0 * 100)
            out("  主显示器 DPI: %d x %d (缩放约 %d%%)" % (dx.value, dy.value, scale))
            if scale != 100:
                item(_WARN, "DPI 缩放 %d%%（非 100%%）" % scale,
                     "驱动已内置 DPI 感知，但坐标校准基于 100%%；若预订失败，"
                     "请把虚拟显示器设为 100%% 缩放或在 config.json 配 book_button_rel。")
                results["dpi"] = _WARN
            else:
                item(_PASS, "DPI 缩放 100%（与坐标校准一致）")
                results["dpi"] = _PASS
        except Exception as e:
            item(_WARN, "DPI 检测失败: %s" % e)
            results["dpi"] = _WARN
    except Exception as e:
        item(_FAIL, "显示器信息检测整体失败: %s" % e)
        results["display"] = _FAIL

    # ---------- [3] 依赖 ----------
    out("\n[3] Python 与依赖")
    if sys.version_info < (3, 10):
        item(_FAIL, "Python 版本过低: %s（需 3.10+）" % platform.python_version())
        results["python"] = _FAIL
    else:
        item(_PASS, "Python %s" % platform.python_version())
        results["python"] = _PASS
    for mod in ("flask", "pywinauto", "win32api", "win32clipboard"):
        try:
            __import__(mod)
            item(_PASS, "依赖 %s" % mod)
        except Exception as e:
            item(_FAIL, "依赖缺失 %s: %s" % (mod, e),
                 "执行: python -m pip install -r requirements.txt")
            results["dep_" + mod] = _FAIL

    # ---------- [4] 客户端 ----------
    out("\n[4] 腾讯会议客户端")
    cfg = {}
    try:
        import json
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception as e:
        item(_FAIL, "读取 config.json 失败: %s" % e)
        results["config"] = _FAIL
    tm = cfg.get("tm", {})
    exe = tm.get("exe") or ""
    exe_name = os.path.basename(exe) or "WeMeetApp.exe"
    out("  配置 exe: %s" % exe)
    if exe and os.path.exists(exe):
        item(_PASS, "腾讯会议主程序存在")
        results["exe"] = _PASS
    else:
        item(_FAIL, "腾讯会议主程序不存在（请修改 config.json 的 tm.exe）")
        results["exe"] = _FAIL
    try:
        from server.driver.pywinauto_tm import _process_pids, PywinautoDriver
        pids = _process_pids(exe_name)
        if pids:
            item(_PASS, "客户端进程运行中: %s" % sorted(pids))
            results["client_proc"] = _PASS
        else:
            item(_WARN, "未发现客户端进程（需先启动并登录腾讯会议）")
            results["client_proc"] = _WARN
        # 窗口枚举
        try:
            drv = PywinautoDriver(tm)
            main = drv._find_window(tm.get("main_window_re", "腾讯会议"), timeout=2,
                                    pids=pids or None)
            if main:
                r = main.rectangle()
                item(_PASS, "主窗口可见: %s 宽x高=%dx%d" %
                     (main.window_text(), r.right - r.left, r.bottom - r.top))
                results["client_window"] = _PASS
            else:
                item(_FAIL, "枚举不到主窗口（客户端未登录？会话不匹配？）")
                results["client_window"] = _FAIL
        except Exception as e:
            item(_WARN, "主窗口枚举异常: %s" % e)
            results["client_window"] = _WARN
    except Exception as e:
        item(_WARN, "客户端检测失败: %s" % e)
        results["client"] = _WARN

    # ---------- [5] 虚拟显示器驱动 ----------
    out("\n[5] 虚拟显示器驱动")
    vdd_found = False
    try:
        # Parsec VDD 设备节点检测：Root\Parsec\VDA
        import subprocess
        q = subprocess.run(["pnputil", "/enum-devices"], capture_output=True,
                           text=True, timeout=20)
        txt = (q.stdout or "") + (q.stderr or "")
        for kw in ("Parsec", "VDA", "Virtual Display", "IddCx", "Indirect Display"):
            if kw.lower() in txt.lower():
                vdd_found = True
                break
    except Exception:
        pass
    if vdd_found:
        item(_PASS, "检测到虚拟显示器驱动迹象（Parsec VDD / IddCx）")
        results["vdd"] = _PASS
    else:
        item(_WARN, "未明确检测到虚拟显示器驱动",
             "若虚拟机无物理显示器，请安装 Parsec VDD（0.41 稳定版）：\n"
             "  1) 下载 https://builds.parsec.app/vdd/parsec-vdd-0.41.0.0.exe\n"
             "  2) 管理员运行: parsec-vdd-0.41.0.0.exe /S\n"
             "  3) 用 ParsecDisplay 或 parsec-vdd CLI 添加一块 1920x1080 虚拟屏并保持在线\n"
             "  详见《无显示器虚拟机部署指南.md》。")
        results["vdd"] = _WARN

    # ---------- [6] 自动登录 / 锁屏 ----------
    out("\n[6] 自动登录与锁屏策略（只读检查）")
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon")
        try:
            auto, _ = winreg.QueryValueEx(key, "AutoAdminLogon")
            if str(auto) == "1":
                item(_PASS, "已启用自动登录 (AutoAdminLogon=1)")
                results["autologon"] = _PASS
            else:
                item(_FAIL, "未启用自动登录 (AutoAdminLogon=%s)" % auto,
                     "无显示器虚拟机必须自动登录到交互桌面，否则服务运行在 session 0、看不到窗口。"
                     "执行 setup_headless.ps1 配置。")
                results["autologon"] = _FAIL
        except FileNotFoundError:
            item(_FAIL, "未配置自动登录 (缺少 AutoAdminLogon)",
                 "执行 setup_headless.ps1 配置。")
            results["autologon"] = _FAIL
        # ScreenSaverIsSecure
        try:
            desk = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            sec, _ = winreg.QueryValueEx(desk, "ScreenSaverIsSecure")
            if str(sec) == "0":
                item(_PASS, "未启用屏保密码锁定")
            else:
                item(_WARN, "屏保密码锁定开启 (=1)，休眠/锁屏会中断 GUI 操作")
        except FileNotFoundError:
            item(_PASS, "未配置屏保锁定")
        winreg.CloseKey(key)
    except Exception as e:
        item(_WARN, "自动登录策略检测失败: %s" % e)
        results["autologon"] = _WARN

    # ---------- [7] 端口 ----------
    out("\n[7] 服务端口占用")
    port = cfg.get("port", 8000)
    try:
        import socket
        s = socket.socket()
        s.settimeout(1)
        r = s.connect_ex(("127.0.0.1", int(port)))
        s.close()
        if r == 0:
            item(_WARN, "端口 %s 已被占用（服务可能已在运行）" % port)
            results["port"] = _WARN
        else:
            item(_PASS, "端口 %s 空闲" % port)
            results["port"] = _PASS
    except Exception as e:
        item(_WARN, "端口检测失败: %s" % e)
        results["port"] = _WARN

    # ---------- [8] 结论 ----------
    out("\n" + "=" * 64)
    out("[8] 综合结论与修复建议")
    fatal = [k for k, v in results.items() if v == _FAIL]
    warns = [k for k, v in results.items() if v == _WARN]
    if not fatal:
        out("  结论: 基本满足运行前提" + ("（有 %d 项警告需留意）" % len(warns) if warns else "。"))
        out("  建议: 先运行 python scripts\\diag_env.py 校准坐标，再启动服务实测预订。")
    else:
        out("  结论: 存在 %d 项致命问题，必须先解决：" % len(fatal))
        for k in fatal:
            out("    - " + k)
        out("  按上面每项 [FAIL] 的提示修复后重新运行本脚本。")
    out("=" * 64)
    flush()
    return 0 if not fatal else 2


if __name__ == "__main__":
    raise SystemExit(main())
