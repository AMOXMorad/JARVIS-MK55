"""
memory/usage_stats.py — Thread-safe daily usage statistics tracker.

Tracks JARVIS usage across sessions and persists to memory/stats.json.
"""
import json
import sys
import threading
from datetime import date
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_STATS_FILE = _base_dir() / "memory" / "stats.json"
_lock = threading.Lock()

# In-memory counters for this session
_session: dict = {
    "commands_today": 0,
    "discord_opens": 0,
    "wake_count": 0,
    "browser_opens": 0,
    "skill_runs": 0,
}


def _load_persistent() -> dict:
    """Load stats.json — returns dict keyed by date string."""
    try:
        if _STATS_FILE.exists():
            return json.loads(_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_persistent(data: dict) -> None:
    """Persist stats.json."""
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def increment(stat_name: str, by: int = 1) -> None:
    """
    Increment a usage counter.
    stat_name: one of 'commands_today', 'discord_opens', 'wake_count',
                       'browser_opens', 'skill_runs'
    """
    with _lock:
        if stat_name in _session:
            _session[stat_name] += by
        # Persist immediately
        data = _load_persistent()
        today = str(date.today())
        if today not in data:
            data[today] = {k: 0 for k in _session}
        data[today][stat_name] = data[today].get(stat_name, 0) + by
        _save_persistent(data)


def get_stats() -> dict:
    """Return current session counters."""
    with _lock:
        return dict(_session)


def get_today_stats() -> dict:
    """Return today's persistent stats (includes all sessions today)."""
    data = _load_persistent()
    today = str(date.today())
    return data.get(today, {k: 0 for k in _session})


def get_stats_summary() -> str:
    """Return a human-readable one-line stats summary."""
    s = get_today_stats()
    return (
        f"🛠 {s.get('commands_today', 0)} cmds | "
        f"💬 {s.get('discord_opens', 0)} Discord | "
        f"💤 {s.get('wake_count', 0)} wakes | "
        f"🌐 {s.get('browser_opens', 0)} browser | "
        f"⚡ {s.get('skill_runs', 0)} skills"
    )
