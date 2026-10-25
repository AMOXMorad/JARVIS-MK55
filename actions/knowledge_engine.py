import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from skills.skill_manager import SkillManager
from memory.memory_manager import load_memory, update_memory, BASE_DIR

_ALLOWED_ACTIONS = {
    "open_app", "close_app", "browser_control", "computer_control",
    "computer_settings", "send_message", "file_processor", "file_controller",
    "screen_grounding", "vision_interact", "discord_control",
    "goal_agent", "flow_control", "web_search", "system_status",
    "reminder", "youtube_video", "weather_report", "telegram_bridge",
    "quick_shortcuts", "cmd_control", "obsidian", "wait",
}


def _natural_to_steps(description: str) -> list[dict]:
    """Convert natural language skill description to structured steps using Gemini."""
    from core import gemini
    prompt = f"""Convert this user task/skill description into a JSON array of structured skill steps for JARVIS on Windows.

Task description: {description}

Available actions and their exact parameter schemas:
- open_app: {{"app_name": "chrome | notepad | obsidian | ..."}}
- close_app: {{"app_name": "..."}}
- browser_control: {{"action": "open_with_profile | navigate | new_tab | reload | search | click | type", "profile_name": "optional Chrome profile name", "url": "optional URL", "bookmark": "optional bookmark keyword", "query": "optional search query"}}
- computer_control: {{"action": "press | hotkey | type | click | read_screen | find_and_click", "keys": "e.g. ctrl+t", "text": "text to type", "description": "element description"}}
- computer_settings: {{"action": "volume_up | volume_down | volume_set | mute | brightness_set | dark_mode | screenshot", "value": 50}}
- send_message: {{"platform": "whatsapp | telegram | discord", "target": "receiver name", "message": "text"}}
- telegram_bridge: {{"action": "send | send_status", "message": "text to send to owner on Telegram"}}
- quick_shortcuts: {{"action": "open", "keyword": "shortcut keyword"}}
- flow_control: {{"action": "start | stop | list", "flow_id": "flow name or id"}}
- youtube_video: {{"action": "play | search", "query": "video topic"}}
- web_search: {{"mode": "search | news", "query": "search query"}}
- cmd_control: {{"action": "run", "command": "shell command"}}
- wait: {{"seconds": 2}}

Reply ONLY with a valid JSON array of step objects:
[
  {{"action": "browser_control", "params": {{"action": "open_with_profile", "profile_name": "mohamed worl", "url": "https://facebook.com"}}, "description": "فتح فيسبوك ببروفايل الشغل", "wait_after_ms": 1000}}
]"""
    result = gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=20000)
    if isinstance(result, list):
        valid_steps = []
        for s in result:
            if isinstance(s, dict) and s.get("action"):
                valid_steps.append({
                    "action": str(s["action"]).strip(),
                    "params": s.get("params") or s.get("parameters") or {},
                    "description": s.get("description") or s.get("label") or str(s["action"]),
                    "wait_after_ms": int(s.get("wait_after_ms", 500)),
                })
        return valid_steps
    return []


def _refresh_ui_if_open(player=None):
    """Refresh the Skills/Flows overlay in the HUD if available."""
    try:
        live = getattr(sys, "_jarvis_live_instance", None)
        ui_obj = player or (getattr(live, "ui", None) if live else None)
        if ui_obj and hasattr(ui_obj, "_flow_overlay") and ui_obj._flow_overlay:
            ov = ui_obj._flow_overlay
            if hasattr(ov, "_refresh_skills_list"):
                ov._refresh_skills_list()
            if hasattr(ov, "_refresh_library"):
                ov._refresh_library()
    except Exception:
        pass


