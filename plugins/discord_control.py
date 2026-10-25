"""
plugins/discord_control.py — Native Discord Control Plugin for JARVIS Mark LIV.

Enables JARVIS to:
1. Rock-solidly focus or launch Discord on Windows using process PID inspection,
   top-level window dimensions (w > 300, h > 300), and Alt-key bypass for SetForegroundWindow.
2. Jump directly to any channel, server, or DM (e.g. "lite") using Discord's Quick Switcher (Ctrl+K).
3. Send messages into channels or chats.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from actions.screen_processor import _capture_screen, _ensure_desktop
from core import gemini

try:
    from google.genai import types as gtypes
    _GENAI_TYPES_OK = True
except ImportError:
    _GENAI_TYPES_OK = False

try:
    from core.keyboard_utils import type_and_send as _kb_type_and_send, press_key, VK_RETURN
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
    "name": "discord_control",
    "description": (
        "Controls Discord on Windows: opens Discord, focuses its window, visually locates elements, "
        "searches for and jumps directly to any person, DM, channel, or server (e.g. 'lite'), "
        "sends messages, and visually reads/analyzes chat conversations and attachments. "
        "Use 'visual_chat' to open a chat, send a message, and read/inspect the chat content in one go."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "visual_chat | open | switch_channel | search | send_message | read_chat"
            },
            "target": {
                "type": "STRING",
                "description": "Person name, friend, channel name, or server to search for and open (e.g. 'lite' or 'Lite AMD')"
            },
            "message": {
                "type": "STRING",
                "description": "Message content to send if action is send_message or visual_chat"
            },
            "read_chat": {
                "type": "BOOLEAN",
                "description": "Whether to visually read and report the chat conversation after navigating or sending (default: True)"
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


def _get_discord_pids() -> set[int]:
    """Retrieve PIDs of all running Discord processes."""
    pids = set()
    if not _PSUTIL_OK:
        return pids
    try:
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info.get("name") or "").lower()
            if "discord" in name:
                pids.add(p.info["pid"])
    except Exception as e:
        print(f"[discord_control] Error inspecting processes: {e}")
    return pids


_LAST_DISCORD_RECT = None


def _find_discord_window(pids: set[int]):
    """Find the top-level main window handle and rect for Discord using pure ctypes."""
    if sys.platform != "win32":
        return None, None

    _ensure_desktop()
    found_hwnd = None
    found_rect = None

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
                if pid.value in pids:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    # Discord main Electron window has substantial width and height
                    if (w > 300 and h > 300) or iconic:
                        found_hwnd = hwnd
                        found_rect = (rect.left, rect.top, rect.right, rect.bottom)
                        return False  # found main window
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception as e:
        print(f"[discord_control] Error enumerating windows: {e}")

    return found_hwnd, found_rect


def _launch_discord_process() -> bool:
    """Launch Discord via standard paths or URI scheme."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    disc_dir = local_app_data / "Discord"
    if disc_dir.exists():
        update_exe = disc_dir / "Update.exe"
        if update_exe.exists():
            try:
                subprocess.Popen(f'"{update_exe}" --processStart Discord.exe', shell=True)
                time.sleep(2.5)
                return True
            except Exception:
                pass
        app_dirs = sorted(disc_dir.glob("app-*"), reverse=True)
        for ad in app_dirs:
            disc_exe = ad / "Discord.exe"
            if disc_exe.exists():
                try:
                    os.startfile(str(disc_exe))
                    time.sleep(2.5)
                    return True
                except Exception:
                    pass

    # Start menu shortcut fallback
    app_data = Path(os.environ.get("APPDATA", ""))
    lnk = app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Discord.lnk"
    if lnk.exists():
        try:
            os.startfile(str(lnk))
            time.sleep(2.5)
            return True
        except Exception:
            pass

    # Protocol URI fallback
    try:
        os.startfile("discord:")
        time.sleep(2.5)
        return True
    except Exception as e:
        print(f"[discord_control] Failed to start discord: protocol: {e}")
        return False


