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


def _candidate_targets(target: str, params: dict | None = None) -> list[str]:
    """Build a list of strings to match against open windows/processes/tabs."""
    from urllib.parse import urlparse
    params = params or {}
    cands: list[str] = []

    def _add(v: str):
        v = (v or "").strip()
        if v and v.lower() not in [x.lower() for x in cands]:
            cands.append(v)

    _add(target)

    # Bilingual keyword expansion (e.g. شوبيفاي -> shopify)
    try:
        from actions.browser_control import _ARABIC_BOOKMARK_KEYWORDS, _lookup_chrome_bookmark, _resolve_chrome_profile
        for w in (target or "").lower().split():
            if w in _ARABIC_BOOKMARK_KEYWORDS:
                _add(_ARABIC_BOOKMARK_KEYWORDS[w])
        prof_q = params.get("profile_name") or params.get("profile") or target
        prof_dir, disp_name = _resolve_chrome_profile(prof_q)
        if params.get("profile_name") and disp_name and disp_name != "Default":
            _add(disp_name)
            _add(prof_dir)
        bm_q = params.get("bookmark") or target
        if bm_q:
            bm = _lookup_chrome_bookmark(prof_dir, bm_q)
            if bm:
                bm_url, bm_title = bm[0], bm[1]
                _add(bm_title)
                _add(bm_url)
    except Exception:
        pass

    # Extract domain stem from any URL in candidates or params
    for u in list(cands) + [params.get("url", "")]:
        if u and ("." in u or "://" in u):
            try:
                parsed = urlparse(u if "://" in u else f"https://{u}")
                host = (parsed.hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    _add(host)
                    for seg in host.split("."):
                        if len(seg) >= 3 and seg not in ("com", "net", "org", "edu", "gov", "www", "co", "io", "app"):
                            _add(seg)
            except Exception:
                pass
    return cands


def _is_target_open(target: str, params: dict | None = None) -> bool:
    """Check if a window, browser tab, profile, or app matching target is currently open."""
    cands = _candidate_targets(target, params)
    if not cands:
        return False

    norm_cands = [(_c.lower().strip(), _normalize_cmd(_c).lower().strip()) for _c in cands if _c.strip()]

    # 1. Check top-level visible window titles via Win32 EnumWindows
    try:
        import ctypes
        import win32gui

        matched = []
        def _scan_windows():
            user32 = ctypes.windll.user32
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
            ctypes.windll.kernel32.SetLastError(0)

            def enum_cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd).strip()
                    if title:
                        tl = title.lower()
                        nt = _normalize_cmd(title).lower()
                        for raw_c, clean_c in norm_cands:
                            if (raw_c and raw_c in tl) or (clean_c and len(clean_c) >= 2 and clean_c in nt):
                                matched.append(title)
                                return
            win32gui.EnumWindows(enum_cb, None)

        t = threading.Thread(target=_scan_windows, daemon=True)
        t.start()
        t.join(timeout=1.0)
        if matched:
            return True
    except Exception as e:
        print(f"[FlowEngine] Window enum check failed: {e}")

    # 2. Check open Chrome tabs via DevTools HTTP endpoint (catches background tabs)
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=0.6) as resp:
            tabs = json.loads(resp.read().decode("utf-8", errors="ignore"))
        for tab in tabs if isinstance(tabs, list) else []:
            hay = f"{tab.get('title', '')} {tab.get('url', '')}".lower()
            nhay = _normalize_cmd(hay).lower()
            for raw_c, clean_c in norm_cands:
                if (raw_c and raw_c in hay) or (clean_c and len(clean_c) >= 2 and clean_c in nhay):
                    return True
    except Exception:
        pass

    # 3. Check running process names and command lines (e.g. --profile-directory=Profile 2)
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            for raw_c, clean_c in norm_cands:
                if raw_c and (raw_c in name or f"--profile-directory={raw_c}" in cmd):
                    return True
                if clean_c and len(clean_c) >= 3 and clean_c in name:
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
    interval_seconds: int = 0        # 0 = run once (no repeat); >0 = repeat every N seconds
    steps: list[FlowStep] = field(default_factory=list)
    explanation: str = ""            # auto-generated plain text explanation
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    run_count: int = 0
    status: str = "IDLE"             # "IDLE" | "RUNNING" | "STOPPED" | "ERROR"
    # Visual node map (n8n-style). Empty → legacy linear `steps` flow.
    #   {"nodes": [{"id", "type": "trigger"|"action", "x", "y", "step": {...}}],
    #    "edges": [{"from", "to", "branch": "main"|"true"|"false"}]}
    graph: dict = field(default_factory=dict)

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
            interval_seconds=max(0, int(data.get("interval_seconds", 0) or 0)),
            steps=steps,
            explanation=data.get("explanation", ""),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at", time.time())),
            last_run=float(data.get("last_run", 0.0)),
            run_count=int(data.get("run_count", 0)),
            status=data.get("status", "IDLE"),
            graph=data.get("graph") or {},
        )