def _teach_skill(parameters: dict, player=None, **kwargs) -> str:
    raw_name = (parameters.get("name") or parameters.get("display_name") or "").strip()
    display_name = (parameters.get("display_name") or raw_name).strip()
    description = (parameters.get("description") or "").strip()
    if not raw_name:
        return "❌ يرجى تحديد اسم المهارة (name)."

    slug = re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]+", "_", raw_name.lower()).strip("_") or f"skill_{int(time.time())}"

    # Accept pre-built steps if provided, otherwise convert natural language description
    raw_steps = parameters.get("steps")
    steps: list[dict] = []
    if isinstance(raw_steps, list) and raw_steps:
        for s in raw_steps:
            if isinstance(s, dict) and s.get("action"):
                steps.append({
                    "action": str(s["action"]).strip(),
                    "params": s.get("params") or s.get("parameters") or {},
                    "description": s.get("description") or s.get("label") or str(s["action"]),
                    "wait_after_ms": int(s.get("wait_after_ms", 500)),
                })
    if not steps and description:
        steps = _natural_to_steps(description)

    if not steps:
        return "❌ لم أتمكن من استخراج خطوات التنفيذ. وضح لي الخطوات بالتفصيل (مثلاً: افتح كروم ثم ادخل على موقع كذا)."

    # Parse trigger phrases / tags
    tags = list(parameters.get("tags") or [])
    triggers_str = parameters.get("triggers") or ""
    if isinstance(triggers_str, str) and triggers_str.strip():
        for part in triggers_str.replace("،", ",").replace("|", ",").split(","):
            p = part.strip()
            if p and p not in tags:
                tags.append(p)
    if display_name and display_name not in tags:
        tags.append(display_name)

    sm = SkillManager()
    skill = sm.teach_skill(
        name=slug,
        display_name=display_name,
        description=description or display_name,
        steps=steps,
        parameters=parameters.get("parameters", []),
        required_packages=parameters.get("required_packages", []),
        tags=tags,
    )
    _refresh_ui_if_open(player)
    if player and hasattr(player, "write_log"):
        player.write_log(f"[skill] 🧠 تم تعلم مهارة جديدة: {skill.display_name} ({len(skill.steps)} خطوات)")

    steps_preview = "\n".join(f"  {i}. {st.description} ({st.action})" for i, st in enumerate(skill.steps, 1))
    return (
        f"✅ تم تعلم وحفظ المهارة **'{skill.display_name}'** بنجاح!\n"
        f"• الكود: `{skill.name}`\n"
        f"• كلمات التشغيل السريع: {', '.join(tags)}\n"
        f"• الخطوات ({len(skill.steps)}):\n{steps_preview}\n"
        f"تقدر تشغلها في أي وقت بقولك: «{skill.display_name}» أو «نفذ مهارة {skill.display_name}»."
    )


def _run_skill(parameters: dict, player=None, **kwargs) -> str:
    name = (parameters.get("name") or "").strip()
    skill_params = parameters.get("parameters") or {}
    sm = SkillManager()
    res = sm.execute_skill(name, skill_params)
    if player and hasattr(player, "write_log"):
        player.write_log(f"[skill] ▶ {name}: {res[:120]}")
    return res


def _list_skills(parameters: dict, **kwargs) -> str:
    sm = SkillManager()
    return sm.list_skills_summary()


def _delete_skill(parameters: dict, player=None, **kwargs) -> str:
    name = (parameters.get("name") or "").strip()
    sm = SkillManager()
    res = sm.delete_skill(name)
    _refresh_ui_if_open(player)
    return res


def _skill_info(parameters: dict, **kwargs) -> str:
    name = (parameters.get("name") or "").strip()
    sm = SkillManager()
    skill = sm.get_skill(name)
    if not skill:
        return f"❌ لم يتم إيجاد مهارة باسم '{name}'."

    info = [f"**{skill.display_name}** ({skill.name})", f"الوصف: {skill.description}"]
    if skill.tags:
        info.append(f"كلمات التشغيل: {', '.join(skill.tags)}")
    info.append(f"الخطوات: {len(skill.steps)}")
    for i, step in enumerate(skill.steps, 1):
        info.append(f"  {i}. {step.action} - {step.description}")
    return "\n".join(info)


