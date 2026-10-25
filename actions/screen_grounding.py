"""
actions/screen_grounding.py — High-Precision Visual UI Grounding & Computer Interaction for JARVIS Mark LIV.

Provides full multimodal Computer Use capabilities:
1. Accurately locates any button, input, icon, window, menu, or text on screen using Gemini Vision
   normalized coordinates [ymin, xmin, ymax, xmax] (0-1000) scaled to native screen pixels with DPI awareness.
2. Interacts with located elements: clicking, double-clicking, right-clicking, hovering, and drag-and-drop.
3. Types text safely into located input fields (with clipboard bypass for Arabic and special characters).
4. Selects options from dropdown menus and lists.
5. Reads, extracts, and analyzes text, error messages, chats, and states inside any open application.
6. Scans and enumerates all interactive elements visible on the screen.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Safe console streams for Windows cp1252 / cp1256 / UTF-8
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

try:
    from google.genai import types as gtypes
    _GENAI_TYPES_OK = True
except ImportError:
    _GENAI_TYPES_OK = False

from actions.screen_processor import _ensure_desktop
from core import gemini
from core.keyboard_utils import type_text_via_clipboard, press_key, press_enter


def _init_dpi():
    """Ensure Windows Per-Monitor DPI awareness is active so physical pixels match mouse coordinates."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _capture_screen_for_grounding() -> tuple[bytes, int, int]:
    """Capture the primary screen in high fidelity, returning (jpeg_bytes, width, height)."""
    if not _MSS:
        raise RuntimeError("mss is required for screen capture. Run: pip install mss")

    _ensure_desktop()
    _init_dpi()

    with mss.mss() as sct:
        monitors = sct.monitors
        monitor = monitors[1] if len(monitors) > 1 else monitors[0]
        shot = sct.grab(monitor)
        w = monitor["width"]
        h = monitor["height"]

        if _PIL:
            img = PIL.Image.frombytes("RGB", shot.size, shot.rgb)
            # Max dimensions for Vision API to keep crispness while remaining well within token limits
            max_w, max_h = 1920, 1080
            if img.width > max_w or img.height > max_h:
                img.thumbnail((max_w, max_h), PIL.Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88, optimize=True)
            return buf.getvalue(), w, h
        else:
            png_bytes = mss.tools.to_png(shot.rgb, shot.size)
            return png_bytes, w, h


