"""Zero-Dependency Windows System Tray Icon (Shell_NotifyIconW) for PyWebView."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("sakuga.tray")

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
TPM_RIGHTBUTTON = 0x0002

ID_TRAY_OPEN = 1001
ID_TRAY_HIDE = 1002
ID_TRAY_EXIT = 1003


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WinSystemTray:
    """Runs a native Windows tray icon with Open / Hide / Exit actions cleanly in a daemon thread."""

    def __init__(
        self,
        icon_path: Path,
        tooltip: str = "Black Sakura Translate",
        on_open: Optional[Callable[[], None]] = None,
        on_hide: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        is_busy_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.icon_path = Path(icon_path)
        self.tooltip = tooltip[:120]
        self.on_open = on_open
        self.on_hide = on_hide
        self.on_exit = on_exit
        self.is_busy_fn = is_busy_fn
        self._hwnd: Optional[int] = None
        self._hicon: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopped = False
        self._wndproc_ref = None

    def start(self) -> None:
        if os.name != "nt" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="SakugaTrayThread", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def show_balloon(self, title: str, msg: str) -> None:
        if os.name != "nt" or not self._hwnd or self._stopped:
            return
        try:
            shell32 = ctypes.windll.shell32
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO | NIF_TIP
            busy = self.is_busy_fn() if self.is_busy_fn else False
            nid.szTip = ("Black Sakura Translate // Đang chạy ngầm..." if busy else self.tooltip)[:127]
            nid.szInfoTitle = title[:63]
            nid.szInfo = msg[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def update_tooltip(self, text: str) -> None:
        if os.name != "nt" or not self._hwnd or self._stopped:
            return
        try:
            shell32 = ctypes.windll.shell32
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_TIP
            nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def stop(self) -> None:
        self._stopped = True
        if os.name != "nt" or not self._hwnd:
            return
        try:
            shell32 = ctypes.windll.shell32
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        except Exception:
            pass

    def _run_loop(self) -> None:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t

            def _wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_TRAYICON:
                    if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        if self.on_open:
                            threading.Thread(target=self.on_open, daemon=True).start()
                    elif lparam == WM_RBUTTONUP:
                        self._show_context_menu(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc_ref = WNDPROC(_wnd_proc)
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = f"SakugaTrayWnd_{os.getpid()}"

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            user32.RegisterClassExW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(0, cls_name, "SakugaTray", 0, 0, 0, 0, 0, 0, 0, hinst, 0)
            self._hwnd = hwnd

            hicon = None
            if self.icon_path.exists():
                hicon = user32.LoadImageW(0, str(self.icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not hicon:
                hicon = user32.LoadIconW(0, IDI_APPLICATION)
            self._hicon = hicon

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = hicon
            nid.szTip = self.tooltip[:127]
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        except Exception as exc:
            logger.debug("Tray init failed cleanly: %s", exc)
            self._ready.set()
            return

        self._ready.set()
        msg = wintypes.MSG()
        while not self._stopped and ctypes.windll.user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def _show_context_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, ID_TRAY_OPEN, "Mở Black Sakura Translate")
            user32.AppendMenuW(hmenu, MF_STRING, ID_TRAY_HIDE, "Ẩn xuống khay")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            busy = self.is_busy_fn() if self.is_busy_fn else False
            exit_lbl = "Thoát hẳn (Đang chạy tiến trình!)" if busy else "Thoát hẳn Black Sakura Translate"
            user32.AppendMenuW(hmenu, MF_STRING, ID_TRAY_EXIT, exit_lbl)

            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None
            )
            if cmd == ID_TRAY_OPEN and self.on_open:
                threading.Thread(target=self.on_open, daemon=True).start()
            elif cmd == ID_TRAY_HIDE and self.on_hide:
                threading.Thread(target=self.on_hide, daemon=True).start()
            elif cmd == ID_TRAY_EXIT and self.on_exit:
                threading.Thread(target=self.on_exit, daemon=True).start()
        finally:
            user32.DestroyMenu(hmenu)