def _learn_preference(parameters: dict, player=None, **kwargs) -> str:
    """Save a user preference, standing behavioral rule, personal fact, or quick shortcut."""
    category = (parameters.get("category") or "rules").strip().lower()
    if category not in ("identity", "rules", "preferences", "projects", "relationships", "wishes", "notes"):
        category = "rules"
    key = (parameters.get("key") or "").strip()
    value = (parameters.get("value") or "").strip()
    shortcut_kw = (parameters.get("shortcut_keyword") or "").strip()
    shortcut_target = (parameters.get("shortcut_target") or "").strip()
    shortcut_profile = (parameters.get("shortcut_profile") or "").strip()

    msgs = []
    if key and value:
        safe_key = re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]+", "_", key.lower()).strip("_") or "learned_rule"
        update_memory({category: {safe_key: {"value": value}}})
        msgs.append(f"🧠 تم حفظ القاعدة/التفضيل في الذاكرة الدائمة (`{category}/{safe_key}`): **{value}**")

    if shortcut_kw and shortcut_target:
        try:
            import plugins.quick_shortcuts as qs
            sc_res = qs.run({
                "action": "add",
                "keyword": shortcut_kw,
                "target": shortcut_target,
                "profile": shortcut_profile,
                "description": value or f"اختصار {shortcut_kw}",
            }, player=player)
            msgs.append(f"⚡ تم تسجيل الاختصار الفوري: {sc_res}")
        except Exception as e:
            msgs.append(f"⚠️ تعذر تسجيل الاختصار: {e}")

    if not msgs:
        return "❌ يرجى تمرير `key` و `value` لحفظ التفضيل أو القاعدة."

    out = "\n".join(msgs)
    if player and hasattr(player, "write_log"):
        player.write_log(f"[learn] {out}")
    return out


