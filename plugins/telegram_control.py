"""
plugins/telegram_control.py — Native Telegram Desktop Control & Vision Plugin for JARVIS Mark LIV.

Enables JARVIS to:
1. Rock-solidly focus or launch Telegram Desktop on Windows using process PID inspection,
   window rect checks (w > 250, h > 250), and Win32 Alt-key bypass for SetForegroundWindow.
   (Strictly excludes JARVIS's own process to prevent any self-interaction loops).
2. Jump directly to any person, contact, channel, or group (e.g. "yahya" or "يحيى")
   using Telegram's Quick Search (Ctrl+F) or visual chat item detection.
3. Visually read and analyze active chat messages, shared media, and unreplied statements
   using Gemini Vision (SMART tier).
4. Automatically formulate context-aware intelligent replies based on what the contact said
   when requested (e.g. "رد عليه بناء على كلامه") and send them via hardware scan codes.
5. Strict safety guards: NEVER type or click into JARVIS's own window or when Telegram is not foregrounded.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from actions.screen_processor import _capture_screen, _ensure_desktop
from core import gemini

try:
    from google.genai import types as gtypes
    _GENAI_TYPES_OK = True
except ImportError:
    _GENAI_TYPES_OK = False

try:
    from core.keyboard_utils import (
        type_and_send as _kb_type_and_send,
        press_key,
        press_enter,
        press_escape,
        ctrl_a,
        paste_clipboard,
        VK_RETURN,
        VK_ESCAPE,
        VK_DOWN,
        VK_UP,
        VK_CONTROL,
        VK_F,
        VK_K,
    )
    _KEYBOARD_UTILS_OK = True
except ImportError:
    _KEYBOARD_UTILS_OK = False

# Safe console streams for Windows cp1252 / cp1256 / UTF-8
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

try:
    import pyautogui
    import pyperclip
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
    _AUTOMATION_OK = True
except ImportError:
    _AUTOMATION_OK = False

PLUGIN = {
    "name": "telegram_control",
    "description": (
        "Controls Telegram Desktop on Windows: focuses or launches Telegram reliably without using the Win key, "
        "searches for and opens any contact, user, group, or channel (e.g. 'yahya' or 'يحيى') using Quick Search (Ctrl+F), "
        "sends messages via clipboard and hardware scan codes, visually reads and analyzes chat conversations and attachments "
        "using Gemini Vision, and can automatically generate and send context-aware intelligent replies based on what the contact said. "
        "Use 'visual_chat' to open a chat, read the conversation, and/or send an intelligent reply in one go."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "visual_chat | open | search | send_message | read_chat | reply | start_auto_reply | stop_auto_reply | auto_reply_status"
            },
            "target": {
                "type": "STRING",
                "description": "Person name, contact, channel, or group to search for and open (e.g. 'yahya' or 'يحيى')"
            },
            "message": {
                "type": "STRING",
                "description": "Optional message content to send if action is send_message or visual_chat"
            },
            "read_chat": {
                "type": "BOOLEAN",
                "description": "Whether to visually read and report the chat conversation after navigating or sending (default: True)"
            },
            "auto_reply": {
                "type": "BOOLEAN",
                "description": "If True, JARVIS reads what the contact said, generates a smart contextual reply on behalf of the user, and sends it (default: False)"
            }
        },
        "required": ["action"]
    }
}


def _ensure_desktop():
    """Ensure the current thread is attached to the interactive Default desktop and DPI aware."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hDesk = user32.OpenInputDesktop(0, False, 0x01FF) or user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hDesk:
            user32.SetThreadDesktop(hDesk)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    except Exception:
        pass


def _get_telegram_pids() -> set[int]:
    """Retrieve PIDs of all running Telegram processes."""
    pids = set()
    if not _PSUTIL_OK:
        return pids
    try:
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info.get("name") or "").lower()
            if "telegram" in name:
                pids.add(p.info["pid"])
    except Exception as e:
        print(f"[telegram_control] Error inspecting processes: {e}")
    return pids


_LAST_TELEGRAM_RECT = None


