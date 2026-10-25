import json
import zipfile
import time
from datetime import datetime
from pathlib import Path
from skills.skill_manager import SkillManager
from memory.memory_manager import load_memory

_ALLOWED_ACTIONS = {
    "open_app", "close_app", "browser_control", "computer_control",
    "computer_settings", "send_message", "file_processor", "file_controller",
    "screen_grounding", "vision_interact", "discord_control", "shopify_work",
    "goal_agent", "flow_control", "web_search", "system_status"
}

def _natural_to_steps(description: str) -> list[dict]:
    """Convert natural language skill description to structured steps using Gemini."""
    from core import gemini
    prompt = f"""Convert this Arabic task description into a JSON list of skill steps for a Windows AI assistant.

Task: {description}

Available actions: open_app, browser_control, computer_control, send_message, computer_settings, file_processor, discord_control, goal_agent

Reply ONLY with a JSON array like:
[
  {{"action": "open_app", "params": {{"app_name": "chrome"}}, "description": "افتح المتصفح", "wait_after_ms": 1000}},
  {{"action": "browser_control", "params": {{"action": "navigate", "url": "https://youtube.com"}}, "description": "افتح يوتيوب", "wait_after_ms": 2000}}
]"""
    result = gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=15000)
    if isinstance(result, list):
        return result
    return []

def _teach_skill(parameters: dict, **kwargs) -> str:
    name = parameters.get("name")
    display_name = parameters.get("display_name", name)
    description = parameters.get("description", "")
    
    steps = _natural_to_steps(description)
    if not steps:
        return "❌ فشل في تحويل الوصف إلى خطوات باستخدام الذكاء الاصطناعي."
        
    sm = SkillManager()
    skill = sm.teach_skill(
        name=name,
        display_name=display_name,
        description=description,
        steps=steps,
        parameters=parameters.get("parameters", []),
        required_packages=parameters.get("required_packages", []),
        tags=parameters.get("tags", [])
    )
    return f"✅ تم تعلم المهارة '{skill.display_name}' بنجاح! تحتوي على {len(skill.steps)} خطوة."

def _run_skill(parameters: dict, **kwargs) -> str:
    name = parameters.get("name")
    skill_params = parameters.get("parameters", {})
    sm = SkillManager()
    return sm.execute_skill(name, skill_params)

def _list_skills(parameters: dict, **kwargs) -> str:
    sm = SkillManager()
    return sm.list_skills_summary()

def _delete_skill(parameters: dict, **kwargs) -> str:
    name = parameters.get("name")
    sm = SkillManager()
    return sm.delete_skill(name)

def _skill_info(parameters: dict, **kwargs) -> str:
    name = parameters.get("name")
    sm = SkillManager()
    skill = sm.get_skill(name)
    if not skill:
        return f"❌ لم يتم إيجاد مهارة باسم '{name}'."
    
    info = [f"**{skill.display_name}** ({skill.name})", f"الوصف: {skill.description}"]
    info.append(f"الخطوات: {len(skill.steps)}")
    for i, step in enumerate(skill.steps, 1):
        info.append(f"  {i}. {step.action} - {step.description}")
    
    return "\n".join(info)

def _export_knowledge(parameters: dict, **kwargs) -> str:
    """
    Exports ALL Jarvis knowledge to a .jarvis bundle (ZIP):
    - All user skills (skills/user_skills/*.json)
    - Long-term memory snapshot (memory/long_term.json) — excluding sessions
    - Quick shortcuts (config/shortcuts.json)
    - A manifest.json with metadata
    - A README.md
    
    Output: Desktop/<username>_jarvis_knowledge_<date>.jarvis
    """
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
        
    from memory.memory_manager import BASE_DIR
    shortcuts_path = BASE_DIR / "config" / "shortcuts.json"
    shortcuts_data = {}
    if shortcuts_path.exists():
        try:
            shortcuts_data = json.loads(shortcuts_path.read_text(encoding="utf-8"))
        except:
            pass
            
    manifest = {
        "version": "1.0",
        "exported_at": datetime.now().isoformat(),
        "skills_count": len(skills),
        "author": username
    }
    
    readme = f"# Jarvis Knowledge Bundle\n\nExported on {manifest['exported_at']} by {manifest['author']}.\nContains {manifest['skills_count']} skills, long-term memory, and shortcuts."
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:

        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("memory.json", json.dumps(memory_data, ensure_ascii=False, indent=2))
        zf.writestr("shortcuts.json", json.dumps(shortcuts_data, ensure_ascii=False, indent=2))
        zf.writestr("README.md", readme)
        
        for skill in skills:
            zf.writestr(f"skills/{skill.name}.json", json.dumps(skill.to_dict(), ensure_ascii=False, indent=2))
            
    return f"✅ تم تصدير المعرفة بنجاح إلى: {output_path}"