def _learn_capability(parameters: dict, player=None, **kwargs) -> str:
    """
    Generate, validate, and hot-load a brand-new Python plugin capability (`plugins/learned_<name>.py`)
    so JARVIS acquires a new tool/ability immediately from chat without restarting!
    """
    from core import gemini

    raw_name = (parameters.get("name") or "").strip().lower()
    description = (parameters.get("description") or "").strip()
    behavior_spec = (parameters.get("implementation_details") or description).strip()
    run_now_args = parameters.get("run_now_args")

    if not raw_name or not description:
        return "❌ يرجى تحديد اسم القدرة البرمجية (`name` بالإنجليزية snake_case) ووصفها (`description`)."

    tool_name = re.sub(r"[^a-z0-9_]+", "_", raw_name).strip("_")
    if not tool_name or not re.match(r"^[a-z_][a-z0-9_]{1,50}$", tool_name):
        tool_name = f"custom_tool_{int(time.time()) % 10000}"
    if not tool_name.startswith("learned_"):
        file_stem = f"learned_{tool_name}"
    else:
        file_stem = tool_name

    prompt = f"""Write a complete, self-contained Python 3 plugin module for JARVIS on Windows.

Plugin Tool Name: "{tool_name}"
What it does (Arabic/English): {description}
Implementation requirements: {behavior_spec}

STRICT CONTRACT:
1. Must define a module-level dict named `PLUGIN`:
   PLUGIN = {{
       "name": "{tool_name}",
       "description": "...", # clear Arabic + English description of when to call this tool and its actions
       "parameters": {{
           "type": "OBJECT",
           "properties": {{ ... }}, # use uppercase types: "STRING", "INTEGER", "NUMBER", "BOOLEAN", "OBJECT"
           "required": [...]
       }}
   }}
2. Must define `def run(parameters: dict, player=None, session_memory=None) -> str:`
   - Reads arguments from `parameters` (a dict).
   - Executes the logic safely using Python standard library (or installed packages like `psutil`, `pyautogui`, `pyperclip`, `requests`, `urllib.request`, `openpyxl`, `core.gemini`).
   - Returns a clear, concise result string in Arabic.
   - Never blocks forever; use timeouts on network/subprocess calls.
3. Reply ONLY with the raw Python code (no markdown fences, no explanations).
"""
    raw_code = gemini.ask(prompt, tier=gemini.SMART, timeout_ms=35000)
    if not raw_code:
        return "❌ تعذر توليد كود القدرة الجديدة عبر Gemini."

    code = raw_code.strip()
    code = re.sub(r"^```[a-zA-Z]*\r?\n?", "", code)
    code = re.sub(r"\r?\n?```\s*$", "", code).strip()

    try:
        compile(code, f"{file_stem}.py", "exec")
    except SyntaxError as se:
        return f"❌ حدث خطأ في بناء كود القدرة الجديدة: {se}"

    plugins_dir = BASE_DIR / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugin_path = plugins_dir / f"{file_stem}.py"
    plugin_path.write_text(code, encoding="utf-8")

    # Hot-load into the live PluginRegistry if running
    live = getattr(sys, "_jarvis_live_instance", None)
    loaded_name = tool_name
    if live and hasattr(live, "_plugin_registry"):
        rec = live._plugin_registry.load_plugin_file(plugin_path)
        if not rec.valid:
            plugin_path.unlink(missing_ok=True)
            return f"❌ تم رفض القدرة البرمجية أثناء التحقق: {rec.error}"
        loaded_name = rec.name
    else:
        from core.plugin_loader import discover_plugins
        reg = discover_plugins(plugins_dir, set())
        if not reg.has(tool_name):
            return f"❌ لم يتم التحقق من البلجن الجديد `{file_stem}.py`."

    if player and hasattr(player, "write_log"):
        player.write_log(f"[capability] 🚀 تم بناء وتفعيل قدرة جديدة: {loaded_name} ({plugin_path.name})")

    # Also record in memory that this capability was learned
    update_memory({"notes": {f"capability_{loaded_name}": {"value": f"Learned plugin tool '{loaded_name}': {description}"}}})

    exec_out = ""
    if isinstance(run_now_args, dict) and live and hasattr(live, "_plugin_registry"):
        try:
            r = live._plugin_registry.run(loaded_name, run_now_args, player=player)
            exec_out = f"\n• **نتيجة التجربة الفورية**: {r}"
        except Exception as e:
            exec_out = f"\n• ⚠️ نتيجة التجربة: {e}"

    return (
        f"✅ **تم تعلم وبناء القدرة الجديدة `{loaded_name}` وتفعيلها فوراً بدون إعادة تشغيل!**\n"
        f"• الملف: `plugins/{plugin_path.name}`\n"
        f"• الوصف: {description}"
        f"{exec_out}"
    )


def _run_learned_capability(parameters: dict, player=None, **kwargs) -> str:
    """Execute any hot-loaded or existing plugin by name immediately, even before session reconnect."""
    plugin_name = (parameters.get("name") or "").strip()
    plugin_args = parameters.get("arguments") or {}
    if not plugin_name:
        return "❌ يرجى تحديد اسم الأداة/القدرة (`name`)."
    live = getattr(sys, "_jarvis_live_instance", None)
    if live and hasattr(live, "_plugin_registry") and live._plugin_registry.has(plugin_name):
        return live._plugin_registry.run(plugin_name, plugin_args, player=player)
    # Fallback: import directly from plugins/
    from core.plugin_loader import discover_plugins
    reg = discover_plugins(BASE_DIR / "plugins", set())
    if reg.has(plugin_name):
        return reg.run(plugin_name, plugin_args, player=player)
    return f"❌ لم يتم العثور على قدرة/بلجن باسم `{plugin_name}`."


