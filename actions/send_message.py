import json
import subprocess
import sys
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
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE    = 0.06
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_os() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _paste_text(text: str) -> None:
    _require_pyautogui()

    os_name = _get_os()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
    else:
        pyautogui.write(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _paste_text(text)

def _open_app(app_name: str) -> bool:
    os_name = _get_os()

    try:
        if os_name == "windows":
            try:
                from actions.open_app import _launch_windows
                return _launch_windows(app_name)
            except Exception as e:
                print(f"[SendMessage] _launch_windows failed: {e}")
                return False

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else: 
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0) 
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False

def _search_in_app(query: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)

def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _open_app(app_name):
        return f"Could not open {app_name}."

    time.sleep(1.0)
    _search_in_app(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via {app_name}."

def _send_whatsapp(receiver: str, message: str) -> str:
    return _desktop_send("WhatsApp", receiver, message)

_SELF_RECEIVERS = {
    "bot", "the bot", "my bot", "jarvis bot", "telegram bot", "me", "myself", "owner",
    "البوت", "بوت", "بوت التليجرام", "بوت تليجرام", "بوت التلجرام", "البوت بتاعي",
    "انا", "أنا", "ليا", "لي", "نفسي", "جارفيس", "jarvis",
}

def _send_telegram(receiver: str, message: str) -> str:
    # "Send it to the bot / to me" means the owner's chat with JARVIS's own bot —
    # use the Bot API, which needs no desktop app at all.
    if (receiver or "").strip().lower() in _SELF_RECEIVERS and message:
        try:
            from plugins.telegram_bridge import send_to_owner
            return send_to_owner(message)
        except Exception as e:
            print(f"[SendMessage] telegram bot send failed: {e}")
    try:
        from plugins.telegram_control import visual_chat, search_and_open, run as run_telegram
        if receiver and message:
            return visual_chat(target=receiver, message=message, read_chat=True)
        elif receiver:
            return search_and_open(target=receiver, message=message)
        elif message:
            return run_telegram({"action": "send_message", "message": message})
        else:
            return run_telegram({"action": "open"})
    except Exception as e:
        print(f"[SendMessage] telegram_control failed, falling back to desktop automation: {e}")

    _require_pyautogui()
    from actions.open_app import _launch_windows
    if _get_os() == "windows":
        _launch_windows("Telegram")
    else:
        _open_app("Telegram")
    time.sleep(1.0)
    _search_in_app(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)
    if message:
        _paste_text(message)
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(0.3)
        return f"Message sent to {receiver} via Telegram."
    return f"Opened chat with {receiver} via Telegram."

def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    try:
        from plugins.discord_control import search_and_open, run as run_discord
        if receiver:
            return search_and_open(target=receiver, message=message)
        elif message:
            return run_discord({"action": "send_message", "message": message})
        else:
            return run_discord({"action": "open"})
    except Exception as e:
        print(f"[SendMessage] discord_control failed, falling back to desktop automation: {e}")

    _require_pyautogui()

    from actions.open_app import _launch_windows
    if _get_os() == "windows":
        _launch_windows("Discord")
    else:
        _open_app("Discord")
    time.sleep(1.0)

    # Discord Quick Switcher hotkey: Ctrl + K (Cmd + K on macOS)
    os_name = _get_os()
    switcher_hotkey = ("command", "k") if os_name == "mac" else ("ctrl", "k")
    pyautogui.hotkey(*switcher_hotkey)
    time.sleep(0.4)

    # Paste channel/user/chat name (safe for Arabic & any keyboard layout)
    _clear_and_paste(receiver)
    time.sleep(0.5)

    # Press Enter to navigate to that channel
    pyautogui.press("enter")
    time.sleep(0.6)

    # Focus the message input field by clicking at calculated coordinates
    try:
        from plugins.discord_control import _focus_message_box
        _focus_message_box()
    except Exception:
        pass

    # Type and send the message in the channel's input box if message provided
    if message:
        time.sleep(0.2)
        try:
            from plugins.discord_control import _type_and_send
            _type_and_send(message)
        except Exception:
            _paste_text(message)
            time.sleep(0.2)
            pyautogui.press("enter")
        time.sleep(0.3)
        return f"Message sent to {receiver} on Discord."

    return f"Opened chat {receiver} on Discord."


def _send_instagram(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return "Could not open Instagram in browser."

    _paste_text(receiver)
    time.sleep(1.5)

    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")   
    time.sleep(0.4)

    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.messenger.com/"):
        return "Could not open Messenger in browser."


    _search_in_app(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Messenger."

_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              _send_whatsapp),
    ({"telegram", "tg"},                      _send_telegram),
    ({"instagram", "ig", "insta"},            _send_instagram),
    ({"signal"},                               _send_signal),
    ({"discord"},                              _send_discord),
    ({"messenger", "facebook", "fb"},         _send_messenger),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip()

    if not receiver:
        return "Please specify a recipient or channel name."
    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed — cannot control the desktop."

    preview = (message_text[:50] + "…") if len(message_text) > 50 else (message_text or "(open chat)")
    print(f"[SendMessage] 📨 {platform} -> {receiver}: {preview}")
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")

    try:
        handler = _resolve_platform(platform)
        result  = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not open chat or send message: {e}"

    print(f"[SendMessage] {'✅' if 'sent' in result.lower() or 'opened' in result.lower() else '❌'} {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "send_message",
    "description": "Sends a text message or opens a specific chat/channel via WhatsApp, Telegram, Discord, or other messaging platform.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "receiver": {
                "type": "STRING",
                "description": "Recipient contact name or channel name (e.g. 'lite' for Discord)"
            },
            "message_text": {
                "type": "STRING",
                "description": "The message to send. Leave empty if the user only wants to open/navigate to the chat."
            },
            "platform": {
                "type": "STRING",
                "description": "Platform: Discord, WhatsApp, Telegram, etc."
            }
        },
        "required": [
            "receiver",
            "platform"
        ]
    },
    "handler": send_message,
}