def _focus_discord() -> bool:
    """
    Find and activate Discord window using PID inspection, window rect check (w > 300 and h > 300),
    and verified Win32 foreground activation to bring Discord to the front reliably.
    Launches Discord if not running.
    """
    global _LAST_DISCORD_RECT
    if sys.platform != "win32":
        # Non-Windows fallback
        try:
            subprocess.Popen(["discord"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
            return True
        except Exception:
            return False

    _ensure_desktop()
    pids = _get_discord_pids()

    # If Discord is not running at all, launch it
    if not pids:
        _launch_discord_process()
        pids = _get_discord_pids()

    hwnd, rect = _find_discord_window(pids)

    # If window not found immediately, give it a moment to render
    if not hwnd:
        time.sleep(1.0)
        pids = _get_discord_pids()
        hwnd, rect = _find_discord_window(pids)

    if not hwnd:
        # Last resort: try launching again and re-finding
        _launch_discord_process()
        pids = _get_discord_pids()
        hwnd, rect = _find_discord_window(pids)

    if hwnd:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            # Win32 foreground unlock bypass: tap Alt down, restore & set foreground, tap Alt up
            VK_MENU = 0x12
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.ShowWindow(hwnd, 9)  # 9 = SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.keybd_event(VK_MENU, 0, 2, 0)

            # Re-read rect after restore to ensure fresh physical coordinates
            rect_buf = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect_buf))
            _LAST_DISCORD_RECT = (rect_buf.left, rect_buf.top, rect_buf.right, rect_buf.bottom)

            time.sleep(0.2)
            if _AUTOMATION_OK:
                pyautogui.press("escape")

            time.sleep(0.1)
            return True
        except Exception as e:
            print(f"[discord_control] Focus hwnd error: {e}")
            return False

    return False


def _parse_coordinates(text: str, screen_w: int, screen_h: int) -> tuple[int, int] | None:
    """Parse coordinates from Gemini Vision output (supporting normalized 0-1000, pixel coords, boxes, JSON)."""
    if not text:
        return None
    raw = text.strip()
    if "NOT_FOUND" in raw.upper():
        return None

    # 1. Bounding box check [ymin, xmin, ymax, xmax] (normalized 0-1000)
    box_match = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", raw)
    if box_match:
        ymin, xmin, ymax, xmax = [float(box_match.group(i)) for i in range(1, 5)]
        norm_x = (xmin + xmax) / 2.0
        norm_y = (ymin + ymax) / 2.0
        x_px = int((norm_x / 1000.0) * screen_w) if norm_x <= 1000 else int(norm_x)
        y_px = int((norm_y / 1000.0) * screen_h) if norm_y <= 1000 else int(norm_y)
        return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))

    # 2. JSON check {"x": ..., "y": ...} or {"point": [y, x]}
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

    # 3. Simple comma-separated x,y
    match = re.search(r"(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)", raw)
    if match:
        nx, ny = float(match.group(1)), float(match.group(2))
        if nx <= 1000 and ny <= 1000:
            x_px = int((nx / 1000.0) * screen_w)
            y_px = int((ny / 1000.0) * screen_h)
        else:
            x_px = int(nx)
            y_px = int(ny)
        return max(0, min(screen_w - 1, x_px)), max(0, min(screen_h - 1, y_px))

    return None


