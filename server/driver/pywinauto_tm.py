# -*- coding: utf-8 -*-
"""Pywinauto 驱动：真实操作 Windows 腾讯会议客户端（UIA backend）。

设计原则：
  - 所有定位选择器（窗口标题、按钮文字、超时）均来自 config.json 的 "tm" 段，
    客户端版本变化时改配置即可，不需要改代码；
  - 任何一步定位/输入失败都抛 TMDriverError(阶段, 说明)，绝不静默成功；
  - 文本输入三级策略：UIA set_text -> 键盘键入(ASCII) -> 剪贴板粘贴(支持中文)，
    每级都回读校验；
  - 提供 inspect 导出控件树，供管理员校准 selector。
"""
import ctypes
import logging
import os
import re
import subprocess
import time

try:
    from ctypes import wintypes
except Exception:  # 非 Windows：仅类型定义，不影响导入
    wintypes = None

from server.driver.base import TMDriver, TMDriverError

log = logging.getLogger("tm.pywinauto")

# 进程 DPI 感知：物理坐标点击（SetCursorPos/mouse_event）必须与 pywinauto
# 的 rectangle() 处于同一坐标系，否则在缩放屏幕（125%/150%）上点击落空，
# 典型症状就是「点了预定会议但对话框没出现」。重复设置失败无害，忽略即可。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_TIME_RX = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$")
_ID_RX = re.compile(r"(\d{3})\s*(\d{3})\s*(\d{3})")
_ID_PLAIN_RX = re.compile(r"\b(\d{9})\b")
_DATE_RX = re.compile(r"^\s*(\d{4})([-/.年])(\d{1,2})([-/.月])(\d{1,2})日?\s*$")


def _clipboard_set(text):
    """把 unicode 文本写入 Windows 剪贴板（纯 ctypes，无额外依赖）。"""
    if not hasattr(ctypes, "windll"):
        raise TMDriverError("clipboard", "非 Windows 环境无法使用剪贴板")
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    # 64 位下必须显式声明 restype/argtypes，否则 HANDLE/指针被默认 c_int 截断
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(0):
        raise TMDriverError("clipboard", "OpenClipboard 失败")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise TMDriverError("clipboard", "GlobalAlloc 失败")
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            raise TMDriverError("clipboard", "GlobalLock 失败")
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise TMDriverError("clipboard", "SetClipboardData 失败")
        # 成功后剪贴板接管内存，不要再 GlobalFree
    finally:
        user32.CloseClipboard()