def _export_knowledge(parameters: dict, **kwargs) -> str:
    import getpass
    username = getpass.getuser()
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    desktop = Path.home() / "Desktop"
    output_path = desktop / f"{username}_jarvis_knowledge_{date_str}.jarvis"

    sm = SkillManager()
    skills = sm.list_skills()

    memory_data = load_memory()
    if "sessions" in memory_data:
        del memory_data["sessions"]

    shortcuts_path = BASE_DIR / "config" / "shortcuts.json"
    shortcuts_data = {}
    if shortcuts_path.exists():
        try:
            shortcuts_data = json.loads(shortcuts_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    manifest = {
        "version": "1.0",
        "exported_at": datetime.now().isoformat(),
        "skills_count": len(skills),
        "author": username,
    }

    readme = (
        f"# Jarvis Knowledge Bundle\n\nExported on {manifest['exported_at']} by {manifest['author']}.\n"
        f"Contains {manifest['skills_count']} skills, long-term memory, and shortcuts."
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("memory.json", json.dumps(memory_data, ensure_ascii=False, indent=2))
        zf.writestr("shortcuts.json", json.dumps(shortcuts_data, ensure_ascii=False, indent=2))
        zf.writestr("README.md", readme)
        for skill in skills:
            zf.writestr(f"skills/{skill.name}.json", json.dumps(skill.to_dict(), ensure_ascii=False, indent=2))

    return f"✅ تم تصدير المعرفة بنجاح إلى: {output_path}"


def _import_knowledge(parameters: dict, player=None, **kwargs) -> str:
    path_str = parameters.get("path")
    if not path_str:
        return "❌ يرجى توفير مسار ملف .jarvis للاستيراد."

    path = Path(path_str.strip("\"'"))
    if not path.exists():
        return f"❌ الملف غير موجود: {path}"

    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "manifest.json" not in zf.namelist():
                return "❌ الملف تالف أو غير مدعوم — لا يحتوي على manifest.json"

            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if manifest.get("version", "1.0") > "1.0":
                return "❌ إصدار الملف أحدث من الإصدار المدعوم."

            skill_files = [f for f in zf.namelist() if f.startswith("skills/") and f.endswith(".json")]
            sm = SkillManager()
            imported_skills = 0

            for sf in skill_files:
                skill_data = json.loads(zf.read(sf).decode("utf-8"))
                safe = all(step.get("action") in _ALLOWED_ACTIONS for step in skill_data.get("steps", []))
                if not safe:
                    continue

                from skills.skill_base import SkillDefinition
                skill = SkillDefinition.from_dict(skill_data)
                existing = sm.get_skill(skill.name)
                if existing:
                    from skills.skill_manager import _bump_version
                    skill.version = _bump_version(existing.version)

                sm._save_skill(skill)
                imported_skills += 1

            merge_memory = parameters.get("merge_memory", False)
            if merge_memory and "memory.json" in zf.namelist():
                memory_data = json.loads(zf.read("memory.json").decode("utf-8"))
                update_memory(memory_data)

    except Exception as e:
        return f"❌ خطأ أثناء الاستيراد: {str(e)}"

    _refresh_ui_if_open(player)
    return f"✅ تم استيراد المعرفة بنجاح! تم إضافة {imported_skills} مهارة."


TOOLS = [
    {
        "name": "teach_skill",
        "description": (
            "Teach JARVIS a reusable multi-step skill from chat (e.g. 'اتعلم مهارة كذا', 'سجل عندك مهارة...'). "
            "Saves the sequence of actions and trigger phrases so the user can invoke it anytime by name or trigger phrase."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Short snake_case identifier (e.g. 'open_work_env')"},
                "display_name": {"type": "STRING", "description": "Arabic or readable name (e.g. 'تجهيز بيئة الشغل')"},
                "description": {"type": "STRING", "description": "Full natural-language description of the steps to perform"},
                "triggers": {"type": "STRING", "description": "Optional comma-separated trigger phrases in Arabic/English (e.g. 'جهز الشغل, ابدا يومي')"},
            },
            "required": ["name", "display_name", "description"],
        },
        "handler": _teach_skill,
    },
    {
        "name": "run_skill",
        "description": (
            "Execute a learned skill by its name, display_name, or trigger phrase. "
            "Call this whenever the user asks to run a previously learned skill."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name or Arabic display_name of the skill to execute"},
                "parameters": {"type": "OBJECT", "description": "Optional template parameters for the skill"},
            },
            "required": ["name"],
        },
        "handler": _run_skill,
    },
    {
        "name": "list_skills",
        "description": "List all saved/learned user skills and their descriptions.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": _list_skills,
    },
    {
        "name": "delete_skill",
        "description": "Delete a saved skill by name or display_name.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name or display_name of the skill to delete"},
            },
            "required": ["name"],
        },
        "handler": _delete_skill,
    },
    {
        "name": "skill_info",
        "description": "Get detailed info and steps for a specific learned skill.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name or display_name of the skill"},
            },
            "required": ["name"],
        },
        "handler": _skill_info,
    },
    {
        "name": "learn_preference",
        "description": (
            "Learn and permanently save a user preference, standing behavioral rule, default setting, "
            "personal fact, or quick shortcut directly from chat (e.g. 'اتعلم دايماً...', 'احفظ عندك إن...', "
            "'لما أقولك X افتح Y'). Immediately takes effect in memory and shortcuts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "'rules' (standing behavioral instructions, default) | 'preferences' | 'identity' | 'projects' | 'notes'",
                },
                "key": {"type": "STRING", "description": "Short key identifier (e.g. 'default_chrome_profile', 'reply_style')"},
                "value": {"type": "STRING", "description": "The exact rule, preference, or fact to remember (in Arabic or English)"},
                "shortcut_keyword": {"type": "STRING", "description": "Optional: if learning a quick launcher keyword (e.g. 'متجري'), put the trigger phrase here"},
                "shortcut_target": {"type": "STRING", "description": "Optional: URL or app path for the quick shortcut"},
                "shortcut_profile": {"type": "STRING", "description": "Optional: Chrome profile name if shortcut_target is a URL"},
            },
            "required": ["key", "value"],
        },
        "handler": _learn_preference,
    },
    {
        "name": "learn_capability",
        "description": (
            "Build, validate, and hot-load a BRAND-NEW Python plugin capability (`plugins/learned_<name>.py`) "
            "when the user asks JARVIS in chat to learn a new capability/tool that does not exist yet "
            "(e.g. a custom calculator, file organizer, API fetcher, clipboard transformer, text/data processor). "
            "Hot-loads immediately without restarting JARVIS, and can run it right away."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Unique English snake_case tool name (e.g. 'currency_converter', 'clipboard_clean')"},
                "description": {"type": "STRING", "description": "What this new capability does and when to use it"},
                "implementation_details": {"type": "STRING", "description": "Detailed specification of inputs, logic, and expected output"},
                "run_now_args": {"type": "OBJECT", "description": "Optional arguments dict to test/run the new capability immediately after creation"},
            },
            "required": ["name", "description"],
        },
        "handler": _learn_capability,
    },
    {
        "name": "run_learned_capability",
        "description": (
            "Execute a newly learned plugin capability by name in the current session "
            "(useful immediately after calling `learn_capability`)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "The tool name of the learned capability (e.g. 'currency_converter')"},
                "arguments": {"type": "OBJECT", "description": "Arguments dictionary to pass to the plugin's run() function"},
            },
            "required": ["name"],
        },
        "handler": _run_learned_capability,
    },
    {
        "name": "export_knowledge",
        "description": "Export ALL Jarvis knowledge (skills, memory, shortcuts) to a shareable .jarvis bundle on the Desktop.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": _export_knowledge,
    },
    {
        "name": "import_knowledge",
        "description": "Import a .jarvis bundle containing skills and memory.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Full path to the .jarvis file"},
                "merge_memory": {"type": "BOOLEAN", "description": "Whether to merge imported memory (default: false)"},
            },
            "required": ["path"],
        },
        "handler": _import_knowledge,
    },
]
