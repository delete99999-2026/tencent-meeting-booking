# -*- coding: utf-8 -*-
"""修复客户端：定位真实 exe 路径、列出窗口、还原最小化/移出屏幕的窗口并置前。

在无显示器 VM 上，腾讯会议窗口可能被挪到 (-32000,-32000)（最小化或虚拟屏
拓扑变化）。本脚本复用驱动已验证的进程枚举，找到真正的安装路径，更新
config.json，并尝试把主窗口还原到屏幕内、置前。

运行（在项目根目录的虚拟环境里）：
    python scripts\\fix_client.py

只读诊断模式（不改 config.json、不动窗口）：
    python scripts\\fix_client.py --dry
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.driver.pywinauto_tm import _process_pids  # noqa: E402
from pywinauto import Desktop  # noqa: E402

EXE_NAME = "WeMeetApp.exe"
CONFIG = os.path.join(ROOT, "config.json")
DRY = "--dry" in sys.argv

# Win32 窗口操作
user32 = ctypes.windll.user32
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def exe_path_of(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def main():
    print("=" * 60)
    print("腾讯会议客户端修复工具")
    print("=" * 60)

    # 1) 找进程与真实 exe 路径
    pids = _process_pids(EXE_NAME)
    print(f"\n[1] 找到客户端进程 PID: {sorted(pids)}")
    exe_paths = {}
    for pid in sorted(pids):
        p = exe_path_of(pid)
        if p:
            exe_paths[pid] = p
    real_paths = sorted(set(exe_paths.values()))
    print(f"[2] 客户端实际安装路径: {real_paths or '（无法读取）'}")

    # 2) 更新 config.json 的 tm.exe
    if real_paths and not DRY:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
        old = cfg.get("tm", {}).get("exe")
        new = real_paths[0]
        if old != new:
            cfg["tm"]["exe"] = new
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"[3] 已更新 config.json tm.exe: {old}  ->  {new}")
        else:
            print(f"[3] config.json tm.exe 已经是正确路径")
    elif DRY:
        print("[3] --dry 模式：不修改 config.json")
    else:
        print("[3] 未找到路径，config.json 未改")

    # 3) 列出窗口并处理离屏窗口
    print("\n[4] 客户端顶层窗口:")
    desk = Desktop(backend="uia")
    moved = 0
    for w in desk.windows():
        try:
            if w.process_id() not in pids:
                continue
            r = w.rectangle()
            hwnd = w.handle
            title = window_title(hwnd)
            iconic = user32.IsIconic(hwnd)
            offscreen = r.left <= -10000 or r.top <= -10000
            print(f"  标题={title!r} rect=({r.left},{r.top},{r.right},{r.bottom}) "
                  f"class={w.class_name()} hwnd={hwnd} 最小化={bool(iconic)} 离屏={offscreen}")
            if (iconic or offscreen) and not DRY:
                # 还原 + 移到屏幕左上角可见区 + 置前
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 100, 100, 900, 650,
                                    SWP_SHOWWINDOW)
                user32.SetForegroundWindow(hwnd)
                moved += 1
                print("      -> 已还原并移到屏幕内 (100,100)，置顶")
        except Exception as e:
            print(f"  （跳过一个窗口: {e}）")

    if DRY:
        print("\n--dry 诊断模式，未做任何修改。")
    else:
        print(f"\n[5] 共还原 {moved} 个窗口。")

    print("\n" + "=" * 60)
    print("下一步：")
    print("  1) 用 RDP 连进虚拟机，看腾讯会议是否已显示正常登录界面；")
    print("  2) 若停在登录框，手动登录一次并保持登录状态；")
    print("  3) 再运行 python scripts\\preflight_headless.py 复查；")
    print("  4) 主窗口标题应为『腾讯会议』（不再是 WeMeetApp）。")
    print("=" * 60)


if __name__ == "__main__":
    main()