def _find_telegram_window(pids: set[int]):
    """
    Find the top-level main window handle and rect for Telegram Desktop using pure ctypes.
    Strictly excludes current process (JARVIS) and requires matching Telegram process PID.
    """
    if sys.platform != "win32":
        return None, None

    _ensure_desktop()
    found_hwnd = None
    found_rect = None
    my_pid = os.getpid()

    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        def _enum_cb(hwnd, _):
            nonlocal found_hwnd, found_rect
            try:
                vis = user32.IsWindowVisible(hwnd)
                iconic = user32.IsIconic(hwnd)
                if not (vis or iconic):
                    return True

                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

                # STRICT CHECK 1: Exclude JARVIS itself
                if pid.value == my_pid:
                    return True

                # STRICT CHECK 2: Must be one of Telegram PIDs if pids list available
                if pids and pid.value not in pids:
                    return True

                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                title = buf.value.lower()

                # STRICT CHECK 3: Exclude any window titled JARVIS
                if any(x in title for x in ("jarvis", "mark liv", "mark 54")):
                    return True

                cls_buf = ctypes.create_unicode_buffer(512)
                user32.GetClassNameW(hwnd, cls_buf, 512)
                cls_name = cls_buf.value.lower()

                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top

                matches_proc = pid.value in pids if pids else False
                matches_class = "qt" in cls_name or "telegram" in cls_name
                matches_title = "telegram" in title

                if (matches_proc or matches_title) and ((w > 250 and h > 250) or iconic):
                    found_hwnd = hwnd
                    found_rect = (rect.left, rect.top, rect.right, rect.bottom)
                    return False  # found main window
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception as e:
        print(f"[telegram_control] Error enumerating windows: {e}")

    return found_hwnd, found_rect


def _get_telegram_exe_path() -> Path | None:
    """Find Telegram.exe binary on system, checking custom shortcuts first."""
    # 1. Check custom shortcuts database
    try:
        from plugins.quick_shortcuts import find_shortcut
        m = find_shortcut("telegram") or find_shortcut("تلجرام")
        if m:
            target = str(m[1].get("target", "")).strip().strip('"\'')
            p = Path(target)
            if p.exists():
                return p
    except Exception:
        pass

    # 2. Check standard Start Menu shortcut
    app_data = Path(os.environ.get("APPDATA", ""))
    lnk = app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Telegram Desktop" / "Telegram.lnk"
    if lnk.exists():
        return lnk

    # 3. Check standard AppData executable
    t_appdata = app_data / "Telegram Desktop" / "Telegram.exe"
    if t_appdata.exists():
        return t_appdata

    # 4. Check LocalAppData
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    t_local = local_app_data / "Programs" / "Telegram Desktop" / "Telegram.exe"
    if t_local.exists():
        return t_local

    # 5. Check Program Files
    for pf in ("C:/Program Files/Telegram Desktop/Telegram.exe", "C:/Program Files (x86)/Telegram Desktop/Telegram.exe"):
        p = Path(pf)
        if p.exists():
            return p

    return None


def _launch_telegram_process() -> bool:
    """Launch or activate Telegram via direct executable, shortcut, or URI protocol."""
    exe_path = _get_telegram_exe_path()
    if exe_path and exe_path.exists():
        try:
            # Clean up working lock file if Telegram was killed improperly earlier
            tdata_working = exe_path.parent / "tdata" / "working"
            if tdata_working.exists():
                try:
                    tdata_working.unlink()
                except Exception:
                    pass

            os.startfile(str(exe_path))
            time.sleep(2.0)
            return True
        except Exception as e:
            print(f"[telegram_control] Could not startfile {exe_path}: {e}")

    try:
        os.startfile("tg://")
        time.sleep(2.0)
        return True
    except Exception as e:
        print(f"[telegram_control] Failed to start tg: protocol: {e}")
        return False


