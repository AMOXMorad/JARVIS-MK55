
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import platform
import shutil
import subprocess
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)
_OS = platform.system()   # "Windows" | "Darwin" | "Linux"

# The port JARVIS and the browser agree on for Chrome DevTools Protocol.
# A Chromium browser started with --remote-debugging-port=<this> can be driven
# in place — its real profile, logins and extensions — instead of having to
# launch a second, empty instance next to it.
_CDP_PORT = 9222

def _normalize_url(url: str) -> str:
    """
    Bare words like "instagram" → "https://instagram.com"
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs pass through unchanged.
    """
    url = url.strip()
    if not url:
        return "about:blank"
    if "://" in url:
        return url
    # No dot at all → assume .com  (e.g. "instagram" → "instagram.com")
    if "." not in url:
        url = url + ".com"
    return "https://" + url


def _user_agent() -> str:
    if _OS == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    if _OS == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def _real_profile_dir(browser: str) -> str:
    home  = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    roam  = os.environ.get("APPDATA", "")

    candidates: list[Path] = []

    if _OS == "Windows":
        m = {
            "chrome":   [Path(local) / "Google"          / "Chrome"          / "User Data"],
            "edge":     [Path(local) / "Microsoft"        / "Edge"            / "User Data"],
            "brave":    [Path(local) / "BraveSoftware"    / "Brave-Browser"   / "User Data"],
            "vivaldi":  [Path(local) / "Vivaldi"          / "User Data"],
            "opera":    [Path(roam)  / "Opera Software"   / "Opera Stable",
                         Path(local) / "Opera Software"   / "Opera Stable"],
            "operagx":  [Path(roam)  / "Opera Software"   / "Opera GX Stable",
                         Path(local) / "Opera Software"   / "Opera GX Stable"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        m = {
            "chrome":   [lib / "Google"             / "Chrome"],
            "edge":     [lib / "Microsoft Edge"],
            "brave":    [lib / "BraveSoftware"       / "Brave-Browser"],
            "vivaldi":  [lib / "Vivaldi"],
            "opera":    [lib / "com.operasoftware.Opera"],
            "operagx":  [lib / "com.operasoftware.OperaGX"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Linux":
        cfg = home / ".config"
        m = {
            "chrome":   [cfg / "google-chrome", cfg / "chromium"],
            "edge":     [cfg / "microsoft-edge"],
            "brave":    [cfg / "BraveSoftware" / "Brave-Browser"],
            "vivaldi":  [cfg / "vivaldi"],
            "opera":    [cfg / "opera"],
            "operagx":  [cfg / "opera-gx"],
        }
        candidates = m.get(browser, [])

    for p in candidates:
        if p.exists():
            print(f"[Browser] ✅ Real profile found for {browser}: {p}")
            return str(p)

    fallback = home / ".jarvis_profiles" / browser
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[Browser] ⚠️  Real profile not found for {browser}, using: {fallback}")
    return str(fallback)

def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()

    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"

    ini = base / "profiles.ini"
    if not ini.exists():
        return None

    current: dict[str, str] = {}
    default_path: Optional[str] = None

    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("["):
            p = current.get("Path", "")
            if p and current.get("Default") == "1":
                is_rel = current.get("IsRelative", "1") == "1"
                default_path = str(base / p) if is_rel else p
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()

    p = current.get("Path", "")
    if p and current.get("Default") == "1":
        is_rel = current.get("IsRelative", "1") == "1"
        default_path = str(base / p) if is_rel else p

    if default_path and Path(default_path).exists():
        print(f"[Browser] Firefox real profile: {default_path}")
        return default_path
    return None

def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Opens the user's REAL browser normally — with their own profile,
    logged-in accounts and extensions. No automation attaches, so an
    about:blank tab or a blank profile NEVER shows up.
    If url is empty the browser starts with no URL (its own start page /
    session restore) — exactly as if the user had opened it themselves.
    Works on all three of Windows / macOS / Linux.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # No URL → only a window will open; needs the default browser's exe
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return f"Opened in {name}: {url}" if url else f"Opened {name}."
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({e}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            try:
                subprocess.Popen(
                    [exe, url] if url else [exe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return f"Opened in {name}: {url}" if url else f"Opened {name}."
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return "Could not find a browser to open."

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return f"Opened in your default browser: {url}"
    except Exception:
        try:
            if webbrowser.open(url):
                return f"Opened in your default browser: {url}"
        except Exception:
            pass
        return f"Could not open a browser for: {url}"


def _cdp_probe(port: int = _CDP_PORT, timeout: float = 2.0) -> bool:
    """Is a Chromium browser exposing its DevTools endpoint on this port?"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _browser_process_running(exe: str | None) -> bool:
    """Is a browser matching this executable already running?"""
    try:
        import psutil
        target = Path(exe).name.lower() if exe else ""
        if not target:
            return False
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == target:
                return True
    except Exception:
        pass
    return False



def _resolve_chrome_profile(query: str | None = None) -> tuple[str, str]:
    """
    Resolve Chrome profile directory name and human display name from User Data/Local State.
    Returns (profile_dir_name, display_name), e.g. ('Profile 1', 'محمد مراد').
    """
    import json
    local_state_path = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State")
    if not os.path.exists(local_state_path):
        return ("Default", "Default")
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ("Default", "Default")

    profiles = data.get("profile", {}).get("info_cache", {})
    if not profiles:
        return ("Default", "Default")

    if not query:
        last = data.get("profile", {}).get("last_used")
        if last and last in profiles:
            return (last, profiles[last].get("gaia_name") or profiles[last].get("name") or last)
        first = next(iter(profiles.keys()))
        return (first, profiles[first].get("gaia_name") or profiles[first].get("name") or first)

    q = str(query).strip().lower()

    # Work / Business profile keywords -> Profile 2 ("Mohamed Work")
    work_keywords = ("work", "شغل", "العمل", "عمل", "بيزنس", "business", "prifix", "بريفكس", "وظيفة")
    if any(k in q for k in work_keywords):
        for k, v in profiles.items():
            hay = f"{v.get('name', '')} {v.get('shortcut_name', '')} {v.get('user_name', '')}".lower()
            if "work" in hay:
                return (k, v.get("gaia_name") or v.get("name") or k)
        if "Profile 2" in profiles:
            return ("Profile 2", profiles["Profile 2"].get("gaia_name") or "Mohamed Morad (Work)")

    # Personal profile keywords -> Profile 1 ("محمد Personal")
    personal_keywords = ("personal", "شخصي", "الشخصي", "خاص", "الخاص")
    if any(k in q for k in personal_keywords):
        for k, v in profiles.items():
            hay = f"{v.get('name', '')} {v.get('shortcut_name', '')} {v.get('user_name', '')}".lower()
            if "personal" in hay:
                return (k, v.get("gaia_name") or v.get("name") or k)
        if "Profile 1" in profiles:
            return ("Profile 1", profiles["Profile 1"].get("gaia_name") or "محمد مراد (Personal)")

    # Specialized avatar & language intent keywords:
    # Profile 2 = Mohamed Morad (English, Lloyd Frontera avatar)
    # Profile 1 = محمد مراد (Arabic, Monogram / Logo avatar)
    lloyd_keywords = ("لويد", "فرونتيرا", "فورنتيرا", "lloyd", "frontera", "anime", "انمي", "estate", "developer")
    if any(k in q for k in lloyd_keywords):
        if "Profile 2" in profiles:
            return ("Profile 2", profiles["Profile 2"].get("gaia_name") or "Mohamed Morad (Lloyd Frontera)")

    logo_keywords = ("شعار", "لوجو", "logo", "monogram", "mm")
    if any(k in q for k in logo_keywords):
        if "Profile 1" in profiles:
            return ("Profile 1", profiles["Profile 1"].get("gaia_name") or "محمد مراد (Logo)")

    if "عربي" in q or "arabic" in q:
        if "Profile 1" in profiles:
            return ("Profile 1", profiles["Profile 1"].get("gaia_name") or "محمد مراد")
    if "انجليزي" in q or "إنجليزي" in q or "english" in q:
        if "Profile 2" in profiles:
            return ("Profile 2", profiles["Profile 2"].get("gaia_name") or "Mohamed Morad")

    # 1. Exact match on directory key ('Profile 1', 'Profile 2', 'Default')
    for k, v in profiles.items():
        if k.lower() == q:
            return (k, v.get("gaia_name") or v.get("name") or k)

    # 2. Check gaia_name, name, user_name exact
    for k, v in profiles.items():
        gname = (v.get("gaia_name") or "").strip().lower()
        name = (v.get("name") or "").strip().lower()
        uname = (v.get("user_name") or "").strip().lower()
        if q == gname or q == name or q == uname:
            return (k, v.get("gaia_name") or v.get("name") or k)

    # 3. Substring match
    for k, v in profiles.items():
        gname = (v.get("gaia_name") or "").strip().lower()
        name = (v.get("name") or "").strip().lower()
        uname = (v.get("user_name") or "").strip().lower()
        if q in gname or q in name or q in uname or gname in q or name in q:
            return (k, v.get("gaia_name") or v.get("name") or k)

    # 4. Token overlap match (e.g. "حساب محمد مراد" -> matches "محمد مراد")
    q_tokens = [t for t in q.split() if len(t) > 2 and t not in ("حساب", "بروفايل", "profile", "user")]
    for k, v in profiles.items():
        haystack = f"{v.get('gaia_name', '')} {v.get('name', '')} {v.get('user_name', '')}".lower()
        if any(t in haystack for t in q_tokens):
            return (k, v.get("gaia_name") or v.get("name") or k)

    return ("Default", query)


_ARABIC_BOOKMARK_KEYWORDS = {
    "شوبيفاي": "shopify",
    "شوبفاي": "shopify",
    "اوردرات": "orders",
    "أوردرات": "orders",
    "طلبات": "orders",
    "شغل": "work",
    "العمل": "work",
    "يوتيوب": "youtube",
    "فيس": "facebook",
    "فيسبوك": "facebook",
    "جوجل": "google",
    "بلوجر": "blogger",
    "تويتر": "twitter",
    "انستغرام": "instagram",
    "انستا": "instagram",
    "ديسكورد": "discord",
    "واتس": "whatsapp",
    "واتساب": "whatsapp",
    "شات": "chatgpt",
    "جيميل": "gmail",
    "بريفكس": "prifix",
}


def _lookup_chrome_bookmark(profile_dir: str, query: str) -> tuple[str, str] | None:
    """
    Search bookmarks in User Data/{profile_dir}/Bookmarks and AccountBookmarks.
    Matches against bookmark name, url, and folder hierarchy.
    Returns (url, title) or None.
    """
    import json
    candidate_files = [
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\User Data\{profile_dir}\AccountBookmarks"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\User Data\{profile_dir}\Bookmarks"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\User Data\{profile_dir}\AccountBookmarks.bak"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\User Data\{profile_dir}\Bookmarks.bak"),
    ]
    existing_files = [p for p in candidate_files if os.path.exists(p)]
    if not existing_files:
        # Fallback: search any Bookmarks/AccountBookmarks under all profiles
        user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        if os.path.exists(user_data):
            try:
                for sub in os.listdir(user_data):
                    for fname in ("AccountBookmarks", "Bookmarks"):
                        fp = os.path.join(user_data, sub, fname)
                        if os.path.exists(fp) and fp not in existing_files:
                            existing_files.append(fp)
            except Exception:
                pass

    if not existing_files:
        return None

    q_raw = query.strip().lower()
    words = q_raw.split()
    tokens = []
    stopwords = {"من", "في", "على", "الي", "إلى", "عايز", "افتح", "موقع", "صفحة", "رابط", "bookmark", "tab", "bar", "بوكمارك"}
    for w in words:
        if w not in stopwords:
            tokens.append(w)
            if w in _ARABIC_BOOKMARK_KEYWORDS:
                tokens.append(_ARABIC_BOOKMARK_KEYWORDS[w])

    matches = []

    def search_node(node, path=""):
        if node.get("type") == "url":
            name = node.get("name", "")
            url = node.get("url", "")
            haystack = (path + "/" + name + " " + url).lower()
            if any(t in haystack for t in tokens) or q_raw in haystack:
                score = 0
                if q_raw in name.lower():
                    score += 100
                elif q_raw in url.lower():
                    score += 50
                for t in tokens:
                    if t in name.lower():
                        score += 40
                    elif t in url.lower():
                        score += 30
                    elif t in path.lower():
                        score += 20
                matches.append((score, url, name))
        elif "children" in node:
            for c in node.get("children", []):
                search_node(c, path + "/" + str(node.get("name", "")))

    for bm_path in existing_files:
        try:
            with open(bm_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            roots = data.get("roots", {})
            for r_name, r_node in roots.items():
                search_node(r_node, r_name)
        except Exception:
            continue

    if matches:
        matches.sort(key=lambda x: x[0], reverse=True)
        return (matches[0][1], matches[0][2])
    return None


def _launch_chrome_with_profile(
    profile_query: str | None = None,
    url: str | None = None,
    bookmark: str | None = None,
) -> str:
    """Launch Chrome directly into the specified user profile, optionally navigating to a bookmark or URL.
    Bypasses the 'Who's using Chrome?' Profile Picker completely using --profile-directory.
    """
    import time as _time

    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    chrome_exe = next((p for p in chrome_paths if os.path.exists(p)), None)
    if not chrome_exe:
        return "❌ Chrome not found on this system"

    profile_dir, display_name = _resolve_chrome_profile(profile_query)
    user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

    # If bookmark is requested or url looks like a bookmark search
    target_url = None
    target_title = None
    if bookmark:
        bm = _lookup_chrome_bookmark(profile_dir, bookmark)
        if bm:
            target_url, target_title = bm[0], bm[1]
    elif url and not url.startswith("http://") and not url.startswith("https://") and ("bookmark" in url.lower() or "tab" in url.lower() or " " in url or "شوبيفاي" in url or "shopify" in url or "orders" in url or "work" in url):
        bm = _lookup_chrome_bookmark(profile_dir, url)
        if bm:
            target_url, target_title = bm[0], bm[1]

    if not target_url and url:
        target_url = _normalize_url(url)

    cmd = [
        chrome_exe,
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile_dir}",
        f"--remote-debugging-port={_CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ]
    if target_url:
        cmd.append(target_url)

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return f"❌ Could not launch Chrome: {e}"

    _time.sleep(1.5)

    # Bring Chrome window to front if running on Windows and ensure MAXIMIZED
    try:
        import win32gui, win32con
        def _enum_chrome(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                cls = win32gui.GetClassName(hwnd)
                if "chrome" in title.lower() or "chrome" in cls.lower():
                    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                    win32gui.SetForegroundWindow(hwnd)
                    return False
            return True
        win32gui.EnumWindows(_enum_chrome, None)
    except Exception:
        pass

    msg = f"✅ Chrome launched with profile '{display_name}' ({profile_dir})"
    if target_url:
        msg += f" -> Opened bookmark '{target_title or target_url}': {target_url}"
    return msg


class _BrowserSession:
    """
    A full session for one browser instance.

    Chromium browsers are driven in place over the DevTools port: JARVIS
    attaches to the browser the user is already running (or starts it on its
    real profile if it is closed), so there is one browser window, with the
    user's logins and extensions — never a second empty instance that trips
    bot detection. Firefox and WebKit keep the old launch_persistent_context
    path, since CDP is Chromium-only.
    """

    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None

        # The CDP connection, when we are attached rather than launched.
        self._cdp_browser: Browser | None = None
        # True when the browser belongs to the *user* (was already running with
        # the debug port). We may disconnect from it; we may never close it.
        self._attached_external = False
        # True when *we* started the browser. Closing it afterwards is fine.
        self._self_launched = False

    def is_controllable(self) -> bool:
        """Is this session actually driving a browser right now?
        A session can exist in the registry while its launch failed — those
        are not a valid target for navigation, and callers should fall back
        to opening the page natively instead."""
        return self._context is not None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> str:
        """Close the browser, or just disconnect if it isn't ours.
        Returns a one-line description of what happened."""
        if not self._loop:
            return f"No active session for: {self.browser_name}"
        was_external = self._attached_external
        try:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)
        except Exception as e:
            print(f"[Browser] close failed for {self.browser_name}: {e}")
            return f"Could not close {self.browser_name}: {e}"
        if was_external:
            return (f"Disconnected from {self.browser_name} — it is still open "
                    f"(your browser, your call).")
        return f"{self.browser_name} closed."

    async def _async_close(self):
        if self._attached_external:
            # The browser is the user's, not ours. Closing a CDP connection does
            # not quit the browser — we drop the handle and let _pw.stop() tear
            # down the websocket. browser.close() is deliberately NOT called:
            # over CDP it would quit the user's whole browser.
            print(f"[Browser] Disconnected from {self.browser_name} "
                  f"(left running — it is the user's browser)")
        elif self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = self._cdp_browser = None
        self._attached_external = False
        self._self_launched = False

    async def _adopt_page(self) -> Page:
        """Pick the page navigation should happen in.

        For a real browser we attached to over CDP — or one we started
        ourselves on the debug port — the open pages are the user's own.
        context.pages is in creation order, so the last entry is the newest
        tab, the one the user is most likely looking at, and that is where
        navigation belongs. A context with no pages at all gets a fresh tab.

        Firefox and WebKit still use launch_persistent_context, which opens a
        starter tab; there we adopt that one (pages[0]) after letting it
        settle, so no extra blank tab ever appears."""
        pages = self._context.pages
        if not pages:
            return await self._context.new_page()
        if self._attached_external or self._self_launched:
            return pages[-1]
        await asyncio.sleep(0.3)   # let the starter tab settle
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    async def _attach_cdp(self) -> bool:
        """Connect to a browser already exposing its DevTools endpoint.

        Attaches to the real profile, the real logins and the real extensions —
        no second window, no bot-detection page. Returns True on success."""
        if not _cdp_probe():
            return False
        try:
            browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{_CDP_PORT}"
            )
        except Exception as e:
            print(f"[Browser] CDP endpoint answered but connect failed: {e}")
            return False
        contexts = browser.contexts
        if not contexts:
            print("[Browser] CDP browser has no browsing contexts")
            return False
        # Prefer a context that already has open pages — that is the window the
        # user is actually looking at, rather than an empty incognito one.
        self._context = next((c for c in contexts if c.pages), contexts[0])
        self._cdp_browser = browser
        self._page = await self._adopt_page()
        return True

    async def _launch_with_cdp(self, exe: str, profile: str, label: str) -> bool:
        """Start the real browser with remote debugging enabled, then attach.

        Launched with none of the automation flags (--disable-default-apps,
        --disable-blink-features=AutomationControlled, …) that used to make
        Google serve a CAPTCHA: the profile loads exactly as it does when the
        user starts the browser themselves."""
        if not exe:
            print(f"[Browser] No executable resolved for {label}")
            return False
        args = [exe, f"--remote-debugging-port={_CDP_PORT}"]
        if profile:
            args.append(f"--user-data-dir={profile}")
            if "chrome" in self.browser_name.lower():
                p_dir, _ = _resolve_chrome_profile()
                args.append(f"--profile-directory={p_dir}")
        try:
            subprocess.Popen(args)
        except Exception as e:
            print(f"[Browser] Could not start {label}: {e}")
            return False
        for _ in range(30):                    # up to ~15 s for a cold start
            await asyncio.sleep(0.5)
            if await self._attach_cdp():
                print(f"[Browser] ✅ Started [{label}] with CDP on "
                      f"port {_CDP_PORT} (real profile)")
                return True
        print(f"[Browser] {label} started, but its debug port never answered.")
        return False

    async def _launch(self):
        """
        Launches the browser with the real user profile.
        Does nothing if the context is already open.
        """
        if self._context is not None:
            return

        if self._spec is None:
            raise RuntimeError(
                f"'{self.browser_name}' bu platformda ({_OS}) desteklenmiyor."
            )

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)

        if engine_name == "firefox":
            profile = _firefox_profile_dir() or str(
                Path.home() / ".jarvis_profiles" / "firefox"
            )
            kwargs: dict = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            if exe:
                kwargs["executable_path"] = exe
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            except Exception as e:
                print(f"[Browser] Firefox real profile failed ({e}), using JARVIS profile")
                jarvis = str(Path.home() / ".jarvis_profiles" / "firefox_jarvis")
                Path(jarvis).mkdir(parents=True, exist_ok=True)
                self._context = await engine_obj.launch_persistent_context(jarvis, **kwargs)

            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Firefox launched")
            return

        if engine_name == "webkit":
            safari_profile = str(Path.home() / ".jarvis_profiles" / "safari")
            Path(safari_profile).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            self._context = await engine_obj.launch_persistent_context(safari_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Safari launched")
            return

        profile = _real_profile_dir(self.browser_name)

        label = (
            f"{self.browser_name}"
            + (f"/{channel}" if channel else "")
            + (f" @ {exe}" if exe else "")
        )

        # ── Chromium: drive the user's real browser over CDP ─────────────────
        # launch_persistent_context on the real profile fails with "profile is
        # already in use by another instance of Chromium" whenever the browser
        # is already open, and the old fallback — a second window on an empty
        # profile — was landing on Google's bot-detection page. Attaching over
        # the DevTools port instead means one browser, the real profile, the
        # real logins, the real extensions.
        if await self._attach_cdp():
            self._attached_external = True
            print(f"[Browser] ✅ Attached to running [{label}] "
                  f"via CDP port {_CDP_PORT} (user's browser)")
            return

        # No debug port answering. If the browser is already running without it,
        # we cannot attach — and must not open a second empty-profile window
        # next to it. Say so, in words the user can act on.
        if _browser_process_running(exe):
            raise RuntimeError(
                f"{self.browser_name} is already open but is not accepting "
                f"remote-control connections, so I can't click or type in it. "
                f"Close and reopen {self.browser_name}, then try again — or "
                f"just ask me to open the page and I'll open it in your "
                f"browser as a new tab."
            )

        # Browser not running at all: start it on the real profile with the
        # debug port enabled, then attach. We own this launch, so closing the
        # session later is closing our own browser.
        self._self_launched = True
        if await self._launch_with_cdp(exe, profile, label):
            return

        raise RuntimeError(
            f"Could not start or attach to {self.browser_name}. "
            f"Is it installed?"
        )

    async def _get_page(self) -> Page:
        await self._launch()
        if self._page is None or self._page.is_closed():
            # For a browser we're driving in place, prefer one of the user's
            # real open tabs over popping a fresh blank one — the tab they just
            # closed isn't coming back, but the browser still has work in it.
            if (self._attached_external or self._self_launched) and self._context.pages:
                self._page = self._context.pages[-1]
            else:
                self._page = await self._context.new_page()
                await asyncio.sleep(0.2)
        return self._page

    async def go_to(self, url: str) -> str:

        url      = _normalize_url(url)
        page     = await self._get_page()
        prev_url = page.url

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                pass   # page may have partially loaded — check URL below
            except Exception as e:
                print(f"[Browser] goto exception (non-fatal): {e}")
            return p.url

        result_url = await _do_goto(page)

        if result_url in ("about:blank", "", None, prev_url) and prev_url in ("about:blank", "", None):
            print(f"[Browser] Still blank after goto — retrying on new tab: {url}")
            try:
                new_page   = await self._context.new_page()
                self._page = new_page
                result_url = await _do_goto(new_page)
            except Exception as e:
                print(f"[Browser] New-tab retry failed: {e}")

        if result_url and result_url not in ("about:blank", "", None):
            return f"Opened: {result_url}"
        return f"Could not open: {url}"

    async def search(self, query: str, engine: str = "google") -> str:
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8_000)
                return f"Clicked text: '{text}'"
            if selector:
                await page.click(selector, timeout=8_000)
                return f"Clicked selector: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found (timeout)."
        except Exception as e:
            return f"Click error: {e}"

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            el = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await el.clear()
            await el.type(text, delay=50)
            return "Text typed."
        except Exception as e:
            return f"Type error: {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4_000]
        except Exception as e:
            return f"Could not get page text: {e}"

    async def get_url(self) -> str:
        page = await self._get_page()
        return page.url

    async def fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    return f"Clicked ({role}): '{description}'"
            except Exception:
                pass
        for attempt in (
            lambda: page.get_by_text(description, exact=False).first.click(timeout=5_000),
            lambda: page.get_by_placeholder(description, exact=False).first.click(timeout=5_000),
            lambda: page.locator(
                f'[alt*="{description}" i],[title*="{description}" i],'
                f'[aria-label*="{description}" i]'
            ).first.click(timeout=5_000),
        ):
            try:
                await attempt()
                return f"Clicked: '{description}'"
            except Exception:
                pass
        return f"Could not find element: '{description}'"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
        ]
        for method, loc in candidates:
            try:
                el = loc.first
                if await el.count() == 0:
                    continue
                await el.clear()
                await el.type(text, delay=50)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue
        return f"Could not find input: '{description}'"

    async def new_tab(self, url: str = "") -> str:
        page = await self._get_page()
        ctx  = page.context
        new  = await ctx.new_page()
        self._page = new
        if url:
            return await self.go_to(url)
        return "New tab opened."

    async def close_tab(self) -> str:
        page = self._page
        if not (page and not page.is_closed()):
            return "No active tab to close."
        ctx = page.context
        # Closing the last tab closes the whole browser — never do that to a
        # browser that belongs to the user.
        if self._attached_external and len(ctx.pages) <= 1:
            return ("That is the last open tab, and this is your browser — "
                    "I won't close the whole thing. Close it yourself if "
                    "you're done with it.")
        await page.close()
        pages = ctx.pages
        self._page = pages[-1] if pages else None
        return "Tab closed."

    async def screenshot(self, path: str = None) -> str:
        page = await self._get_page()
        try:
            save_path = path or str(Path.home() / "Desktop" / "jarvis_screenshot.png")
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    async def close_browser(self) -> str:
        was_external = self._attached_external
        await self._async_close()
        if was_external:
            return (f"Disconnected from {self.browser_name} — it is still open. "
                    f"It is your browser, so I leave closing it to you.")
        return f"{self.browser_name} closed."

    async def analyze_page(self, url: str, question: str = "") -> str:
        """Navigate to url, extract content, screenshot, then answer question in Arabic via Gemini.

        Returns the Gemini answer (in Arabic) or a plain content summary if Gemini
        is unavailable. Screenshot is saved to Desktop.
        """
        # 1. Navigate
        nav_result = await self.go_to(url)
        page       = await self._get_page()
        await asyncio.sleep(1)   # let JS settle

        # 2. Extract title + text
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body_text = await page.inner_text("body")
            body_text = body_text[:3000]
        except Exception:
            body_text = ""

        # 3. Screenshot
        screenshot_path = str(Path.home() / "Desktop" / "jarvis_analyze.png")
        try:
            await page.screenshot(path=screenshot_path, full_page=False)
        except Exception:
            screenshot_path = ""

        # 4. Build prompt for Gemini
        q = question.strip() or "ما هو موضوع هذه الصفحة؟"
        prompt = (
            f"أجب على السؤال التالي بالعربية بناءً على محتوى الصفحة:\n\n"
            f"السؤال: {q}\n\n"
            f"عنوان الصفحة: {title}\n\n"
            f"محتوى الصفحة (أول 3000 حرف):\n{body_text}"
        )

        # 5. Try calling Gemini (model injected via session_memory or direct import)
        try:
            import google.generativeai as genai  # type: ignore
            # Expect the API key to already be configured by the time this runs
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(prompt)
            answer = (response.text or "").strip()
        except Exception as e:
            print(f"[Browser] Gemini call failed in analyze_page: {e}")
            answer = f"[Gemini غير متاح] عنوان الصفحة: {title}\n\nمحتوى: {body_text[:500]}…"

        return (
            f"🔍 تحليل الصفحة: {nav_result}\n"
            f"📸 لقطة الشاشة: {screenshot_path or 'فشلت'}\n\n"
            f"💬 الإجابة:\n{answer}"
        )


class _SessionRegistry:
    """Manages all active browser sessions."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""

    def has(self, browser_name: str | None = None) -> bool:
        """Is there an active automation session for this browser (or any)?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def usable(self, browser_name: str | None = None) -> Optional[_BrowserSession]:
        """A session that is really driving a browser right now, or None.

        A session can sit in the registry with no browser behind it — the
        launch failed, or the browser was closed since. Navigation must not be
        routed into one of those: it should open the page natively instead.
        Interactive actions still go through get(), so a broken session
        surfaces its error instead of silently doing nothing."""
        with self._lock:
            if not browser_name:
                candidates = list(self._sessions.values())
            else:
                name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
                candidates = [self._sessions[name]] if name in self._sessions else []
        return next((s for s in candidates if s.is_controllable()), None)

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Returns the last natively-opened URL once (consumed to avoid repeats)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}")
            return self._sessions[browser_name]

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _detect_default_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            msg = sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return msg
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        if not sessions:
            return "No active browser sessions."
        lines = []
        for name, s in zip(names, sessions):
            try:
                lines.append(f"  • {name}: {s.close()}")
            except Exception as e:
                lines.append(f"  • {name}: could not close ({e})")
        return "Browser sessions:\n" + "\n".join(lines)

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "No active browser sessions."
            lines = []
            for name in self._sessions:
                marker = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}{marker}")
            return "Open browsers:\n" + "\n".join(lines)


_registry = _SessionRegistry()

def browser_control(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = "Unknown action."

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        result = _registry.close_one(target) if target else "No browser specified."
        _log(player, result)
        return result

    if action in ("open_with_profile", "open", "launch", "start", "profile") or params.get("profile_name") or params.get("profile") or params.get("bookmark"):
        p_query = params.get("profile_name") or params.get("profile") or params.get("profile_dir")
        bmark = params.get("bookmark")
        u = params.get("url")
        result = _launch_chrome_with_profile(p_query, url=u, bookmark=bmark)
        inspect_goal = params.get("question") or params.get("instruction")
        if inspect_goal:
            try:
                from actions.computer_control import _maximize_window
                _maximize_window("chrome")
            except Exception:
                pass
            time.sleep(2.0)
            try:
                from actions.screen_grounding import _autonomous_read_and_decide
                decision_res = _autonomous_read_and_decide(inspect_goal, player=player)
                result = f"{result}\n\n{decision_res}"
            except Exception as e:
                print(f"[Browser] Autonomous inspection error: {e}")
        _log(player, result)
        return result

    # ── Navigation prefers an existing controllable browser ──────────────────
    # go_to / search / new_tab open the site in the user's own browser — their
    # own profile, logged-in accounts and start page; exactly as if they had
    # opened it themselves. If a real CDP session is already attached, the page
    # goes there instead (so multi-step tasks stay in one window). A session
    # that exists but isn't driving anything is skipped — native open beats a
    # broken window every time.
    if action in ("go_to", "search", "new_tab"):
        if params.get("profile_name") or params.get("profile") or params.get("bookmark"):
            p_query = params.get("profile_name") or params.get("profile")
            bmark = params.get("bookmark")
            u = params.get("url")
            result = _launch_chrome_with_profile(p_query, url=u, bookmark=bmark)
            inspect_goal = params.get("question") or params.get("instruction")
            if inspect_goal:
                try:
                    from actions.computer_control import _maximize_window
                    _maximize_window("chrome")
                except Exception:
                    pass
                time.sleep(2.0)
                try:
                    from actions.screen_grounding import _autonomous_read_and_decide
                    decision_res = _autonomous_read_and_decide(inspect_goal, player=player)
                    result = f"{result}\n\n{decision_res}"
                except Exception as e:
                    print(f"[Browser] Autonomous inspection error: {e}")
            _log(player, result)
            return result

        sess = _registry.usable(browser)
        if sess is not None:
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                result = f"Browser error ({action}): {e}"
            _log(player, result)
            return result

        if action == "search":
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()

        result = _open_native(nav_url, browser)
        if result.startswith("Opened") and nav_url:
            _registry.note_native_url(_normalize_url(nav_url))

        inspect_goal = params.get("question") or params.get("instruction")
        if inspect_goal:
            try:
                from actions.computer_control import _maximize_window
                _maximize_window("chrome")
            except Exception:
                pass
            time.sleep(2.0)
            try:
                from actions.goal_agent import run_goal_agent
                agent_res = run_goal_agent(goal=inspect_goal, target_app_or_site=params.get("query") or params.get("bookmark") or "", player=player)
                result = f"{result}\n\n{agent_res}"
            except Exception as e:
                print(f"[Browser] Goal agent error: {e}")

        _log(player, result)
        return result

    # ── Interactive actions (click/type/read…) ───────────────────────────────
    # These require a physically controllable browser; the automation window
    # only opens here, and as soon as it opens it goes to the user's last
    # navigated page — it doesn't sit on a blank page.
    try:
        sess = _registry.get(browser)
    except Exception as e:
        target_text = params.get("text") or params.get("description")
        if target_text and action in ("click", "smart_click"):
            try:
                from actions.computer_control import _find_and_click
                print(f"[Browser] CDP unavailable, falling back to visual click on '{target_text}'...")
                res = _find_and_click(target_text)
                _log(player, f"Visual click: {res}")
                return f"Visual click: {res}"
            except Exception as ex:
                print(f"[Browser] Visual click failed: {ex}")
        elif action in ("type", "smart_type") and (params.get("text") or params.get("description")):
            try:
                from actions.computer_control import _smart_type, _type
                if params.get("description"):
                    res = _smart_type(params.get("text", ""), clear_first=params.get("clear_first", True))
                else:
                    res = _type(params.get("text", ""))
                _log(player, f"Visual type: {res}")
                return f"Visual type: {res}"
            except Exception as ex:
                pass
        result = f"Could not start browser session: {e}"
        _log(player, result)
        return result

    try:
        last = _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                print(f"[Browser] Could not resume last page ({last}): {e}")

        if action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url())
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "close_tab":
            result = sess.run(sess.close_tab())
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        elif action == "analyze_page":
            result = sess.run(
                sess.analyze_page(
                    params.get("url", ""),
                    params.get("question", ""),
                ),
                timeout=90,
            )
        else:
            result = f"Unknown browser action: '{action}'"


    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        target_text = params.get("text") or params.get("description")
        if target_text and action in ("click", "smart_click"):
            try:
                from actions.computer_control import _find_and_click
                print(f"[Browser] CDP action failed ({e}), falling back to visual click on '{target_text}'...")
                res = _find_and_click(target_text)
                _log(player, f"Visual click: {res}")
                return f"Visual click: {res}"
            except Exception as ex:
                print(f"[Browser] Visual click failed: {ex}")
        result = f"Browser error ({action}): {e}"

    _log(player, result)
    return result


def _log(player, text: str):
    short = str(text)[:80]
    print(f"[Browser] {short}")
    if player:
        player.write_log(f"[browser] {short[:60]}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "browser_control",
    "description": "Controls any web browser. Use for: opening websites, searching the web, clicking elements, filling forms, scrolling, screenshots, navigation, analyzing page content, and any web-based task. Simple open/search requests open the user's own browser normally (their real profile and logged-in accounts); interactive actions (click, type, fill_form...) attach to the browser the user is already running over its DevTools port — the same window, the same profile, the same logged-in accounts — so nothing opens a second, empty browser window. If the browser is closed, it is started on the real profile first. Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', 'use Firefox', 'open Chrome').",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all | analyze_page | open_with_profile"
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."
            },
            "url": {
                "type": "STRING",
                "description": "URL for go_to / new_tab / analyze_page action"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for search action"
            },
            "engine": {
                "type": "STRING",
                "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"
            },
            "selector": {
                "type": "STRING",
                "description": "CSS selector for click/type"
            },
            "text": {
                "type": "STRING",
                "description": "Text to click or type"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for smart_click/smart_type"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down for scroll"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount in pixels (default: 500)"
            },
            "key": {
                "type": "STRING",
                "description": "Key name for press action (e.g. Enter, Escape, F5)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            },
            "incognito": {
                "type": "BOOLEAN",
                "description": "Open in private/incognito mode"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "question": {
                "type": "STRING",
                "description": "Question, query, or information to check/find out on the page or search results (e.g. 'انمي ريزيرو نزل منه الحلقه الكام'). Automatically inspects the opened page, clicks into relevant results, and returns the answer in Arabic."
            },
            "instruction": {
                "type": "STRING",
                "description": "Optional Arabic/English task instruction to interact with the webpage or search results using vision (e.g. 'ادخل على أول نتيجة وشوف كذا')."
            },
            "profile_dir": {
                "type": "STRING",
                "description": "Custom Chrome user-data-dir path for open_with_profile action. Omit to use the default profile."
            },
            "profile_name": {
                "type": "STRING",
                "description": "Name, gaia name, email or directory of the Chrome profile to use (e.g. 'محمد مراد', 'Mohamed', 'Profile 1')."
            },
            "bookmark": {
                "type": "STRING",
                "description": "Bookmark name, folder, or search query to look up and open directly from the user's bookmarks (e.g. 'shopify', 'work')."
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": browser_control,
}