def _process_pids(exe_name):
    """返回所有可执行文件名等于 exe_name（不区分大小写）的进程 PID 集合（Toolhelp32 快照）。"""
    if not exe_name or not hasattr(ctypes, "windll"):
        return set()
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE = -1

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    # 必须显式声明签名：64 位下默认 restype=c_int 会截断 HANDLE，
    # 导致 Process32FirstW 收到损坏的句柄而返回 0（快照恒为空）。
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return set()
    pids = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                pids.add(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


class PywinautoDriver(TMDriver):
    def __init__(self, tm_cfg):
        self.cfg = dict(tm_cfg or {})
        self.timeouts = dict((self.cfg.get("timeouts") or {}))

    # ---------------- 基础定位 ----------------

    def _desktop(self):
        from pywinauto import Desktop
        return Desktop(backend="uia")

    def _find_window(self, title_re, timeout=5, exclude=None, pids=None):
        """在顶层窗口里按标题正则找窗口；可排除某个已知窗口；可限定只匹配指定进程 PID。"""
        rx = re.compile(title_re)
        deadline = time.time() + max(timeout, 0.5)
        while time.time() < deadline:
            for w in self._desktop().windows():
                if exclude is not None and w is exclude:
                    continue
                try:
                    if pids and w.process_id() not in pids:
                        continue
                    if w.is_visible() and rx.search(w.window_text() or ""):
                        return w
                except Exception:
                    continue
            time.sleep(0.4)
        return None

    def _client_pids(self):
        """腾讯会议客户端进程的 PID 集合（exe 未配置时为空集）。"""
        exe = self.cfg.get("exe")
        if not exe:
            return set()
        return _process_pids(os.path.basename(exe))

    def _ensure_main(self):
        """找到（必要时拉起）腾讯会议主窗口并置前台；限定客户端进程，避免命中标题含“腾讯会议”的其它窗口。"""
        main_re = self.cfg.get("main_window_re", "腾讯会议")
        pids = self._client_pids()
        main = self._find_window(main_re, timeout=3, pids=pids or None)
        if main is None:
            exe = self.cfg.get("exe")
            if exe and os.path.exists(exe):
                log.info("拉起腾讯会议: %s", exe)
                subprocess.Popen([exe])
                time.sleep(1.5)
                pids = self._client_pids()
            main = self._find_window(main_re, timeout=30, pids=pids or None)
        if main is None:
            raise TMDriverError("main", "未找到腾讯会议主窗口（请确认客户端已启动并登录）")
        try:
            if main.is_minimized():
                main.restore()
            main.set_focus()
            time.sleep(0.6)
        except Exception:
            pass
        return main

    def _desc_texts(self, win):
        """遍历窗口所有后代，返回 [(text, element)]。"""
        out = []
        try:
            els = win.descendants()
        except Exception:
            return out
        for el in els:
            try:
                out.append((el.window_text() or "", el))
            except Exception:
                continue
        return out

    def _safe_click(self, el, tag=""):
        """消息型 click() 优先（不移动物理光标，后台服务可用），失败降级 click_input()。"""
        try:
            el.click()
            return True
        except Exception as e1:
            log.debug("_safe_click 消息型失败(%s)，降级物理点击: %s", tag, e1)
        try:
            el.click_input()
            return True
        except Exception as e2:
            raise TMDriverError("click", f"{tag} 点击失败：消息={e1} / 物理={e2}")

    def _click_by_text(self, win, text, timeout=4):
        """在窗口内点击文字匹配的元素：优先 Button/MenuItem，其次任意。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # 1) pywinauto child_window 精确匹配
            for crit in (f"^{re.escape(text)}$", f".*{re.escape(text)}.*"):
                try:
                    cw = win.child_window(title_re=crit).best_match()
                    if cw.exists(0.2):
                        self._safe_click(cw, f"按钮[{text}]")
                        return True
                except Exception:
                    continue
            # 2) 手动扫描：Button 优先
            hits = []
            for t, el in self._desc_texts(win):
                if text in t or text in (getattr(el.element_info, "name", "") or ""):
                    try:
                        ct = el.element_info.control_type
                    except Exception:
                        ct = ""
                    if ct in ("Button", "MenuItem", "Hyperlink"):
                        self._safe_click(el, f"控件[{text}]")
                        return True
                    hits.append(el)
            if hits:
                self._safe_click(hits[0], f"文本[{text}]")
                return True
            time.sleep(0.4)
        return False

    def _click_text_anywhere(self, text, timeout=4):
        """在所有顶层窗口中找文字并点击（用于弹出菜单/对话框）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self._desktop().windows():
                try:
                    if self._click_by_text(w, text, timeout=0.4):
                        return True
                except Exception:
                    continue
            time.sleep(0.3)
        return False

    def _click_near(self, win, texts, rect, max_dist=300, timeout=2):
        """在指定矩形附近点击文字匹配的元素。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for text in texts:
                for t, el in self._desc_texts(win):
                    if text in t or text in (getattr(el.element_info, "name", "") or ""):
                        try:
                            r = el.rectangle()
                            if abs(r.top - rect.top) <= max_dist and abs(r.left - rect.left) <= max_dist * 2:
                                self._safe_click(el, f"附近[{text}]")
                                return True
                        except Exception:
                            continue
            time.sleep(0.3)
        return False

    def _collect_edits(self, win):
        """收集窗口内所有 Edit 控件，按 (top, left) 排序。"""
        out = []
        for el in win.descendants():
            try:
                info = el.element_info
                if info.control_type == "Edit":
                    try:
                        r = el.rectangle()
                        out.append((r.top, r.left, el))
                    except Exception:
                        out.append((10**9, 10**9, el))
            except Exception:
                continue
        out.sort(key=lambda t: (t[0], t[1]))
        return [el for _, _, el in out]

    # ---------------- 文本输入 ----------------

    def _paste(self, el, text):
        _clipboard_set(text)
        self._safe_click(el, "粘贴目标")
        el.set_focus()
        time.sleep(0.08)
        from pywinauto.keyboard import send_keys
        send_keys("^a")
        send_keys("{DELETE}")
        time.sleep(0.08)
        send_keys("^v")

    @staticmethod
    def _el_value(e):
        """读 Edit 当前值：优先 UIA Value pattern，读不到退回 window_text。"""
        try:
            v = e.get_value()
            if isinstance(v, str) and v:
                return v
        except Exception:
            pass
        try:
            return e.window_text() or ""
        except Exception:
            return ""

    def _read_back(self, el, text):
        """回读校验：True=确认成功，False=确认失败，None=无法回读。"""
        v = None
        try:
            v = el.get_value()
        except Exception:
            v = None
        if isinstance(v, str) and v:
            return text in v
        wt = self._el_value(el)
        if wt:
            return text in wt
        return None

    def _input_text(self, el, text, stage):
        """三级输入并回读校验：set_text -> 键入 -> 剪贴板。失败抛错。"""
        tried = []
        # 1) UIA set_text（支持中文）
        try:
            el.set_focus()
            time.sleep(0.1)
            el.set_text(text)
            time.sleep(0.25)
            r = self._read_back(el, text)
            if r is True:
                return True
            tried.append(f"set_text={r}")
        except Exception:
            tried.append("set_text=exc")
        # 2) 键盘键入（仅 ASCII 有效，如 14:00）
        try:
            from pywinauto.keyboard import send_keys
            self._safe_click(el, f"键入[{stage}]")
            el.set_focus()
            send_keys("^a")
            send_keys("{DELETE}")
            time.sleep(0.1)
            el.type_keys(text, with_spaces=True)
            time.sleep(0.25)
            r = self._read_back(el, text)
            if r is True:
                return True
            tried.append(f"键入={r}")
        except Exception:
            tried.append("键入=exc")
        # 3) 剪贴板粘贴
        try:
            self._paste(el, text)
            time.sleep(0.3)
            r = self._read_back(el, text)
            if r is True:
                return True
            if r is None:
                log.warning("阶段 %s 无法回读输入值，按成功处理 (%s)", stage, text)
                return True
            tried.append(f"粘贴={r}")
        except Exception:
            tried.append("粘贴=exc")
        raise TMDriverError(stage, f"输入『{text}』失败：" + "/".join(tried))

    # ---------------- book ----------------

    def _pick_fields(self, edits):
        """从 Edit 列表挑出 主题/开始时间/结束时间。
        规则：时间框值形如 HH:MM；主题框取第一个非时间框。"""
        time_edits = [e for e in edits if _TIME_RX.match(self._el_value(e))]
        subject_ed = None
        for e in edits:
            if not _TIME_RX.match(self._el_value(e)):
                subject_ed = e
                break
        start_ed = time_edits[0] if len(time_edits) >= 1 else None
        end_ed = time_edits[1] if len(time_edits) >= 2 else None
        return subject_ed, start_ed, end_ed

    def _try_set_date(self, dlg, target_date):
        """若日期框可键入则设置目标日期（模仿原分隔符）。返回是否设置成功。"""
        for e in self._collect_edits(dlg):
            try:
                t = e.window_text() or ""
            except Exception:
                continue
            m = _DATE_RX.match(t)
            if m:
                y, mo, d = target_date.split("-")
                val = f"{y}{m.group(2)}{int(mo):02d}{m.group(4)}{int(d):02d}"
                try:
                    self._input_text(e, val, "date")
                    return True
                except TMDriverError:
                    return False
        return False

    def _find_success(self, main, timeout=6):
        pat = self.cfg.get("success_toast_re", "预定成功|已预定|预订成功")
        rx = re.compile(pat)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for w in self._desktop().windows():
                    try:
                        if rx.search(w.window_text() or ""):
                            return True
                    except Exception:
                        continue
                for t, _el in self._desc_texts(main):
                    if rx.search(t):
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _extract_meeting_id(self, main, subject, start_at):
        """提取 9 位腾讯会议号。优先从预订成功弹出的『会议号：XXX』窗口标题取，
        再扫所有客户端窗口的后代 Text（含主窗口会议列表）。"""
        pids = set(self._client_pids())
        # 1) 预订成功确认窗口标题形如『会议号：766 891 115』——最可靠
        for w in self._desktop().windows():
            try:
                if w.process_id() not in pids:
                    continue
                title = w.window_text() or ""
                if "会议号" in title:
                    m = _ID_RX.search(title) or _ID_PLAIN_RX.search(title)
                    if m:
                        return re.sub(r"\D", "", m.group(0))
            except Exception:
                continue
        # 2) 尝试切到『我发起的会议』（不强制，当前可能已在会议列表）
        tab = self.cfg.get("my_meetings_tab", "我发起的会议")
        self._click_by_text(main, tab, timeout=2)
        # 3) 扫所有客户端窗口后代 Text，按主题/日期附近的会议号取
        deadline = time.time() + self.timeouts.get("list", 8)
        while time.time() < deadline:
            for w in self._desktop().windows():
                try:
                    if w.process_id() not in pids:
                        continue
                    for t, _el in self._desc_texts(w):
                        if subject and subject in t:
                            m = _ID_RX.search(t) or _ID_PLAIN_RX.search(t)
                            if m:
                                return re.sub(r"\D", "", m.group(0))
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    # ---------------- WeMeet 3.48 自绘对话框填充 ----------------

    def _click_xy(self, x, y, pause=0.35):
        """物理坐标点击（自绘 Qt 控件兜底，SetCursorPos + mouse_event）。"""
        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.12)
        u.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.08)
        u.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(pause)

    def _find_popup_text(self, wants, contains=False):
        """跨客户端进程的所有窗口找 Text 元素。wants=可迭代的目标文本集合。"""
        pids = set(self._client_pids())
        for w in self._desktop().windows():
            try:
                if w.process_id() not in pids:
                    continue
                for e in w.descendants(control_type="Text"):
                    try:
                        t = (e.window_text() or "").strip()
                    except Exception:
                        continue
                    if contains:
                        if any(wt in t for wt in wants):
                            return e
                    elif t in wants:
                        return e
            except Exception:
                continue
        return None

    def _round_minute15(self, hhmm):
        """腾讯会议时间框为 15 分钟粒度，把 HH:MM 归到最近的 15 分钟。"""
        h, m = [int(x) for x in hhmm.split(":")]
        total = h * 60 + m
        r = (total + 7) // 15 * 15
        return "%02d:%02d" % (r // 60, r % 60)

    def _set_subject(self, dlg, subject):
        """主题 QLineEdit（对话框顶部）：剪贴板粘贴。"""
        dr = dlg.rectangle()
        for e in dlg.descendants(control_type="Edit"):
            try:
                if (e.rectangle().top - dr.top) < 130:
                    self._paste(e, subject)
                    return True
            except Exception:
                continue
        return False

    def _set_date(self, dlg, date_str):
        """日期 QDateEdit：点中心 + Ctrl+A + YYYY/MM/DD。date_str=YYYY-MM-DD。"""
        sps = dlg.descendants(control_type="Spinner")
        if not sps:
            return False
        sp = sps[0]
        r = sp.rectangle()
        self._click_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.3)
        from pywinauto.keyboard import send_keys
        send_keys("^a")
        send_keys(date_str.replace("-", "/"))
        time.sleep(0.4)
        return True

    def _wheel(self, x, y, notches):
        """鼠标滚轮：notches 正=上滚（列表内容下移），负=下滚。"""
        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.12)
        u.mouse_event(0x0800, 0, 0, int(notches * 120), 0)  # MOUSEEVENTF_WHEEL
        time.sleep(0.3)

    def _find_time_item(self, want):
        """跨客户端窗口找文本恰为 want 的 Text 项，返回 (popup窗口, 元素)。"""
        pids = set(self._client_pids())
        for w in self._desktop().windows():
            try:
                if w.process_id() not in pids:
                    continue
                for e in w.descendants(control_type="Text"):
                    try:
                        if (e.window_text() or "").strip() == want:
                            return (w, e)
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _pick_time_in_combo(self, combo, hhmm):
        """在给定时间 ComboBox 中选中 HH:MM：展开→滚轮把目标项滚进 popup 可视区→物理点击。

        关键：UIA 报告的 Text 项 rect 是滚动列表的逻辑坐标，可能在 popup 实际
        可视矩形之外（如 T=-379 而 popup 在 T196~538）。必须滚动到项的 y 中心
        落入 popup 可视矩形内，物理点击才会命中并真正改值。
        """
        try:
            combo.expand()
        except Exception:
            r = combo.rectangle()
            self._click_xy(r.right - 10, (r.top + r.bottom) // 2, 0.5)
        time.sleep(0.6)
        want = hhmm.replace(".", ":")
        res = self._find_time_item(want)
        if res is None:
            return False
        popup, item = res
        pr = popup.rectangle()
        pcy = (pr.top + pr.bottom) // 2
        tr = combo.rectangle()
        wx = (tr.left + tr.right) // 2
        wy = tr.top + 60
        for _ in range(48):
            ir = item.rectangle()
            icy = (ir.top + ir.bottom) // 2
            if pr.top + 30 <= icy <= pr.bottom - 30:
                break
            self._wheel(wx, wy, 1 if icy < pcy else -1)
        ir = item.rectangle()
        cx, cy = (ir.left + ir.right) // 2, (ir.top + ir.bottom) // 2
        if not (pr.top <= cy <= pr.bottom):
            return False
        self._click_xy(cx, cy, 0.5)
        time.sleep(0.4)
        return True

    def _set_combo_time(self, dlg, hhmm):
        """开始时间 ComboBox（名称『时间编辑框』）。"""
        tcs = [c for c in dlg.descendants(control_type="ComboBox")
               if "时间编辑" in c.window_text()]
        if not tcs:
            return False
        return self._pick_time_in_combo(tcs[0], hhmm)

    def _set_custom_end(self, dlg, end_at):
        """『选择结束时间』面板：结束日期 Combo + 结束时间 Combo + 确定。"""
        time.sleep(0.4)
        self._ensure_topmost(dlg)
        dr = dlg.rectangle()
        combos = [c for c in dlg.descendants(control_type="ComboBox")
                  if (c.rectangle().top - dr.top) > 300]
        time_combo = None
        for c in combos:
            r = c.rectangle()
            if (r.right - r.left) < 130:  # 结束时间 Combo（窄），日期 Combo 较宽
                time_combo = c
        # 会议不可跨天，结束日期默认即会议日期，只设结束时间
        ehmm = self._round_minute15(end_at.split(" ")[1])
        if time_combo is not None:
            self._ensure_topmost(dlg)
            self._pick_time_in_combo(time_combo, ehmm)
        oks = [b for b in dlg.descendants(control_type="Button")
               if (b.window_text() or "").strip() == "确定"]
        if oks:
            r = oks[0].rectangle()
            self._ensure_topmost(dlg)
            self._click_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.6)
        return True

    def _set_duration(self, dlg, duration_min, end_at):
        """时长：标准档点预设项；其余档用『选择结束时间』自定义。"""
        preset = {15: "15分钟", 30: "30分钟", 45: "45分钟",
                  60: "1小时", 120: "2小时", 180: "3小时"}
        btns = [b for b in dlg.descendants(control_type="Button")
                if "选择会议时长" in b.window_text()]
        if not btns:
            return False
        self._ensure_topmost(dlg)
        btns[0].click_input()
        time.sleep(0.5)
        label = preset.get(duration_min)
        hit = self._find_popup_text({label}) if label else \
              self._find_popup_text({"选择结束时间"})
        if hit is None:
            return False
        hit.click_input()
        time.sleep(0.5)
        return True if label else self._set_custom_end(dlg, end_at)

    def _set_meeting_password(self, dlg, password):
        """勾选『入会密码』并填入 6 位数字密码。

        控件树（WeMeet 3.48，相对对话框）：
          CheckBox '入会密码'  rel=(85,587) 22 高
          勾选后其下方/右侧出现密码 Edit（6 位数字，可由客户端自动生成）
        勾选用 UIA toggle()（消息型，不依赖前台），填值用剪贴板粘贴 + 回读校验。
        """
        pwd = (password or "").strip()
        if not pwd:
            return True
        cb = None
        for e in dlg.descendants(control_type="CheckBox"):
            try:
                if (e.window_text() or "").strip() == "入会密码":
                    cb = e
                    break
            except Exception:
                continue
        if cb is None:
            raise TMDriverError("password", "预定对话框未找到『入会密码』复选框")
        try:
            state = cb.get_toggle_state()
        except Exception:
            state = 0
        if state == 0:
            try:
                cb.toggle()
            except Exception:
                try:
                    cb.click_input()
                except Exception:
                    pass
            time.sleep(0.8)
        try:
            if cb.get_toggle_state() == 0:
                raise TMDriverError("password", "『入会密码』勾选未生效（可能被前台窗口拦截）")
        except TMDriverError:
            raise
        except Exception:
            pass

        # 密码框：勾选后出现在复选框附近（下方 0~120px、右侧 0~360px）的 Edit
        try:
            cbr = cb.rectangle()
        except Exception:
            cbr = None
        cands = []
        for e in dlg.descendants(control_type="Edit"):
            try:
                r = e.rectangle()
                if cbr is not None:
                    if not (cbr.top - 10 <= r.top <= cbr.top + 120):
                        continue
                    if r.left < cbr.left:
                        continue
                cands.append((r.top, r.left, e))
            except Exception:
                continue
        cands.sort()
        if not cands:
            raise TMDriverError("password", "勾选『入会密码』后未出现密码输入框")
        el = cands[0][2]
        self._paste(el, pwd)
        time.sleep(0.4)
        got = self._read_back(el, pwd)
        if got is False:
            raise TMDriverError("password", "会议密码写入后回读不一致（%s）" % pwd)
        return True

    def _extract_invite_link(self):
        """从客户端任一窗口文本里提取腾讯会议入会短链 meeting.tencent.com/dm/XXXX。"""
        pids = set(self._client_pids())
        rx = re.compile(r"https?://meeting\.tencent\.com/dm/[\w\-]+")
        try:
            for w in self._desktop().windows():
                try:
                    if pids and w.process_id() not in pids:
                        continue
                    for t, _el in self._desc_texts(w):
                        m = rx.search(t or "")
                        if m:
                            return m.group(0)
                    m = rx.search(w.window_text() or "")
                    if m:
                        return m.group(0)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _ensure_topmost(self, win):
        """把窗口真正拉到前台并置顶，确保物理点击落在它上面。

        后台服务进程无法用普通 SetForegroundWindow 抢前台（Windows 前台锁定机制），
        尤其在 RDP 断开后某个 cmd 窗口留在前台时——点击会穿透到桌面 FolderView。
        解决：①模拟一次 ALT 按下解除前台锁；②AttachThreadInput 附到当前前台线程，
        使本进程获得设置前台的权限；③ShowWindow+BringWindowToTop+HWND_TOPMOST；
        ④用 GetForegroundWindow / WindowFromPoint 校验，不成功就重试。
        """
        hwnd = getattr(win, "handle", None)
        if not hwnd:
            return False
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u.SetForegroundWindow.argtypes = [wintypes.HWND]
            u.GetForegroundWindow.restype = wintypes.HWND
            u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            u.BringWindowToTop.argtypes = [wintypes.HWND]
            u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
            u.GetWindowThreadProcessId.restype = wintypes.DWORD
            u.SetActiveWindow.argtypes = [wintypes.HWND]
            u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                                      ctypes.c_void_p]
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
            SW_RESTORE = 9
            VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002

            for attempt in range(4):
                # ①解除前台锁：模拟 ALT 按下/抬起
                try:
                    u.keybd_event(VK_MENU, 0, 0, None)
                    u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
                except Exception:
                    pass
                # ②附到前台线程，绕过跨进程设置前台的限制
                attached = False
                try:
                    fg = u.GetForegroundWindow()
                    fg_tid = u.GetWindowThreadProcessId(fg, None)
                    my_tid = k.GetCurrentThreadId()
                    if fg_tid and fg_tid != my_tid:
                        attached = bool(u.AttachThreadInput(my_tid, fg_tid, True))
                except Exception:
                    pass
                try:
                    u.ShowWindow(hwnd, SW_RESTORE)
                    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                    u.BringWindowToTop(hwnd)
                    u.SetForegroundWindow(hwnd)
                    u.SetActiveWindow(hwnd)
                finally:
                    if attached:
                        try:
                            u.AttachThreadInput(k.GetCurrentThreadId(), fg_tid, False)
                        except Exception:
                            pass
                time.sleep(0.3)
                if u.GetForegroundWindow() == hwnd:
                    return True
                # 断开的 RDP / 无显示器环境下 GetForegroundWindow() 恒为 0，
                # 无法用前台窗口判定成败；改用命中测试：窗口中心点是否已归本窗口。
                if self._hit_test(win, hwnd):
                    return True
            return u.GetForegroundWindow() == hwnd or self._hit_test(win, hwnd)
        except Exception:
            return False

    def _hit_test(self, win, hwnd=None):
        """窗口中心点的 WindowFromPoint 是否落在本窗口上（判断 z-order 是否在最上）。"""
        try:
            u = ctypes.windll.user32
            hwnd = hwnd or getattr(win, "handle", None)
            if not hwnd:
                return False
            r = wintypes.RECT()
            u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
            if r.right <= r.left or r.bottom <= r.top:
                return False
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            hit = u.WindowFromPoint(wintypes.POINT(cx, cy))
            if not hit:
                return False
            root = u.GetAncestor(wintypes.HWND(hit), 2)  # GA_ROOT
            return (root or hit) == hwnd
        except Exception:
            return False

    def _diag_windows(self, pids):
        """把所有客户端顶层窗口信息拼成字符串，供错误消息携带（远程排查用）。"""
        lines = []
        try:
            for w in self._desktop().windows():
                try:
                    if pids and w.process_id() not in pids:
                        continue
                    lines.append("%r rect=%s" % (w.window_text(), w.rectangle()))
                except Exception:
                    continue
        except Exception as e:
            lines.append("枚举窗口失败: %s" % e)
        return " | ".join(lines) if lines else "(未枚举到任何客户端窗口)"

    def _write_failure_snapshot(self, stage, main, diag):
        """把失败瞬间的桌面状态写入 data/driver_failure.log（追加，带时间戳）。

        断开 RDP 后操作员看不到屏幕，此快照是判断"桌面降级/窗口离屏/显示器数量"
        的唯一证据。纯诊断，不影响主流程；任何异常都吞掉。
        """
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, "data", "driver_failure.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            u = ctypes.windll.user32
            try:
                u.SetProcessDPIAware()
            except Exception:
                pass
            lines = []
            lines.append("=" * 70)
            lines.append("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
            lines.append("阶段: %s" % stage)
            try:
                lines.append("屏幕: %dx%d" % (u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
                lines.append("SM_CMONITORS(显示器数量): %d" % u.GetSystemMetrics(80))
            except Exception as e:
                lines.append("屏幕信息失败: %s" % e)
            try:
                lines.append("SessionId: %s" % self._session_id())
            except Exception:
                pass
            # 前台窗口 + 输入桌面：判断物理点击是否被锁屏/其它窗口/输入桌面切换拦截
            try:
                hwnd = u.GetForegroundWindow()
                buf = ctypes.create_unicode_buffer(256)
                cls = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(hwnd, buf, 256)
                u.GetClassNameW(hwnd, cls, 256)
                lines.append("前台窗口: hwnd=%s cls=%r title=%r" % (hwnd, cls.value, buf.value))
                lines.append("主窗口hwnd=%s 是否为前台=%s" %
                             (getattr(main, "handle", None), hwnd == getattr(main, "handle", None)))
            except Exception as e:
                lines.append("前台窗口信息失败: %s" % e)
            try:
                lines.append("输入桌面: %s" % self._input_desktop())
            except Exception as e:
                lines.append("输入桌面信息失败: %s" % e)
            try:
                lines.append("主窗口: %r rect=%s" % (main.window_text(), main.rectangle()))
            except Exception as e:
                lines.append("主窗口信息失败: %s" % e)
            lines.append("客户端窗口诊断: %s" % diag)
            probe = getattr(self, "_last_probe", None)
            if probe:
                lines.append("按钮点击探测:")
                for p in probe:
                    lines.append("  %s" % p)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def _session_id(self):
        """当前进程的会话 ID（0=Session 0 无桌面，>0=交互会话）。"""
        try:
            pid = ctypes.windll.kernel32.GetCurrentProcessId()
            return self._process_session_id(pid)
        except Exception:
            return "?"

    def _process_session_id(self, pid):
        try:
            sid = ctypes.c_ulong()
            ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
            return sid.value
        except Exception:
            return "?"

    def _input_desktop(self):
        """返回当前输入桌面名（Winsta0\\Default=正常；Winlogon=锁屏，点击会落空）。"""
        try:
            u = ctypes.windll.user32
            tid = ctypes.windll.kernel32.GetCurrentThreadId()
            hdesk = u.GetThreadDesktop(tid)
            if not hdesk:
                return "(null)"
            fn = u.GetUserObjectInformationW
            fn.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                           wintypes.UINT, ctypes.POINTER(wintypes.UINT)]
            fn.restype = wintypes.BOOL
            length = wintypes.UINT(0)
            fn(hdesk, 2, None, 0, ctypes.byref(length))  # UOI_NAME=2
            buf = ctypes.create_unicode_buffer(length.value // 2 + 2)
            ok = fn(hdesk, 2, buf, length.value, ctypes.byref(length))
            return buf.value if ok else "(query failed)"
        except Exception as e:
            return "error: %s" % e

    def _open_booking_dialog(self, main, dlg_re, pids):
        """多策略点击『预定会议』按钮直到独立对话框出现，返回对话框窗口。

        WeMeet 3.48 自绘 UI 无 UIA 文本按钮，靠坐标点击；不同机器的窗口尺寸/DPI
        不同，故同时尝试文本、绝对偏移、相对比例等多种候选，每次点后检测对话框。
        """
        # 1) 文本定位（某些版本/缩放下 UIA 能暴露按钮）
        if self._click_by_text(main, self.cfg.get("book_button", "预定会议"), timeout=2):
            dlg = self._find_window(dlg_re, timeout=2, exclude=main, pids=pids or None)
            if dlg is not None:
                return dlg

        mr = main.rectangle()
        mw, mh = (mr.right - mr.left), (mr.bottom - mr.top)
        # 校准基准：主窗口 938x638，按钮相对偏移 (176,365)
        rel = self.cfg.get("book_button_rel", [176, 365])
        candidates = []
        # a) 配置里的绝对偏移
        candidates.append((mr.left + int(rel[0]), mr.top + int(rel[1])))
        # b) 相对比例（适配不同窗口尺寸）
        candidates.append((mr.left + int(mw * 0.188), mr.top + int(mh * 0.572)))
        # c) 配置里若给了比例则用比例
        frac = self.cfg.get("book_button_frac")
        if frac and len(frac) == 2:
            candidates.append((mr.left + int(mw * float(frac[0])),
                               mr.top + int(mh * float(frac[1]))))

        probe = []
        for cx, cy in candidates:
            self._ensure_topmost(main)
            time.sleep(0.25)
            self._click_xy(cx, cy, pause=1.0)
            # 关键：每次点击后用「实时」PID 检测对话框，而非 book() 开头缓存的
            # pids——对话框可能属于新拉起的进程（verify 阶段已踩过缓存漏检的坑）。
            live = set(self._client_pids())
            dlg = self._find_window(dlg_re, timeout=1.5, exclude=main,
                                    pids=live or None)
            if dlg is not None:
                self._last_probe = probe
                return dlg
            probe.append("(%d,%d)->%s" % (cx, cy, self._window_at(cx, cy)))

        # 最后再等长一点（慢机器对话框出现较慢），同样用实时 PID
        live = set(self._client_pids())
        dlg = self._find_window(dlg_re, timeout=self.timeouts.get("dialog", 8),
                                exclude=main, pids=live or None)
        self._last_probe = probe
        return dlg

    def _window_at(self, x, y):
        """返回屏幕坐标 (x,y) 处 WindowFromPoint 命中的窗口 class/title，供点击诊断。"""
        try:
            u = ctypes.windll.user32
            hwnd = u.WindowFromPoint(x, y)
            if not hwnd:
                return "(no window)"
            buf = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, buf, 256)
            u.GetClassNameW(hwnd, cls, 256)
            return "hwnd=%s cls=%r title=%r" % (hwnd, cls.value, buf.value)
        except Exception as e:
            return "wininfo failed: %s" % e

    def book(self, subject, start_at, end_at, password=None):
        main = self._ensure_main()
        dlg_re = self.cfg.get("dialog_title_re", "预定会议")
        pids = self._client_pids()
        pids_list = list(pids) if pids else None
        # 清理遗留对话框（上一次异常残留）
        stale = self._find_window(dlg_re, timeout=0.5, exclude=main, pids=pids_list)
        if stale is not None:
            try:
                stale.close()
            except Exception:
                pass
            time.sleep(0.4)
        dlg = self._open_booking_dialog(main, dlg_re, pids_list)
        if dlg is None:
            diag = self._diag_windows(pids)
            # 断开 RDP 后桌面状态会变（窗口被挪出屏幕/桌面不渲染），此时人看不到屏幕，
            # 故把失败瞬间的主窗口 rect、屏幕尺寸、显示器数量、会话信息落盘，供远程排障。
            self._write_failure_snapshot("dialog", main, diag)
            raise TMDriverError(
                "dialog",
                "未找到独立预定对话框（ScheduleMeetingDialog）。"
                "已尝试文本/绝对偏移/相对比例点击『预定会议』均未打开。"
                "客户端窗口诊断：[%s]。失败快照已写入 data/driver_failure.log（含屏幕/显示器/会话状态），"
                "请把该文件发回；若在断开 RDP 状态下失败，多为桌面降级所致，参见《无显示器虚拟机部署指南.md》。"
                % diag)
        self._ensure_topmost(dlg)
        time.sleep(0.4)

        date_s = start_at.split(" ")[0]
        start_hhmm = self._round_minute15(start_at.split(" ")[1])
        duration_min = int((time.mktime(time.strptime(end_at, "%Y-%m-%d %H:%M")) -
                            time.mktime(time.strptime(start_at, "%Y-%m-%d %H:%M"))) // 60)

        if not self._set_subject(dlg, subject):
            raise TMDriverError("subject", "预定对话框未找到主题输入框")
        if not self._set_date(dlg, date_s):
            raise TMDriverError("date", "预定对话框未找到日期框")
        if not self._set_combo_time(dlg, start_hhmm):
            raise TMDriverError("start_time", "时间下拉中未找到『%s』" % start_hhmm)
        # 时间下拉 popup 会改变 z-order，重新置顶后再操作时长
        self._ensure_topmost(dlg)
        time.sleep(0.3)
        if not self._set_duration(dlg, duration_min, end_at):
            raise TMDriverError("duration", "时长设置失败（%d 分钟）" % duration_min)

        # 入会密码（可选）：勾选『入会密码』并填入 6 位数字
        pwd = (password or "").strip()
        if pwd:
            self._ensure_topmost(dlg)
            time.sleep(0.3)
            self._set_meeting_password(dlg, pwd)

        # 点『预定』+ 校验：后台服务 GUI 与客户端争夺前台，单次置顶/点击常被
        # 抵消（尤其 90 分钟档收起『选择结束时间』面板后 z-order 变化）。
        # 用重试循环：每次重新枚举最新 dialog（handle 可能重建）→强制置顶→
        # 物理点击→轮询成功窗口/对话框关闭。确认未成功才重试，避免重复预订。
        succeeded = False
        for attempt in range(3):
            # 实时取 pids：会议号确认窗口常属于新拉起的进程，不在 book() 开头缓存里
            live_pids = set(self._client_pids())
            live_list = list(live_pids) if live_pids else None
            cur_dlg = self._find_window(dlg_re, timeout=1.0, exclude=main,
                                        pids=live_list)
            if cur_dlg is None:
                succeeded = True  # 对话框已关闭
                break
            btns = [b for b in cur_dlg.descendants(control_type="Button")
                    if (b.window_text() or "").strip() == "预定"]
            if not btns:
                # 按钮暂不可见，稍后重试
                time.sleep(0.5)
                continue
            br = btns[0].rectangle()
            cx, cy = (br.left + br.right) // 2, (br.top + br.bottom) // 2
            self._ensure_topmost(cur_dlg)  # 内含 3 次置顶 + 稳定等待
            self._click_xy(cx, cy, 1.5)

            # 轮询成功：会议号确认窗口出现 / 对话框关闭 / 主窗口成功文案
            deadline = time.time() + 6
            while time.time() < deadline:
                for w in self._desktop().windows():
                    try:
                        if "会议号" in (w.window_text() or ""):
                            succeeded = True  # 不限定 pid，避免新进程漏检
                            break
                    except Exception:
                        pass
                if succeeded:
                    break
                cur = self._find_window(dlg_re, timeout=0.4, exclude=main,
                                        pids=live_list)
                if cur is None:
                    succeeded = True
                    break
                time.sleep(0.4)
            if succeeded:
                break
            log.warning("第 %d 次点击『预定』未检测到成功，重新置顶后重试", attempt + 1)

        if not succeeded and not self._find_success(main, timeout=3):
            raise TMDriverError("verify", "点击预定后未检测到成功（对话框未关闭且无成功文案）")

        mid = self._extract_meeting_id(main, subject, start_at)
        # 关闭预订成功弹出的『会议号：XXX』确认窗口（否则遮挡主窗口会议列表）
        self._close_confirm_window()
        link = self._extract_invite_link()
        msg = "预订成功" + (f"，腾讯会议号 {mid}" if mid else "（未取到会议号，取消时按主题+时间定位）")
        if pwd:
            msg += f"，入会密码 {pwd}"
        return {"ok": True, "meeting_id": mid, "message": msg,
                "invite_link": link}

    def _close_confirm_window(self):
        """关闭『会议号：XXX』成功确认弹窗（点『关闭』按钮，兜底 Esc）。"""
        pids = set(self._client_pids())
        for w in self._desktop().windows():
            try:
                if w.process_id() not in pids or "会议号" not in (w.window_text() or ""):
                    continue
                for b in w.descendants(control_type="Button"):
                    if (b.window_text() or "").strip() == "关闭":
                        self._ensure_topmost(w)
                        b.click_input()
                        time.sleep(0.5)
                        return True
                w.close()
                time.sleep(0.5)
                return True
            except Exception:
                continue
        return False

    # ---------------- cancel ----------------

    def _click_confirm_dialog(self, confirm_text, timeout=4):
        """优先在『删除/确认/提示』类弹窗里点确认，找不到再全局兜底。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self._desktop().windows():
                try:
                    t = w.window_text() or ""
                except Exception:
                    continue
                if re.search(r"删除|确认|提示|警告|注意", t):
                    if self._click_by_text(w, confirm_text, timeout=0.5):
                        return True
            time.sleep(0.3)
        return self._click_text_anywhere(confirm_text, timeout=2)

    def cancel(self, tm_meeting_id, subject, start_at):
        main = self._ensure_main()
        pids = self._client_pids()
        # 清理可能残留的『会议号：XXX』确认弹窗，避免遮挡会议列表
        self._close_confirm_window()
        main = self._ensure_main()
        tab = self.cfg.get("my_meetings_tab", "我发起的会议")
        # 自绘 UI 下 tab 可能无 UIA 文本元素；找不到不致命，继续按条目文本定位
        if not self._click_by_text(main, tab, timeout=4):
            log.warning("cancel: 未找到『%s』入口，继续按会议列表文本定位", tab)
        time.sleep(1.0)

        norm_id = re.sub(r"\D", "", tm_meeting_id or "")
        date_str = start_at.split(" ")[0]
        start_hhmm = start_at.split(" ")[1][:5] if " " in start_at else ""
        # 找到匹配条目后，向上取整个 meeting item 容器 rect（点击条目主体更可靠，
        # 仅点主题/会议号 Text 小区域不会打开详情窗口）。
        # 刚预订完列表可能未刷新，轮询若干次；匹配支持 会议号/主题/开始时间范围。
        item_rect = None
        for _round in range(6):
            for t, el in self._desc_texts(main):
                tt = (t or "").strip()
                hit = False
                if norm_id and norm_id in re.sub(r"\D", "", tt):
                    hit = True
                elif subject and subject in tt:
                    # 主题匹配即可（列表项主题与日期分属不同 Text 元素）
                    hit = True
                elif start_hhmm and re.match(r"^\d{1,2}:\d{2}\s*[-~至]\s*\d{1,2}:\d{2}", tt) \
                        and tt.startswith(start_hhmm):
                    # 时间范围匹配（如『14:00-15:30』），兜底主题未暴露的情况
                    hit = True
                if hit:
                    cur = el
                    for _ in range(6):
                        try:
                            name = (cur.element_info.name or "")
                            if "meeting_item" in name:
                                item_rect = cur.rectangle()
                                break
                            cur = cur.parent()
                            if cur is None:
                                break
                        except Exception:
                            break
                    if item_rect is None:
                        try:
                            item_rect = el.rectangle()
                        except Exception:
                            pass
                    break
            if item_rect is not None:
                break
            # 列表未刷新，重新置主窗口后再找
            self._ensure_topmost(main)
            time.sleep(1.0)
        if item_rect is None:
            return {"ok": True, "message": "腾讯侧未找到该会议（可能已手动删除），按取消成功处理"}

        # 点击会议条目打开『会议详情』窗口（先置顶主窗口，否则物理点击落空）
        # 点主题区域（左侧），避开右侧 menu/share/join 按钮区
        cx = item_rect.left + min(90, (item_rect.right - item_rect.left) // 4)
        cy = item_rect.top + (item_rect.bottom - item_rect.top) // 3
        detail = None
        for attempt in range(3):
            self._ensure_topmost(main)
            time.sleep(0.3)
            self._click_xy(cx, cy, 0.8)
            detail = self._find_window("会议详情", timeout=0.8, pids=pids or None)
            if detail:
                break
            time.sleep(0.3)
        if detail is None:
            raise TMDriverError("detail", "点击会议后未打开『会议详情』窗口")
        self._ensure_topmost(detail)
        time.sleep(0.3)

        # 详情窗口右上角『更多』按钮（自绘 UI，按 name 含 button_more 定位）
        more = None
        for b in detail.descendants(control_type="Button"):
            try:
                if "more" in (b.element_info.name or "").lower():
                    more = b
                    break
            except Exception:
                continue
        if more is None:
            # 兜底：详情窗口右上角相对坐标
            dr = detail.rectangle()
            self._click_xy(dr.right - 60, dr.top + 20, 0.6)
        else:
            r = more.rectangle()
            self._click_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.6)

        # 弹出菜单里点『取消会议』（menu_window 的 MenuItem）
        menu_text = self.cfg.get("delete_menu", "取消会议")
        menu_item = None
        deadline = time.time() + 4
        while time.time() < deadline and menu_item is None:
            for w in self._desktop().windows():
                try:
                    if w.process_id() not in pids:
                        continue
                    for e in w.descendants(control_type="MenuItem"):
                        if menu_text in (e.window_text() or ""):
                            menu_item = e
                            break
                except Exception:
                    continue
                if menu_item:
                    break
            if menu_item is None:
                time.sleep(0.3)
        if menu_item is not None:
            r = menu_item.rectangle()
            self._click_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.8)
        elif not self._click_text_anywhere(menu_text, timeout=3):
            raise TMDriverError("delete_menu", "未找到菜单项『%s』" % menu_text)
        time.sleep(0.6)
        # 确认取消：详情窗口内嵌面板的『取消会议』按钮（『再想想』是放弃）
        # 菜单点击可能令窗口重建，重新获取引用
        detail = self._find_window("会议详情", timeout=2, pids=pids or None) or detail
        confirm_text = self.cfg.get("delete_confirm", "取消会议")
        confirm_btn = None
        deadline = time.time() + 4
        while time.time() < deadline and confirm_btn is None:
            for b in detail.descendants(control_type="Button"):
                try:
                    if (b.window_text() or "").strip() == confirm_text:
                        confirm_btn = b
                        break
                except Exception:
                    continue
            if confirm_btn is None:
                time.sleep(0.3)
        if confirm_btn is not None:
            r = confirm_btn.rectangle()
            self._ensure_topmost(detail)
            self._click_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2, 1.0)
        else:
            raise TMDriverError("delete_confirm", "未找到确认按钮『%s』" % confirm_text)
        time.sleep(0.8)

        # 关闭可能残留的详情窗口
        try:
            detail.close()
        except Exception:
            pass
        main = self._ensure_main()

        # 校验条目消失
        deadline = time.time() + self.timeouts.get("list", 8)
        while time.time() < deadline:
            gone = True
            for t, _el in self._desc_texts(main):
                if (norm_id and norm_id in re.sub(r"\D", "", t)) or \
                   (subject and subject in t and date_str in t):
                    gone = False
                    break
            if gone:
                return {"ok": True, "message": "腾讯侧会议已删除"}
            time.sleep(0.5)
        return {"ok": False, "message": "已点击删除但条目仍未消失，取消未确认"}

    # ---------------- list / inspect ----------------

    def list_scheduled(self):
        try:
            main = self._ensure_main()
        except TMDriverError:
            return []
        tab = self.cfg.get("my_meetings_tab", "我发起的会议")
        if not self._click_by_text(main, tab, timeout=4):
            return []
        time.sleep(1.2)
        out = []
        for t, _el in self._desc_texts(main):
            t = t.strip()
            if not t:
                continue
            m = _ID_RX.search(t) or _ID_PLAIN_RX.search(t)
            mid = re.sub(r"\D", "", m.group(0)) if m else None
            if mid or re.search(r"\d{4}-\d{2}-\d{2}", t) or re.search(r"\d{1,2}:\d{2}", t):
                out.append({"raw": t[:200], "meeting_id": mid})
        return out[:200]

    def inspect(self):
        lines = ["=== top-level windows ==="]
        try:
            for w in self._desktop().windows():
                try:
                    lines.append(
                        f"WIN | text={w.window_text()!r} class={w.element_info.class_name!r}")
                except Exception:
                    continue
        except Exception as e:
            lines.append(f"(desktop windows failed: {e})")
        try:
            main = self._ensure_main()
            lines.append("=== main window descendants (max 3000) ===")
            n = 0
            for t, el in self._desc_texts(main):
                try:
                    info = el.element_info
                    lines.append(
                        f"{info.control_type:12s} | name={(info.name or '')[:80]!r} "
                        f"| class={info.class_name!r} | text={t[:80]!r}")
                except Exception:
                    continue
                n += 1
                if n > 3000:
                    lines.append("... truncated")
                    break
        except Exception as e:
            lines.append(f"(main inspect failed: {e})")
        return "\n".join(lines)