def _focus_telegram() -> bool:
    """
    Find and activate Telegram Desktop window using PID inspection, window rect check,
    and verified Win32 foreground activation with Alt-key bypass to bring Telegram to front.
    Verifies that the resulting foreground window is truly Telegram and NOT JARVIS.
    """
    global _LAST_TELEGRAM_RECT
    if sys.platform != "win32":
        try:
            subprocess.Popen(["telegram-desktop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
            return True
        except Exception:
            return False

    _ensure_desktop()
    pids = _get_telegram_pids()

    # 1. Look for existing visible window
    hwnd, rect = _find_telegram_window(pids)

    # 2. If no window found (either not running or hidden in tray), launch Telegram and poll
    if not hwnd:
        _launch_telegram_process()
        for _ in range(12):
            time.sleep(0.35)
            pids = _get_telegram_pids()
            hwnd, rect = _find_telegram_window(pids)
            if hwnd:
                break

    if not hwnd:
        print("[telegram_control] ❌ Telegram window could not be found or launched.")
        return False

    # 3. Bring Telegram to foreground with Win32 Alt-bypass
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        VK_MENU = 0x12
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.ShowWindow(hwnd, 9)  # 9 = SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.keybd_event(VK_MENU, 0, 2, 0)

        time.sleep(0.3)
        rect_buf = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect_buf))
        _LAST_TELEGRAM_RECT = (rect_buf.left, rect_buf.top, rect_buf.right, rect_buf.bottom)

        # Safety verification: ensure foreground window is NOT JARVIS
        fg = user32.GetForegroundWindow()
        fg_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        if fg_pid.value == os.getpid():
            print("[telegram_control] ⚠️ Foreground window is still JARVIS, retrying foreground activation...")
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.2)

        return True
    except Exception as e:
        print(f"[telegram_control] Focus hwnd error: {e}")
        return False


def _is_foreground_safe_for_telegram() -> bool:
    """Strict safety check: returns True only if Telegram is active and JARVIS is NOT in foreground."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        fg_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))

        # NEVER ALLOW TYPING IF CURRENT PROCESS IS JARVIS
        if fg_pid.value == os.getpid():
            print("[telegram_control] 🛑 SAFETY BLOCK: Current foreground window is JARVIS! Refusing to click or type into JARVIS!")
            return False

        pids = _get_telegram_pids()
        if pids and fg_pid.value not in pids:
            # Try to bring telegram to front once more
            hwnd, _ = _find_telegram_window(pids)
            if hwnd:
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.2)
                fg = user32.GetForegroundWindow()
                user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
                if fg_pid.value in pids:
                    return True
            print("[telegram_control] 🛑 SAFETY BLOCK: Foreground window is not Telegram!")
            return False

        return True
    except Exception as e:
        print(f"[telegram_control] Safety check error: {e}")
        return False


def _parse_coordinates(text: str, screen_w: int, screen_h: int) -> tuple[int, int] | None:
    """Parse coordinates from Gemini Vision output."""
    if not text:
        return None
    raw = text.strip()
    if "NOT_FOUND" in raw.upper():
        return None

    # Bounding box [ymin, xmin, ymax, xmax]
    box_match = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", raw)
    if box_match:
        ymin, xmin, ymax, xmax = [float(box_match.group(i)) for i in range(1, 5)]
        norm_x = (xmin + xmax) / 2.0
        norm_y = (ymin + ymax) / 2.0
        x_px = int((norm_x / 1000.0) * screen_w) if norm_x <= 1000 else int(norm_x)
        y_px = int((norm_y / 1000.0) * screen_h) if norm_y <= 1000 else int(norm_y)
        return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))

    # JSON {"point": [y, x]} or {"x": ..., "y": ...}
    if "{" in raw and "}" in raw:
        try:
            sub = raw[raw.find("{"):raw.rfind("}") + 1]
            data = json.loads(sub)
            if "point" in data and isinstance(data["point"], list) and len(data["point"]) >= 2:
                ny, nx = float(data["point"][0]), float(data["point"][1])
                x_px = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                y_px = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))
            if "x" in data and "y" in data:
                nx, ny = float(data["x"]), float(data["y"])
                x_px = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                y_px = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))
        except Exception:
            pass

    # Comma-separated x,y
    match = re.search(r"(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)", raw)
    if match:
        nx, ny = float(match.group(1)), float(match.group(2))
        x_px = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
        y_px = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
        return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))

    return None


def visual_locate(description: str, tier: str = gemini.FAST) -> tuple[int, int] | None:
    """Locate a UI element on screen using Gemini Vision."""
    if not _AUTOMATION_OK:
        return None
    try:
        _ensure_desktop()
        w, h = pyautogui.size()
        img_bytes, mime = _capture_screen()

        prompt = (
            f"This is a screenshot of a {w}x{h} pixel screen with Telegram Desktop open. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the normalized center coordinates (0 to 1000) as: x,y "
            f"(where 0,0 is top-left and 1000,1000 is bottom-right). "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        if _GENAI_TYPES_OK:
            part = gtypes.Part.from_bytes(data=img_bytes, mime_type=mime)
        else:
            part = {"inline_data": {"mime_type": mime, "data": img_bytes}}

        resp = gemini.call([part, prompt], tier=tier, timeout_ms=25_000)
        if resp is None:
            return None
        text = (getattr(resp, "text", None) or "").strip()
        coords = _parse_coordinates(text, w, h)
        return coords
    except Exception as e:
        print(f"[telegram_control] visual_locate error: {e}")
        return None


def visual_locate_and_click(description: str, clicks: int = 1, button: str = "left", tier: str = gemini.FAST) -> bool:
    """Visually locate the target on screen and click it only if safe."""
    if not _is_foreground_safe_for_telegram():
        return False
    coords = visual_locate(description, tier=tier)
    if coords:
        time.sleep(0.1)
        pyautogui.click(coords[0], coords[1], clicks=clicks, button=button)
        return True
    return False


def _focus_message_box(rect=None) -> bool:
    """Click Telegram's message input box at the bottom right of the window."""
    if not _is_foreground_safe_for_telegram():
        return False

    target_rect = rect or _LAST_TELEGRAM_RECT
    if not target_rect and sys.platform == "win32":
        try:
            _ensure_desktop()
            pids = _get_telegram_pids()
            _, target_rect = _find_telegram_window(pids)
        except Exception:
            pass

    if target_rect:
        try:
            w = target_rect[2] - target_rect[0]
            click_x = target_rect[0] + int(w * 0.65)
            click_y = target_rect[3] - 45
            pyautogui.moveTo(click_x, click_y)
            time.sleep(0.1)
            pyautogui.mouseDown()
            time.sleep(0.08)
            pyautogui.mouseUp()
            time.sleep(0.2)
            return True
        except Exception as e:
            print(f"[telegram_control] Geometry message box focus failed: {e}")

    return visual_locate_and_click("Message input text box or 'Write a message...' placeholder at the bottom of active Telegram chat")


