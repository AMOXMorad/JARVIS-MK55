"""
actions/close_app.py — Process termination & application closing for JARVIS Mark LIV.

Discovered automatically by core.action_loader.
Closes running applications by name (e.g. 'Telegram', 'Chrome', 'Discord', 'Spotify')
using psutil and Windows taskkill. Protects JARVIS itself from accidental closure.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
from typing import Any

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_OS = platform.system()

# Known app names mapped to likely executable/process names (lowercase)
_PROCESS_MAP: dict[str, list[str]] = {
    "telegram": ["telegram.exe", "telegram"],
    "تليجرام": ["telegram.exe", "telegram"],
    "تلجرام": ["telegram.exe", "telegram"],
    "chrome": ["chrome.exe", "chrome"],
    "كروم": ["chrome.exe", "chrome"],
    "google chrome": ["chrome.exe", "chrome"],
    "firefox": ["firefox.exe", "firefox"],
    "فايرفوكس": ["firefox.exe", "firefox"],
    "edge": ["msedge.exe", "msedge"],
    "brave": ["brave.exe", "brave"],
    "discord": ["discord.exe", "discord"],
    "ديسكورد": ["discord.exe", "discord"],
    "whatsapp": ["whatsapp.exe", "whatsapp"],
    "واتساب": ["whatsapp.exe", "whatsapp"],
    "واتس": ["whatsapp.exe", "whatsapp"],
    "spotify": ["spotify.exe", "spotify"],
    "سبوتيفاي": ["spotify.exe", "spotify"],
    "vlc": ["vlc.exe", "vlc"],
    "steam": ["steam.exe", "steam"],
    "ستيم": ["steam.exe", "steam"],
    "notepad": ["notepad.exe", "notepad"],
    "calculator": ["calc.exe", "calculatorapp.exe", "calculator.exe"],
    "calc": ["calc.exe", "calculatorapp.exe"],
    "code": ["code.exe", "code"],
    "vscode": ["code.exe", "code"],
    "visual studio code": ["code.exe", "code"],
    "antigravity": ["antigravity.exe", "antigravity"],
    "انتي جرافيتي": ["antigravity.exe", "antigravity"],
    "انتيجرافيتي": ["antigravity.exe", "antigravity"],
    "blender": ["blender.exe", "blender"],
    "بلندر": ["blender.exe", "blender"],
}

_PROTECTED_NAMES = {"jarvis", "jarvis_ai", "jarvis mk", "mark liv", "mark 54", "main.py", "pythonw.exe"}


def _normalize_name(name: str) -> str:
    n = name.strip().lower()
    n = n.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("-", " ")
    for prefix in ("تطبيق ", "برنامج ", "ابلكيشن ", "برمجيه ", "app ", "application "):
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
    return n


def close_app(parameters: dict[str, Any], player=None) -> str:
    """Closes or terminates a running application by name."""
    raw_name = str(parameters.get("app_name", "")).strip()
    if not raw_name:
        return "لم يتم تحديد اسم التطبيق المطلوب إغلاقه."

    norm = _normalize_name(raw_name)

    # Prevent accidental closure of JARVIS itself
    if any(p in norm for p in ["jarvis", "جارفيس", "نفسك", "المساعد", "yourself"]):
        return "لن أقوم بإغلاق جارفيس تلقائياً عبر هذا الأمر لحماية الجلسة. إذا أردت إغلاق جارفيس تماماً يمكنك إغلاق النافذة يدوياً."

    if player:
        try:
            player.write_log(f"[close_app] Request to close: '{raw_name}'")
        except Exception:
            pass

    targets = _PROCESS_MAP.get(norm, [])
    if not targets:
        # Check individual words in norm
        for word in norm.split():
            clean_word = word.strip()
            if clean_word in _PROCESS_MAP:
                targets = _PROCESS_MAP[clean_word]
                break

    if not targets:
        # Also check open_app aliases if available
        try:
            from actions.open_app import _APP_ALIASES
            lookup_key = norm
            if lookup_key in _APP_ALIASES:
                target_exe = _APP_ALIASES[lookup_key].get(_OS, "")
            else:
                for word in norm.split():
                    if word in _APP_ALIASES:
                        target_exe = _APP_ALIASES[word].get(_OS, "")
                        break
                else:
                    target_exe = ""
            if target_exe:
                base_exe = target_exe.lower()
                if not base_exe.endswith(".exe") and _OS == "Windows":
                    targets = [base_exe + ".exe", base_exe]
                else:
                    targets = [base_exe]
        except Exception:
            pass

    if not targets:
        # Fallback to direct name and name.exe
        base = norm.replace(" ", "")
        targets = [base, base + ".exe", raw_name.lower(), raw_name.lower() + ".exe"]

    closed_pids: list[int] = []
    closed_names: set[str] = set()

    if _PSUTIL:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                # Do not kill current python process
                if proc.pid == os.getpid():
                    continue

                matched = False
                for tgt in targets:
                    tgt_clean = tgt.lower()
                    if pname == tgt_clean or pname.startswith(tgt_clean.replace(".exe", "")):
                        matched = True
                        break

                if matched:
                    # Double check not killing jarvis/python running our app
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    if "jarvis" in cmdline and "python" in pname:
                        continue

                    proc.terminate()
                    closed_pids.append(proc.pid)
                    closed_names.add(pname)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if closed_pids:
            # Wait briefly for graceful termination
            gone, alive = psutil.wait_procs([psutil.Process(pid) for pid in closed_pids if psutil.pid_exists(pid)], timeout=1.5)
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass

    # Fallback to taskkill on Windows if nothing was matched or process still lingering
    if _OS == "Windows" and not closed_pids:
        for tgt in targets:
            exe_target = tgt if tgt.endswith(".exe") else f"{tgt}.exe"
            try:
                res = subprocess.run(
                    ["taskkill", "/IM", exe_target, "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=3
                )
                if res.returncode == 0:
                    closed_names.add(exe_target)
            except Exception:
                pass

    if closed_names or closed_pids:
        name_str = ", ".join(closed_names) if closed_names else raw_name
        return f"تم إغلاق تطبيق {raw_name} بنجاح ({name_str})."
    else:
        return f"لم يتم العثور على أي نافذة أو عملية نشطة لتطبيق '{raw_name}' على الجهاز."


TOOL = {
    "name": "close_app",
    "description": (
        "Closes or terminates a running desktop application by name (e.g. 'Telegram', 'Chrome', 'Discord', 'Spotify', 'Notepad'). "
        "Always call this tool whenever the user asks to close, terminate, or quit an application on the PC (e.g. 'اقفل تلجرام', 'اقفل كروم', 'close Discord'). "
        "DO NOT use telegram_bridge to close Telegram — telegram_bridge is only for the phone remote bot. "
        "DO NOT use this to close JARVIS itself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Name of the desktop application to close (e.g. 'Telegram', 'Chrome', 'Discord', 'Spotify')"
            }
        },
        "required": ["app_name"]
    },
    "handler": close_app,
}
