"""
plugins/telegram_bridge.py — Telegram Remote Control Bridge for JARVIS MK.

Allows the owner to control JARVIS remotely from Telegram.
Long-polling only — NO port forwarding, NO extra libraries.
SECURITY: Only responds to user IDs in config/telegram.json allowed_user_ids.
"""

from __future__ import annotations
import json
import queue
import sys
import threading
import time
from pathlib import Path

for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

PLUGIN = {
    "name": "telegram_bridge",
    "description": (
        "Controls the background Telegram remote bot bridge between the user's phone and JARVIS, "
        "and sends messages TO THE USER through JARVIS's own Telegram bot (Bot API — no desktop app needed). "
        "ACTIONS: 'send' (send `message` to the user on Telegram via the bot) | "
        "'send_status' (send a system status report to the user on Telegram) | "
        "'start' (start phone bridge polling) | 'stop' (stop phone bridge polling) | 'status' (is the bridge running). "
        "Use 'send' / 'send_status' whenever the user says things like: send it to me on Telegram, "
        "ابعتلي على التليجرام, ابعت للبوت, ابعت رسالة لبوت التليجرام, ابعتلي تقرير الحالة. "
        "Messages the user sends to the bot (text or voice) arrive here as normal user turns, and your spoken "
        "replies are mirrored back to the Telegram chat automatically — you don't need to call 'send' to answer them. "
        "IMPORTANT: NEVER call this tool to open/close/operate the Telegram DESKTOP app or to message OTHER people — "
        "use telegram_control / send_message / close_app for that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["send", "send_status", "start", "stop", "status"],
                "description": "send | send_status | start | stop | status"
            },
            "message": {
                "type": "STRING",
                "description": "Text to send to the user on Telegram (required for action='send')."
            }
        },
        "required": ["action"]
    },
    "behavior": "NON_BLOCKING",
}

PLUGIN_SETTINGS = {
    "bot_token": {
        "type": "password",
        "label": "Telegram Bot Token (from @BotFather)",
        "default": "",
    },
    "polling_timeout": {
        "type": "integer",
        "label": "Long-Poll Timeout (seconds)",
        "default": 30,
        "min": 10,
        "max": 60,
    },
}

# ── Process-wide persistent state (survives module re-imports by plugin loader) ──
class _BridgeState:
    def __init__(self):
        self.bridge_thread: threading.Thread | None = None
        self.bridge_running: bool = False
        self.bridge_lock = threading.Lock()
        self.jarvis_instance = None
        self.player_ref = None
        self.response_queue: queue.Queue = queue.Queue()
        self.auto_start_done: bool = False
        # Remote-chat session: the chat that last talked to JARVIS and when.
        # While it is recent, every JARVIS reply is mirrored to that chat.
        self.token: str = ""
        self.allowed_ids: set = set()
        self.active_chat_id = None
        self.last_remote_at: float = 0.0
        self.forwarder_thread: threading.Thread | None = None
        self.worker_thread: threading.Thread | None = None
        self.inbox: queue.Queue = queue.Queue()

if not hasattr(sys, "_jarvis_telegram_state"):
    sys._jarvis_telegram_state = _BridgeState()

_state = sys._jarvis_telegram_state


def is_running() -> bool:
    return bool(_state.bridge_running and _state.bridge_thread and _state.bridge_thread.is_alive())


def push_response(text: str):
    if text:
        _state.response_queue.put_nowait(text)


def set_jarvis(jarvis):
    """Register live JarvisLive instance so telegram bridge can wake, sleep, and command directly."""
    _state.jarvis_instance = jarvis
    if jarvis and hasattr(jarvis, "ui"):
        _state.player_ref = jarvis.ui


def get_jarvis():
    return _state.jarvis_instance or getattr(sys, "_jarvis_live_instance", None)


def set_player(player):
    """Register UI player callback reference."""
    _state.player_ref = player


def get_player():
    return _state.player_ref or getattr(sys, "_jarvis_ui_instance", None)


# Module-level attribute access compatibility
def __getattr__(name: str):
    if name == "_bridge_running":
        return _state.bridge_running
    elif name == "_bridge_thread":
        return _state.bridge_thread
    elif name == "_bridge_lock":
        return _state.bridge_lock
    elif name == "_jarvis_instance":
        return get_jarvis()
    elif name == "_player_ref":
        return get_player()
    elif name == "_response_queue":
        return _state.response_queue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config" / "telegram.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": True, "bot_token": "", "allowed_user_ids": [], "polling_interval": 30, "auto_start": True}