def _type_and_send(message: str) -> bool:
    """
    Reliably types and sends a message into Telegram's active message input box.
    Uses hardware scan codes for Ctrl+V and Enter on Windows.
    Strictly verifies foreground window is Telegram before touching keyboard.
    """
    if not message:
        return False

    if not _is_foreground_safe_for_telegram():
        print("[telegram_control] 🛑 Refusing to send message: Telegram is not foreground window!")
        return False

    if _KEYBOARD_UTILS_OK:
        try:
            _kb_type_and_send(message)
            return True
        except Exception as e:
            print(f"[telegram_control] _kb_type_and_send error, using fallback: {e}")

    try:
        pyperclip.copy(message)
        time.sleep(0.15)

        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_V = 0x56
            VK_RETURN = 0x0D

            sc_ctrl = user32.MapVirtualKeyW(VK_CONTROL, 0)
            sc_v = user32.MapVirtualKeyW(VK_V, 0)
            sc_enter = user32.MapVirtualKeyW(VK_RETURN, 0)

            user32.keybd_event(VK_CONTROL, sc_ctrl, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_V, sc_v, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_V, sc_v, 2, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_CONTROL, sc_ctrl, 2, 0)
            time.sleep(0.3)

            user32.keybd_event(VK_RETURN, sc_enter, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_RETURN, sc_enter, 2, 0)
            time.sleep(0.3)
            return True
        else:
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyautogui.press("enter")
            time.sleep(0.3)
            return True
    except Exception as e:
        print(f"[telegram_control] _type_and_send error: {e}")
        return False


