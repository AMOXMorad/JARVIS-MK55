"""
core/flow_engine.py - Autonomous Automation Flow Map Engine (n8n-style) for JARVIS Mark LIV.

Allows defining multi-step automation workflows that run continuously in the background,
each triggered by a specific Start Command in chat and halted cleanly by a Stop Command.
Includes visual builder support, step explainability, and portable .flow import/export.
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Any


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_flows_dir() -> Path:
    d = _get_base_dir() / "config" / "flows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_cmd(text: str) -> str:
    """Normalize command text for robust fuzzy/phrase matching."""
    if not text:
        return ""
    t = text.lower().strip()
    # Normalize common Arabic characters
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    # Remove punctuation
    for p in ".,!?;:\"'()[]{}«»-_\n\r\t":
        t = t.replace(p, " ")
    return " ".join(t.split())


def _is_target_open(target: str) -> bool:
    """Check if a window or app with title/name matching target is currently open/running."""
    if not target:
        return False
    target_clean = _normalize_cmd(target).lower().strip()
    target_raw = target.lower().strip()

    # 1. Check top-level visible window titles via Win32 EnumWindows
    try:
        import ctypes
        import win32gui
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)

        matched = []
        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title:
                    norm_title = _normalize_cmd(title).lower()
                    if target_raw in title.lower() or (target_clean and target_clean in norm_title):
                        matched.append(title)
        win32gui.EnumWindows(enum_cb, None)
        if matched:
            return True
    except Exception as e:
        print(f"[FlowEngine] Window enum check failed: {e}")

    # 2. Check running process names via psutil
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if target_raw in name or (target_clean and target_clean in name):
                return True
    except Exception:
        pass

    return False


@dataclass
class FlowStep:
    action: str                       # e.g. "browser_control", "computer_control", "open_app", "send_message", "wait"
    parameters: dict = field(default_factory=dict)
    label: str = ""
    condition: str = ""              # optional check
    on_failure: str = "continue"      # "continue" | "stop" | "retry"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> FlowStep:
        return cls(
            action=data.get("action", "wait"),
            parameters=data.get("parameters", {}),
            label=data.get("label", ""),
            condition=data.get("condition", ""),
            on_failure=data.get("on_failure", "continue"),
        )


@dataclass
class AutomationFlow:
    id: str
    name: str
    description: str = ""
    start_command: str = ""          # e.g. "ابدأ مراقبة شوبيفاي" or "start shopify monitor"
    stop_command: str = ""           # e.g. "وقف مراقبة شوبيفاي" or "stop shopify monitor"
    interval_seconds: int = 60       # loop interval
    steps: list[FlowStep] = field(default_factory=list)
    explanation: str = ""            # auto-generated plain text explanation
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    run_count: int = 0
    status: str = "IDLE"             # "IDLE" | "RUNNING" | "STOPPED" | "ERROR"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [s.to_dict() if isinstance(s, FlowStep) else s for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> AutomationFlow:
        raw_steps = data.get("steps", [])
        steps = [FlowStep.from_dict(s) if isinstance(s, dict) else s for s in raw_steps]
        return cls(
            id=data.get("id", f"flow_{int(time.time())}"),
            name=data.get("name", "Untitled Flow"),
            description=data.get("description", ""),
            start_command=data.get("start_command", ""),
            stop_command=data.get("stop_command", ""),
            interval_seconds=int(data.get("interval_seconds", 60)),
            steps=steps,
            explanation=data.get("explanation", ""),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at", time.time())),
            last_run=float(data.get("last_run", 0.0)),
            run_count=int(data.get("run_count", 0)),
            status=data.get("status", "IDLE"),
        )


class FlowEngine:
    """
    Autonomous Execution Engine for Automation Flow Maps.
    Manages background loops, start/stop command triggers from chat,
    and portable flow import/export.
    """
    _instance: Optional[FlowEngine] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> FlowEngine:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._flows_dir = _get_flows_dir()
        self._active_threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._status_listeners: list[Callable[[str, str], None]] = []
        self._init_defaults()

    def _init_defaults(self):
        """No hardcoded default flows — user defines custom flows."""
        pass

    def add_status_listener(self, callback: Callable[[str, str], None]):
        """Register a callback for flow state updates: cb(flow_id, status)."""
        if callback not in self._status_listeners:
            self._status_listeners.append(callback)

    def _notify_status(self, flow_id: str, status: str):
        for cb in self._status_listeners:
            try:
                cb(flow_id, status)
            except Exception:
                pass

    # ── CRUD operations ──────────────────────────────────────────────────────

    def list_flows(self) -> list[AutomationFlow]:
        flows: list[AutomationFlow] = []
        for p in self._flows_dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                flow = AutomationFlow.from_dict(data)
                # reflect running status
                if flow.id in self._stop_events and not self._stop_events[flow.id].is_set():
                    flow.status = "RUNNING"
                else:
                    if flow.status == "RUNNING":
                        flow.status = "IDLE"
                flows.append(flow)
            except Exception as e:
                print(f"[FlowEngine] Error loading flow {p}: {e}")
        flows.sort(key=lambda x: x.created_at)
        return flows

    def get_flow(self, flow_id: str) -> Optional[AutomationFlow]:
        path = self._flows_dir / f"{flow_id}.json"
        if not path.exists():
            # Try fuzzy match on name or ID
            for f in self.list_flows():
                if f.id.lower() == flow_id.lower() or f.name.lower() == flow_id.lower():
                    return f
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            flow = AutomationFlow.from_dict(data)
            if flow.id in self._stop_events and not self._stop_events[flow.id].is_set():
                flow.status = "RUNNING"
            return flow
        except Exception:
            return None

    def save_flow(self, flow: AutomationFlow) -> bool:
        if not flow.explanation and flow.steps:
            flow.explanation = self.generate_explanation(flow.to_dict())
        path = self._flows_dir / f"{flow.id}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flow.to_dict(), f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[FlowEngine] Save flow failed: {e}")
            return False

    def delete_flow(self, flow_id: str) -> bool:
        self.stop_flow(flow_id)
        path = self._flows_dir / f"{flow_id}.json"
        if path.exists():
            try:
                path.unlink()
                return True
            except Exception:
                return False
        return False

    # ── Explainability ───────────────────────────────────────────────────────

    def generate_explanation(self, flow_dict: dict) -> str:
        """Synthesize a human-readable explanation of what the flow does."""
        name = flow_dict.get("name", "برنامج الأوتوميشن")
        steps = flow_dict.get("steps", [])
        interval = flow_dict.get("interval_seconds", 60)
        start_cmd = flow_dict.get("start_command", "")
        stop_cmd = flow_dict.get("stop_command", "")

        lines = [
            f"📋 **شرح برنامج: {name}**",
            f"• **أمر التشغيل**: `{start_cmd}` (يكتبه المستخدم في الشات لبدء الحلقة).",
            f"• **أمر الإيقاف**: `{stop_cmd}` (يكتبه المستخدم في أي وقت لإنهاء الحلقة فوراً).",
            f"• **الفاصل الزمني**: يتكرر التنفيذ كل {interval} ثانية.",
            "",
            "**خطوات التنفيذ (Nodes)**:",
        ]

        for i, s in enumerate(steps, 1):
            act = s.get("action", "") if isinstance(s, dict) else s.action
            params = s.get("parameters", {}) if isinstance(s, dict) else s.parameters
            label = s.get("label", "") if isinstance(s, dict) else s.label
            lbl_text = f" ({label})" if label else ""

            if act == "browser_control":
                sub_act = params.get("action", "")
                prof = params.get("profile_name", "")
                bm = params.get("bookmark", "")
                url = params.get("url", "")
                detail = f"المتصفح [{sub_act}]"
                if prof:
                    detail += f" بحساب: {prof}"
                if bm:
                    detail += f" - إشارة مرجعية: {bm}"
                if url:
                    detail += f" - رابط: {url}"
                if params.get("skip_if_open"):
                    detail += " (مع تخطي الفتح إذا كان مفتوحاً بالفعل)"
                lines.append(f"{i}. 🌐 **تحكم المتصفح**{lbl_text}: {detail}")
            elif act in ("refresh_page", "reload"):
                lines.append(f"{i}. 🔄 **تحديث الصفحة (Refresh)**{lbl_text}: إعادة تحميل الصفحة النشطة في المتصفح")
            elif act in ("click_element", "click_button", "click"):
                tgt = params.get("target") or params.get("description") or params.get("selector") or ""
                lines.append(f"{i}. 👆 **نقر على عنصر/زر**{lbl_text}: الضغط على '{tgt}' عبر الرؤية الذكية")
            elif act == "check_condition":
                tgt = params.get("target", "")
                chk = params.get("check_type", "is_open")
                on_m = params.get("on_met", "skip_next")
                chk_lbl = "مفتوحاً حالياً" if chk == "is_open" else "ظاهراً على الشاشة"
                on_lbl = "تخطي الخطوة التالية ⏩" if on_m == "skip_next" else "إيقاف البرنامج 🛑"
                lines.append(f"{i}. 🔍 **تحقق وشرط**{lbl_text}: فحص هل '{tgt}' {chk_lbl}؟ إذا نعم ➔ {on_lbl}")
            elif act == "computer_control":
                c_act = params.get("action", "")
                desc = params.get("description", "") or params.get("question", "") or params.get("text", "")
                lines.append(f"{i}. 👁️ **تحكم النظام والرؤية**{lbl_text}: إجراء [{c_act}] - {desc}")
            elif act == "open_app":
                app = params.get("app_name", "")
                inst = params.get("instruction", "")
                detail = f"{app} ({inst})" if inst else app
                if params.get("skip_if_open"):
                    detail += " (مع تخطي الفتح إذا كان قيد التشغيل)"
                lines.append(f"{i}. 🚀 **فتح تطبيق**{lbl_text}: {detail}")
            elif act == "send_message":
                target = params.get("target", "")
                lines.append(f"{i}. 💬 **إرسال رسالة**{lbl_text}: إلى {target}")
            elif act == "wait":
                secs = params.get("seconds", 1)
                lines.append(f"{i}. ⏱️ **انتظار**{lbl_text}: {secs} ثوانٍ")
            else:
                lines.append(f"{i}. ⚙️ **إجراء**{lbl_text}: {act} مع المعاملات {params}")

        return "\n".join(lines)

    # ── Execution Loop ───────────────────────────────────────────────────────

    def start_flow(self, flow_id: str, player=None) -> str:
        flow = self.get_flow(flow_id)
        if not flow:
            return f"❌ Flow '{flow_id}' not found."

        if flow.id in self._stop_events and not self._stop_events[flow.id].is_set():
            return f"⚠️ Flow '{flow.name}' is already running."

        stop_event = threading.Event()
        self._stop_events[flow.id] = stop_event

        thread = threading.Thread(
            target=self._run_loop,
            args=(flow, stop_event, player),
            daemon=True,
            name=f"FlowWorker-{flow.id}",
        )
        self._active_threads[flow.id] = thread
        thread.start()

        flow.status = "RUNNING"
        self._notify_status(flow.id, "RUNNING")
        if player:
            player.write_log(f"[flow] 🚀 Started: {flow.name}")

        return (
            f"🚀 **تم تشغيل برنامج الأوتوميشن بنجاح!**\n"
            f"• الاسم: **{flow.name}**\n"
            f"• الفاصل الزمني: كل {flow.interval_seconds} ثانية\n"
            f"• لإيقاف البرنامج في أي وقت، اكتب: `{flow.stop_command}`"
        )

    def stop_flow(self, flow_id: str, player=None) -> str:
        flow = self.get_flow(flow_id)
        name = flow.name if flow else flow_id

        if flow_id in self._stop_events:
            self._stop_events[flow_id].set()
            self._stop_events.pop(flow_id, None)
            self._active_threads.pop(flow_id, None)
            if flow:
                flow.status = "STOPPED"
                self.save_flow(flow)
            self._notify_status(flow_id, "STOPPED")
            if player:
                player.write_log(f"[flow] 🛑 Stopped: {name}")
            return f"🛑 **تم إيقاف برنامج الأوتوميشن [{name}] بنجاح.**"

        return f"ℹ️ Flow '{name}' is not currently running."

    def stop_all_flows(self, player=None) -> str:
        stopped = []
        for fid in list(self._stop_events.keys()):
            self.stop_flow(fid, player=player)
            stopped.append(fid)
        return f"🛑 Stopped {len(stopped)} running flows."

    def is_running(self, flow_id: str) -> bool:
        return flow_id in self._stop_events and not self._stop_events[flow_id].is_set()

    def _run_loop(self, flow: AutomationFlow, stop_event: threading.Event, player=None):
        """Worker thread executing the recurring automation flow."""
        print(f"[FlowEngine] Starting loop for [{flow.name}] (Interval: {flow.interval_seconds}s)")
        cycle = 0

        while not stop_event.is_set():
            cycle += 1
            flow.run_count += 1
            flow.last_run = time.time()
            self.save_flow(flow)

            log_msg = f"[flow] ▶ {flow.name} (Cycle #{cycle})"
            print(f"[FlowEngine] {log_msg}")
            if player:
                player.write_log(log_msg)

            # Execute all steps in sequence with skip_next support
            skip_next = False
            for idx, step in enumerate(flow.steps, 1):
                if stop_event.is_set():
                    break

                # 1. Skip if previous condition requested skip_next
                if skip_next:
                    skip_next = False
                    skip_log = f"[step] ⏩ تخطي الخطوة #{idx} ({step.label or step.action}) بناءً على نتيجة التحقق السابقة."
                    print(f"[FlowEngine] {skip_log}")
                    if player:
                        player.write_log(skip_log)
                    continue

                # 2. Built-in pre-condition check on the step itself (skip_if_open)
                cond = (step.condition or "").strip().lower()
                params = step.parameters or {}
                if cond.startswith("skip_if_open") or params.get("skip_if_open"):
                    target_check = ""
                    if ":" in cond:
                        target_check = cond.split(":", 1)[1].strip()
                    else:
                        target_check = (
                            params.get("bookmark")
                            or params.get("app_name")
                            or params.get("url")
                            or params.get("profile_name")
                            or ""
                        )
                    if target_check and _is_target_open(target_check):
                        skip_log = f"[step] ⏩ تخطي الخطوة #{idx} ({step.label or step.action}) لأن '{target_check}' مفتوح بالفعل."
                        print(f"[FlowEngine] {skip_log}")
                        if player:
                            player.write_log(skip_log)
                        continue

                # 3. Execute step
                try:
                    res = self._execute_step(step, player)

                    # 4. Handle check_condition step outcome
                    if step.action == "check_condition" and isinstance(res, dict):
                        is_met = bool(res.get("met", False))
                        target_name = params.get("target", "")
                        on_met = params.get("on_met", "skip_next")
                        if is_met:
                            cond_msg = f"[condition] ✅ تحقق الشرط: '{target_name}' مفتوح/ظاهر بالفعل."
                            if on_met == "skip_next":
                                skip_next = True
                                cond_msg += " (سيتم تخطي الخطوة التالية ⏩)"
                            elif on_met == "stop":
                                cond_msg += " (تم إيقاف البرنامج بناءً على الشرط 🛑)"
                                stop_event.set()
                            if player:
                                player.write_log(cond_msg)
                            if on_met == "stop":
                                break
                        else:
                            cond_msg = f"[condition] ℹ️ الشرط لم يتحقق: '{target_name}' غير مفتوح (سيتم تنفيذ الخطوة التالية بشكل طبيعي)."
                            if on_met == "skip_if_false":
                                skip_next = True
                                cond_msg += " (تخطي الخطوة التالية ⏩)"
                            if player:
                                player.write_log(cond_msg)

                except Exception as ex:
                    print(f"[FlowEngine] Step #{idx} ({step.label or step.action}) failed: {ex}")
                    if step.on_failure == "stop":
                        print(f"[FlowEngine] Halting flow on error as requested.")
                        stop_event.set()
                        break

            # Sleep interval in small increments to allow instant responsive cancellation
            remaining = flow.interval_seconds
            while remaining > 0 and not stop_event.is_set():
                sleep_chunk = min(1.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk

        print(f"[FlowEngine] Loop finished for [{flow.name}]")
        flow.status = "IDLE"
        self.save_flow(flow)
        self._notify_status(flow.id, "IDLE")

    def _execute_step(self, step: FlowStep, player=None):
        """Dispatch a single flow step to JARVIS's existing action tools."""
        act = step.action.lower().strip()
        params = step.parameters or {}

        if player and step.label:
            player.write_log(f"[step] {step.label}")

        if act == "wait":
            time.sleep(float(params.get("seconds", 1)))
            return None

        if act == "check_condition":
            target = params.get("target", "").strip()
            chk_type = params.get("check_type", "is_open")
            is_met = False
            if chk_type == "is_open":
                is_met = _is_target_open(target)
            elif chk_type == "is_visible":
                try:
                    from actions.computer_control import computer_control
                    res = computer_control({
                        "action": "read_screen",
                        "question": f"Is '{target}' visible on screen right now? Answer with only YES or NO."
                    })
                    is_met = "yes" in str(res).lower() or "نعم" in str(res)
                except Exception:
                    is_met = False
            return {"action": "check_condition", "target": target, "met": is_met}

        if act in ("refresh_page", "reload"):
            # Try browser_control reload
            try:
                from actions.browser_control import browser_control
                browser_control({"action": "reload"}, player=player)
                return "reloaded"
            except Exception:
                pass
            # Fallback: Win32 F5 hotkey to active window
            try:
                import ctypes
                user32 = ctypes.windll.user32
                VK_F5 = 0x74
                user32.keybd_event(VK_F5, 0, 0, 0)
                time.sleep(0.05)
                user32.keybd_event(VK_F5, 0, 2, 0)
                return "f5_sent"
            except Exception:
                pass
            return "refresh_failed"

        if act in ("click_element", "click_button", "click"):
            target = params.get("target", "") or params.get("description", "") or params.get("selector", "")
            # Try browser_control selector click
            if params.get("selector"):
                try:
                    from actions.browser_control import browser_control
                    browser_control({"action": "click", "selector": params["selector"]}, player=player)
                    return "browser_clicked"
                except Exception:
                    pass
            # Fallback: Visual find_and_click via Gemini Vision
            from actions.computer_control import computer_control
            return computer_control({"action": "find_and_click", "description": target}, player=player)

        if act == "browser_control":
            from actions.browser_control import browser_control
            return browser_control(params, player=player)

        if act == "computer_control":
            from actions.computer_control import computer_control
            return computer_control(params, player=player)

        if act == "open_app":
            from actions.open_app import open_app
            return open_app(params, player=player)

        if act == "send_message":
            from actions.send_message import send_message
            return send_message(params, player=player)

        if act == "screen_processor":
            from actions.screen_processor import screen_process
            return screen_process(params, player=player)

        print(f"[FlowEngine] Unknown step action: {act}")
        return None

    # ── Chat Command Interception ────────────────────────────────────────────

    def check_chat_command(self, user_text: str) -> Optional[tuple[str, str, AutomationFlow]]:
        """
        Check if the incoming user text matches ANY registered flow's start or stop command.
        Returns: (type: 'start' | 'stop', flow_id, flow) or None.
        """
        if not user_text:
            return None

        norm_user = _normalize_cmd(user_text)
        flows = self.list_flows()

        for flow in flows:
            # Check stop command first
            if flow.stop_command:
                norm_stop = _normalize_cmd(flow.stop_command)
                if norm_stop and (norm_stop in norm_user or norm_user in norm_stop):
                    return ("stop", flow.id, flow)

            # Check start command
            if flow.start_command:
                norm_start = _normalize_cmd(flow.start_command)
                if norm_start and (norm_start in norm_user or norm_user in norm_start):
                    return ("start", flow.id, flow)

        return None

    # ── Import & Export ──────────────────────────────────────────────────────

    def export_flow(self, flow_id: str, dest_dir: str | Path | None = None) -> Path:
        """Export flow as a portable .flow package (JSON + README markdown)."""
        flow = self.get_flow(flow_id)
        if not flow:
            raise ValueError(f"Flow '{flow_id}' not found.")

        target_dir = Path(dest_dir) if dest_dir else (Path.home() / "Desktop")
        target_dir.mkdir(parents=True, exist_ok=True)

        slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in flow.name)
        out_file = target_dir / f"{slug}.flow"
        readme_file = target_dir / f"{slug}_EXPLANATION.md"

        payload = {
            "version": "1.0",
            "type": "jarvis_automation_flow",
            "exported_at": time.time(),
            "flow": flow.to_dict(),
        }

        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        # Write rich human-readable explanation
        explanation_content = (
            f"# ⚡ JARVIS Automation Flow: {flow.name}\n\n"
            f"> {flow.description}\n\n"
            f"### 🎯 Triggers & Commands:\n"
            f"- **أمر البدء (Start Command)**: `{flow.start_command}`\n"
            f"- **أمر الإيقاف (Stop Command)**: `{flow.stop_command}`\n"
            f"- **الفاصل الزمني (Interval)**: كل {flow.interval_seconds} ثانية\n\n"
            f"### 📋 شرح الخطوات التفصيلية:\n"
            f"{flow.explanation or self.generate_explanation(flow.to_dict())}\n\n"
            f"### 📥 كيفية الاستيراد (How to Import):\n"
            f"1. افتح نافذة **FLOW MAP** في جارفيس.\n"
            f"2. اضغط على زر **IMPORT .FLOW** واختر هذا الملف (`{out_file.name}`).\n"
            f"3. سيتعرف جارفيس على أوامر البدء والإيقاف فوراً ويمكنك تشغيله من الشات مباشرة!\n"
        )
        with open(readme_file, "w", encoding="utf-8") as f:
            f.write(explanation_content)

        return out_file

    def import_flow(self, file_path: str | Path) -> tuple[bool, str]:
        """Import a foreign .flow or .json workflow file."""
        p = Path(file_path)
        if not p.exists():
            return False, f"File does not exist: {p}"

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_flow = data.get("flow", data)
            flow = AutomationFlow.from_dict(raw_flow)
            # Ensure unique ID to avoid accidental overwrite
            if (self._flows_dir / f"{flow.id}.json").exists():
                flow.id = f"{flow.id}_{int(time.time())}"

            self.save_flow(flow)
            return True, f"✅ Imported flow '{flow.name}' with commands: [{flow.start_command}] / [{flow.stop_command}]"
        except Exception as e:
            return False, f"Failed to import flow: {e}"