def _send_message(token: str, chat_id: int | str, text: str):
    if not _REQUESTS_OK:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print(f"[TelegramBridge] Send error: {e}")


def _send_photo(token: str, chat_id: int | str, image_bytes: bytes, caption: str = ""):
    if not _REQUESTS_OK:
        return
    try:
        if isinstance(image_bytes, (tuple, list)):
            image_bytes = image_bytes[0]
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=data,
            files={"photo": ("screenshot.jpg", image_bytes, "image/jpeg")},
            timeout=15
        )
    except Exception as e:
        print(f"[TelegramBridge] Send photo error: {e}")


def _normalize_mime(mime_type: str) -> str:
    """Strip codec suffixes and normalize Telegram audio mime types for Gemini API.

    Telegram sends e.g. 'audio/ogg; codecs=opus' — the Gemini API rejects
    anything after the semicolon. We also map formats Gemini doesn't support
    to the closest acceptable equivalent.
    """
    if not mime_type:
        return "audio/ogg"
    # Strip everything after semicolon (e.g. '; codecs=opus')
    base = mime_type.split(";")[0].strip().lower()
    # Normalize to Gemini-supported types
    _map = {
        "audio/ogg": "audio/ogg",
        "audio/mpeg": "audio/mpeg",
        "audio/mp3": "audio/mpeg",
        "audio/mp4": "audio/mp4",
        "audio/m4a": "audio/mp4",
        "audio/wav": "audio/wav",
        "audio/x-wav": "audio/wav",
        "audio/webm": "audio/webm",
        "audio/aac": "audio/aac",
        "audio/flac": "audio/flac",
    }
    return _map.get(base, "audio/ogg")


def _transcribe_audio(token: str, file_id: str, mime_type: str = "audio/ogg") -> str:
    """Download Telegram voice note and transcribe via Gemini."""
    if not _REQUESTS_OK:
        return ""
    try:
        # 1. Get file path from Telegram
        res = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=15)
        if not res.ok:
            return ""
        file_path = res.json().get("result", {}).get("file_path")
        if not file_path:
            return ""

        # 2. Download audio bytes
        audio_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        audio_resp = requests.get(audio_url, timeout=30)
        if not audio_resp.ok:
            return ""
        audio_bytes = audio_resp.content

        # 3. Normalize mime type — Telegram appends '; codecs=opus' which Gemini rejects
        clean_mime = _normalize_mime(mime_type)
        print(f"[TelegramBridge] Transcribing audio: mime={mime_type!r} → {clean_mime!r}, size={len(audio_bytes)}B")

        # 4. Transcribe via Gemini
        from core import gemini
        from google.genai import types
        part = types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime)
        prompt = (
            "Transcribe this voice message accurately into text. "
            "Output ONLY the exact spoken words in the speaker's language (Arabic or English), "
            "with no extra commentary, quotation marks, or explanations."
        )
        result = gemini.text([part, prompt], tier=gemini.FAST, timeout_ms=30000)
        return (result or "").strip()
    except Exception as e:
        print(f"[TelegramBridge] Audio transcription failed: {e}")
        return ""


def _get_status_text() -> str:
    jarvis = get_jarvis()
    if jarvis:
        awake_status = "🟢 مستيقظ (Awake)" if getattr(jarvis, "_awake", False) else "😴 نائم (Sleeping)"
        session_status = "🟢 متصل بـ Gemini Live" if getattr(jarvis, "session", None) else "🟡 جاري الاتصال بالخادم..."
    else:
        awake_status = "⚪ جاري تشغيل النظام"
        session_status = "⚪ انتظر بدء الجلسة"

    cpu = "N/A"
    ram = "N/A"
    if _PSUTIL_OK:
        try:
            cpu = f"{psutil.cpu_percent(interval=0.2):.1f}%"
            ram = f"{psutil.virtual_memory().percent:.1f}%"
        except Exception:
            pass
            
    flows_count = 0
    try:
        from core.flow_engine import FlowEngine
        flows = [f for f in FlowEngine.get_instance()._flows.values() if f.running]
        flows_count = len(flows)
    except Exception:
        pass
        
    return (
        f"📊 حالة JARVIS MK:\n"
        f"• حالة اليقظة: {awake_status}\n"
        f"• اتصال الذكاء: {session_status}\n"
        f"• المعالج CPU: {cpu}\n"
        f"• الذاكرة RAM: {ram}\n"
        f"• أتمتة الـ Flows النشطة: {flows_count}"
    )