def _read_screen_chat(target: str = "") -> str:
    """
    Captures the current screen frame and asks Gemini Vision (SMART tier)
    to analyze and read the Telegram chat in detail in clear Arabic.
    Detects if the screen shows JARVIS instead of Telegram.
    """
    try:
        _ensure_desktop()
        time.sleep(0.6)
        img_bytes, mime = _capture_screen()

        target_hint = f" with '{target}'" if target else ""
        prompt = (
            f"Analyze this screen capture of Telegram Desktop{target_hint}. "
            f"First: Verify whether Telegram Desktop is clearly visible on screen. "
            f"If the screen shows the desktop or JARVIS assistant window instead of Telegram Desktop, reply: NOT_TELEGRAM_WINDOW. "
            f"Otherwise: Identify who the active conversation is with, read the latest messages in detail "
            f"(distinguishing clearly between what the contact said and what the user sent), "
            f"and describe any visible images, voice messages, files, or attachments. "
            f"Report concisely and clearly in Arabic for AMOX."
        )

        if _GENAI_TYPES_OK:
            part = gtypes.Part.from_bytes(data=img_bytes, mime_type=mime)
        else:
            part = {"inline_data": {"mime_type": mime, "data": img_bytes}}

        resp = gemini.call([part, prompt], tier=gemini.SMART, timeout_ms=35_000)
        report = (getattr(resp, "text", None) or "").strip()
        if not report:
            return "تعذر قراءة محتوى محادثة تلجرام بصرياً في الوقت الحالي."
        return report
    except Exception as e:
        print(f"[telegram_control] ⚠️ _read_screen_chat error: {e}")
        return f"حدث خطأ أثناء قراءة الشات: {e}"


def _generate_auto_reply(target: str, chat_context: str) -> str:
    """
    Formulates a smart, context-aware reply based on the contact's latest messages.
    Returns empty string if context indicates Telegram wasn't visible or has no message to reply to.
    """
    if not chat_context or "NOT_TELEGRAM_WINDOW" in chat_context or "ليست لتطبيق telegram" in chat_context.lower() or "واجهة برنامج jarvis" in chat_context.lower():
        print("[telegram_control] ⚠️ Skipping auto-reply generation: chat context is invalid or shows JARVIS.")
        return ""

    prompt = (
        f"You are JARVIS, personal AI assistant for AMOX.\n"
        f"The user wants you to reply to '{target}' on Telegram based on their latest message.\n"
        f"Here is what the chat shows:\n"
        f"---\n{chat_context}\n---\n\n"
        f"Task:\n"
        f"1. Understand what '{target}' asked or stated in their last message.\n"
        f"2. If there are no clear messages from '{target}' to reply to, or if this is not a Telegram conversation, return: NO_REPLY\n"
        f"3. Otherwise, write a natural, concise, polite, and helpful response on behalf of AMOX in the same language and tone (e.g. Egyptian Arabic or English).\n"
        f"4. Return ONLY the exact reply text to send, without quotes, formatting, or prefixes like 'AMOX:' or 'الرد:'."
    )
    try:
        resp = gemini.call(prompt, tier=gemini.FAST, timeout_ms=20_000)
        reply = (getattr(resp, "text", None) or "").strip()
        reply = reply.strip('"\'')
        if "NO_REPLY" in reply.upper():
            return ""
        return reply
    except Exception as e:
        print(f"[telegram_control] Error generating auto-reply: {e}")
        return ""