def _import_knowledge(parameters: dict, **kwargs) -> str:
    """
    Imports a .jarvis bundle:
    1. Validates ZIP structure (must have manifest.json)
    2. Checks version compatibility
    3. SECURITY: validates skill actions against ALLOWED_ACTIONS whitelist
    4. Merges skills (doesn't overwrite, bumps version if conflict)
    5. Optionally merges memory (user can choose)
    6. Reports what was imported
    """
    path_str = parameters.get("path")
    if not path_str:
        return "❌ يرجى توفير مسار ملف .jarvis للاستيراد."
        
    path = Path(path_str.strip('"\''))
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
                
                # Check actions against whitelist
                safe = True
                for step in skill_data.get("steps", []):
                    if step.get("action") not in _ALLOWED_ACTIONS:
                        safe = False
                        break
                        
                if not safe:
                    continue # Skip unsafe skills
                    
                from skills.skill_base import SkillDefinition
                skill = SkillDefinition.from_dict(skill_data)
                
                existing = sm.get_skill(skill.name)
                if existing:
                    # bump version
                    from skills.skill_manager import _bump_version
                    skill.version = _bump_version(existing.version)
                
                sm._save_skill(skill)
                imported_skills += 1
                
            # Optionally import memory
            merge_memory = parameters.get("merge_memory", False)
            if merge_memory and "memory.json" in zf.namelist():
                memory_data = json.loads(zf.read("memory.json").decode("utf-8"))
                from memory.memory_manager import update_memory
                update_memory(memory_data)
                
    except Exception as e:
        return f"❌ خطأ أثناء الاستيراد: {str(e)}"
        
    return f"✅ تم استيراد المعرفة بنجاح! تم إضافة {imported_skills} مهارة."


TOOLS = [
    {
        "name": "teach_skill",
        "description": "Teach JARVIS a new skill from a natural language description.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Short internal name for the skill (e.g. 'open_my_email')"},
                "display_name": {"type": "STRING", "description": "Readable name (e.g. 'افتح ايميلي')"},
                "description": {"type": "STRING", "description": "Full natural language description of what to do (e.g. 'افتح المتصفح وادخل على الجيميل واستنى ثانيتين')"},
            },
            "required": ["name", "display_name", "description"]
        },
        "handler": _teach_skill
    },
    {
        "name": "run_skill",
        "description": "Execute a saved skill by name.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name of the skill to execute"},
                "parameters": {"type": "OBJECT", "description": "Optional parameters to pass to the skill"}
            },
            "required": ["name"]
        },
        "handler": _run_skill
    },
    {
        "name": "list_skills",
        "description": "List all saved skills and their descriptions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        },
        "handler": _list_skills
    },
    {
        "name": "delete_skill",
        "description": "Delete a saved skill.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name of the skill to delete"}
            },
            "required": ["name"]
        },
        "handler": _delete_skill
    },
    {
        "name": "skill_info",
        "description": "Get detailed info and steps for a specific skill.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Name of the skill"}
            },
            "required": ["name"]
        },
        "handler": _skill_info
    },
    {
        "name": "export_knowledge",
        "description": "Export ALL Jarvis knowledge to a shareable .jarvis bundle on the Desktop.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        },
        "handler": _export_knowledge
    },
    {
        "name": "import_knowledge",
        "description": "Import a .jarvis bundle containing skills and memory.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Full path to the .jarvis file"},
                "merge_memory": {"type": "BOOLEAN", "description": "Whether to merge imported memory (default: false)"}
            },
            "required": ["path"]
        },
        "handler": _import_knowledge
    }
]
