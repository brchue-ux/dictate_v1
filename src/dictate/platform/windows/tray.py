"""The notification-area icon, in Win32.

Everything about *what* the icon says and offers is in `dictate/tray.py`, which
is plain Python and tested. This file is the part that cannot be: one window
that is never shown, `Shell_NotifyIconW`, and a popup menu.

Three things about it are load-bearing:

* **It owns a thread and a window, and they are the same thread.** A window's
  messages are delivered to the thread that created it, so the icon is created,
  updated and destroyed there and nowhere else. `update()` is called from the
  hotkey and pipeline threads; it stores the new state and posts a message, and
  the tray thread does the drawing. Nothing here ever touches Tk, and the
  overlay's main-thread rule is untouched.
* **It never blocks the app.** `start()` waits only for the icon to appear or
  fail, `close()` posts a quit and joins with a timeout, and a tray that cannot
  be created is reported and skipped - dictate runs perfectly well without one,
  and refusing to start over a missing icon would be absurd.
* **It re-adds itself when Explorer restarts.** Windows broadcasts
  `TaskbarCreated` when the shell comes back, and an icon that does not listen
  for it silently disappears for the rest of the session - which is exactly the
  "it is running but I cannot see it" state this exists to prevent.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from pathlib import Path

from ... import tray as tray_mod
from ...errors import DictateError

log = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
WM_USER = 0x0400

#: Our own two messages: the icon's mouse callback, and "the state changed".
WM_TRAY_CALLBACK = WM_USER + 20
WM_TRAY_REFRESH = WM_USER + 21

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

#: A window procedure returns an LRESULT, which is pointer-sized. Declaring it
#: as a 32-bit long works on a 32-bit Windows and quietly truncates on a 64-bit
#: one, so it is spelled out.
LRESULT = ctypes.c_ssize_t

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_DISABLED = 0x0000, 0x0800, 0x0001, 0x0002
#: A submenu hangs off an item with MF_POPUP, and its handle goes where the
#: command id would. `DestroyMenu` on the menu it is attached to destroys it
#: too, which is why nothing here keeps a list of them.
MF_POPUP, MF_CHECKED = 0x0010, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

#: The window this owns is an ordinary top-level window that is simply never
#: shown, NOT a message-only (HWND_MESSAGE) one. A message-only window cannot be
#: brought to the foreground, and `SetForegroundWindow` before `TrackPopupMenu`
#: is what makes a tray menu close when you click somewhere else. WS_EX_TOOLWINDOW
#: keeps it out of the taskbar and out of Alt+Tab.
WS_EX_TOOLWINDOW = 0x00000080
WS_OVERLAPPED = 0x00000000

#: Command ids start here so nothing can collide with a system id.
_FIRST_COMMAND = 100


class _NOTIFYICONDATAW(ctypes.Structure):
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
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class WindowsTrayIcon:
    """Implements `platform.base.TrayIcon`."""

    def __init__(self, actions: tray_mod.TrayActions, *, icon_dir: Path,
                 state: tray_mod.TrayState | None = None) -> None:
        self.actions = actions
        self.icon_dir = Path(icon_dir)
        self._state = state or tray_mod.TrayState()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._icons: dict[str, int] = {}
        self._commands: dict[int, str] = {}
        self._proc = _WNDPROC(self._on_message)   # kept alive: Windows holds it
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._taskbar_created = 0
        self._declare()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dictate-tray",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            # Leave nothing half-alive behind a failure: a thread still trying
            # to put an icon up while the app believes there is none would be a
            # second icon the next time it succeeded.
            self.close()
            raise DictateError(
                "dictate's icon in the notification area did not appear.",
                "dictate is running anyway - stop it with `dictate stop`.",
            )
        if self._failure is not None:
            raise DictateError(
                f"dictate could not put an icon in the notification area: "
                f"{self._failure}",
                "dictate is running anyway - stop it with `dictate stop`.",
            )

    def update(self, state: tray_mod.TrayState) -> None:
        with self._lock:
            if state == self._state:
                return
            self._state = state
        hwnd = self._hwnd
        if hwnd:
            self.user32.PostMessageW(hwnd, WM_TRAY_REFRESH, 0, 0)

    def close(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    @property
    def describe(self) -> str:
        return ("an icon in the notification area, with stop, restart, the "
                "update commands and the hotkey on it")

    # -- the thread that owns the window ---------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._failure = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            self._pump()
        except Exception:
            log.exception("the tray icon's message loop stopped")
        finally:
            self._remove_icon()

    def _pump(self) -> None:
        message = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))

    def _declare(self) -> None:
        """Spell out every signature this file uses.

        ctypes guesses when it is not told, and its guesses are 32-bit: a
        handle passed to an undeclared function is squeezed through a C `int`,
        and a window procedure that returns an undeclared value is truncated.
        Neither shows up as an error - they show up as a menu that does
        nothing. Nobody here can see that happen, so nothing is left to a guess.
        """
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        self.user32.DefWindowProcW.restype = LRESULT
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.RegisterWindowMessageW.restype = wintypes.UINT
        self.user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = LRESULT
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.LoadImageW.restype = wintypes.HANDLE
        self.user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.CreatePopupMenu.argtypes = []
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        self.user32.DestroyMenu.restype = wintypes.BOOL
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.TrackPopupMenu.restype = wintypes.BOOL
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self.shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]

    def _create_window(self) -> None:
        instance = self.kernel32.GetModuleHandleW(None)
        name = f"dictate-tray-{os.getpid()}"
        wndclass = _WNDCLASS()
        wndclass.lpfnWndProc = self._proc
        wndclass.hInstance = instance
        wndclass.lpszClassName = name
        if not self.user32.RegisterClassW(ctypes.byref(wndclass)):
            raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
        self._class = wndclass          # kept alive for as long as the window is
        hwnd = self.user32.CreateWindowExW(
            WS_EX_TOOLWINDOW, name, "dictate", WS_OVERLAPPED, 0, 0, 0, 0,
            None, None, instance, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        self._hwnd = hwnd
        # Explorer broadcasts this when the shell restarts. Without it the icon
        # is gone for the rest of the session and dictate looks dead.
        self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")

    # -- the icon --------------------------------------------------------

    def _icon_for(self, colour: str) -> int:
        handle = self._icons.get(colour)
        if handle:
            return handle
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        path = self.icon_dir / f"tray-{colour.lstrip('#')}.ico"
        if not path.exists():
            path.write_bytes(tray_mod.ico_bytes(colour))
        handle = self.user32.LoadImageW(
            None, str(path), IMAGE_ICON, 16, 16,
            LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not handle:
            raise OSError(f"LoadImageW({path}) failed: {ctypes.get_last_error()}")
        self._icons[colour] = handle
        return handle

    def _data(self, state: tray_mod.TrayState) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self._icon_for(state.colour)
        data.szTip = tray_mod.tooltip(state)[:127]
        return data

    def _add_icon(self) -> None:
        with self._lock:
            state = self._state
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data(state))):
            raise OSError(f"Shell_NotifyIconW(ADD) failed: {ctypes.get_last_error()}")

    def _refresh(self) -> None:
        with self._lock:
            state = self._state
        try:
            self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(state)))
        except OSError:
            log.debug("the tray icon could not be updated", exc_info=True)

    def _remove_icon(self) -> None:
        if not self._hwnd:
            return
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        try:
            self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        except OSError:
            log.debug("the tray icon could not be removed", exc_info=True)

    # -- messages --------------------------------------------------------

    def _on_message(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_TRAY_CALLBACK:
                self._on_click(lparam & 0xFFFF)
                return 0
            if message == WM_TRAY_REFRESH:
                self._refresh()
                return 0
            if message == WM_COMMAND:
                self._invoke(wparam & 0xFFFF)
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._add_icon()
                return 0
            if message == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self._remove_icon()
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("the tray icon failed to handle a message; carrying on")
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _on_click(self, event: int) -> None:
        # Either button opens the menu, deliberately. Which button shows a menu
        # is not something to have to remember, and there is nothing else the
        # icon could usefully do on a left click.
        if event in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            self._show_menu()

    def _append(self, handle, items) -> None:
        """Fill a popup menu, submenus and all.

        Command ids are handed out as the items are walked, so a submenu's items
        get ids of their own and `TrackPopupMenu` returns whichever was chosen,
        at whatever depth. The submenu handle goes in the id's place under
        MF_POPUP and is destroyed with its parent.
        """
        for item in items:
            if item.children:
                submenu = self.user32.CreatePopupMenu()
                if submenu:
                    self._append(submenu, item.children)
                    self.user32.AppendMenuW(handle, MF_POPUP | MF_STRING,
                                            submenu, item.text)
            else:
                command = _FIRST_COMMAND + len(self._commands)
                self._commands[command] = item.key
                flags = MF_STRING
                flags |= 0 if item.enabled else MF_GRAYED | MF_DISABLED
                flags |= MF_CHECKED if item.checked else 0
                self.user32.AppendMenuW(handle, flags, command, item.text)
            if item.separator_after:
                self.user32.AppendMenuW(handle, MF_SEPARATOR, 0, None)

    def _show_menu(self) -> None:
        with self._lock:
            state = self._state
        items = tray_mod.menu(state)
        handle = self.user32.CreatePopupMenu()
        if not handle:
            return
        self._commands = {}
        chosen = 0
        try:
            self._append(handle, items)
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            # Documented requirement: without this the menu does not close when
            # the user clicks elsewhere, and it is the classic tray-menu bug.
            self.user32.SetForegroundWindow(self._hwnd)
            chosen = self.user32.TrackPopupMenu(
                handle, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x, point.y, 0, self._hwnd, None)
            self.user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            self.user32.DestroyMenu(handle)
        if chosen:
            self._invoke(chosen)

    def _invoke(self, command: int) -> None:
        key = self._commands.get(command)
        if not key:
            return
        try:
            self.actions.invoke(key)
        except Exception:
            log.exception("the tray's %s action failed", key)