def visual_locate(description: str, tier: str = gemini.FAST) -> tuple[int, int] | None:
    """
    High-accuracy visual locator using Gemini Vision.
    Captures screen with _ensure_desktop(), asks Gemini Vision for normalized coords (0-1000),
    and converts to actual screen coordinates.
    """
    if not _AUTOMATION_OK:
        return None
    try:
        _ensure_desktop()
        w, h = pyautogui.size()
        img_bytes, mime = _capture_screen()

        prompt = (
            f"This is a screenshot of a {w}x{h} pixel screen. "
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
        if coords:
            print(f"[discord_control] 🎯 Visual locate '{description}' → screen coords {coords} (vision: '{text}')")
        else:
            print(f"[discord_control] ⚠️ Visual locate '{description}' → NOT_FOUND (vision: '{text}')")
        return coords
    except Exception as e:
        print(f"[discord_control] ⚠️ visual_locate error: {e}")
        return None


def visual_locate_and_click(description: str, clicks: int = 1, button: str = "left", tier: str = gemini.FAST) -> bool:
    """Visually locate the target on screen and click it."""
    coords = visual_locate(description, tier=tier)
    if coords:
        time.sleep(0.1)
        pyautogui.click(coords[0], coords[1], clicks=clicks, button=button)
        return True
    return False


def _read_screen_chat() -> str:
    """
    Captures the current screen frame after navigation or sending and sends
    it to Gemini Vision (SMART tier) to analyze and read the chat in detail in clear Arabic.
    """
    try:
        _ensure_desktop()
        time.sleep(0.6)  # allow UI to settle / new messages to render
        img_bytes, mime = _capture_screen()

        prompt = (
            "Analyze this screen capture of Discord/the chat. "
            "Read who the conversation is with, what the latest messages say in detail, "
            "and what any visible images or attachments contain. "
            "Report in clear Arabic for AMOX."
        )

        if _GENAI_TYPES_OK:
            part = gtypes.Part.from_bytes(data=img_bytes, mime_type=mime)
        else:
            part = {"inline_data": {"mime_type": mime, "data": img_bytes}}

        resp = gemini.call([part, prompt], tier=gemini.SMART, timeout_ms=35_000)
        report = (getattr(resp, "text", None) or "").strip()
        if not report:
            return "تعذر قراءة محتوى الشات بصرياً في الوقت الحالي."
        return report
    except Exception as e:
        print(f"[discord_control] ⚠️ _read_screen_chat error: {e}")
        return f"حدث خطأ أثناء قراءة الشات: {e}"


def _focus_message_box(rect=None) -> bool:
    """
    Click Discord's message input box at the bottom of the window to ensure keyboard focus.
    Coordinates calculated relative to Discord's window rect, with visual locator fallback.
    Uses dwell-time click to allow Chromium/Electron to activate contenteditable text area.
    """
    global _LAST_DISCORD_RECT
    if not _AUTOMATION_OK:
        return False

    target_rect = rect or _LAST_DISCORD_RECT
    if not target_rect and sys.platform == "win32":
        try:
            _ensure_desktop()
            pids = _get_discord_pids()
            _, target_rect = _find_discord_window(pids)
        except Exception:
            pass

    if target_rect:
        try:
            w = target_rect[2] - target_rect[0]
            click_x = target_rect[0] + min(650, int(w * 0.45))
            click_y = target_rect[3] - 45
            pyautogui.moveTo(click_x, click_y)
            time.sleep(0.1)
            pyautogui.mouseDown()
            time.sleep(0.08)
            pyautogui.mouseUp()
            time.sleep(0.3)
            return True
        except Exception as e:
            print(f"[discord_control] Geometry message box focus failed: {e}")

    # Fallback to visual locator
    return visual_locate_and_click("Message input text box or text area at the bottom of the active Discord chat")


def _type_and_send(message: str) -> bool:
    """
    Reliably types and sends a message into Discord's active message input box.
    Uses hardware scan codes for Ctrl+V and Enter on Windows to bypass keyboard layout issues (e.g. Arabic 101/102).
    """
    if not message:
        return False

    if _KEYBOARD_UTILS_OK:
        try:
            _kb_type_and_send(message)
            return True
        except Exception as e:
            print(f"[discord_control] _kb_type_and_send error, using fallback: {e}")

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

            # Send Ctrl+V with hardware scan codes
            user32.keybd_event(VK_CONTROL, sc_ctrl, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_V, sc_v, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_V, sc_v, 2, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_CONTROL, sc_ctrl, 2, 0)
            time.sleep(0.3)

            # Press Enter to send
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
        print(f"[discord_control] _type_and_send error: {e}")
        try:
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyautogui.press("enter")
            return True
        except Exception:
            return False


def search_and_open(target: str, message: str = "") -> str:
    """
    Focuses Discord, opens the Quick Switcher (Ctrl+K), pastes the target name,
    presses Enter to navigate, clicks the message input field to guarantee focus,
    and optionally sends a message.
    """
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Discord automation."

    if not target:
        _focus_discord()
        return "Focused Discord."

    _focus_discord()
    time.sleep(0.4)

    # Trigger Discord Quick Switcher
    pyautogui.hotkey("ctrl", "k")
    time.sleep(0.35)

    # Clear any previous search text
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.05)
    pyautogui.press("backspace")
    time.sleep(0.05)

    # Paste target via clipboard (safe for Arabic & any keyboard layout)
    pyperclip.copy(target)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.5)

    # Press Enter to open the top matched channel / DM / server
    pyautogui.press("enter")
    time.sleep(0.6)

    # Focus the message input field by clicking at calculated coordinates
    _focus_message_box()

    if message:
        time.sleep(0.2)
        _type_and_send(message)
        return f"Opened Discord, navigated to '{target}', and sent message."

    return f"Opened Discord, navigated to '{target}', and focused the message area."