def linearize_graph(graph: dict) -> list[dict]:
    """Flatten a node map into an ordered step list (BFS from the trigger).

    Used for the library card counts, the plain-text explanation and for
    backward compatibility with tools that only understand `steps`.
    """
    nodes = {n.get("id"): n for n in (graph or {}).get("nodes", []) if n.get("id")}
    edges = [e for e in (graph or {}).get("edges", [])
             if e.get("from") in nodes and e.get("to") in nodes]
    out: dict[str, list[str]] = {}
    indeg: dict[str, int] = {nid: 0 for nid in nodes}
    for e in edges:
        out.setdefault(e["from"], []).append(e["to"])
        indeg[e["to"]] = indeg.get(e["to"], 0) + 1
    pos = lambda nid: (float(nodes[nid].get("y", 0)), float(nodes[nid].get("x", 0)))
    starts = [nid for nid, n in nodes.items() if n.get("type") == "trigger"]
    if not starts:
        starts = sorted([nid for nid in nodes if indeg.get(nid, 0) == 0],
                        key=lambda k: (float(nodes[k].get("x", 0)), float(nodes[k].get("y", 0))))
    order, seen, queue = [], set(), list(starts)
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        order.append(nid)
        queue.extend(sorted(out.get(nid, []), key=pos))
    # Unconnected nodes are kept at the end so nothing silently disappears.
    order += [nid for nid in nodes if nid not in seen]
    return [nodes[nid].get("step", {}) for nid in order
            if nodes[nid].get("type") != "trigger" and nodes[nid].get("step")]


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
        self._node_listeners: list[Callable[[str, str, str], None]] = []
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

    def add_node_listener(self, callback: Callable[[str, str, str], None]):
        """Live per-node progress: cb(flow_id, node_id, state).

        state is 'running' | 'ok' | 'error' | 'skipped'; node_id '__cycle__'
        with state 'start' marks the beginning of a new pass over the map.
        """
        if callback not in self._node_listeners:
            self._node_listeners.append(callback)

    def _notify_node(self, flow_id: str, node_id: str, state: str):
        for cb in self._node_listeners:
            try:
                cb(flow_id, node_id, state)
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
        graph = flow_dict.get("graph") or {}
        if graph.get("nodes"):
            steps = linearize_graph(graph)
        interval = int(flow_dict.get("interval_seconds", 0) or 0)
        start_cmd = flow_dict.get("start_command", "")
        stop_cmd = flow_dict.get("stop_command", "")

        mode_line = (
            f"• **نظام التشغيل**: 🔄 يتكرر تلقائياً كل {interval} ثانية."
            if interval > 0
            else "• **نظام التشغيل**: ⚡ يعمل مرة واحدة فقط عند الطلب (بدون تكرار)."
        )
        lines = [
            f"📋 **شرح برنامج: {name}**",
            f"• **أمر التشغيل**: `{start_cmd}`",
        ]
        if stop_cmd:
            lines.append(f"• **أمر الإيقاف**: `{stop_cmd}`")
        lines += [
            mode_line,
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
            elif act == "jarvis_command":
                lines.append(f"{i}. 🤖 **أمر لجارفيس**{lbl_text}: «{params.get('command', '')}»")
            elif act == "telegram_send":
                lines.append(f"{i}. ✈️ **رسالة تليجرام لك**{lbl_text}: «{params.get('message', '')}»")
            else:
                lines.append(f"{i}. ⚙️ **إجراء**{lbl_text}: {act} مع المعاملات {params}")

        if graph.get("nodes"):
            nodes = {n.get("id"): n for n in graph.get("nodes", [])}
            def _nm(nid):
                st = (nodes.get(nid) or {}).get("step", {})
                return st.get("label") or st.get("action") or nid
            branch_lines = []
            for e in graph.get("edges", []):
                br = e.get("branch", "main")
                if br in ("true", "false") and e.get("from") in nodes:
                    tag = "✅ لو الشرط اتحقق" if br == "true" else "❌ لو الشرط ماتحققش"
                    branch_lines.append(f"• {_nm(e['from'])}: {tag} ➔ {_nm(e.get('to'))}")
            lines += ["", f"🗺 **خريطة**: {len(graph.get('nodes', []))} نود، {len(graph.get('edges', []))} توصيلة."]
            lines += branch_lines

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

        if flow.interval_seconds <= 0:
            return (
                f"🚀 **جاري تنفيذ الفلو [{flow.name}] (مرة واحدة بدون تكرار)...**"
            )
        stop_hint = f"\n• لإيقاف البرنامج في أي وقت، اكتب: `{flow.stop_command}`" if flow.stop_command else ""
        return (
            f"🚀 **تم تشغيل برنامج الأوتوميشن بنجاح!**\n"
            f"• الاسم: **{flow.name}**\n"
            f"• الفاصل الزمني: كل {flow.interval_seconds} ثانية"
            f"{stop_hint}"
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

            self._run_cycle(flow, stop_event, player)

            # One-shot flow (interval_seconds <= 0) -> finish immediately after 1 cycle
            if flow.interval_seconds <= 0:
                if player:
                    player.write_log(f"[flow] ✅ Completed one-shot flow: {flow.name}")
                break

            # Sleep interval in small increments to allow instant responsive cancellation
            remaining = flow.interval_seconds
            while remaining > 0 and not stop_event.is_set():
                sleep_chunk = min(1.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk

        print(f"[FlowEngine] Loop finished for [{flow.name}]")
        self._stop_events.pop(flow.id, None)
        self._active_threads.pop(flow.id, None)
        flow.status = "IDLE"
        self.save_flow(flow)
        self._notify_status(flow.id, "IDLE")

    def _run_cycle(self, flow: AutomationFlow, stop_event: threading.Event, player=None):
        """One pass over the flow — graph map if it has one, else the legacy step list."""
        if (flow.graph or {}).get("nodes"):
            self._run_graph_cycle(flow, stop_event, player)
        else:
            self._run_linear_cycle(flow, stop_event, player)

    def _skip_target(self, step: FlowStep) -> str:
        """Return the target that makes this step skippable (already open), else ''."""
        cond = (step.condition or "").strip().lower()
        params = step.parameters or {}
        if not (cond.startswith("skip_if_open") or params.get("skip_if_open")):
            return ""
        if ":" in cond:
            target_check = cond.split(":", 1)[1].strip()
        else:
            target_check = (
                params.get("bookmark")
                or params.get("app_name")
                or params.get("url")
                or params.get("profile_name")
                or ("chrome" if step.action == "browser_control" else "")
            )
        return target_check if (target_check and _is_target_open(target_check, params)) else ""

    def _run_graph_cycle(self, flow: AutomationFlow, stop_event: threading.Event, player=None):
        """Walk the node map from the trigger, following IF branches."""
        graph = flow.graph or {}
        nodes = {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}
        out: dict[str, list[dict]] = {}
        indeg: dict[str, int] = {nid: 0 for nid in nodes}
        for e in graph.get("edges", []):
            if e.get("from") in nodes and e.get("to") in nodes:
                out.setdefault(e["from"], []).append(e)
                indeg[e["to"]] = indeg.get(e["to"], 0) + 1
        ypos = lambda nid: float(nodes[nid].get("y", 0))

        self._notify_node(flow.id, "__cycle__", "start")

        triggers = [nid for nid, n in nodes.items() if n.get("type") == "trigger"]
        queue: list[str] = []
        for t in triggers:
            queue += sorted([e["to"] for e in out.get(t, [])], key=ypos)
        if not queue:
            queue = sorted([nid for nid in nodes if indeg.get(nid, 0) == 0 and nid not in triggers],
                           key=lambda k: (float(nodes[k].get("x", 0)), ypos(k)))

        budget = max(50, len(nodes) * 10)       # guards against wiring loops
        while queue and not stop_event.is_set():
            budget -= 1
            if budget < 0:
                msg = f"[flow] ⚠ {flow.name}: توقفت الدورة — في لوب في التوصيلات."
                print(f"[FlowEngine] {msg}")
                if player:
                    player.write_log(msg)
                break
            nid = queue.pop(0)
            node = nodes.get(nid) or {}
            if node.get("type") == "trigger":
                queue += sorted([e["to"] for e in out.get(nid, [])], key=ypos)
                continue

            step = FlowStep.from_dict(node.get("step") or {})
            branch = "main"

            skip_t = self._skip_target(step)
            if skip_t:
                self._notify_node(flow.id, nid, "skipped")
                if player:
                    player.write_log(f"[step] ⏩ تخطي ({step.label or step.action}) لأن '{skip_t}' مفتوح بالفعل.")
            else:
                self._notify_node(flow.id, nid, "running")
                try:
                    res = self._execute_step(step, player)
                    if step.action == "check_condition" and isinstance(res, dict):
                        met = bool(res.get("met", False))
                        branch = "true" if met else "false"
                        if player:
                            player.write_log(
                                f"[condition] {'✅' if met else '❌'} '{res.get('target', '')}' ➔ "
                                f"{'مسار نعم' if met else 'مسار لا'}")
                    self._notify_node(flow.id, nid, "ok")
                except Exception as ex:
                    self._notify_node(flow.id, nid, "error")
                    print(f"[FlowEngine] Node ({step.label or step.action}) failed: {ex}")
                    if player:
                        player.write_log(f"[step] ❌ {step.label or step.action}: {ex}")
                    if step.on_failure == "stop":
                        stop_event.set()
                        break

            nxt = [e for e in out.get(nid, []) if e.get("branch", "main") == branch]
            if branch == "main" and not nxt:
                nxt = [e for e in out.get(nid, []) if e.get("branch", "main") == "main"]
            queue += sorted([e["to"] for e in nxt], key=ypos)

    def run_once(self, flow: AutomationFlow, player=None) -> str:
        """Execute a single pass right now (the map's ▶ test button). Non-blocking."""
        stop_event = threading.Event()

        def _worker():
            try:
                if player:
                    player.write_log(f"[flow] 🧪 تجربة: {flow.name}")
                self._run_cycle(flow, stop_event, player)
                if player:
                    player.write_log(f"[flow] ✅ خلصت التجربة: {flow.name}")
            except Exception as e:
                print(f"[FlowEngine] Test run failed: {e}")
                if player:
                    player.write_log(f"[flow] ❌ فشلت التجربة: {e}")

        threading.Thread(target=_worker, daemon=True, name=f"FlowTest-{flow.id}").start()
        return "started"

    def _run_linear_cycle(self, flow: AutomationFlow, stop_event: threading.Event, player=None):
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
            params = step.parameters or {}
            target_check = self._skip_target(step)
            if target_check:
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

        if act == "jarvis_command":
            # Hand a natural-language instruction to the live assistant exactly
            # as if it had been typed in the chat box.
            text = (params.get("command") or params.get("text") or "").strip()
            if not text:
                return None
            jarvis = getattr(sys, "_jarvis_live_instance", None)
            if jarvis is None or not hasattr(jarvis, "_on_text_command"):
                raise RuntimeError("JARVIS live session is not available")
            jarvis._on_text_command(text)
            return "sent"

        if act == "telegram_send":
            from plugins.telegram_bridge import send_to_owner
            return send_to_owner(params.get("message", ""))

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

        def _matches_cmd_field(field_val: str, user_norm: str) -> bool:
            if not field_val or not user_norm:
                return False
            # Support comma/pipe separated aliases like "Shift Facebook , شفت فيس بوك"
            raw_parts = field_val.replace("،", ",").replace("|", ",").split(",")
            for part in raw_parts:
                p = _normalize_cmd(part)
                if p and len(p) >= 2 and (p in user_norm or (len(user_norm) >= 3 and user_norm == p)):
                    return True
            full = _normalize_cmd(field_val)
            return bool(full and (full in user_norm or user_norm == full))

        for flow in flows:
            running = self.is_running(flow.id)
            # Only prioritize stop_command when the flow is actually running (or if start_command doesn't match)
            if running and flow.stop_command and _matches_cmd_field(flow.stop_command, norm_user):
                return ("stop", flow.id, flow)

            if flow.start_command and _matches_cmd_field(flow.start_command, norm_user):
                return ("start", flow.id, flow)

            if not running and flow.stop_command and _matches_cmd_field(flow.stop_command, norm_user):
                return ("stop", flow.id, flow)

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