def _refresh_sleep_timer():
    """Reset Jarvis's auto-sleep countdown on every incoming Telegram message.

    The sleep watchdog in main.py fires when (now - _last_user_speech) exceeds
    the configured timeout. Any Telegram activity — a text command, a voice
    note, even just /status — counts as user presence and should restart the
    window so Jarvis doesn't doze off in the middle of a remote session.
    """
    jarvis = get_jarvis()
    if jarvis and hasattr(jarvis, "_last_user_speech"):
        jarvis._last_user_speech = time.monotonic()


def _handle_message(token: str, chat_id: int | str, text: str):
    text_clean = text.strip()
    text_lower = text_clean.lower()
    jarvis = get_jarvis()
    player = get_player()

    # Reset auto-sleep timer on every incoming Telegram message
    _refresh_sleep_timer()

    # ── Wake commands ────────────────────────────────────────────────────────
    wake_triggers = (
        "/wake", "/start", "اصحى", "اصحي", "صحى", "صحي", "صحصح", "قوم", "فوق", "استيقظ",
        "wake up", "wake", "جارفيس", "يا جارفيس", "hey jarvis"
    )
    is_wake = (
        text_lower in wake_triggers
        or any(text_lower == w for w in wake_triggers)
        or any(text_lower.startswith(w + " ") for w in wake_triggers)
    )
    if is_wake:
        if jarvis:
            jarvis.wake(reason="أمر تيليجرام (Telegram Wake)")
            _send_message(token, chat_id, "🟢 صباح الخير يا AMOX! أنا صحيت ومعاك وجاهز لأي حاجة تطلبها.")
        elif player and hasattr(player, "on_text_command") and callable(player.on_text_command):
            player.on_text_command("جارفيس")
            _send_message(token, chat_id, "🟢 تم إرسال إشارة الإيقاظ إلى JARVIS — أنا معك الآن.")
        else:
            _send_message(token, chat_id, "🟢 مرحباً بك! JARVIS استيقظ ويستمع إليك الآن.")
        return

    # ── Sleep commands ───────────────────────────────────────────────────────
    sleep_triggers = ("/sleep", "نام", "ادخل وضع النوم", "sleep", "انام")
    if text_lower in sleep_triggers:
        if jarvis:
            jarvis.sleep(reason="أمر تيليجرام (Telegram Sleep)")
            _send_message(token, chat_id, "😴 تم وضع JARVIS في وضع السكون. يمكنك إيقاظه في أي وقت بكتابة /wake أو 'اصحى'.")
        else:
            _send_message(token, chat_id, "😴 تم تحويل الأمر للنوم.")
        return

    # ── Screenshot ───────────────────────────────────────────────────────────
    screenshot_triggers = (
        "/screenshot", "/screen", "/screanshot", "screanshot", "screenshot",
        "صورة الشاشة", "شاشة", "سكرين", "سكرين شوت", "خد سكرين", "خد سكرين شوت",
        "صور الشاشة", "صور الشاشه", "لقطة الشاشة", "لقطه الشاشه", "سكرينشوت"
    )
    is_screenshot = (
        text_lower in screenshot_triggers or
        any(k in text_lower for k in ("screenshot", "screanshot", "سكرين شوت", "صورة الشاشة", "لقطة الشاشة", "خد سكرين", "صور الشاشه", "صور الشاشة"))
    )
    if is_screenshot:
        try:
            from actions.screen_processor import _capture_screen
            res = _capture_screen()
            img_bytes = res[0] if isinstance(res, (tuple, list)) else res
            if img_bytes:
                _send_photo(token, chat_id, img_bytes, caption="📸 لقطة شاشة كمبيوتر JARVIS")
                return
        except Exception as e:
            _send_message(token, chat_id, f"❌ فشل التقاط الشاشة: {e}")
            return
        _send_message(token, chat_id, "❌ تعذر التقاط الشاشة حالياً.")
        return

    # ── Status ───────────────────────────────────────────────────────────────
    if text_lower in ("/status", "/حالة", "الحالة", "status"):
        _send_message(token, chat_id, _get_status_text())
        return

    # ── Help ─────────────────────────────────────────────────────────────────
    if text_lower in ("/help", "/مساعدة", "مساعدة", "أوامر"):
        _send_message(
            token, chat_id,
            "📋 أوامر التحكم عن بُعد لـ JARVIS:\n\n"
            "🟢 /wake أو 'اصحى' — إيقاظ جارفيس\n"
            "😴 /sleep أو 'نام' — وضع جارفيس في وضع السكون\n"
            "📸 /screenshot أو 'شاشة' — التقاط صورة شاشة الكمبيوتر فوراً\n"
            "📊 /status أو 'الحالة' — تقرير حالة النظام والاتصال\n"
            "⏹ /stop_bridge — إيقاف جسر التيليجرام مؤقتاً\n\n"
            "💬 أي رسالة أخرى تُرسل مباشرة لجارفيس ويقوم بإيقاظ نفسه وتنفيذها والرد عليك!"
        )
        return

    # ── Stop Bridge ──────────────────────────────────────────────────────────
    if text_lower in ("/stop_bridge",):
        _send_message(token, chat_id, "🔴 جاري إيقاف جسر التيليجرام...")
        _state.bridge_running = False
        return

    # ── Automation Flow check ────────────────────────────────────────────────
    try:
        from core.flow_engine import FlowEngine
        engine = FlowEngine.get_instance()
        match = engine.check_chat_command(text_clean)
        if match:
            cmd_type, flow_id, flow = match
            res = engine.start_flow(flow_id, player=player) if cmd_type == "start" else engine.stop_flow(flow_id, player=player)
            _send_message(token, chat_id, f"🔀 [Flow Engine]\n{res}")
            return
    except Exception as e:
        print(f"[TelegramBridge] Flow command check error: {e}")

    # ── General Text Command Forwarding ───────────────────────────────────────
    # Replies are no longer awaited here: the forwarder thread mirrors every
    # JARVIS reply to this chat while the remote session is active, so long
    # tool runs and multi-turn answers all reach Telegram instead of being
    # cut off after a fixed timeout.
    _mark_remote(chat_id)
    _send_chat_action(token, chat_id, "typing")

    executed = False

    # Auto-wake JARVIS when a remote command arrives
    if jarvis:
        if not getattr(jarvis, "_awake", True):
            jarvis.wake(reason="أمر تيليجرام مباشر (Auto-wake)")
        # ── Keep-awake: reset the sleep timer on EVERY Telegram command ──────
        # The sleep watchdog fires when (now - _last_user_speech) > timeout.
        # wake() sets _last_user_speech once, but by the time the command
        # reaches Gemini and a reply comes back, several seconds have passed.
        # If the user was already awake (wake() is a no-op), _last_user_speech
        # was never touched — so a command that arrives just before the timeout
        # would put Jarvis back to sleep the instant it finishes speaking.
        # Refreshing here guarantees the full sleep window from the command.
        if hasattr(jarvis, "_last_user_speech"):
            jarvis._last_user_speech = time.monotonic()
        # If session is connecting, wait up to 3s for session to be ready
        if not getattr(jarvis, "session", None):
            for _ in range(15):
                time.sleep(0.2)
                if getattr(jarvis, "session", None):
                    break
        if hasattr(jarvis, "_on_text_command"):
            jarvis._on_text_command(text_clean)
            executed = True
    elif player:
        cmd_fn = getattr(player, "on_text_command", None)
        if callable(cmd_fn):
            cmd_fn(text_clean)
            executed = True

    if not executed:
        _send_message(token, chat_id, "⚠️ جار الاتصال بـ JARVIS... يرجى إعادة إرسال الأمر بعد ثوانٍ.")
        return


