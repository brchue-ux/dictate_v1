"""Delivering the finished text to the window that had focus.

Two methods, chosen in config. The default is `sendinput`.

**Why `sendinput` is the default.** It synthesises the characters as keystrokes
and never touches the clipboard, so there is no clipboard to preserve and no
window in which a clipboard manager, a second paste, or a slow application can
observe or clobber the wrong contents. Save-paste-restore is a race by
construction: you cannot know when the target application has finished reading
the clipboard, so the restore is always a guess, and getting it wrong either
loses the user's clipboard or leaves the dictated text sitting in it.

**Why `clipboard` exists anyway.** A very long dictation is thousands of
synthetic keystrokes, and a few applications (some remote-desktop and terminal
emulators) drop them or reorder them. For those, Ctrl+V is more reliable.

When `clipboard` is selected, the previous contents ARE saved and restored - and
if the clipboard holds something that cannot be faithfully restored (an image,
a file list), this falls back to `sendinput` for that paste rather than
destroying it. That behaviour is deliberate: silently eating the user's
clipboard would be worse than being slightly slower.

**Neither method may press a key on his behalf.** Whatever the text contains, it
is delivered as characters unless `[paste] line_breaks` says otherwise: a line
break becomes a space rather than a Return, because Return runs the command line
in a terminal. That is applied here, once, to the payload both methods share -
so the clipboard route cannot submit either, which it would if only the
keystroke plan knew about it. See `platform/line_breaks.py`.

Prior art: `PinW/whisper-key-local` (MIT) uses the same SendInput +
KEYEVENTF_UNICODE approach for its own typing path, and reading it confirmed the
layout-independent Unicode route was the right one. No code was copied.
"""

from __future__ import annotations

import ctypes
import logging
import time

from ...errors import InjectionError
from .. import line_breaks, modifier_guard
from ..base import TargetWindow
from ..injection_plan import KeyEvent, chunk, plan_text
from .win32 import (
    CF_UNICODETEXT,
    GMEM_MOVEABLE,
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_CONTROL,
    VK_V,
    kernel32,
    last_error,
    user32,
)

log = logging.getLogger(__name__)

#: Clipboard formats we can save and put back byte-for-byte.
_RESTORABLE_FORMATS = {CF_UNICODETEXT}

#: Virtual keys for the three modifiers `modifier_guard` names. Control and Menu
#: (which is Alt) are the "either side" codes, which is what `GetAsyncKeyState`
#: answers for and what a key-up has to name to clear both. Windows has no such
#: combined code, so both of its keys are listed.
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
_MODIFIER_VKS = {
    modifier_guard.CONTROL: (VK_CONTROL,),
    modifier_guard.ALT: (VK_MENU,),
    modifier_guard.WINDOW: (VK_LWIN, VK_RWIN),
}


def _to_input(ev: KeyEvent) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    flags = KEYEVENTF_KEYUP if ev.up else 0
    if ev.kind == "unicode":
        item.ki.wVk = 0
        item.ki.wScan = ev.code
        item.ki.dwFlags = flags | KEYEVENTF_UNICODE
    else:
        item.ki.wVk = ev.code
        item.ki.wScan = 0
        item.ki.dwFlags = flags
    return item


def _send(events: list[KeyEvent]) -> None:
    if not events:
        return
    array = (INPUT * len(events))(*[_to_input(e) for e in events])
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise InjectionError(
            f"Windows accepted only {sent} of {len(events)} keystrokes. "
            f"{last_error()}",
            "This usually means another program is blocking synthetic input - "
            "most often an application running as administrator while dictate "
            "is not. Run dictate as administrator, or switch [paste] method to "
            '"clipboard".',
        )