def _parse_coordinates(text: str, screen_w: int, screen_h: int) -> tuple[int, int, tuple[int, int, int, int]] | None:
    """
    Parse coordinates from Gemini Vision output.
    Supports normalized [ymin, xmin, ymax, xmax] (0 to 1000), POINT: [x, y], CLICK:x,y, and JSON.
    Returns (center_x, center_y, (ymin, xmin, ymax, xmax)) in screen pixels.
    """
    if not text:
        return None
    raw = text.strip()
    if "NOT_FOUND" in raw.upper() and "[" not in raw and "{" not in raw:
        return None

    # 1. Bounding box check [ymin, xmin, ymax, xmax] (normalized 0-1000)
    box_match = re.search(
        r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]",
        raw
    )
    if box_match:
        ymin, xmin, ymax, xmax = [float(box_match.group(i)) for i in range(1, 5)]
        norm_x = (xmin + xmax) / 2.0
        norm_y = (ymin + ymax) / 2.0
        x_px = int((norm_x / 1000.0) * screen_w) if norm_x <= 1000 else int(norm_x)
        y_px = int((norm_y / 1000.0) * screen_h) if norm_y <= 1000 else int(norm_y)
        cx = max(0, min(screen_w - 1, x_px))
        cy = max(0, min(screen_h - 1, y_px))
        return cx, cy, (int(ymin), int(xmin), int(ymax), int(xmax))

    # 2. JSON check {"point": [y, x]} or {"box_2d": [ymin, xmin, ymax, xmax]}
    if "{" in raw and "}" in raw:
        try:
            sub = raw[raw.find("{"):raw.rfind("}") + 1]
            data = json.loads(sub)
            if "box_2d" in data and len(data["box_2d"]) >= 4:
                ymin, xmin, ymax, xmax = [float(v) for v in data["box_2d"][:4]]
                norm_x = (xmin + xmax) / 2.0
                norm_y = (ymin + ymax) / 2.0
                cx = int((norm_x / 1000.0) * screen_w) if norm_x <= 1000 else int(norm_x)
                cy = int((norm_y / 1000.0) * screen_h) if norm_y <= 1000 else int(norm_y)
                return max(0, min(screen_w - 1, cx)), max(0, min(screen_h - 1, cy)), (int(ymin), int(xmin), int(ymax), int(xmax))
            if "point" in data and len(data["point"]) >= 2:
                ny, nx = float(data["point"][0]), float(data["point"][1])
                cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                return max(0, min(screen_w - 1, cx)), max(0, min(screen_h - 1, cy)), (0, 0, 0, 0)
            if "x" in data and "y" in data:
                nx, ny = float(data["x"]), float(data["y"])
                cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                return max(0, min(screen_w - 1, cx)), max(0, min(screen_h - 1, cy)), (0, 0, 0, 0)
        except Exception:
            pass

    # 3. Direct CLICK:x,y or POINT:[x, y] or x,y
    pt_match = re.search(r"(?:CLICK|POINT|COORD)?[:\s=]*(\d+)\s*,\s*(\d+)", raw, re.IGNORECASE)
    if pt_match:
        x_val, y_val = float(pt_match.group(1)), float(pt_match.group(2))
        cx = int((x_val / 1000.0) * screen_w) if x_val <= 1000 else int(x_val)
        cy = int((y_val / 1000.0) * screen_h) if y_val <= 1000 else int(y_val)
        return max(0, min(screen_w - 1, cx)), max(0, min(screen_h - 1, cy)), (0, 0, 0, 0)

    return None


def locate_ui_element(description: str) -> dict | None:
    """
    Finds a UI element on screen by natural language description using Gemini Vision Grounding.
    Returns dict with {cx, cy, box, screen_w, screen_h, location_desc} or None.
    """
    img_bytes, screen_w, screen_h = _capture_screen_for_grounding()

    prompt = (
        f"You are an expert visual UI grounding engine running on a Windows PC.\n"
        f"The current screen resolution is {screen_w}x{screen_h} pixels.\n"
        f"Locate the UI element described as: '{description}'.\n\n"
        f"Instructions:\n"
        f"1. Detect the exact visual element (button, input field, tab, menu, icon, profile, link, checkbox, or text).\n"
        f"2. Output its bounding box in normalized coordinates [0 to 1000] as: BOX: [ymin, xmin, ymax, xmax]\n"
        f"   where 0,0 is the top-left and 1000,1000 is the bottom-right of the screen.\n"
        f"3. If the element is not found on screen, reply ONLY: NOT_FOUND"
    )

    if _GENAI_TYPES_OK:
        part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
    else:
        part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

    try:
        response = gemini.call(
            [part, prompt],
            tier=gemini.FAST,
            timeout_ms=25_000,
        )
        if response is None:
            return None
        text = (getattr(response, "text", None) or "").strip()
        parsed = _parse_coordinates(text, screen_w, screen_h)
        if not parsed:
            return None

        cx, cy, box = parsed

        # Describe relative position in Arabic
        h_pos = "في المنتصف"
        if cx < screen_w * 0.33:
            h_pos = "في اليسار"
        elif cx > screen_w * 0.66:
            h_pos = "في اليمين"

        v_pos = "في الوسط"
        if cy < screen_h * 0.33:
            v_pos = "في الأعلى"
        elif cy > screen_h * 0.66:
            v_pos = "في الأسفل"

        loc_desc = f"{v_pos} {h_pos} عند الإحداثيات ({cx}, {cy})"

        return {
            "cx": cx,
            "cy": cy,
            "box": box,
            "screen_w": screen_w,
            "screen_h": screen_h,
            "location_desc": loc_desc,
            "raw_response": text,
        }
    except Exception as e:
        print(f"[screen_grounding] locate_ui_element failed: {e}")
        return None


