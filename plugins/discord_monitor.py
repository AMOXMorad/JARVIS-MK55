"""
plugins/discord_monitor.py — Proactive Discord ping/notification monitor for JARVIS MK.

Runs a background polling loop that periodically checks the Discord window
for new notifications or messages. If detected, wakes JARVIS and alerts the user.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Safe console encoding
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import ctypes
    _WIN32_OK = True
except ImportError:
    _WIN32_OK = False

try:
    from core import gemini as _gemini_core
    from google.genai import types as _gtypes
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

try:
    from actions.screen_processor import _capture_screen
    _SCREEN_OK = True
except ImportError:
    _SCREEN_OK = False

from memory.config_manager import get_plugin_setting, save_plugin_config

# ── Plugin metadata ───────────────────────────────────────────────────────

PLUGIN = {
    "name": "discord_monitor",
    "description": "Monitor Discord for new messages/pings and alert JARVIS proactively.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["start", "stop", "status"],
                "description": "Control the Discord monitor: start/stop background polling, or check status.",
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "poll_interval_seconds": {
        "type": "integer",
        "label": "Poll Interval (seconds)",
        "default": 30,
        "min": 10,
        "max": 300,
    },
    "notify_on_any_message": {
        "type": "boolean",
        "label": "Notify on any new message",
        "default": False,
    },
    "notify_on_mention": {
        "type": "boolean",
        "label": "Notify on mentions (@you) only",
        "default": True,
    },
}

# ── Monitor state ────────────────────────────────────────────────────────────

_monitor_thread: threading.Thread | None = None
_monitor_running = False
_monitor_lock = threading.Lock()
_last_notification_text: str = ""


def _get_discord_hwnd() -> int | None:
    """Find Discord's main window handle."""
    if not _WIN32_OK:
        return None
    try:
        import ctypes.wintypes
        _found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        def _cb(hwnd, _):
            title_buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, 256)
            title = title_buf.value
            if ctypes.windll.user32.IsWindowVisible(hwnd) and "discord" in title.lower():
                rect = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 300 and h > 300:
                    _found.append(hwnd)
            return True

        ctypes.windll.user32.EnumWindows(_cb, 0)
        return _found[0] if _found else None
    except Exception:
        return None


def _capture_discord_window() -> bytes | None:
    """Capture Discord window as PNG bytes."""
    if not _SCREEN_OK:
        return None
    try:
        import io
        import ctypes.wintypes
        hwnd = _get_discord_hwnd()
        if not hwnd:
            return None
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        region = {
            "top": rect.top,
            "left": rect.left,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        }
        import mss
        with mss.mss() as sct:
            shot = sct.grab(region)
            from mss.tools import to_png
            return to_png(shot.rgb, shot.size)
    except Exception:
        return None


def _check_for_notifications(player=None) -> str | None:
    """
    Check Discord for new messages/notifications using Gemini vision.
    Returns alert text if found, None otherwise.
    """
    global _last_notification_text
    if not _GEMINI_OK or not _SCREEN_OK:
        return None

    img_bytes = _capture_discord_window()
    if not img_bytes:
        return None

    try:
        api_key = ""
        try:
            from memory.config_manager import get_gemini_key
            api_key = get_gemini_key() or ""
        except Exception:
            pass
        if not api_key:
            return None

        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            "هل توجد رسائل جديدة أو إشعارات غير مقروءة في نافذة Discord هذه?"
            " إذا كان هناك notification أو رسالة جديدة، اكتب ملخصًا قصيرًا بالعربية عن من أرسل وماذا قال."
            " إذا لم يكن هناك شيء، أجب فقط بكلمة واحدة: NONE"
        )
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                {"role": "user", "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": img_bytes}},
                    {"text": prompt},
                ]},
            ],
        )
        text = (resp.text or "").strip()
        if text.upper() == "NONE" or not text:
            return None
        # Deduplicate: don't alert twice for the same notification
        if text == _last_notification_text:
            return None
        _last_notification_text = text
        return text
    except Exception:
        return None


def _monitor_loop(player=None):
    """Background thread: poll Discord for notifications."""
    global _monitor_running
    print("[discord_monitor] ▶ Monitor started.")
    while _monitor_running:
        try:
            poll_interval = int(get_plugin_setting("discord_monitor", "poll_interval_seconds", 30))
            alert = _check_for_notifications(player=player)
            if alert and player:
                msg = f"🔔 لديك رسالة جديدة على Discord: {alert}"
                try:
                    # Try to wake JARVIS if sleeping
                    if hasattr(player, '_wake_fn') and player._wake_fn:
                        player._wake_fn()
                    if hasattr(player, 'speak'):
                        player.speak(msg)
                    elif hasattr(player, 'write_log'):
                        player.write_log(msg)
                except Exception:
                    pass
        except Exception as e:
            print(f"[discord_monitor] Error in poll loop: {e}")
        # Sleep in small increments to allow clean shutdown
        poll_interval = int(get_plugin_setting("discord_monitor", "poll_interval_seconds", 30))
        for _ in range(max(5, poll_interval)):
            if not _monitor_running:
                break
            time.sleep(1)
    print("[discord_monitor] ■ Monitor stopped.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point."""
    global _monitor_thread, _monitor_running
    action = (parameters.get("action") or "status").lower().strip()

    if action == "start":
        with _monitor_lock:
            if _monitor_running:
                return "✅ المراقب شغّال بالفعل."
            _monitor_running = True
            _monitor_thread = threading.Thread(
                target=_monitor_loop,
                args=(player,),
                daemon=True,
                name="discord-monitor",
            )
            _monitor_thread.start()
        poll = int(get_plugin_setting("discord_monitor", "poll_interval_seconds", 30))
        return f"✅ بدأت مراقبة Discord كل {poll} ثانية. سأخبرك بأي رسائل جديدة."

    elif action == "stop":
        with _monitor_lock:
            _monitor_running = False
        return "✅ تم إيقاف مراقبة Discord."

    elif action == "status":
        status = "شغّال" if _monitor_running else "موقوف"
        poll = int(get_plugin_setting("discord_monitor", "poll_interval_seconds", 30))
        return f"🔍 مراقب Discord: {status} | الفحص كل {poll}s"

    return f"❌ أمر غير معروف: {action}. الأوامر المتاحة: start, stop, status"
