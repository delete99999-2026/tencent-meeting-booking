# -*- coding: utf-8 -*-
"""Delayed desktop snapshot for headless diagnosis.

Usage: python scripts/snapshot_headless.py [delay_seconds]

Started by keep_session.bat BEFORE tscon disconnects RDP. It sleeps, then
captures the real desktop/window state after the session was switched to
console, and writes scripts/headless_snapshot.txt (UTF-8). This is the only
way to see what the desktop looks like when nobody is connected.

No screenshot/OCR/PIL needed; pure ctypes + pywinauto.
"""
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPORT = os.path.join(ROOT, "scripts", "headless_snapshot.txt")
delay = int(sys.argv[1]) if len(sys.argv) > 1 else 45

lines = []


def out(s=""):
    lines.append(str(s))


def flush():
    try:
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


out("=" * 60)
out("Headless desktop snapshot (after RDP disconnected)")
out("=" * 60)
out("Snapshot will be taken in %d seconds..." % delay)
flush()

# Sleep in the background; time.sleep keeps running after RDP disconnect.
time.sleep(delay)

out("\nTaken at: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
try:
    u = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
    out("Screen: %dx%d" % (u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
    out("SM_CMONITORS (monitor count): %d" % u.GetSystemMetrics(80))
except Exception as e:
    out("Screen info failed: %s" % e)

try:
    sid = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(
        ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    out("SessionId: %d (0=Session 0 no desktop, >0=interactive)" % sid.value)
except Exception as e:
    out("Session info failed: %s" % e)

try:
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    from server.driver.pywinauto_tm import _process_pids
    from pywinauto import Desktop
    exe = os.path.basename(cfg.get("tm", {}).get("exe") or "WeMeetApp.exe")
    pids = _process_pids(exe)
    out("Client PIDs (%s): %s" % (exe, sorted(pids)))
    out("\nTop-level windows of client processes:")
    found = False
    for w in Desktop(backend="uia").windows():
        try:
            if pids and w.process_id() not in pids:
                continue
            found = True
            r = w.rectangle()
            out("  title=%r rect=(%d,%d,%d,%d) class=%s offscreen=%s" %
                (w.window_text(), r.left, r.top, r.right, r.bottom,
                 w.element_info.class_name,
                 (r.left <= -10000 or r.top <= -10000)))
        except Exception:
            continue
    if not found:
        out("  (no client window enumerable)")
except Exception as e:
    out("Window enumeration failed: %s: %s" % (type(e).__name__, e))

out("\n" + "=" * 60)
out("Send headless_snapshot.txt back for analysis.")
flush()