def search_and_open(target: str, message: str = "") -> str:
    """
    Focuses Telegram, searches for target contact/channel via Quick Search (Ctrl+F),
    opens the chat, focuses the message input field, and optionally sends a message.
    """
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Telegram automation."

    if not _focus_telegram():
        return "لم أتمكن من العثور على نافذة تطبيق تلجرام أو تشغيلها."

    if not target:
        return "تم فتح تطبيق تلجرام."

    time.sleep(0.4)

    if not _is_foreground_safe_for_telegram():
        return "تطبيق تلجرام غير نشط في مقدمة الشاشة حالياً."

    # 1. Try visual click on existing chat list item first
    desc = f"Chat item or contact named '{target}' in the Telegram chat list on the left"
    if visual_locate_and_click(desc):
        print(f"[telegram_control] ✅ Navigated to '{target}' via visual click!")
    else:
        # 2. Quick Search via Ctrl + F (Telegram standard search)
        print(f"[telegram_control] ℹ️ Navigating to '{target}' using Telegram search...")
        if _KEYBOARD_UTILS_OK:
            press_escape()
            time.sleep(0.1)
            scan_ctrl = ctypes.windll.user32.MapVirtualKeyW(0x11, 0)
            scan_f = ctypes.windll.user32.MapVirtualKeyW(0x46, 0)
            ctypes.windll.user32.keybd_event(0x11, scan_ctrl, 0, 0)
            ctypes.windll.user32.keybd_event(0x46, scan_f, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.keybd_event(0x46, scan_f, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, scan_ctrl, 2, 0)
        else:
            pyautogui.press("escape")
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "f")

        time.sleep(0.3)
        ctrl_a() if _KEYBOARD_UTILS_OK else pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        pyautogui.press("backspace")
        time.sleep(0.05)

        pyperclip.copy(target)
        paste_clipboard() if _KEYBOARD_UTILS_OK else pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)

        pyautogui.press("down")
        time.sleep(0.15)
        press_enter() if _KEYBOARD_UTILS_OK else pyautogui.press("enter")
        time.sleep(0.6)

        press_escape() if _KEYBOARD_UTILS_OK else pyautogui.press("escape")
        time.sleep(0.2)

    # Focus message input field
    _focus_message_box()

    if message:
        time.sleep(0.2)
        if _type_and_send(message):
            return f"تم فتح محادثة '{target}' في تلجرام وإرسال الرسالة بنجاح."
        else:
            return f"تم فتح محادثة '{target}' في تلجرام، ولكن تعذر إرسال الرسالة بأمان."

    return f"تم فتح محادثة '{target}' في تلجرام وتجهيز حقل الكتابة."


def visual_chat(target: str = "", message: str = "", read_chat: bool = True, auto_reply: bool = False, player=None) -> str:
    """
    Complete End-to-End Vision-Driven Telegram Interaction Loop:
    Step 1: Ensure Telegram is in foreground via _focus_telegram().
    Step 2: Navigate to target via visual click or Ctrl+F Quick Search.
    Step 3: Read chat via Gemini Vision.
    Step 4: If auto_reply: formulate smart contextual reply.
    Step 5: Send message via hardware scan codes (with strict foreground verification).
    """
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Telegram automation."

    if not _focus_telegram():
        return "❌ لم أتمكن من العثور على نافذة تطبيق تلجرام أو رفعها للواجهة. يرجى التأكد من تشغيل تلجرام على الجهاز."

    time.sleep(0.3)

    target_switched = False
    if target:
        res = search_and_open(target)
        target_switched = "تم فتح" in res
        time.sleep(0.5)

    chat_report = ""
    if read_chat or auto_reply:
        chat_report = _read_screen_chat(target=target)

    # Check if vision saw JARVIS instead of Telegram
    is_invalid_window = (
        "NOT_TELEGRAM_WINDOW" in chat_report
        or "ليست لتطبيق telegram" in chat_report.lower()
        or "واجهة برنامج jarvis" in chat_report.lower()
        or "واجهة جارفيس" in chat_report
    )

    if is_invalid_window:
        print("[telegram_control] 🛑 Screen analysis shows non-Telegram window. Aborting reply.")
        return "⚠️ نافذة تطبيق تلجرام لم تكن ظاهرة بوضوح في الواجهة (ظهرت واجهة جارفيس أو سطح المكتب). تم إلغاء الإرسال التلقائي منعاً لأي خطأ."

    # Handle auto-reply
    reply_generated = False
    if auto_reply and not message and chat_report:
        auto_msg = _generate_auto_reply(target, chat_report)
        if auto_msg:
            message = auto_msg
            reply_generated = True

    message_sent = False
    if message:
        _focus_message_box()
        time.sleep(0.15)
        message_sent = _type_and_send(message)
        time.sleep(0.4)

    results = []
    if target and target_switched:
        results.append(f"تم فتح المحادثة مع '{target}' على تلجرام.")

    if chat_report and not is_invalid_window:
        results.append(f"محتوى الشات:\n{chat_report}")

    if message and message_sent:
        if reply_generated:
            results.append(f"تم الرد عليه تلقائياً بالرسالة التالية:\n\"{message}\"")
        else:
            results.append(f"تم إرسال الرسالة: \"{message}\" بنجاح.")
    elif message and not message_sent:
        results.append(f"تعذر إرسال الرسالة: لم تكن نافذة تلجرام في المقدمة بشكل آمن.")

    final_msg = "\n\n".join(results) if results else "تم تنفيذ الإجراء بنجاح في تلجرام."
    return final_msg