def _smooth_click(cx: int, cy: int, clicks: int = 1, button: str = "left"):
    """Smoothly moves the cursor to (cx, cy) and clicks, providing visual grounding feedback."""
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed.")
    _init_dpi()
    # Smooth move takes 0.15s to let the user see the target being acquired
    pyautogui.moveTo(cx, cy, duration=0.15)
    time.sleep(0.05)
    pyautogui.click(cx, cy, clicks=clicks, button=button)


def _autonomous_read_and_decide(goal: str, player=None) -> str:
    """
    Autonomous computer use decision loop:
    Delegates to actions.goal_agent.run_goal_agent for full multi-step loop support.
    """
    if not goal:
        return "لم يتم تحديد الهدف أو الأمر المطلوب تنفيذه."

    try:
        from actions.goal_agent import run_goal_agent
        return run_goal_agent(goal=goal, max_steps=5, player=player)
    except Exception as e:
        print(f"[AutoDecide] GoalAgent delegation failed ({e}), falling back to local inspection...")

    print(f"[AutoDecide] 🧠 Goal: '{goal}'")
    if player:
        try:
            player.write_log(f"[AutoDecide] فحص الشاشة واتخاذ قرار: '{goal}'")
        except Exception:
            pass

    time.sleep(0.8)
    img_bytes, screen_w, screen_h = _capture_screen_for_grounding()

    prompt_step1 = (
        f"You are an autonomous AI computer operator running on Windows.\n"
        f"The user's goal / request is: '{goal}'\n"
        f"The screen resolution is {screen_w}x{screen_h}.\n"
        f"Look carefully at the current open window, webpage, or application.\n\n"
        f"Decide the single best immediate action to fulfill the goal:\n"
        f"Option 1: If the answer / information is ALREADY clearly visible right now on screen:\n"
        f'{{"decision": "ANSWER", "answer": "<complete, accurate Arabic answer to the user\'s goal>"}}\n\n'
        f"Option 2: If we need to click a specific link, search result, button, tab, or card (e.g. to enter a website, open an article, or view an anime page):\n"
        f'{{"decision": "CLICK", "target_description": "<description of the element to click>", "point": [<normalized y from 0 to 1000>, <normalized x from 0 to 1000>], "reason": "<why clicking this leads to the answer in Arabic>"}}\n\n'
        f"Option 3: If we need to scroll down to see more results or details:\n"
        f'{{"decision": "SCROLL", "direction": "down", "reason": "<reason in Arabic>"}}\n\n'
        f"Reply ONLY in STRICT JSON format with no extra markdown wrapping."
    )

    if _GENAI_TYPES_OK:
        part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
    else:
        part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

    try:
        resp = gemini.call([part, prompt_step1], tier=gemini.FAST, timeout_ms=30_000)
        raw_text = (getattr(resp, "text", None) or "").strip()
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            decision = str(data.get("decision", "ANSWER")).upper()

            # 1. ANSWER DIRECTLY
            if decision == "ANSWER":
                ans = data.get("answer", "")
                if ans:
                    return f"✅ تم فحص الشاشة وقراءة البيانات:\n{ans}"

            # 2. CLICK TO NAVIGATE
            elif decision == "CLICK":
                point = data.get("point")
                target_desc = data.get("target_description", "الرابط المطلوب")
                reason = data.get("reason", "")
                if point and len(point) >= 2:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                    print(f"[AutoDecide] 🖱️ Decision: CLICK on '{target_desc}' at ({cx}, {cy}). Reason: {reason}")
                    if player:
                        try:
                            player.write_log(f"[AutoDecide] 🖱️ النقر على '{target_desc}'...")
                        except Exception:
                            pass
                    _smooth_click(cx, cy, clicks=1, button="left")
                    time.sleep(2.5)  # Wait for page navigation / load

                    # Step 2: Observe resulting screen and extract final answer
                    img_bytes2, w2, h2 = _capture_screen_for_grounding()
                    prompt_step2 = (
                        f"You are JARVIS. The user's request is: '{goal}'.\n"
                        f"We just clicked on '{target_desc}' ({reason}).\n"
                        f"Here is the resulting screen after navigation ({w2}x{h2}).\n"
                        f"Read the page, find the exact answer or status fulfilling the user's request (e.g. episode number, date, details, or facts).\n"
                        f"Provide a clear, direct, and conclusive answer in Arabic."
                    )
                    part2 = gtypes.Part.from_bytes(data=img_bytes2, mime_type="image/jpeg") if _GENAI_TYPES_OK else {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes2}}
                    resp2 = gemini.call([part2, prompt_step2], tier=gemini.FAST, timeout_ms=30_000)
                    final_answer = (getattr(resp2, "text", None) or "").strip()
                    if final_answer:
                        return f"✅ تم الدخول على '{target_desc}' وقراءة المحتوى:\n\n{final_answer}"

            # 3. SCROLL
            elif decision == "SCROLL":
                direction = data.get("direction", "down")
                scroll_amt = -600 if direction == "down" else 600
                print(f"[AutoDecide] 📜 Scrolling {direction}...")
                pyautogui.scroll(scroll_amt)
                time.sleep(1.0)
                img_bytes3, w3, h3 = _capture_screen_for_grounding()
                prompt_step3 = (
                    f"The user's request is: '{goal}'.\n"
                    f"We scrolled down to reveal more content. Here is the screen ({w3}x{h3}).\n"
                    f"Read the content and provide a conclusive Arabic answer to the user's request."
                )
                part3 = gtypes.Part.from_bytes(data=img_bytes3, mime_type="image/jpeg") if _GENAI_TYPES_OK else {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes3}}
                resp3 = gemini.call([part3, prompt_step3], tier=gemini.FAST, timeout_ms=25_000)
                final_answer = (getattr(resp3, "text", None) or "").strip()
                if final_answer:
                    return f"✅ تم التمرير وقراءة المحتوى:\n\n{final_answer}"

    except Exception as e:
        print(f"[AutoDecide] Execution error: {e}")

    # Fallback to standard screen read
    return screen_grounding_handler({"action": "read_screen", "question": goal}, player=player)