class WindowsTextInjector:
    """Implements `platform.base.TextInjector`."""

    def __init__(self, tracker, *, method: str = "sendinput", restore_focus: bool = True,
                 per_char_delay_ms: float = 0.0, clipboard_restore_delay_ms: int = 300,
                 trailing_space: bool = True, line_break_mode: str = line_breaks.SPACE,
                 modifier_wait_ms: int = 400, is_recording=None) -> None:
        self.tracker = tracker
        self.method = method
        self.restore_focus = restore_focus
        self.per_char_delay_ms = per_char_delay_ms
        self.clipboard_restore_delay_ms = clipboard_restore_delay_ms
        self.trailing_space = trailing_space
        self.line_break_mode = line_break_mode
        self.modifier_wait_ms = modifier_wait_ms
        #: "Is he speaking right now?", set by `app.Application` once the
        #: pipeline exists. The modifier guard needs it and nothing else here
        #: does - see `_settle_modifiers`.
        self.is_recording = is_recording

    @property
    def describe(self) -> str:
        how = ("clipboard paste (Ctrl+V), previous clipboard restored"
               if self.method == "clipboard"
               else "direct keystroke synthesis (SendInput, Unicode) - "
                    "clipboard untouched")
        breaks = ("line breaks sent as Return"
                  if self.line_break_mode == line_breaks.RETURN
                  else "line breaks pasted as a space, never Return")
        return f"{how}, {breaks}"

    def send(self, text: str, target: TargetWindow | None) -> int:
        """Deliver `text`. Returns how many Return keypresses it sent.

        That count is what the dictation history reports, so an Enter he did not
        ask for is something he can look up afterwards. With the default
        `[paste] line_breaks` it is always 0, and that is the point of it.
        """
        if not text:
            return 0
        text = line_breaks.apply(text, self.line_break_mode)
        payload = text + " " if self.trailing_space and not text.endswith(" ") else text

        if target is not None and self.restore_focus:
            if not self.tracker.focus(target):
                raise InjectionError(
                    f"The window you were typing into ({target}) could not be "
                    f"brought back to the front, so dictate did not paste "
                    f"anywhere.",
                    "Click that window and dictate again. Nothing was lost from "
                    "your clipboard.",
                )

        self._settle_modifiers()
        if self.method == "clipboard":
            self._send_via_clipboard(payload)
        else:
            self._send_via_keystrokes(payload)
        return line_breaks.returns_in(payload)

    # -- his own modifiers ------------------------------------------------

    def _settle_modifiers(self) -> None:
        """Do not type into a chord he has not finished letting go of.

        Never allowed to raise: this is a guard in front of the paste, and a
        paste that failed because the guard failed would be strictly worse than
        the shortcuts it exists to prevent.
        """
        try:
            held, forced = modifier_guard.settle(
                _modifier_is_down, _force_modifiers_up,
                wait_s=self.modifier_wait_ms / 1000.0,
                recording=self.is_recording)
        except Exception:
            log.debug("could not check which modifier keys are held", exc_info=True)
            return
        if forced:
            log.warning("pasted with %s still held; sent the key-up so the text "
                        "was typed rather than run as shortcuts", ", ".join(forced))
        elif held:
            log.debug("waited for %s to come up before pasting", ", ".join(held))

    # -- keystroke path --------------------------------------------------

    def _send_via_keystrokes(self, text: str) -> None:
        events = plan_text(text,
                           allow_return=self.line_break_mode == line_breaks.RETURN)
        if self.per_char_delay_ms > 0:
            gap = self.per_char_delay_ms / 1000.0
            for batch in chunk(events, 2):  # one character at a time
                _send(batch)
                time.sleep(gap)
        else:
            for batch in chunk(events, 200):
                _send(batch)
        log.debug("sent %d characters as keystrokes", len(text))

    # -- clipboard path --------------------------------------------------

    def _send_via_clipboard(self, text: str) -> None:
        formats = _clipboard_formats()
        unrestorable = [f for f in formats if f not in _RESTORABLE_FORMATS]
        if unrestorable:
            log.warning(
                "the clipboard holds content dictate cannot put back "
                "(format ids %s), so this paste used keystrokes instead",
                ", ".join(str(f) for f in unrestorable),
            )
            self._send_via_keystrokes(text)
            return

        saved = _clipboard_text()
        try:
            _set_clipboard_text(text)
            _send([
                KeyEvent("vk", VK_CONTROL, False),
                KeyEvent("vk", VK_V, False),
                KeyEvent("vk", VK_V, True),
                KeyEvent("vk", VK_CONTROL, True),
            ])
            # There is no way to know when the target has finished reading, so
            # this wait is a considered guess. See the module docstring.
            time.sleep(self.clipboard_restore_delay_ms / 1000.0)
        finally:
            try:
                if saved is None:
                    _empty_clipboard()
                else:
                    _set_clipboard_text(saved)
            except InjectionError:
                log.exception("could not restore the previous clipboard contents")


def _modifier_is_down(name: str) -> bool:
    """Is he physically holding this modifier right now?

    `GetAsyncKeyState`'s high bit is "down now", which is the question - the low
    bit is "pressed since last asked" and would answer a different one.
    """
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS[name])


def _force_modifiers_up(names: list[str]) -> None:
    """Tell Windows those keys are up. Key-ups only: this never presses anything."""
    events = [KeyEvent("vk", vk, True) for name in names for vk in _MODIFIER_VKS[name]]
    _send(events)


def _open_clipboard(attempts: int = 12, gap_s: float = 0.02) -> None:
    """The clipboard is a single global lock; another app may hold it briefly."""
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return
        time.sleep(gap_s)
    raise InjectionError(
        f"Could not open the Windows clipboard. {last_error()}",
        "Another program is holding the clipboard open. Close clipboard-manager "
        'tools, or switch [paste] method to "sendinput" (the default), which '
        "does not use the clipboard at all.",
    )


def _clipboard_formats() -> list[int]:
    _open_clipboard()
    try:
        formats, fmt = [], 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            formats.append(fmt)
        return formats
    finally:
        user32.CloseClipboard()


def _clipboard_text() -> str | None:
    _open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise InjectionError(f"Out of memory writing to the clipboard. {last_error()}",
                             'Switch [paste] method to "sendinput".')
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise InjectionError(f"Could not lock clipboard memory. {last_error()}",
                             'Switch [paste] method to "sendinput".')
    ctypes.memmove(ptr, ctypes.byref(data), size)
    kernel32.GlobalUnlock(handle)

    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise InjectionError(f"Could not write to the clipboard. {last_error()}",
                                 'Switch [paste] method to "sendinput".')
        # Ownership of `handle` passed to the system; do not free it now.
    finally:
        user32.CloseClipboard()


def _empty_clipboard() -> None:
    _open_clipboard()
    try:
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()
