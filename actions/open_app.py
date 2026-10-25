import time
import subprocess
import platform
import shutil

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "antigravity":        {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "anti-gravity":       {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "anti gravity":       {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "انتي جرافيتي":       {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "انتيجرافيتي":        {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "انتي-جرافيتي":       {"Windows": "Antigravity",             "Darwin": "Antigravity",          "Linux": "antigravity"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}

import re


def _clean_str(s: str) -> str:
    """Normalize string by removing spaces, hyphens, and non-alphanumeric chars."""
    if not s:
        return ""
    return re.sub(r"[^a-zA-Z0-9\u0600-\u06FF]", "", s.lower())


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    clean_key = _clean_str(raw)
    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)
        clean_alias = _clean_str(alias_key)
        if clean_key and (clean_key == clean_alias or clean_key in clean_alias or clean_alias in clean_key):
            return os_map.get(_SYSTEM, raw)

    return raw  

def _launch_windows(app_name: str) -> bool:
    import os
    from pathlib import Path

    # Ensure thread is on Default desktop for window enumeration
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hDesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hDesk:
            user32.SetThreadDesktop(hDesk)
    except Exception:
        pass

    clean_target = _clean_str(app_name)
    normalized = _normalize(app_name)
    clean_norm = _clean_str(normalized)
    norm = app_name.lower().replace(" ", "")

    # For Discord, use dedicated discord_control plugin
    if "discord" in norm or "discord" in clean_norm:
        try:
            from plugins.discord_control import _focus_discord
            if _focus_discord():
                return True
        except Exception:
            pass

    # For Telegram, use dedicated telegram_control plugin
    if "telegram" in norm or "telegram" in clean_norm or "تلجرام" in norm or "تليجرام" in norm:
        try:
            from plugins.telegram_control import _focus_telegram
            if _focus_telegram():
                return True
        except Exception:
            pass

    # 1. Bring existing window to front if app is already running
    try:
        import win32gui, win32con, win32process
        matching_pids = set()
        if _PSUTIL:
            for p in psutil.process_iter(["pid", "name"]):
                pname = (p.info.get("name") or "").lower()
                pclean = _clean_str(pname)
                if (clean_target and (clean_target in pclean or pclean in clean_target)) or \
                   (clean_norm and (clean_norm in pclean or pclean in clean_norm)):
                    matching_pids.add(p.info["pid"])

        found_hwnd = None
        def _enum_win(hwnd, _):
            nonlocal found_hwnd
            try:
                vis = win32gui.IsWindowVisible(hwnd)
                iconic = win32gui.IsIconic(hwnd)
                if not (vis or iconic):
                    return True
                title = win32gui.GetWindowText(hwnd).strip()
                t_clean = _clean_str(title)
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]

                # Match by title OR by matching process PID with top-level dimensions (w > 200, h > 200)
                matched_title = bool(t_clean and ((clean_target and clean_target in t_clean) or (clean_norm and clean_norm in t_clean)))
                matched_pid = bool(pid in matching_pids and ((w > 200 and h > 200) or iconic))
                if matched_title or matched_pid:
                    found_hwnd = hwnd
                    return False
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum_win, None)
        except Exception:
            pass

        if found_hwnd:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            cur_thread = kernel32.GetCurrentThreadId()
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
            win32gui.ShowWindow(found_hwnd, win32con.SW_MAXIMIZE)
            win32gui.SetForegroundWindow(found_hwnd)
            user32.BringWindowToTop(found_hwnd)
            if attached:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
            time.sleep(0.2)
            try:
                import pyautogui
                pyautogui.press("escape")
            except Exception:
                pass
            time.sleep(0.2)
            return True
    except Exception as e:
        print(f"[open_app] Window activation error: {e}")

    # 2. Known Windows app paths and protocols
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    app_data = Path(os.environ.get("APPDATA", ""))

    if "discord" in norm:
        disc_dir = local_app_data / "Discord"
        if disc_dir.exists():
            update_exe = disc_dir / "Update.exe"
            if update_exe.exists():
                try:
                    subprocess.Popen(f'"{update_exe}" --processStart Discord.exe', shell=True)
                    time.sleep(1.5)
                    return True
                except Exception:
                    pass
            app_dirs = sorted(disc_dir.glob("app-*"), reverse=True)
            for ad in app_dirs:
                disc_exe = ad / "Discord.exe"
                if disc_exe.exists():
                    try:
                        os.startfile(str(disc_exe))
                        time.sleep(1.5)
                        return True
                    except Exception:
                        pass
        try:
            os.startfile("discord:")
            time.sleep(1.5)
            return True
        except Exception:
            pass

    if "telegram" in norm:
        tg_exe = app_data / "Telegram Desktop" / "Telegram.exe"
        if tg_exe.exists():
            try:
                os.startfile(str(tg_exe))
                time.sleep(1.5)
                return True
            except Exception:
                pass

    if "spotify" in norm:
        sp_exe = app_data / "Spotify" / "Spotify.exe"
        if sp_exe.exists():
            try:
                os.startfile(str(sp_exe))
                time.sleep(1.5)
                return True
            except Exception:
                pass
        try:
            os.startfile("spotify:")
            time.sleep(1.5)
            return True
        except Exception:
            pass

    # 3. Direct check in known Programs directories (LocalAppData, Program Files)
    prog_dirs = [
        local_app_data / "Programs",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
    ]
    for p_dir in prog_dirs:
        if not p_dir.exists():
            continue
        try:
            for sub in p_dir.iterdir():
                s_clean = _clean_str(sub.name)
                # Matches directory or standalone executable
                if sub.is_dir():
                    if (clean_target and (clean_target in s_clean or s_clean in clean_target)) or \
                       (clean_norm and (clean_norm in s_clean or s_clean in clean_norm)):
                        for exe in sub.glob("*.exe"):
                            exe_clean = _clean_str(exe.stem)
                            if "uninstall" not in exe_clean and ((clean_target and clean_target in exe_clean) or (clean_norm and clean_norm in exe_clean)):
                                try:
                                    os.startfile(str(exe))
                                    time.sleep(1.5)
                                    return True
                                except Exception:
                                    pass
                elif sub.suffix.lower() == ".exe":
                    if (clean_target and clean_target in s_clean) or (clean_norm and clean_norm in s_clean):
                        try:
                            os.startfile(str(sub))
                            time.sleep(1.5)
                            return True
                        except Exception:
                            pass
        except Exception:
            pass

    # 4. Check Start Menu and Desktop shortcuts (.lnk files)
    shortcut_dirs = [
        app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("ALLUSERSPROFILE", "C:/ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path("C:/Users/Public/Desktop"),
    ]
    for s_dir in shortcut_dirs:
        if not s_dir.exists():
            continue
        try:
            for lnk in s_dir.rglob("*.lnk"):
                s_clean = _clean_str(lnk.stem)
                if (clean_target and (clean_target == s_clean or clean_target in s_clean or s_clean in clean_target)) or \
                   (clean_norm and (clean_norm == s_clean or clean_norm in s_clean or s_clean in clean_norm)):
                    try:
                        os.startfile(str(lnk))
                        time.sleep(1.5)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

    # 5. Direct binary check in PATH
    for candidate in [app_name, normalized, app_name.split(".")[0], normalized.split(".")[0]]:
        if shutil.which(candidate):
            try:
                subprocess.Popen(
                    candidate,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(1.5)
                return True
            except Exception as e:
                print(f"[open_app] subprocess failed: {e}")

    # 6. Protocol URL or existing file
    if ":" in app_name or Path(app_name).exists():
        try:
            os.startfile(app_name)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    # 7. Windows Search (Win + S) with robust typing and enter
    try:
        import pyautogui
        from core.keyboard_utils import type_text_via_clipboard
        pyautogui.PAUSE = 0.05
        # Win + S directly opens Windows Search and focuses the input box on Windows 10 & 11
        pyautogui.hotkey("win", "s")
        time.sleep(0.5)
        # Type the app name via clipboard (supports Arabic, English, symbols)
        typed = False
        try:
            type_text_via_clipboard(app_name)
            typed = True
        except Exception:
            pass
        if not typed:
            try:
                import pyperclip
                pyperclip.copy(app_name)
                pyautogui.hotkey("ctrl", "v")
                typed = True
            except Exception:
                pass
        if not typed:
            pyautogui.write(app_name, interval=0.03)

        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(2.0)
        return True
    except Exception as e:
        print(f"[open_app] Windows search failed: {e}")

    # 8. Visual Grounding Fallback: locate app icon on desktop or taskbar
    try:
        from actions.screen_grounding import locate_ui_element, _smooth_click
        res = locate_ui_element(f"{app_name} icon on desktop or taskbar")
        if res:
            _smooth_click(res["cx"], res["cy"], clicks=2)
            time.sleep(2.0)
            return True
    except Exception:
        pass

    return False


def _launch_macos(app_name: str) -> bool:

    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]

def _launch_linux(app_name: str) -> bool:

    # terminal emulators: try common ones in order
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if shutil.which(term):
                try:
                    subprocess.Popen([term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    return True
                except Exception:
                    continue

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def _interact_with_app(app_name: str, instruction: str) -> str:
    """Open an app and use screen_grounding to inspect it and execute the instruction."""
    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    normalized = _normalize(app_name)
    if launcher:
        launcher(normalized)
    time.sleep(2.0)

    try:
        from actions.screen_grounding import screen_grounding_handler
        return screen_grounding_handler({
            "action": "read_and_decide",
            "question": f"التطبيق المفتوح هو {app_name}. المطلوب تنفيذه: '{instruction}'. اقرأ الشاشة بدقة واتخذ القرار المناسب (نقر أو تصفح أو قراءة) ونفذ المطلوب وقدم الإجابة بالعربية."
        })
    except Exception as e:
        return f"تم فتح {app_name} ولكن تعذر التفاعل البصري: {e}"


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    app_name = params.get("app_name", "").strip()
    target = params.get("target", "").strip()
    message = params.get("message", "").strip()
    instruction = params.get("instruction", "").strip()

    if not app_name:
        return "No application name provided."

    # Check custom shortcuts database first
    try:
        from plugins.quick_shortcuts import find_shortcut, run as run_shortcut
        match = find_shortcut(app_name)
        if match:
            return run_shortcut({"action": "open", "keyword": match[0]}, player=player, session_memory=session_memory)
    except Exception:
        pass

    # If Chrome, and target or instruction has profile/bookmark info, delegate to browser_control
    norm = app_name.lower().replace(" ", "")
    if "chrome" in norm or "chrome" in _normalize(app_name).lower():
        p_name = target or None
        bmark = None
        url = None
        if instruction:
            inst_lower = instruction.lower()
            if "bookmark" in inst_lower or "بوك مارك" in inst_lower:
                bmark = instruction
            elif "http://" in instruction or "https://" in instruction:
                url = instruction
            elif not p_name and ("محمد" in instruction or "mohamed" in inst_lower):
                p_name = instruction
        if p_name or bmark or url:
            try:
                from actions.browser_control import browser_control
                return browser_control(
                    {
                        "action": "open_with_profile",
                        "browser": "chrome",
                        "profile_name": p_name,
                        "bookmark": bmark,
                        "url": url,
                    },
                    player=player,
                    session_memory=session_memory,
                )
            except Exception as e:
                print(f"[open_app] chrome browser_control delegation failed: {e}")

    # If instruction is provided, open app and interact via vision
    if instruction:
        if player:
            player.write_log(f"[open_app] {app_name} -> {instruction}")
        return _interact_with_app(app_name, instruction)

    # If Discord, delegate directly to plugins.discord_control for unified navigation
    norm = app_name.lower().replace(" ", "")
    if "discord" in norm or "discord" in _normalize(app_name).lower():
        try:
            from plugins import discord_control
            action = "search" if target else ("send_message" if message else "open")
            return discord_control.run(
                {"action": action, "target": target, "message": message},
                player=player,
                session_memory=session_memory,
            )
        except Exception as e:
            print(f"[open_app] discord_control delegation failed: {e}")

    # If Telegram, delegate directly to plugins.telegram_control for unified navigation
    if "telegram" in norm or "telegram" in _normalize(app_name).lower() or "تلجرام" in norm or "تليجرام" in norm:
        try:
            from plugins import telegram_control
            action = "search" if target else ("send_message" if message else "open")
            return telegram_control.run(
                {"action": action, "target": target, "message": message},
                player=player,
                session_memory=session_memory,
            )
        except Exception as e:
            print(f"[open_app] telegram_control delegation failed: {e}")

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' -> '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        if launcher(normalized):
            return f"Opened {app_name}."
        if normalized.lower() != app_name.lower():
            if launcher(app_name):
                return f"Opened {app_name}."
        return (
            f"Could not confirm that {app_name} launched. "
            f"It may still be loading, or it might not be installed."
        )
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": (
        "Opens any application on the computer, brings its window to focus, and optionally "
        "interacts with it using vision (instruction), switches to or searches for a specific person, channel, chat, contact, or server (e.g. 'lite' in Discord), "
        "and optionally sends a message to them. "
        "Always call this tool when the user asks to open an app, interact with an app, navigate to someone, or send a message in an app."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Exact name of the application (e.g. 'Discord', 'WhatsApp', 'Chrome', 'Spotify')"
            },
            "instruction": {
                "type": "STRING",
                "description": "Optional Arabic/English task instruction to interact with the app using Vision after opening it (e.g. 'شغل الأغنية', 'ابحث عن ملف')"
            },
            "target": {
                "type": "STRING",
                "description": "Optional person, channel, chat, contact, room, or server name to search and open inside the app (e.g. 'lite' in Discord)"
            },
            "message": {
                "type": "STRING",
                "description": "Optional message content to send to the target person/channel once opened"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}