# ── Remote chat session / reply mirroring ─────────────────────────────────────

def _remote_window_s() -> float:
    try:
        return max(30.0, float(_load_config().get("mirror_window_min", 15)) * 60.0)
    except Exception:
        return 900.0


def _mark_remote(chat_id):
    _state.active_chat_id = chat_id
    _state.last_remote_at = time.monotonic()


def remote_active() -> bool:
    """True while the user is chatting with JARVIS from Telegram."""
    if _state.active_chat_id is None:
        return False
    if _load_config().get("mirror_all", False):
        return True
    return (time.monotonic() - _state.last_remote_at) < _remote_window_s()


def _send_chat_action(token: str, chat_id, action: str = "typing"):
    if not _REQUESTS_OK:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendChatAction",
                      json={"chat_id": chat_id, "action": action}, timeout=5)
    except Exception:
        pass


def _owner_chat_id():
    if _state.active_chat_id is not None:
        return _state.active_chat_id
    ids = list(_state.allowed_ids or _load_config().get("allowed_user_ids", []))
    return ids[0] if ids else None


def send_to_owner(text: str) -> str:
    """Send a message to the owner via the bot (works even if the bridge is stopped)."""
    text = (text or "").strip()
    if not text:
        return "⚠️ No message text given."
    token = _state.token or _resolve_token(_load_config())
    chat_id = _owner_chat_id()
    if not token:
        return "⚠️ Telegram bot token is missing — set it in Settings → TELEGRAM."
    if chat_id is None:
        return "⚠️ No Telegram user ID configured (allowed_user_ids in config/telegram.json)."
    # Telegram caps a message at 4096 chars.
    for i in range(0, len(text), 4000):
        _send_message(token, chat_id, text[i:i + 4000])
    return "✅ Sent to the user on Telegram."


