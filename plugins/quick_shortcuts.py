"""
plugins/quick_shortcuts.py — Custom Shortcuts & Quick Launcher Plugin for JARVIS Mark LIV.

Allows the user to define custom keywords/aliases mapped to URLs or applications:
1. 'open': Opens a URL or application by keyword, respecting window mode (fullscreen or minimize).
2. 'add': Registers a new shortcut (keyword, URL/path, type, mode, profile, description).
3. 'list': Lists all registered shortcuts.
4. 'delete': Removes a shortcut.
5. 'check_chat_shortcut': Directly intercepts typed chat commands for instant execution without LLM latency.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# Safe console streams for Windows cp1252 / cp1256 / UTF-8
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PLUGIN = {
    "name": "quick_shortcuts",
    "description": (
        "بلجن الاختصارات السريعة وتشغيل البرامج والمواقع (Custom Shortcuts & Quick Launcher). "
        "يتيح للمستخدم تحديد كلمة مفتاحية لفتح أي برنامج عبر مساره، أو أي موقع عبر رابطه ببروفايل كروم المطلوب. "
        "يدعم وضعين للنوافذ: fullscreen (تكبير الشاشة الكاملة) أو minimize (تصغير النافذة للأسفل). "
        "العمليات: open (فتح عبر الكلمة المفتاحية) | add (تسجيل اختصار جديد) | list (عرض الاختصارات المسجلة) | delete (حذف اختصار)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "open (افتراضي) | add | list | delete"
            },
            "keyword": {
                "type": "STRING",
                "description": "الكلمة المفتاحية أو اسم الاختصار (مثال: 'فيسبوك', 'antigravity', 'بلندر', 'متجر العمل')"
            },
            "target": {
                "type": "STRING",
                "description": "رابط الموقع (URL) أو مسار البرنامج التنفيذي (.exe أو مسار ملف أو اسم تطبيق)"
            },
            "type": {
                "type": "STRING",
                "description": "نوع الاختصار: 'url' للمواقع أو 'app' للبرامج (يتم اكتشافه تلقائياً إذا لم يُحدد)"
            },
            "mode": {
                "type": "STRING",
                "description": "وضع النافذة: 'fullscreen' (تكبير كامل الشاشة) أو 'minimize' (تصغير النافذة)"
            },
            "profile": {
                "type": "STRING",
                "description": "بروفايل المتصفح للمواقع (مثال: 'work' أو 'personal' أو 'default')"
            },
            "description": {
                "type": "STRING",
                "description": "وصف توضيحي اختياري للاختصار"
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "default_mode": {
        "type": "string",
        "label": "Default Window Mode (fullscreen / minimize)",
        "default": "fullscreen",
    }
}


def _shortcuts_file() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    p = base / "config" / "shortcuts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_shortcuts() -> dict[str, dict]:
    f = _shortcuts_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_shortcuts(data: dict[str, dict]) -> None:
    f = _shortcuts_file()
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_str(s: str) -> str:
    if not s:
        return ""
    res = s.strip().lower()
    return (
        res.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
        .replace("-", " ")
        .replace("_", " ")
    )


def find_shortcut(query: str) -> tuple[str, dict] | None:
    """Find a shortcut by keyword or alias (case-insensitive & Arabic normalized)."""
    data = load_shortcuts()
    if not query:
        return None
    q = _norm_str(query)
    q_tokens = set(q.split())

    def _extract_candidates(item: dict, key: str) -> list[str]:
        raw_list = [key, item.get("keyword", "")] + item.get("aliases", [])
        candidates = []
        for raw in raw_list:
            if not raw:
                continue
            # Split on commas, slashes, or pipes if user entered multiple aliases in one field
            for part in re.split(r"[,/|]+", str(raw)):
                p = _norm_str(part)
                if p and p not in candidates:
                    candidates.append(p)
        return candidates

    # 1. Exact match first
    for key, item in data.items():
        candidates = _extract_candidates(item, key)
        for cand in candidates:
            if q == cand or q.replace(" ", "") == cand.replace(" ", ""):
                return (key, item)

    # 2. Token-based / Substring match
    for key, item in data.items():
        candidates = _extract_candidates(item, key)
        for cand in candidates:
            cand_tokens = set(cand.split())
            if cand_tokens and cand_tokens.issubset(q_tokens):
                return (key, item)
            # Check if cand is a distinct phrase inside query
            if len(cand) >= 3 and (f" {cand} " in f" {q} " or q.startswith(f"{cand} ") or q.endswith(f" {cand}")):
                return (key, item)

    return None


def _apply_window_mode(mode: str, target_name: str = ""):
    """Apply fullscreen/maximized or minimized window state."""
    mode_clean = (mode or "fullscreen").lower()
    time.sleep(0.5)

    if mode_clean in ("minimize", "minimized", "min"):
        if sys.platform == "win32":
            try:
                import win32gui
                import win32con
                matched = None
                def enum_win(hwnd, _):
                    nonlocal matched
                    if win32gui.IsWindowVisible(hwnd):
                        txt = win32gui.GetWindowText(hwnd).lower()
                        if target_name and _norm_str(target_name) in _norm_str(txt):
                            matched = hwnd
                if target_name:
                    win32gui.EnumWindows(enum_win, None)
                if matched:
                    win32gui.ShowWindow(matched, win32con.SW_MINIMIZE)
                    return
                # Fallback: minimize foreground window
                fg = win32gui.GetForegroundWindow()
                if fg:
                    win32gui.ShowWindow(fg, win32con.SW_MINIMIZE)
                    return
            except Exception:
                pass
        try:
            import pyautogui
            pyautogui.hotkey("win", "down")
        except Exception:
            pass

    else:
        # Fullscreen / Maximized
        try:
            from actions.computer_control import _maximize_window
            _maximize_window(target_name)
        except Exception:
            try:
                import pyautogui
                pyautogui.hotkey("win", "up")
            except Exception:
                pass


def check_chat_shortcut(text: str, player=None) -> str | None:
    """Checks if typed text matches a registered shortcut, and executes it immediately."""
    if not text:
        return None
    raw = text.strip()
    norm = _norm_str(raw)

    # Strip prefixes like "افتح", "شغل", "open", "launch", "run", "وريني"
    prefixes = [
        "افتح لي ", "افتحلي ", "افتح ", "شغل لي ", "شغللي ", "شغل ",
        "ابدأ ", "هات ", "وريني ", "open up ", "open ", "launch ", "start ", "run "
    ]
    candidate = raw
    for p in prefixes:
        if norm.startswith(_norm_str(p)):
            candidate = raw[len(p):].strip()
            break

    match = find_shortcut(candidate)
    if not match and candidate != raw:
        match = find_shortcut(raw)

    if not match:
        return None

    # Found match! Execute it directly!
    key, sc = match
    res = run({"action": "open", "keyword": key}, player=player)
    return res


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower() or "open"
    keyword = str(params.get("keyword", "")).strip()
    target = str(params.get("target", "")).strip()
    sc_type = str(params.get("type", "")).strip().lower()
    mode = str(params.get("mode", "")).strip().lower()
    profile = str(params.get("profile", "")).strip()
    description = str(params.get("description", "")).strip()

    if player:
        try:
            player.write_log(f"SHORTCUTS: {action} (keyword='{keyword}')")
        except Exception:
            pass

    # 1. LIST SHORTCUTS
    if action in ("list", "show", "all"):
        data = load_shortcuts()
        if not data:
            return "📋 لا توجد اختصارات مسجلة حالياً. يمكنك إضافة اختصار جديد من قائمة الإعدادات أو كتابة: 'سجل اختصار [اسم] لرابط أو مسار [كذا]'."
        lines = ["📋 قائمة الاختصارات السريعة المسجلة:"]
        for key, sc in data.items():
            t = sc.get("type", "unknown").upper()
            m = "⛶ تكبير" if sc.get("mode", "fullscreen") == "fullscreen" else "_ تصغير"
            tgt = sc.get("target", "")
            prof = f" (بروفايل: {sc.get('profile')})" if sc.get("profile") else ""
            desc = f" — {sc.get('description')}" if sc.get("description") else ""
            aliases = f" [مرادفات: {', '.join(sc.get('aliases', []))}]" if sc.get("aliases") else ""
            lines.append(f"• [{t} | {m}] **{key}**{aliases}{prof}: `{tgt}`{desc}")
        return "\n".join(lines)

    # 2. ADD / CREATE / UPDATE SHORTCUT
    if action in ("add", "create", "save", "register"):
        if not keyword or not target:
            return "❌ يرجى تحديد الكلمة المفتاحية (keyword) والهدف (target: رابط الموقع أو مسار البرنامج)."

        # Auto-detect type if not provided
        if not sc_type:
            if target.startswith("http://") or target.startswith("https://") or any(ext in target for ext in (".com", ".org", ".net", ".io", ".co")):
                sc_type = "url"
            else:
                sc_type = "app"

        final_mode = mode if mode in ("fullscreen", "minimize") else "fullscreen"

        data = load_shortcuts()
        slug = keyword.lower().replace(" ", "_")
        existing = data.get(slug, {})
        aliases = existing.get("aliases", [])
        if keyword not in aliases:
            aliases.append(keyword)

        data[slug] = {
            "keyword": keyword,
            "aliases": aliases,
            "type": sc_type,
            "target": target,
            "mode": final_mode,
            "profile": profile or existing.get("profile", ""),
            "description": description or existing.get("description", f"اختصار {keyword}"),
        }
        save_shortcuts(data)
        mode_label = "تكبير كامل الشاشة (Fullscreen)" if final_mode == "fullscreen" else "تصغير للأسفل (Minimized)"
        return (
            f"✅ تم حفظ الاختصار السريع بنجاح!\n"
            f"• الكلمة المفتاحية: **{keyword}**\n"
            f"• النوع: **{sc_type.upper()}**\n"
            f"• الوضع: **{mode_label}**\n"
            f"• الهدف: `{target}`\n"
            f"• البروفايل: {profile or 'افتراضي'}\n"
            f"👉 يمكنك تشغيله في أي وقت بكتابة أو قول اسمه وسيتنفذ فوراً!"
        )

    # 3. DELETE / REMOVE SHORTCUT
    if action in ("delete", "remove"):
        if not keyword:
            return "❌ يرجى تحديد الكلمة المفتاحية للاختصار المراد حذفه."
        data = load_shortcuts()
        found = find_shortcut(keyword)
        if not found:
            return f"❌ لم يتم العثور على اختصار بالكلمة المفتاحية '{keyword}'."
        key_to_delete = found[0]
        data.pop(key_to_delete, None)
        save_shortcuts(data)
        return f"✅ تم حذف الاختصار '{keyword}' بنجاح."

    # 4. OPEN / LAUNCH SHORTCUT (DEFAULT)
    if action in ("open", "launch", "run", "start"):
        lookup = keyword or target
        if not lookup:
            return "❌ يرجى تحديد الكلمة المفتاحية للاختصار المطلوب فتحه."

        match = find_shortcut(lookup)
        if not match:
            # Fallback: check if target is a direct URL or path
            if lookup.startswith("http://") or lookup.startswith("https://"):
                match = ("temp_url", {"keyword": lookup, "type": "url", "target": lookup, "mode": mode or "fullscreen", "profile": profile or "work"})
            else:
                return (
                    f"❌ لم يتم العثور على اختصار باسم '{lookup}'. "
                    f"يمكنك تسجيله أولاً بقول: 'سجل اختصار {lookup} لموقع [الرابط]' أو 'لمسار [المسار]'."
                )

        key, sc = match
        item_type = sc.get("type", "url").lower()
        target_path = sc.get("target", "")
        item_profile = sc.get("profile") or profile
        item_mode = mode or sc.get("mode", "fullscreen")

        mode_desc = "وتكبير الشاشة" if item_mode == "fullscreen" else "في وضع التصغير"

        # A. URL SHORTCUT
        if item_type == "url":
            try:
                from actions.browser_control import _launch_chrome_with_profile
                _launch_chrome_with_profile(
                    profile_query=item_profile or "work",
                    url=target_path,
                )
                _apply_window_mode(item_mode, "chrome")
                return (
                    f"✅ تم فتح الاختصار '{sc.get('keyword', key)}' في متصفح كروم {mode_desc}!\n"
                    f"• الرابط: {target_path}\n"
                    f"• الوضع: {item_mode.upper()}"
                )
            except Exception as e:
                return f"❌ فشل فتح الرابط '{target_path}': {e}"

        # B. APPLICATION SHORTCUT
        if item_type == "app":
            try:
                clean_target = str(target_path).strip().strip('"\'')
                # If target is an existing executable or shortcut file, start it directly
                if os.path.exists(clean_target):
                    os.startfile(clean_target)
                    res = f"تم تشغيل {clean_target} بنجاح."
                else:
                    from actions.open_app import open_app
                    res = open_app({"app_name": clean_target})
                _apply_window_mode(item_mode, clean_target)
                return (
                    f"✅ تم فتح برنامج '{sc.get('keyword', key)}' {mode_desc}!\n"
                    f"• المسار/التطبيق: {clean_target}\n"
                    f"• الوضع: {item_mode.upper()}\n"
                    f"• النتيجة: {res}"
                )
            except Exception as e:
                return f"❌ فشل فتح البرنامج '{target_path}': {e}"

    return f"Unknown action: '{action}'. Available: open, add, list, delete."
