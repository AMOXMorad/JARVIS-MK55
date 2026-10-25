"""
skills/skill_manager.py — Core Skill Learning System for JARVIS MK.

Handles:
- Teaching new skills from step sequences
- Executing saved skills with parameter substitution
- Exporting skills as .skill ZIP files
- Importing skills from .skill ZIP files
- Listing and managing user skills
"""
from __future__ import annotations

import importlib
import json
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from skills.skill_base import SkillDefinition, SkillStep

_ALLOWED_SKILL_ACTIONS = {
    "open_app", "close_app", "browser_control", "computer_control",
    "computer_settings", "send_message", "file_processor", "file_controller",
    "screen_grounding", "vision_interact", "discord_control",
    "goal_agent", "flow_control", "web_search", "system_status",
    "reminder", "youtube_video", "weather_report", "telegram_bridge",
    "quick_shortcuts", "cmd_control", "obsidian", "wait",
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_SKILLS_DIR = _base_dir() / "skills" / "user_skills"
_SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _default_dispatcher(action_name: str, params: dict) -> str:
    """Dispatches a skill step to the appropriate JARVIS action or plugin."""
    live = getattr(sys, "_jarvis_live_instance", None)
    if live:
        try:
            if hasattr(live, "_action_registry") and live._action_registry.has(action_name):
                return live._action_registry.run(action_name, params)
            if hasattr(live, "_plugin_registry") and live._plugin_registry.has(action_name):
                return live._plugin_registry.run(action_name, params)
        except Exception as e:
            return f"Error executing via registry: {e}"

    # Fallback to direct imports
    try:
        if action_name == "browser_control":
            from actions.browser_control import browser_control
            return browser_control(parameters=params)
        elif action_name == "open_app":
            from actions.open_app import open_app
            return open_app(parameters=params)
        elif action_name == "computer_control":
            from actions.computer_control import computer_control
            return computer_control(parameters=params)
        elif action_name in ("screen_grounding", "vision_interact"):
            from actions.screen_grounding import screen_grounding_handler
            return screen_grounding_handler(parameters=params)
        elif action_name == "send_message":
            from actions.send_message import send_message
            return send_message(parameters=params)
        elif action_name == "wait":
            time.sleep(float(params.get("seconds", 1)))
            return f"Waited {params.get('seconds', 1)}s"
        elif action_name == "close_app":
            from actions.close_app import close_app
            return close_app(parameters=params)
        elif action_name == "telegram_bridge":
            import plugins.telegram_bridge as tb
            return tb.run(parameters=params)
        elif action_name == "quick_shortcuts":
            import plugins.quick_shortcuts as qs
            return qs.run(parameters=params)
        elif action_name == "flow_control":
            from actions.flow_action import flow_control
            return flow_control(parameters=params)
        elif action_name == "discord_control":
            import plugins.discord_control as dc
            return dc.run(parameters=params)
        elif action_name == "goal_agent":
            try:
                from actions.goal_agent import goal_agent_handler
                return goal_agent_handler(parameters=params)
            except Exception as e:
                return f"Error in goal_agent: {e}"
    except Exception as e:
        return f"Error executing {action_name}: {e}"

    return f"Done: {action_name}"


class SkillManager:
    """Manages the lifecycle of user-defined JARVIS skills."""

    def __init__(self, user_name: str = "User"):
        self.user_name = user_name
        self._skills_dir = _SKILLS_DIR
        self._skills_dir.mkdir(parents=True, exist_ok=True)

    # ── Discovery ────────────────────────────────────────────────────────────

    def list_skills(self) -> list[SkillDefinition]:
        """Return all saved user skills."""
        skills = []
        for f in sorted(self._skills_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                skills.append(SkillDefinition.from_dict(data))
            except Exception:
                pass
        return skills

    def get_skill(self, name: str) -> SkillDefinition | None:
        """Find a skill by name (case-insensitive)."""
        path = self._skills_dir / f"{name.lower().replace(' ', '_')}.json"
        if path.exists():
            try:
                return SkillDefinition.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:
                return None
        # Fuzzy search by name, display_name, or trigger tags
        q = (name or "").strip().lower()
        for skill in self.list_skills():
            if (skill.name.lower() == q
                    or skill.display_name.strip().lower() == q
                    or q in [t.strip().lower() for t in (skill.tags or [])]
                    or (len(q) >= 3 and (q in skill.display_name.lower() or q in skill.name.lower()))):
                return skill
        return None

    # ── Teaching ─────────────────────────────────────────────────────────────

    def teach_skill(
        self,
        name: str,
        display_name: str,
        description: str,
        steps: list[dict],
        parameters: list[str] | None = None,
        required_packages: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> SkillDefinition:
        """
        Record a new skill from a list of step dicts.

        Example steps:
        [
          {"action": "open_app", "params": {"app_name": "chrome"}, "description": "افتح كروم"},
          {"action": "browser_control", "params": {"action": "navigate", "url": "{url}"}, "description": "افتح الموقع"},
        ]
        """
        skill_steps = [SkillStep.from_dict(s) for s in steps]
        skill = SkillDefinition(
            name=name.lower().replace(" ", "_"),
            display_name=display_name,
            description=description,
            author=self.user_name,
            version="1.0",
            created_at=datetime.now().isoformat(),
            parameters=parameters or [],
            steps=skill_steps,
            required_files=[],
            required_packages=required_packages or [],
            tags=tags or [],
        )
        self._save_skill(skill)
        return skill

    def _save_skill(self, skill: SkillDefinition) -> None:
        path = self._skills_dir / f"{skill.name}.json"
        path.write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Execution ────────────────────────────────────────────────────────────

    def execute_skill(
        self,
        name: str,
        params: dict | None = None,
        tool_dispatcher=None,
    ) -> str:
        """
        Execute a saved skill.
        If tool_dispatcher is not provided, uses _default_dispatcher to run actions for real.
        """
        skill = self.get_skill(name)
        if not skill:
            return f"❌ لم يتم إيجاد مهارة باسم '{name}'. استخدم 'list_skills' لرؤية المهارات المتاحة."

        dispatcher = tool_dispatcher or _default_dispatcher
        results = []
        for i, step in enumerate(skill.steps, 1):
            # Apply parameter substitution
            resolved_params = _apply_params(step.params, params or {})

            try:
                result = dispatcher(step.action, resolved_params)
                results.append(f"✅ خطوة {i}: {step.description} — {result}")
            except Exception as e:
                results.append(f"❌ خطوة {i}: {step.description} — خطأ: {e}")

            if step.wait_after_ms > 0:
                time.sleep(step.wait_after_ms / 1000)

        return "\n".join(results)

    # ── Export / Import ──────────────────────────────────────────────────────

    def export_skill(self, name: str, output_path: str | None = None) -> str:
        """
        Export a skill as a .skill ZIP file.
        Returns the path of the created file.
        """
        skill = self.get_skill(name)
        if not skill:
            return f"❌ لم يتم إيجاد مهارة باسم '{name}'."

        if not output_path:
            desktop = Path.home() / "Desktop"
            output_path = str(desktop / f"{skill.name}.skill")

        out = Path(output_path)
        if out.suffix not in (".skill", ".jarvis"):
            out = out.with_suffix(".skill")

        manifest = skill.generate_manifest()
        readme = _generate_readme(skill)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            # skill.json — full definition
            zf.writestr("skill.json", json.dumps(skill.to_dict(), ensure_ascii=False, indent=2))
            # manifest.md — installation instructions
            zf.writestr("manifest.md", manifest)
            # README.md — user-facing description
            zf.writestr("README.md", readme)

        return f"✅ تم تصدير المهارة '{skill.display_name}' إلى: {out}"

    def import_skill(self, zip_path: str) -> str:
        """
        Import a skill from a .skill ZIP file.
        Returns success/error message.
        """
        path = Path(zip_path)
        if not path.exists():
            return f"❌ الملف غير موجود: {zip_path}"
        if path.suffix not in (".skill", ".zip", ".jarvis"):
            return f"❌ الملف يجب أن يكون .skill أو .jarvis أو .zip"

        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "skill.json" not in zf.namelist():
                    return "❌ الملف تالف — لا يحتوي على skill.json"
                data = json.loads(zf.read("skill.json").decode("utf-8"))
                skill = SkillDefinition.from_dict(data)
        except Exception as e:
            return f"❌ خطأ في قراءة الملف: {e}"

        # Security: validate all step actions against whitelist
        unknown_actions = [
            step.action for step in skill.steps 
            if step.action not in _ALLOWED_SKILL_ACTIONS
        ]
        if unknown_actions:
            return (
                f"❌ رُفضت المهارة '{skill.display_name}' — "
                f"تحتوي على أوامر غير مسموح بها: {unknown_actions}. "
                f"الاستيراد مرفوض لأسباب أمنية."
            )

        # Check for name conflicts
        existing = self.get_skill(skill.name)
        if existing:
            # Overwrite with newer version
            skill.version = _bump_version(existing.version)

        self._save_skill(skill)
        return (
            f"✅ تم استيراد المهارة '{skill.display_name}' بنجاح!\n"
            f"المؤلف: {skill.author} | الإصدار: {skill.version}\n"
            f"الخطوات: {len(skill.steps)} خطوة\n"
            f"قل 'نفّذ مهارة {skill.display_name}' لتشغيلها."
        )

    def delete_skill(self, name: str) -> str:
        """Delete a saved skill."""
        skill = self.get_skill(name)
        if not skill:
            return f"❌ لم يتم إيجاد مهارة باسم '{name}'."
        path = self._skills_dir / f"{skill.name}.json"
        path.unlink(missing_ok=True)
        return f"✅ تم حذف المهارة '{skill.display_name}'."

    def get_required_changes(self, name: str) -> str:
        """Return the Markdown manifest for a skill."""
        skill = self.get_skill(name)
        if not skill:
            return f"❌ لم يتم إيجاد مهارة باسم '{name}'."
        return skill.generate_manifest()

    def list_skills_summary(self) -> str:
        """Return Arabic summary of all skills for JARVIS to read aloud."""
        skills = self.list_skills()
        if not skills:
            return "لا توجد مهارات محفوظة بعد. قل 'علّمني مهارة جديدة' لإضافة أول مهارة!"
        lines = [f"عندك {len(skills)} مهارة محفوظة:\n"]
        for s in skills:
            lines.append(f"• **{s.display_name}** — {s.description}")
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_params(params: dict, values: dict) -> dict:
    """Replace {placeholder} tokens in param values with actual values."""
    result = {}
    for k, v in params.items():
        if isinstance(v, str):
            for param_name, param_val in values.items():
                v = v.replace(f"{{{param_name}}}", str(param_val))
        result[k] = v
    return result


def _bump_version(version: str) -> str:
    """Increment minor version: '1.0' → '1.1'."""
    try:
        parts = version.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        return version


def _generate_readme(skill: SkillDefinition) -> str:
    """Generate user-facing README.md for the exported skill."""
    return f"""# {skill.display_name}

> {skill.description}

**Author**: {skill.author}  
**Version**: {skill.version}  
**Created**: {skill.created_at[:10] if skill.created_at else 'Unknown'}  
**Tags**: {', '.join(skill.tags) if skill.tags else 'None'}  

## How to Use

1. Import this `.skill` file into JARVIS MK
2. Say: **"نفّذ مهارة {skill.display_name}"**
{chr(10).join(f'3. Provide parameter `{p}` when prompted.' for p in skill.parameters) if skill.parameters else ''}

## Steps ({len(skill.steps)} steps)

{chr(10).join(f'{i}. {step.description}' for i, step in enumerate(skill.steps, 1))}

## Required Packages

{('```\npip install ' + ' '.join(skill.required_packages) + '\n```') if skill.required_packages else 'None'}
"""


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        t = t.replace(a, b)
    for p in ".,!?;:\"'()[]{}«»-_\n\r\t":
        t = t.replace(p, " ")
    return " ".join(t.split())


def check_chat_skill(user_text: str, player=None) -> str | None:
    """Check if user_text triggers a saved skill directly from chat/Telegram."""
    if not user_text:
        return None
    norm = _norm_ar(user_text)
    if not norm:
        return None

    sm = SkillManager()
    skills = sm.list_skills()
    if not skills:
        return None

    # Explicit prefix: "نفذ مهارة X" / "شغل مهارة X" / "run skill X"
    for prefix in ("نفذ مهاره ", "شغل مهاره ", "تشغيل مهاره ", "run skill ", "execute skill "):
        if norm.startswith(prefix):
            target = norm[len(prefix):].strip()
            for s in skills:
                if target and (target in _norm_ar(s.display_name) or target in _norm_ar(s.name)):
                    if player and hasattr(player, "write_log"):
                        player.write_log(f"[skill] ⚡ جاري تنفيذ مهارة: {s.display_name}")
                    return sm.execute_skill(s.name)

    # Direct match on display_name or trigger tags
    for s in skills:
        triggers = [s.display_name, s.name.replace("_", " ")] + list(s.tags or [])
        for trig in triggers:
            nt = _norm_ar(trig)
            if nt and len(nt) >= 3 and (norm == nt or norm in (f"شغل {nt}", f"نفذ {nt}", f"افتح {nt}")):
                if player and hasattr(player, "write_log"):
                    player.write_log(f"[skill] ⚡ جاري تنفيذ مهارة: {s.display_name}")
                return sm.execute_skill(s.name)
    return None


def format_learned_for_prompt() -> str:
    """Build a compact prompt block of all learned skills, flows, and shortcuts."""
    lines: list[str] = []
    try:
        sm = SkillManager()
        skills = sm.list_skills()
        if skills:
            lines.append("Learned Skills (call `run_skill` with `name` when requested):")
            for s in skills[:20]:
                trig = f" | triggers: {', '.join(s.tags)}" if s.tags else ""
                lines.append(f"  - {s.name} ({s.display_name}): {s.description[:120]}{trig}")
    except Exception:
        pass

    try:
        from core.flow_engine import FlowEngine
        flows = FlowEngine.get_instance().list_flows()
        if flows:
            lines.append("Registered Automation Flows (call `flow_control` or triggered by start_command):")
            for f in flows[:15]:
                mode = f"every {f.interval_seconds}s" if f.interval_seconds > 0 else "one-shot"
                lines.append(f"  - {f.id} ({f.name}) [{mode}] — start: '{f.start_command}'")
    except Exception:
        pass

    try:
        from plugins.quick_shortcuts import load_shortcuts
        shortcuts = load_shortcuts()
        if shortcuts:
            lines.append("Quick Shortcuts (call `quick_shortcuts` with action='open', keyword=...):")
            for k, v in list(shortcuts.items())[:20]:
                kw = v.get("keyword", k)
                desc = v.get("description") or v.get("target", "")
                lines.append(f"  - '{kw}': {str(desc)[:100]}")
    except Exception:
        pass

    if not lines:
        return ""
    return "[LEARNED SKILLS, FLOWS & SHORTCUTS — use these automatically when relevant]\n" + "\n".join(lines) + "\n"