def _forwarder_loop():
    """Mirror JARVIS replies to the active Telegram chat while a remote session is on."""
    while _state.bridge_running:
        try:
            r = _state.response_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        parts = [r.strip()] if r and r.strip() else []
        # Gather follow-up sentences that belong to the same answer.
        grace = time.monotonic() + 1.2
        while time.monotonic() < grace:
            try:
                extra = _state.response_queue.get(timeout=0.3)
                if extra and extra.strip():
                    parts.append(extra.strip())
                    grace = time.monotonic() + 1.0
            except queue.Empty:
                pass
        if not parts or not remote_active():
            continue
        token, chat_id = _state.token, _state.active_chat_id
        if token and chat_id is not None:
            send_text = "🤖 JARVIS:\n" + "\n".join(parts)
            for i in range(0, len(send_text), 4000):
                _send_message(token, chat_id, send_text[i:i + 4000])


def _process_update(token: str, chat_id, msg: dict):
    """Handle one incoming message (text or voice) — runs on the worker thread."""
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Support Voice Notes & Audio Records
    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if not text and voice:
        file_id = voice.get("file_id")
        mime = voice.get("mime_type", "audio/ogg" if not msg.get("video_note") else "video/mp4")
        _mark_remote(chat_id)
        _send_chat_action(token, chat_id, "typing")
        transcribed = _transcribe_audio(token, file_id, mime)
        if transcribed:
            _send_message(token, chat_id, f"🎙️ «{transcribed}»")
            text = transcribed
        else:
            _send_message(token, chat_id, "⚠️ تعذر فهم الرسالة الصوتية أو تفريغها، حاول مجدداً أو اكتب الأمر.")
            return

    if text:
        _handle_message(token, chat_id, text)


def _worker_loop():
    """Process incoming messages in order without blocking Telegram polling."""
    while _state.bridge_running:
        try:
            token, chat_id, msg = _state.inbox.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _process_update(token, chat_id, msg)
        except Exception as e:
            print(f"[TelegramBridge] Worker error: {e}")
            try:
                _send_message(token, chat_id, f"❌ خطأ أثناء تنفيذ الأمر: {e}")
            except Exception:
                pass