# ── Continuous Auto-Reply Background Monitor ─────────────────────────────────
_AUTO_REPLY_THREAD: Optional[threading.Thread] = None
_AUTO_REPLY_STOP_EVENT = threading.Event()
_AUTO_REPLY_TARGET = ""


def start_auto_reply_monitor(target: str, poll_interval: int = 8, player=None) -> str:
    """Starts a background daemon thread that monitors the Telegram chat and automatically replies."""
    global _AUTO_REPLY_THREAD, _AUTO_REPLY_STOP_EVENT, _AUTO_REPLY_TARGET
    if not target:
        target = _AUTO_REPLY_TARGET or "yahya"

    if _AUTO_REPLY_THREAD and _AUTO_REPLY_THREAD.is_alive():
        if _AUTO_REPLY_TARGET.lower() == target.lower():
            return f"برنامج الرد التلقائي على '{target}' قيد التشغيل بالفعل في الخلفية."
        else:
            stop_auto_reply_monitor()

    _AUTO_REPLY_TARGET = target
    _AUTO_REPLY_STOP_EVENT.clear()

    def _monitor_loop():
        print(f"[TelegramAutoReply] 🟢 Started monitoring chat with '{target}'...")
        last_seen = ""
        try:
            initial_report = visual_chat(target=target, read_chat=True, auto_reply=False, player=player)
            last_seen = initial_report
        except Exception as e:
            print(f"[TelegramAutoReply] Initial read error: {e}")

        while not _AUTO_REPLY_STOP_EVENT.is_set():
            if _AUTO_REPLY_STOP_EVENT.wait(timeout=poll_interval):
                break

            try:
                pids = _get_telegram_pids()
                if not pids:
                    continue

                chat_report = _read_screen_chat(target=target)
                if not chat_report or "NOT_TELEGRAM_WINDOW" in chat_report:
                    continue

                if chat_report != last_seen:
                    prompt_check = (
                        f"Analyze this Telegram chat conversation with '{target}':\n{chat_report}\n\n"
                        f"Previous state:\n{last_seen}\n\n"
                        f"Did the other contact ('{target}') send a NEW message, statement, or question that requires a response from us?\n"
                        f"Reply ONLY in STRICT JSON:\n"
                        f'{{"has_new_incoming": true, "latest_sender": "contact", "new_message_text": "..."}}'
                    )
                    resp = gemini.call(prompt_check, tier=gemini.FAST, timeout_ms=10_000)
                    txt = (getattr(resp, "text", None) or "").strip()
                    m = re.search(r"\{.*\}", txt, re.DOTALL)
                    if m:
                        data = json.loads(m.group(0))
                        if data.get("has_new_incoming") and str(data.get("latest_sender", "")).lower() == "contact":
                            new_msg = data.get("new_message_text", "")
                            print(f"[TelegramAutoReply] 📩 New message from {target}: '{new_msg}'")
                            if player:
                                try:
                                    player.write_log(f"[TelegramAutoReply] 📩 رسالة جديدة من {target}: '{new_msg}'")
                                except Exception:
                                    pass

                            reply_text = _generate_auto_reply(target, chat_report)
                            if reply_text:
                                _focus_telegram()
                                time.sleep(0.2)
                                _focus_message_box()
                                time.sleep(0.15)
                                if _type_and_send(reply_text):
                                    print(f"[TelegramAutoReply] ✅ Sent auto-reply to {target}: '{reply_text}'")
                                    if player:
                                        try:
                                            player.write_log(f"[TelegramAutoReply] 🤖 تم الرد تلقائياً على {target}: '{reply_text}'")
                                        except Exception:
                                            pass
                    last_seen = chat_report
            except Exception as loop_e:
                print(f"[TelegramAutoReply] Monitor loop error: {loop_e}")

        print(f"[TelegramAutoReply] 🛑 Stopped monitoring chat with '{target}'.")

    _AUTO_REPLY_THREAD = threading.Thread(target=_monitor_loop, daemon=True, name=f"TgAutoReply-{target}")
    _AUTO_REPLY_THREAD.start()
    return f"✅ تم تفعيل برنامج الرد التلقائي على '{target}' في الخلفية بنجاح! سيقوم جارفيس بمراقبة الشات والرد عليه تلقائياً فور وصول أي رسالة جديدة."


