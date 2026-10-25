"""
core/keyboard_utils.py — Unified Win32 hardware-scancode keyboard helper.

Works regardless of system keyboard layout (Arabic, English, etc.).
Use this instead of pyautogui keyboard functions everywhere.
"""
import sys
import time

try:
    import ctypes
    from ctypes import wintypes
    _CTYPES_OK = True
except ImportError:
    _CTYPES_OK = False

try:
    import pyperclip
    _PYPERCLIP_OK = True
except ImportError:
    _PYPERCLIP_OK = False

# Win32 VK codes
VK_CONTROL = 0x11
VK_V       = 0x56
VK_RETURN  = 0x0D
VK_ESCAPE  = 0x1B
VK_TAB     = 0x09
VK_BACK    = 0x08
VK_DELETE  = 0x2E
VK_LEFT    = 0x25
VK_RIGHT   = 0x27
VK_UP      = 0x26
VK_DOWN    = 0x28
VK_HOME    = 0x24
VK_END     = 0x23
VK_A       = 0x41

KEYEVENTF_KEYUP = 0x0002


def _keybd_event(vk: int, scan: int, flags: int = 0) -> None:
    """Raw Win32 keybd_event wrapper."""
    if not _CTYPES_OK:
        return
    ctypes.windll.user32.keybd_event(vk, scan, flags, 0)


def _get_scan(vk: int) -> int:
    """Map VK code to hardware scan code."""
    if not _CTYPES_OK:
        return 0
    return ctypes.windll.user32.MapVirtualKeyW(vk, 0)


def press_key(vk: int, hold_ms: int = 30) -> None:
    """Press and release a single key by VK code."""
    scan = _get_scan(vk)
    _keybd_event(vk, scan, 0)
    time.sleep(hold_ms / 1000)
    _keybd_event(vk, scan, KEYEVENTF_KEYUP)
    time.sleep(0.03)


def press_enter() -> None:
    """Press Enter key."""
    press_key(VK_RETURN)


def press_escape() -> None:
    """Press Escape key."""
    press_key(VK_ESCAPE)


def press_tab() -> None:
    """Press Tab key."""
    press_key(VK_TAB)


def ctrl_a() -> None:
    """Ctrl+A (Select All)."""
    scan_ctrl = _get_scan(VK_CONTROL)
    scan_a    = _get_scan(VK_A)
    _keybd_event(VK_CONTROL, scan_ctrl, 0)
    time.sleep(0.03)
    _keybd_event(VK_A, scan_a, 0)
    time.sleep(0.03)
    _keybd_event(VK_A, scan_a, KEYEVENTF_KEYUP)
    time.sleep(0.03)
    _keybd_event(VK_CONTROL, scan_ctrl, KEYEVENTF_KEYUP)
    time.sleep(0.05)


def paste_clipboard() -> None:
    """Ctrl+V via Win32 scan codes — layout-independent paste."""
    scan_ctrl = _get_scan(VK_CONTROL)
    scan_v    = _get_scan(VK_V)
    _keybd_event(VK_CONTROL, scan_ctrl, 0)
    time.sleep(0.05)
    _keybd_event(VK_V, scan_v, 0)
    time.sleep(0.05)
    _keybd_event(VK_V, scan_v, KEYEVENTF_KEYUP)
    time.sleep(0.05)
    _keybd_event(VK_CONTROL, scan_ctrl, KEYEVENTF_KEYUP)
    time.sleep(0.1)


def type_text_via_clipboard(text: str) -> None:
    """
    Set clipboard to `text` then paste via Win32 Ctrl+V.
    Bypasses keyboard layout completely — works with Arabic/English/any layout.
    """
    if not _PYPERCLIP_OK:
        raise RuntimeError("pyperclip not installed")
    pyperclip.copy(text)
    time.sleep(0.1)
    paste_clipboard()


def type_and_send(text: str) -> None:
    """
    Type `text` via clipboard paste then press Enter to send.
    The canonical way to send text in any app regardless of keyboard layout.
    """
    type_text_via_clipboard(text)
    time.sleep(0.15)
    press_enter()