def visual_chat(target: str = "", message: str = "", read_chat: bool = True, player=None) -> str:
    """
    Complete End-to-End Vision-Driven Interaction Loop:
    Step 1: Ensure Discord is in the foreground via _focus_discord().
    Step 2: Navigate to target:
      - Check if target is visible on screen (e.g. in DMs list). If found via vision, click it directly!
      - Fallback: Use Quick Switcher (Ctrl+K -> paste target -> Enter).
    Step 3: If message is provided:
      - Focus message input box (_focus_message_box or visual click on Message @...).
      - Paste message via clipboard (Ctrl + V).
      - Press Enter to send.
    Step 4: If read_chat:
      - Wait 0.6s.
      - Call _read_screen_chat() and return the report.
    """
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Discord automation."

    _focus_discord()
    time.sleep(0.4)

    target_switched = False
    if target:
        # Step 2a: Try visual locate on screen
        desc = (
            f"Direct message, conversation item, or user named '{target}' (or similar matching name) "
            f"in Discord's direct messages list or channels sidebar on the left"
        )
        clicked = visual_locate_and_click(desc)
        if clicked:
            print(f"[discord_control] ✅ Navigated to '{target}' via visual click!")
            target_switched = True
            time.sleep(0.6)
        else:
            # Step 2b: Fallback to Quick Switcher (Ctrl+K)
            print(f"[discord_control] ℹ️ '{target}' not visible directly; using Quick Switcher (Ctrl+K)...")
            pyautogui.hotkey("ctrl", "k")
            time.sleep(0.35)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            pyautogui.press("backspace")
            time.sleep(0.05)
            pyperclip.copy(target)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            pyautogui.press("enter")
            target_switched = True
            time.sleep(0.8)

    message_sent = False
    if message:
        # Step 3: Focus message input box and send
        if not _focus_message_box():
            visual_locate_and_click("Message input text box at the bottom of the active Discord chat")
        time.sleep(0.2)
        _type_and_send(message)
        message_sent = True
        time.sleep(0.5)

    chat_report = ""
    if read_chat:
        time.sleep(0.6)
        chat_report = _read_screen_chat()

    results = []
    if target and target_switched:
        results.append(f"تم فتح المحادثة مع '{target}'.")
    if message and message_sent:
        results.append(f"تم إرسال الرسالة: \"{message}\" بنجاح.")
    if chat_report:
        results.append(chat_report)

    final_msg = "\n\n".join(results) if results else "تم تنفيذ الإجراء بنجاح."
    return final_msg


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not _AUTOMATION_OK:
        return "PyAutoGUI or Pyperclip is not available for Discord automation."

    action = (parameters.get("action") or "open").lower().strip()
    target = (parameters.get("target") or "").strip()
    message = (parameters.get("message") or "").strip()
    read_chat_val = parameters.get("read_chat", True)
    if isinstance(read_chat_val, str):
        read_chat = read_chat_val.lower().strip() not in ("false", "0", "no")
    else:
        read_chat = bool(read_chat_val)

    if action == "visual_chat" or (read_chat and (target or message)):
        msg = visual_chat(target=target, message=message, read_chat=read_chat, player=player)
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
            _focus_discord()
        msg = _read_screen_chat()
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    if action in ("switch_channel", "open_channel", "search", "goto") or target:
        msg = search_and_open(target=target, message=message)
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    elif action == "send_message":
        if not message:
            return "Please provide a message to send."
        _focus_discord()
        time.sleep(0.3)
        _focus_message_box()
        time.sleep(0.2)
        _type_and_send(message)
        msg = "Sent message in Discord."
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg

    else:
        # Simple open / focus
        _focus_discord()
        msg = "Opened Discord."
        if player:
            try:
                player.write_log(f"JARVIS: {msg}")
            except Exception:
                pass
        return msg