def stop_auto_reply_monitor() -> str:
    """Stops the active auto-reply monitor."""
    global _AUTO_REPLY_THREAD, _AUTO_REPLY_STOP_EVENT, _AUTO_REPLY_TARGET
    if not _AUTO_REPLY_THREAD or not _AUTO_REPLY_THREAD.is_alive():
        return "برنامج الرد التلقائي على تلجرام متوقف حالياً."
    _AUTO_REPLY_STOP_EVENT.set()
    _AUTO_REPLY_THREAD.join(timeout=2.5)
    tgt = _AUTO_REPLY_TARGET
    _AUTO_REPLY_TARGET = ""
    return f"🛑 تم إيقاف برنامج الرد التلقائي على '{tgt}' بنجاح."


def get_auto_reply_status() -> str:
    """Returns current auto-reply monitor status."""
    if _AUTO_REPLY_THREAD and _AUTO_REPLY_THREAD.is_alive():
        return f"🟢 برنامج الرد التلقائي شغال حالياً في الخلفية لمراقبة محادثة '{_AUTO_REPLY_TARGET}'."
    return "⚪ برنامج الرد التلقائي متوقف حالياً."


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point called by core.plugin_loader."""
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Telegram automation."

    action = (parameters.get("action") or "open").lower().strip()
    target = (parameters.get("target") or "").strip()
    message = (parameters.get("message") or "").strip()

    # 1. Start Auto-Reply Continuous Background Monitor
    if action in ("start_auto_reply", "start_monitor", "monitor", "auto_reply_monitor") or any(w in action for w in ("استمر", "تابع", "راقب")):
        msg = start_auto_reply_monitor(target=target, player=player)
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    # 2. Stop Auto-Reply
    if action in ("stop_auto_reply", "stop_monitor", "stop_reply") or "وقف" in action:
        msg = stop_auto_reply_monitor()
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    # 3. Status
    if action in ("auto_reply_status", "status"):
        return get_auto_reply_status()

    read_chat_val = parameters.get("read_chat", True)
    if isinstance(read_chat_val, str):
        read_chat = read_chat_val.lower().strip() not in ("false", "0", "no")
    else:
        read_chat = bool(read_chat_val)

    auto_reply_val = parameters.get("auto_reply", False)
    if isinstance(auto_reply_val, str):
        auto_reply = auto_reply_val.lower().strip() in ("true", "1", "yes")
    else:
        auto_reply = bool(auto_reply_val)

    if action in ("reply", "auto_reply") or "رد" in action:
        auto_reply = True

    if action in ("visual_chat", "reply") or auto_reply or (read_chat and (target or message)):
        msg = visual_chat(target=target, message=message, read_chat=read_chat, auto_reply=auto_reply, player=player)
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    if action == "read_chat":
        if target:
            search_and_open(target=target)
            time.sleep(0.5)
        else:
            _focus_telegram()
        msg = _read_screen_chat(target=target)
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    if action in ("search", "open_chat", "goto") or target:
        msg = search_and_open(target=target, message=message)
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    elif action == "send_message":
        if not message:
            return "يرجى تحديد نص الرسالة المطلوب إرسالها."
        if not _focus_telegram():
            return "تعذر فتح تطبيق تلجرام."
        time.sleep(0.3)
        _focus_message_box()
        time.sleep(0.2)
        if _type_and_send(message):
            msg = "تم إرسال الرسالة في تلجرام بنجاح."
        else:
            msg = "تعذر إرسال الرسالة: لم تكن نافذة تلجرام في المقدمة بأمان."
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    else:
        if _focus_telegram():
            msg = "تم فتح وتجهيز تطبيق تلجرام على الشاشة."
        else:
            msg = "تعذر فتح تطبيق تلجرام على الجهاز."
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg
