"""
actions/flow_action.py - Action Tool for JARVIS Automation Flow Engine.
Allows Gemini Live and the assistant to start, stop, list, create, explain, and export n8n-style flows.
"""

import os
import json
from pathlib import Path
from core.flow_engine import FlowEngine, AutomationFlow, FlowStep


def flow_control(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    flow_id = params.get("flow_id", "").strip()
    flow_name = params.get("flow_name", "").strip() or flow_id

    engine = FlowEngine.get_instance()

    if action in ("start", "run"):
        target = flow_id or flow_name
        if not target:
            flows = engine.list_flows()
            if flows:
                target = flows[0].id
            else:
                return "❌ لا توجد برامج أوتوميشن مسجلة حالياً."
        res = engine.start_flow(target, player=player)
        return res

    if action in ("stop", "halt", "kill"):
        target = flow_id or flow_name
        if not target:
            return engine.stop_all_flows(player=player)
        return engine.stop_flow(target, player=player)

    if action in ("list", "show"):
        flows = engine.list_flows()
        if not flows:
            return "لا توجد برامج أوتوميشن محفوظة حالياً. يمكنك إنشاء برنامج جديد عبر تاب FLOW MAP."
        lines = ["⚡ **قائمة برامج الأوتوميشن (Automation Flows):**"]
        for f in flows:
            status_icon = "🟢" if f.status == "RUNNING" else ("🟡" if f.status == "IDLE" else "🔴")
            lines.append(
                f"- {status_icon} **{f.name}** (`{f.id}`):\n"
                f"  • أمر البدء: `{f.start_command}` | أمر الإيقاف: `{f.stop_command}`\n"
                f"  • التكرار: كل {f.interval_seconds} ثانية | عدد الدورات: {f.run_count} | الحالة: {f.status}"
            )
        return "\n".join(lines)

    if action in ("explain", "info"):
        flow = engine.get_flow(flow_id or flow_name)
        if not flow:
            return f"❌ لم يتم العثور على البرنامج '{flow_id or flow_name}'."
        return flow.explanation or engine.generate_explanation(flow.to_dict())

    if action in ("export", "share"):
        flow = engine.get_flow(flow_id or flow_name)
        if not flow:
            return f"❌ لم يتم العثور على البرنامج '{flow_id or flow_name}' للتصدير."
        out_path = engine.export_flow(flow.id)
        return f"✅ تم تصدير البرنامج بنجاح كملف قابل للمشاركة على سطح المكتب:\n`{out_path}`"

    if action == "import":
        file_path = params.get("file_path", "")
        if not file_path:
            return "❌ يرجى تحديد مسار ملف .flow للاستيراد."
        ok, msg = engine.import_flow(file_path)
        return msg

    if action == "create":
        name = params.get("name", "برنامج مخصص")
        start_cmd = params.get("start_command", "")
        stop_cmd = params.get("stop_command", "")
        interval = int(params.get("interval", 60))
        steps_raw = params.get("steps", [])

        flow_slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
        new_flow = AutomationFlow(
            id=f"flow_{flow_slug}_{int(os.times()[4])}",
            name=name,
            description=params.get("description", ""),
            start_command=start_cmd,
            stop_command=stop_cmd,
            interval_seconds=interval,
            steps=[FlowStep.from_dict(s) if isinstance(s, dict) else s for s in steps_raw],
        )
        ok = engine.save_flow(new_flow)
        if ok:
            return (
                f"✅ تم إنشاء وحفظ برنامج الأوتوميشن بنجاح!\n"
                f"• الاسم: **{new_flow.name}**\n"
                f"• أمر البدء: `{new_flow.start_command}`\n"
                f"• أمر الإيقاف: `{new_flow.stop_command}`"
            )
        return "❌ فشل حفظ البرنامج."

    return f"Unknown flow_control action: '{action}'"


TOOL = {
    "name": "flow_control",
    "description": (
        "Controls and teaches recurring automation flow maps (n8n-style workflows). "
        "Allows starting a recurring task loop, stopping it via its stop command, listing existing flows, "
        "explaining how a flow works, exporting/importing .flow packages, or creating/teaching new flows "
        "directly from user descriptions in chat (e.g. when user says 'عايز اعمل برنامج جديد...')."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start | stop | list | explain | export | import | create"
            },
            "flow_id": {
                "type": "STRING",
                "description": "Unique flow ID or flow name (e.g. 'shopify_order_monitor')"
            },
            "flow_name": {
                "type": "STRING",
                "description": "Display name of the flow"
            },
            "file_path": {
                "type": "STRING",
                "description": "Path to .flow file for import"
            },
            "name": {
                "type": "STRING",
                "description": "When action='create': Human-friendly name of the automation flow (e.g. 'مراقبة طلبات شوبيفاي')"
            },
            "description": {
                "type": "STRING",
                "description": "When action='create': Brief summary of what this automation does"
            },
            "start_command": {
                "type": "STRING",
                "description": "When action='create': The exact command phrase the user will type/speak to start this flow (e.g. 'ابدأ مراقبة شوبيفاي')"
            },
            "stop_command": {
                "type": "STRING",
                "description": "When action='create': The command phrase the user will type/speak to halt this flow (e.g. 'وقف مراقبة شوبيفاي')"
            },
            "interval": {
                "type": "INTEGER",
                "description": "When action='create': Loop interval in seconds between runs (e.g. 60, 120, 300)"
            },
            "steps": {
                "type": "ARRAY",
                "description": (
                    "When action='create': Ordered list of step objects. Each object should have: "
                    "'action' (one of: 'check_condition', 'browser_control', 'refresh_page', 'click_element', 'computer_control', 'open_app', 'send_message', 'wait'), "
                    "'parameters' (dict matching the action: for check_condition {'target': 'shopify', 'check_type': 'is_open', 'on_met': 'skip_next'}; "
                    "for browser_control {'action': 'open_with_profile', 'profile_name': 'لويد فورنتيرا', 'bookmark': 'shopify', 'skip_if_open': True}; "
                    "for refresh_page {}; for click_element {'target': 'Orders button'}; for wait {'seconds': 5}; for computer_control {'action': 'read_screen', 'question': '...'}), "
                    "'label' (short title for this node)."
                ),
                "items": {
                    "type": "OBJECT"
                }
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": flow_control,
}