def _bridge_loop(token: str, allowed_ids: set, polling_timeout: int):
    offset = 0
    base_url = f"https://api.telegram.org/bot{token}"

    print(f"[TelegramBridge] 🟢 Bridge polling loop started for user IDs: {allowed_ids}")

    # Notify owner on startup
    for uid in allowed_ids:
        _send_message(token, uid, "🟢 JARVIS Telegram Bridge متصل ومستعد للأوامر.\nاكتب /help أو 'مساعدة' لرؤية الأوامر.")

    while _state.bridge_running:
        if not _REQUESTS_OK:
            time.sleep(5)
            continue

        try:
            resp = requests.get(
                f"{base_url}/getUpdates",
                params={"offset": offset, "timeout": polling_timeout, "allowed_updates": ["message"]},
                timeout=polling_timeout + 5
            )
            if not resp.ok:
                # If 409 conflict happens (another instance), back off longer
                if resp.status_code == 409:
                    print("[TelegramBridge] ⚠️ 409 Conflict: another polling instance detected. Backing off 10s...")
                    time.sleep(10)
                else:
                    time.sleep(5)
                continue
            data = resp.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message", {})
                user_id = msg.get("from", {}).get("id")
                chat_id = msg.get("chat", {}).get("id")

                # SECURITY: Whitelist enforcement
                if user_id not in allowed_ids:
                    continue
                if not chat_id:
                    continue

                # Hand off to the worker so a slow transcription or command
                # never stalls polling (voice + text handled in _process_update).
                _state.inbox.put_nowait((token, chat_id, msg))

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            time.sleep(8)
        except Exception as e:
            print(f"[TelegramBridge] Loop exception: {e}")
            time.sleep(4)

    # Notify owner on shutdown
    try:
        for uid in allowed_ids:
            _send_message(token, uid, "🔴 JARVIS Telegram Bridge أُوقف مؤقتاً.")
    except Exception:
        pass


def _resolve_token(cfg: dict) -> str:
    token = ""
    try:
        from memory.config_manager import get_plugin_setting
        token = get_plugin_setting("telegram_bridge", "bot_token", "") or ""
    except Exception:
        pass
    return token or cfg.get("bot_token", "")


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "status")

    if player:
        set_player(player)

    _cfg = _load_config()
    if not _cfg.get("enabled", True):
        return "Telegram bridge is disabled in config."

    token = _resolve_token(_cfg)

    allowed_ids = set(_cfg.get("allowed_user_ids", []))
    polling_timeout = max(10, int(_cfg.get("polling_interval", 30)))
    _state.token = token
    _state.allowed_ids = allowed_ids

    if action == "send":
        return send_to_owner(parameters.get("message", ""))

    if action == "send_status":
        return send_to_owner(_get_status_text())

    if action == "start":
        with _state.bridge_lock:
            if not token:
                return "⚠️ Bot Token غير متوفر. يرجى إدخاله في الإعدادات."
            if _state.bridge_running and _state.bridge_thread and _state.bridge_thread.is_alive():
                return "✅ Telegram bridge is already running."
            _state.bridge_running = True
            _state.bridge_thread = threading.Thread(
                target=_bridge_loop,
                args=(token, allowed_ids, polling_timeout),
                daemon=True,
                name="TelegramBridge"
            )
            _state.bridge_thread.start()
            _state.forwarder_thread = threading.Thread(
                target=_forwarder_loop, daemon=True, name="TelegramForwarder")
            _state.forwarder_thread.start()
            _state.worker_thread = threading.Thread(
                target=_worker_loop, daemon=True, name="TelegramWorker")
            _state.worker_thread.start()
            p = get_player()
            if p and hasattr(p, "write_log"):
                p.write_log("[Telegram] 🟢 Bridge started — listening for messages...")
            return "🟢 Telegram Bridge شغال وجاهز لاستقبال الأوامر!"

    elif action == "stop":
        with _state.bridge_lock:
            if not _state.bridge_running:
                return "⚠️ Telegram bridge is not running."
            _state.bridge_running = False
            p = get_player()
            if p and hasattr(p, "write_log"):
                p.write_log("[Telegram] 🔴 Bridge stopped.")
            return "🔴 Telegram Bridge أُوقف بنجاح."

    elif action == "status":
        with _state.bridge_lock:
            alive = _state.bridge_running and _state.bridge_thread and _state.bridge_thread.is_alive()
            return "🟢 Running" if alive else "🔴 Stopped"

    return "Unknown action."


def _auto_start():
    if _state.auto_start_done:
        return
    _state.auto_start_done = True
    cfg = _load_config()
    if cfg.get("auto_start", False) and cfg.get("bot_token", "") and cfg.get("enabled", True):
        threading.Thread(target=lambda: run({"action": "start"}), daemon=True, name="TelegramBridgeAutoStart").start()

_auto_start()