def screen_grounding_handler(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Main handler for screen_grounding action tool.
    Dispatches: locate, click, double_click, right_click, hover, type_into, select_option, read_screen, read_area, scan_elements.
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    description = params.get("description", "").strip()
    text = params.get("text", "").strip()
    option = params.get("option", "").strip()
    question = params.get("question", "").strip()
    clear_first = params.get("clear_first", True)

    if not action:
        return "No action specified for screen_grounding."

    if player:
        player.write_log(f"[Grounding] {action} {description or question}")

    print(f"[screen_grounding] ▶ {action} | desc='{description}' | text='{text}' | opt='{option}'")

    try:
        # 1. LOCATE ONLY
        if action == "locate":
            if not description:
                return "يرجى تحديد وصف العنصر المراد البحث عنه."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من العثور على '{description}' على الشاشة الحالية."
            return f"تم تحديد موقع '{description}' بدقة: {res['location_desc']}."

        # 2. CLICK / DOUBLE_CLICK / RIGHT_CLICK / HOVER
        if action in ("click", "left_click"):
            if not description:
                return "يرجى تحديد وصف الزر أو العنصر المراد النقر عليه."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من رصد العنصر '{description}' على الشاشة للنقر عليه."
            _smooth_click(res["cx"], res["cy"], clicks=1, button="left")
            return f"تم النقر على '{description}' {res['location_desc']}."

        if action == "double_click":
            if not description:
                return "يرجى تحديد وصف العنصر المراد النقر عليه نقراً مزدوجاً."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من العثور على '{description}' للنقر المزدوج عليه."
            _smooth_click(res["cx"], res["cy"], clicks=2, button="left")
            return f"تم النقر المزدوج على '{description}' {res['location_desc']}."

        if action in ("right_click", "context_menu"):
            if not description:
                return "يرجى تحديد وصف العنصر لفتح قائمته."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من العثور على '{description}' للنقر بالزر الأيمن."
            _smooth_click(res["cx"], res["cy"], clicks=1, button="right")
            return f"تم فتح القائمة السياقية (Right Click) لـ '{description}' {res['location_desc']}."

        if action in ("hover", "move"):
            if not description:
                return "يرجى تحديد وصف العنصر لتحريك المؤشر إليه."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من العثور على '{description}' على الشاشة."
            _init_dpi()
            pyautogui.moveTo(res["cx"], res["cy"], duration=0.25)
            return f"تم تحريك المؤشر إلى '{description}' {res['location_desc']}."

        # 3. TYPE INTO FIELD
        if action == "type_into":
            if not description:
                return "يرجى تحديد وصف حقل الإدخال المطلوب الكتابة داخله."
            res = locate_ui_element(description)
            if not res:
                return f"لم أتمكن من العثور على حقل الإدخال '{description}' على الشاشة."

            # Focus the field
            _smooth_click(res["cx"], res["cy"], clicks=1, button="left")
            time.sleep(0.15)

            if clear_first:
                # Select all and delete to clear existing text
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyautogui.press("delete")
                time.sleep(0.05)

            # Paste text cleanly using clipboard bypass (handles Arabic, symbols, etc.)
            if text:
                type_text_via_clipboard(text)
                time.sleep(0.1)

            return f"تمت الكتابة بنجاح داخل حقل '{description}': \"{text}\"."

        # 4. SELECT DROPDOWN OPTION
        if action in ("select_option", "choose_option"):
            if not description or not option:
                return "يرجى تحديد وصف القائمة المنسدلة (description) والخيار المطلوب (option)."
            # Step A: click dropdown
            res_dd = locate_ui_element(description)
            if not res_dd:
                return f"لم أتمكن من رصد القائمة المنسدلة '{description}'."
            _smooth_click(res_dd["cx"], res_dd["cy"], clicks=1, button="left")
            time.sleep(0.4)  # Wait for dropdown animation

            # Step B: locate option
            res_opt = locate_ui_element(f"option or menu item '{option}' in the opened dropdown")
            if res_opt:
                _smooth_click(res_opt["cx"], res_opt["cy"], clicks=1, button="left")
                return f"تم فتح القائمة '{description}' واختيار '{option}' بنجاح."
            else:
                # Fallback: type option and press enter
                type_text_via_clipboard(option)
                time.sleep(0.2)
                press_enter()
                return f"تم فتح القائمة '{description}' وكتابة الخيار '{option}' والضغط على Enter."

        # 5. READ & ANALYZE SCREEN / ACTIVE APPLICATION
        if action in ("read_screen", "analyze_screen"):
            img_bytes, screen_w, screen_h = _capture_screen_for_grounding()
            q = question or "اقرأ وحلل محتوى الشاشة والنافذة المفتوحة حالياً بالتفصيل واشرح ما هو معروض بالعربية."
            prompt = (
                f"أنت المساعد الذكي JARVIS. انظر إلى لقطة الشاشة هذه (الدقة: {screen_w}x{screen_h}).\n"
                f"المطلوب: {q}\n"
                f"اقرأ النصوص، البيانات، الرسائل، أو الأزرار بدقة وقدم تقريراً واضحاً وموجزاً باللغة العربية."
            )
            if _GENAI_TYPES_OK:
                part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            else:
                part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

            resp = gemini.call([part, prompt], tier=gemini.FAST, timeout_ms=30_000)
            if resp and getattr(resp, "text", None):
                return resp.text.strip()
            return "تم فحص الشاشة ولكن لم أتمكن من استخراج تحليل مفصل."

        # 6. READ SPECIFIC AREA / COMPONENT
        if action == "read_area":
            target = description or question or "المحتوى الرئيسي"
            img_bytes, screen_w, screen_h = _capture_screen_for_grounding()
            prompt = (
                f"انظر إلى لقطة الشاشة هذه ({screen_w}x{screen_h}).\n"
                f"ركز بدقة على المنطقة أو العنصر التالي: '{target}'.\n"
                f"استخرج كل النصوص المكتوبة، الأرقام، والحالة في هذه المنطقة بالتفصيل واشرحها بالعربية."
            )
            if _GENAI_TYPES_OK:
                part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            else:
                part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

            resp = gemini.call([part, prompt], tier=gemini.FAST, timeout_ms=25_000)
            if resp and getattr(resp, "text", None):
                return resp.text.strip()
            return f"تعذر قراءة المنطقة '{target}'."

        # 7. SCAN ALL INTERACTIVE ELEMENTS
        if action in ("scan_elements", "inspect_ui"):
            img_bytes, screen_w, screen_h = _capture_screen_for_grounding()
            prompt = (
                f"Analyze this UI screenshot ({screen_w}x{screen_h}).\n"
                f"Identify all primary interactive elements (buttons, inputs, tabs, icons, links, dropdowns).\n"
                f"Return a structured list in Arabic summarizing what can be clicked or interacted with, "
                f"and their general locations on screen."
            )
            if _GENAI_TYPES_OK:
                part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            else:
                part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

            resp = gemini.call([part, prompt], tier=gemini.FAST, timeout_ms=30_000)
            if resp and getattr(resp, "text", None):
                return resp.text.strip()
            return "تعذر جرد عناصر الشاشة حالياً."

        # 8. READ AND DECIDE / AUTONOMOUS INTERACTION & DECISION LOOP
        if action in ("read_and_decide", "autonomous_act", "decide_and_act", "inspect_and_answer"):
            goal = question or description or text or "اقرأ ما هو معروض على الشاشة واتخذ القرار المناسب بالدخول أو النقر أو استخراج الإجابة المطلوبة بالعربية."
            return _autonomous_read_and_decide(goal, player=player)

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[screen_grounding] Error: {e}")
        return f"حدث خطأ أثناء تنفيذ عملية الشاشة ({action}): {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "screen_grounding",
    "description": (
        "High-precision visual UI grounding, computer interaction, and autonomous screen decisions. "
        "Locates any button, input field, icon, tab, or text on screen using normalized coordinates (0-1000) "
        "and DPI-aware screen scaling. Enables JARVIS to click, double-click, right-click, type text into input fields, "
        "select options from dropdowns, scan interactive elements, and read/analyze content and messages inside any application. "
        "Use 'read_and_decide' to autonomously inspect what's open, decide whether to click a search result or link, enter the page, read information, and make the final decision."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read_and_decide | read_screen | click | double_click | right_click | hover | type_into | select_option | read_area | scan_elements | locate"
            },
            "description": {
                "type": "STRING",
                "description": "Visual description of the target button, icon, input field, or element on screen (e.g. 'Anti-Gravity icon', 'save button', 'search box', 'close button')"
            },
            "text": {
                "type": "STRING",
                "description": "Text to type into the field when action is 'type_into'"
            },
            "option": {
                "type": "STRING",
                "description": "Option name to select from a dropdown or menu when action is 'select_option'"
            },
            "question": {
                "type": "STRING",
                "description": "Question, goal, or task instruction about what to read/decide on screen for 'read_and_decide', 'read_screen', or 'read_area'"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Whether to clear existing text before typing in type_into (default: true)"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": screen_grounding_handler,
}
