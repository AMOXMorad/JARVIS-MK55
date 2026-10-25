"""
actions/goal_agent.py — General Autonomous Multi-Step Goal Planner & Computer Agent for JARVIS Mark LIV.

Provides full agentic Tree-of-Thought / ReAct autonomous execution:
When the user asks a compound or goal-oriented request (e.g.:
"افتحلي wit anime شوف rezero اخر حلقه نزلت كام" or "ادخل على موقع كذا واعرف سعر كذا"):
1. Analyzes the high-level goal and determines the initial site or application to navigate to.
2. Checks Chrome bookmarks / profiles or search engine to reach the website/app cleanly.
3. Enters an autonomous perception-reasoning-action loop (up to max_steps):
   - Perceive: Captures the screen in high fidelity.
   - Reason: Gemini Vision analyzes the screen against the ultimate goal and past action history.
   - Decide:
     * DONE: If the target information or episode or fact is visible -> extracts and reports the answer.
     * CLICK: Clicks a specific link, anime card, series category, search result, or button.
     * TYPE: Clicks into a search bar, types the target name (e.g. 'Re:Zero'), and presses Enter.
     * SCROLL: Scrolls down to reveal episodes, products, or details.
     * NAVIGATE: Navigates directly if needed.
   - Act & Settle: Executes the physical mouse/keyboard action, waits for the page to load, and repeats.
4. Delivers the conclusive, factual final answer directly to the user in fluent Arabic.
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

# Safe console encoding for Windows cp1252/cp1256
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
    _AUTOMATION_OK = True
except ImportError:
    _AUTOMATION_OK = False

try:
    from google.genai import types as gtypes
    _GENAI_TYPES_OK = True
except ImportError:
    _GENAI_TYPES_OK = False

from core import gemini
from core.keyboard_utils import type_text_via_clipboard, press_enter
from actions.screen_grounding import _capture_screen_for_grounding, _smooth_click, _init_dpi
from actions.browser_control import _lookup_chrome_bookmark, _resolve_chrome_profile, _launch_chrome_with_profile
from actions.computer_control import _maximize_window


def _focus_browser_window():
    """Ensure the browser window is topmost and active before sending keyboard shortcuts."""
    if sys.platform != "win32":
        return
    try:
        import win32gui
        import ctypes
        user32 = ctypes.windll.user32
        hDesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hDesk:
            user32.SetThreadDesktop(hDesk)

        candidates = []
        def _enum(hwnd, _):
            try:
                if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
                    title = win32gui.GetWindowText(hwnd).lower()
                    cls = win32gui.GetClassName(hwnd).lower()
                    if any(x in cls or x in title for x in ("chrome", "edge", "witanime", "melbet", "firefox", "opera")):
                        candidates.append(hwnd)
            except Exception:
                pass
            return True
        win32gui.EnumWindows(_enum, 0)
        if candidates:
            hwnd = candidates[0]
            user32.keybd_event(0x12, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.1)
    except Exception:
        pass



def _extract_target_from_goal(goal: str) -> dict:
    """
    General AI extraction to determine how to start fulfilling ANY user goal,
    completely dynamically without any hardcoded website or application lists.
    """
    prompt = (
        f"Analyze this computer user request: '{goal}'.\n"
        f"Determine how to start fulfilling it on a Windows computer:\n"
        f"1. Is a specific desktop application mentioned (e.g. Telegram, Discord, Steam, Settings, Notepad, Excel, etc.)?\n"
        f"2. Is a specific website, service, or bookmark mentioned to start with (e.g. YouTube, Wikipedia, Reddit, GitHub, WitAnime, Amazon, or ANY other website)?\n"
        f"3. Or should we start with a Google web search for a query?\n\n"
        f"Reply ONLY in STRICT JSON format:\n"
        f"{{\n"
        f'  "type": "APP" | "BOOKMARK_OR_SITE" | "WEB_SEARCH",\n'
        f'  "target_name": "<exact name of app, site/bookmark, or search query>"\n'
        f"}}"
    )
    try:
        resp = gemini.call(prompt, tier=gemini.FAST, timeout_ms=6_000)
        txt = (getattr(resp, "text", None) or "").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"[GoalAgent] Target extraction error: {e}")
    return {"type": "WEB_SEARCH", "target_name": goal}


def _launch_initial_target(target: str, goal: str, player=None) -> bool:
    """
    Universally opens the initial website, bookmark, or application needed for the goal,
    completely dynamically with zero hardcoded sites.
    """
    target_info = None
    if target:
        clean = target.strip()
        target_info = {"type": "BOOKMARK_OR_SITE", "target_name": clean}
    else:
        target_info = _extract_target_from_goal(goal)

    t_type = str(target_info.get("type", "WEB_SEARCH")).upper()
    t_name = str(target_info.get("target_name", "")).strip()

    if not t_name:
        return False

    print(f"[GoalAgent] 🚀 Target identified: {t_type} -> '{t_name}'")
    if player:
        try:
            player.write_log(f"[GoalAgent] 🚀 البدء بالتوجه نحو: '{t_name}' ({t_type})...")
        except Exception:
            pass

    # 1. Desktop App
    if t_type == "APP":
        try:
            from actions.open_app import open_app
            open_app({"app_name": t_name}, player=player)
            time.sleep(1.5)
            _maximize_window(t_name)
            time.sleep(1.0)
            return True
        except Exception as e:
            print(f"[GoalAgent] Could not launch app '{t_name}': {e}")

    # 2. Bookmark / Website
    p_dir, p_name = _resolve_chrome_profile()
    bmark_res = _lookup_chrome_bookmark(p_dir, t_name)
    if not bmark_res and p_dir != "Default":
        bmark_res = _lookup_chrome_bookmark("Default", t_name)

    if bmark_res:
        url, title = bmark_res
        print(f"[GoalAgent] 🔖 Found bookmark for '{t_name}': {url} ('{title}')")
        _launch_chrome_with_profile(p_dir, url=url)
        time.sleep(1.0)
        _maximize_window("chrome")
        time.sleep(2.0)
        return True

    # Check if URL directly
    if "." in t_name or "http://" in t_name or "https://" in t_name:
        from actions.browser_control import _normalize_url
        _launch_chrome_with_profile(p_dir, url=_normalize_url(t_name))
        time.sleep(1.0)
        _maximize_window("chrome")
        time.sleep(2.0)
        return True

    # 3. Google Web Search
    search_url = f"https://www.google.com/search?q={t_name.replace(' ', '+')}"
    _launch_chrome_with_profile(p_dir, url=search_url)
    time.sleep(1.0)
    _maximize_window("chrome")
    time.sleep(2.0)
    return True


def run_goal_agent(goal: str, target_app_or_site: str = "", max_steps: int = 8, player=None) -> str:
    """
    Executes an autonomous goal-oriented loop with perception, reasoning, and multi-step action.
    Includes built-in anti-drift and pop-up ad detection/recovery (closes ad tabs, dismisses popups,
    switches back to target tabs via top tab strip, or goes back to maintain focus on the target goal).
    """
    if not goal:
        return "لم يتم تحديد الهدف المطلوب الوصول إليه."

    goal = goal.strip()
    print(f"\n=======================================================")
    print(f"[GoalAgent] 🎯 Starting Autonomous Goal Agent")
    print(f"[GoalAgent] Goal: '{goal}'")
    print(f"[GoalAgent] Target: '{target_app_or_site}' | Max steps: {max_steps}")
    print(f"=======================================================\n")

    if player:
        try:
            player.write_log(f"[GoalAgent] 🎯 جاري التخطيط للهدف: '{goal}'")
        except Exception:
            pass

    # Step 0: Ensure target is launched/focused if specified
    if target_app_or_site or any(w in goal for w in ("افتح", "ادخل", "موقع", "برنامج", "تطبيق", "ابحث", "دور", "روح", "شغل", "open", "launch", "search", "go")):
        _launch_initial_target(target_app_or_site, goal, player=player)

    history: list[str] = []
    step = 1
    ad_recovery_count = 0
    max_ad_recoveries = 6

    while step <= max_steps:
        print(f"\n[GoalAgent] ── Step {step}/{max_steps} (Ad recoveries: {ad_recovery_count}) ──")
        time.sleep(1.0)
        img_bytes, screen_w, screen_h = _capture_screen_for_grounding()

        history_str = "\n".join(f"- {h_item}" for h_item in history) if history else "لا توجد إجراءات سابقة (الشاشة الأولية)."

        prompt = (
            f"You are JARVIS's Autonomous Computer Agent operating a Windows PC.\n"
            f"The user's ultimate goal is:\n"
            f"'{goal}'\n\n"
            f"Current step: {step} of {max_steps}\n"
            f"History of actions taken so far:\n{history_str}\n\n"
            f"Screen Resolution: {screen_w}x{screen_h} pixels.\n\n"
            f"CRITICAL RULE 1 — ANTI-DRIFT & POP-UP AD DETECTION:\n"
            f"Examine the current screen carefully. Did the last action cause the browser or window to drift away from the goal?\n"
            f"- Is this an intrusive pop-up advertisement, betting/gambling site (e.g. Melbet, 1xBet), scam/fake warning, software download, or random sponsor page opened in a new tab or window?\n"
            f"- Did a modal overlay or ad banner pop up covering the real website content?\n"
            f"- Did the browser navigate away from the target website or topic?\n\n"
            f"LOOK AT THE BROWSER TAB STRIP AT THE VERY TOP (y from 0 to 45 pixels):\n"
            f"If multiple tabs are open and the current active tab is an ad/divergent page:\n"
            f"- Look at the tabs to the left: if you see your target tab ('WitAnime' / 'قائمة الأنمي' / etc.):\n"
            f"  CHOOSE ACTION 'SWITCH_TAB' and provide 'point': [y, x] to click directly on the target tab (e.g. point=[18, 110])!\n"
            f"- OR choose action 'CLOSE_TAB' (presses Ctrl+W or clicks the 'x' on the ad tab to close it and return to the main tab).\n"
            f"- If the page itself redirected to an ad: choose action 'GO_BACK' (presses Alt+Left to go back to the original page).\n"
            f"- If an in-page modal/overlay ad popped up covering the page: choose action 'CLOSE_POPUP' (provide point of the 'X' / 'Close' / 'إغلاق' button, or omit point to press Escape).\n\n"
            f"CRITICAL RULE 2 — NORMAL GOAL EXECUTION:\n"
            f"If the screen is on the target website/application:\n"
            f"1. Does the current screen already display the exact answer/information needed to fulfill the goal?\n"
            f"   -> Choose 'DONE' and provide the full conclusive Arabic answer.\n"
            f"2. Does the page have a search bar to search for the specific anime / item / topic?\n"
            f"   -> Choose 'TYPE' with the search bar coordinates and text.\n"
            f"3. Do you see a card, link, episode, category, or button that leads to the goal?\n"
            f"   -> Choose 'CLICK' with the target coordinates.\n"
            f"4. Do you need to scroll down to reveal more episodes or details?\n"
            f"   -> Choose 'SCROLL' with direction 'down'.\n\n"
            f"Reply ONLY in STRICT JSON format with one of the following schemas:\n\n"
            f"Recovery Schemas (when ad / drift detected):\n"
            f'{{\n  "action": "CLOSE_TAB",\n  "thought": "<reason in Arabic explaining the ad detected and why closing this tab returns to the target page>",\n  "point": [<y 0-1000>, <x 0-1000>]\n}}\n'
            f'OR\n'
            f'{{\n  "action": "SWITCH_TAB",\n  "thought": "<reason in Arabic>",\n  "target_description": "<target tab name>",\n  "point": [<y 0-1000>, <x 0-1000>]\n}}\n'
            f'OR\n'
            f'{{\n  "action": "GO_BACK",\n  "thought": "<reason in Arabic explaining that we were redirected to an ad and are going back>"\n}}\n'
            f'OR\n'
            f'{{\n  "action": "CLOSE_POPUP",\n  "thought": "<reason in Arabic>",\n  "target_description": "<description of X or close button>",\n  "point": [<y 0-1000>, <x 0-1000>]\n}}\n\n'
            f"Goal Action Schemas:\n"
            f'{{\n  "action": "DONE",\n  "thought": "<explanation in Arabic>",\n  "answer": "<thorough, clear, direct final answer in Arabic>"\n}}\n'
            f'OR\n'
            f'{{\n  "action": "TYPE",\n  "thought": "<reason in Arabic>",\n  "target_description": "<description>",\n  "point": [<y 0-1000>, <x 0-1000>],\n  "text": "<text to type>",\n  "press_enter": true\n}}\n'
            f'OR\n'
            f'{{\n  "action": "CLICK",\n  "thought": "<reason in Arabic>",\n  "target_description": "<description>",\n  "point": [<y 0-1000>, <x 0-1000>]\n}}\n'
            f'OR\n'
            f'{{\n  "action": "SCROLL",\n  "thought": "<reason in Arabic>",\n  "direction": "down"\n}}\n'
            f'OR\n'
            f'{{\n  "action": "NAVIGATE",\n  "thought": "<reason in Arabic>",\n  "url": "<url>"\n}}'
        )

        if _GENAI_TYPES_OK:
            part = gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
        else:
            part = {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes}}

        try:
            resp = gemini.call([part, prompt], tier=gemini.FAST, timeout_ms=30_000)
            raw_text = (getattr(resp, "text", None) or "").strip()
            m = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not m:
                print(f"[GoalAgent] ⚠️ Could not parse JSON from model output: {raw_text[:200]}")
                time.sleep(1.0)
                step += 1
                continue

            decision_data = json.loads(m.group(0))
            action_type = str(decision_data.get("action", "")).upper().strip()
            thought = decision_data.get("thought", "")

            print(f"[GoalAgent] 💡 Decision [{action_type}]: {thought}")
            if player:
                try:
                    player.write_log(f"[GoalAgent] الخطوة {step}: {thought}")
                except Exception:
                    pass

            # ── ANTI-DRIFT & RECOVERY ACTIONS ─────────────────────────────────
            # 1. CLOSE_TAB (Ctrl+W or Click Tab Close 'x')
            if action_type in ("CLOSE_TAB", "CLOSE_AD"):
                print(f"[GoalAgent] 🚫 Detected Pop-up Ad / Divergent Tab: {thought}. Closing tab...")
                if player:
                    try:
                        player.write_log(f"[GoalAgent] 🚫 إغلاق إعلان منبثق: {thought}")
                    except Exception:
                        pass
                _focus_browser_window()
                point = decision_data.get("point")
                if point and len(point) >= 2:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                    print(f"[GoalAgent] 🖱️ Clicking close 'x' on ad tab at ({cx}, {cy})...")
                    _smooth_click(cx, cy, clicks=1, button="left")
                else:
                    # Focus Chrome by clicking safe title/address bar zone, then send Ctrl+W
                    pyautogui.click(screen_w // 2, 80)
                    time.sleep(0.05)
                    pyautogui.hotkey("ctrl", "w")
                    if sys.platform == "win32":
                        try:
                            import ctypes
                            user32 = ctypes.windll.user32
                            user32.keybd_event(0x11, 0, 0, 0)
                            user32.keybd_event(0x57, 0, 0, 0)
                            time.sleep(0.05)
                            user32.keybd_event(0x57, 0, 2, 0)
                            user32.keybd_event(0x11, 0, 2, 0)
                        except Exception:
                            pass

                history.append(f"تم إغلاق إعلان منبثق / تبويب دخيل: {thought}")
                time.sleep(1.8)
                if ad_recovery_count < max_ad_recoveries:
                    ad_recovery_count += 1
                    continue
                step += 1
                continue

            # 2. SWITCH_TAB (Click Target Tab in Tab Strip)
            elif action_type in ("SWITCH_TAB", "SELECT_TAB"):
                point = decision_data.get("point")
                target_desc = decision_data.get("target_description", "التبويب الأساسي")
                print(f"[GoalAgent] 📑 Switching back to tab '{target_desc}': {thought}...")
                if player:
                    try:
                        player.write_log(f"[GoalAgent] 📑 التبديل إلى التبويب الأصلي '{target_desc}'...")
                    except Exception:
                        pass
                _focus_browser_window()
                if point and len(point) >= 2:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                    _smooth_click(cx, cy, clicks=1, button="left")
                else:
                    # Default: First tab in maximized Chrome is around (110, 18)
                    _smooth_click(110, 18, clicks=1, button="left")
                history.append(f"تم العودة للتبويب الأصلي '{target_desc}': {thought}")
                time.sleep(1.5)
                if ad_recovery_count < max_ad_recoveries:
                    ad_recovery_count += 1
                    continue
                step += 1
                continue

            # 3. GO_BACK (Alt+Left)
            elif action_type == "GO_BACK":
                print(f"[GoalAgent] ↩️ Detected Redirect / Ad Page: {thought}. Going back (Alt+Left)...")
                if player:
                    try:
                        player.write_log(f"[GoalAgent] ↩️ رجوع للصفحة السابقة بعد تحويل إعلاني: {thought}")
                    except Exception:
                        pass
                _focus_browser_window()
                pyautogui.hotkey("alt", "left")
                history.append(f"تم الرجوع للصفحة السابقة (Alt+Left) بعد رصد تحويل إعلاني: {thought}")
                time.sleep(2.0)
                if ad_recovery_count < max_ad_recoveries:
                    ad_recovery_count += 1
                    continue
                step += 1
                continue

            # 4. CLOSE_POPUP
            elif action_type in ("CLOSE_POPUP", "DISMISS_POPUP"):
                point = decision_data.get("point")
                target_desc = decision_data.get("target_description", "زر إغلاق الإعلان")
                print(f"[GoalAgent] ✖️ Dismissing in-page popup overlay: {thought}...")
                if player:
                    try:
                        player.write_log(f"[GoalAgent] ✖️ إغلاق نافذة إعلانية منبثقة: {thought}")
                    except Exception:
                        pass
                if point and len(point) >= 2:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)
                    _smooth_click(cx, cy, clicks=1, button="left")
                else:
                    pyautogui.press("esc")
                history.append(f"تم إغلاق نافذة إعلانية منبثقة (Popup) داخل الصفحة: {thought}")
                time.sleep(1.2)
                if ad_recovery_count < max_ad_recoveries:
                    ad_recovery_count += 1
                    continue
                step += 1
                continue

            # ── 1. DONE ──────────────────────────────────────────────────────
            elif action_type == "DONE":
                final_answer = decision_data.get("answer", "")
                if not final_answer:
                    final_answer = thought
                history_summary = "\n".join(f"• {h_item}" for h_item in history)
                print(f"[GoalAgent] 🎯 Goal satisfied at step {step}!")
                return (
                    f"✅ تم تنفيذ الهدف بنجاح:\n"
                    f"{final_answer}\n\n"
                    f"📝 مسار الوصول:\n{history_summary or '• القراءة المباشرة من الشاشة'}"
                )

            # ── 2. TYPE ──────────────────────────────────────────────────────
            elif action_type == "TYPE":
                point = decision_data.get("point")
                text_to_type = decision_data.get("text", "")
                target_desc = decision_data.get("target_description", "حقل البحث")
                do_enter = decision_data.get("press_enter", True)

                if point and len(point) >= 2 and text_to_type:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)

                    print(f"[GoalAgent] ⌨️ Typing '{text_to_type}' into '{target_desc}' at ({cx}, {cy})...")
                    _smooth_click(cx, cy, clicks=1, button="left")
                    time.sleep(0.15)
                    pyautogui.hotkey("ctrl", "a")
                    time.sleep(0.05)
                    pyautogui.press("backspace")
                    time.sleep(0.05)
                    type_text_via_clipboard(text_to_type)
                    time.sleep(0.15)
                    if do_enter:
                        press_enter()
                    history.append(f"تمت الكتابة والبحث عن '{text_to_type}' في {target_desc}.")
                    time.sleep(2.5)  # Wait for search results to load
                    step += 1
                    continue

            # ── 3. CLICK ─────────────────────────────────────────────────────
            elif action_type == "CLICK":
                point = decision_data.get("point")
                target_desc = decision_data.get("target_description", "الرابط المطلوب")

                if point and len(point) >= 2:
                    ny, nx = float(point[0]), float(point[1])
                    cx = int((nx / 1000.0) * screen_w) if nx <= 1000 else int(nx)
                    cy = int((ny / 1000.0) * screen_h) if ny <= 1000 else int(ny)

                    print(f"[GoalAgent] 🖱️ Clicking on '{target_desc}' at ({cx}, {cy})...")
                    _smooth_click(cx, cy, clicks=1, button="left")
                    history.append(f"تم النقر على '{target_desc}'.")
                    time.sleep(2.5)  # Wait for page/card/dialog to load
                    step += 1
                    continue

            # ── 4. SCROLL ────────────────────────────────────────────────────
            elif action_type == "SCROLL":
                direction = decision_data.get("direction", "down")
                scroll_amt = -700 if direction == "down" else 700
                print(f"[GoalAgent] 📜 Scrolling {direction} ({scroll_amt}px)...")
                pyautogui.scroll(scroll_amt)
                history.append(f"تم التمرير لأسفل الصفحة لرؤية المزيد.")
                time.sleep(1.2)
                step += 1
                continue

            # ── 5. NAVIGATE ──────────────────────────────────────────────────
            elif action_type == "NAVIGATE":
                url = decision_data.get("url", "")
                if url:
                    print(f"[GoalAgent] 🌐 Navigating to {url}...")
                    from actions.browser_control import _open_native
                    _open_native(url, "chrome")
                    history.append(f"تم الانتقال إلى الرابط: {url}")
                    time.sleep(2.5)
                    step += 1
                    continue

        except Exception as e:
            print(f"[GoalAgent] ⚠️ Error during step {step}: {e}")
            time.sleep(1.0)
            step += 1

    # If max steps reached without explicit DONE, capture final screen and extract best answer
    print(f"[GoalAgent] ℹ️ Max steps ({max_steps}) reached. Extracting final conclusion from screen...")
    try:
        img_bytes_final, w_f, h_f = _capture_screen_for_grounding()
        prompt_final = (
            f"You are JARVIS. The user's original goal was:\n'{goal}'\n\n"
            f"Actions performed:\n" + "\n".join(history) + "\n\n"
            f"Look at the final screen ({w_f}x{h_f}) and answer the user's goal with whatever information is visible."
        )
        part_f = gtypes.Part.from_bytes(data=img_bytes_final, mime_type="image/jpeg") if _GENAI_TYPES_OK else {"inline_data": {"mime_type": "image/jpeg", "data": img_bytes_final}}
        resp_f = gemini.call([part_f, prompt_final], tier=gemini.FAST, timeout_ms=25_000)
        final_text = (getattr(resp_f, "text", None) or "").strip()
        history_summary = "\n".join(f"• {h_item}" for h_item in history)
        return (
            f"✅ التقرير والنتيجة النهائية للهدف:\n"
            f"{final_text}\n\n"
            f"📝 مسار العمل:\n{history_summary}"
        )
    except Exception as e:
        return f"تم تنفيذ خطوات البحث والملاحة ولكن تعذر استخراج تقرير نهائي: {e}"


def goal_agent_handler(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Entry point for core/action_loader.py."""
    params = parameters or {}
    goal = params.get("goal") or params.get("instruction") or params.get("question") or ""
    target = params.get("target_app_or_site") or params.get("target") or params.get("site") or ""
    max_steps = int(params.get("max_steps") or 8)

    return run_goal_agent(goal=goal, target_app_or_site=target, max_steps=max_steps, player=player)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "goal_agent",
    "description": (
        "General autonomous multi-step computer agent & goal planner. "
        "Solves compound, exploratory, or multi-step tasks across websites and applications (e.g. 'افتح موقع كذا وابحث عن كذا وشوف آخر حلقة/سعر', "
        "'ادخل على كذا وشوف انمي ريزيرو نزل حلقة كام', 'ابحث في المتصفح عن كذا وادخل على النتيجة واعرف التفاصيل'). "
        "Autonomously plans steps, navigates to sites/bookmarks, searches internal site search bars, clicks cards/links, scrolls, and extracts the final conclusive answer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The complete user goal, command, or question to achieve (e.g. 'افتح wit anime وشوف انمي rezero اخر حلقه نزلت كام')"
            },
            "target_app_or_site": {
                "type": "STRING",
                "description": "Optional website name, bookmark keyword, or application name to open first (e.g. 'wit anime', 'chrome', 'youtube')"
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Maximum autonomous navigation and decision steps (default: 8)"
            }
        },
        "required": [
            "goal"
        ]
    },
    "handler": goal_agent_handler,
}
