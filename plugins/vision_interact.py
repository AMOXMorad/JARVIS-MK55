"""
plugins/vision_interact.py — High-Precision Visual UI Grounding & App Interaction Plugin for JARVIS Mark LIV.

Provides full multimodal Computer Use capabilities:
1. Accurately locates any button, input field, icon, tab, or text on screen using Gemini Vision
   normalized coordinates (0-1000) scaled to native screen pixels with DPI awareness.
2. Interacts with located elements: clicking, double-clicking, right-clicking, hovering.
3. Types text safely into located input fields (with clipboard bypass for Arabic and special characters).
4. Selects options from dropdown menus and lists.
5. Reads, extracts, and analyzes text, error messages, chats, and states inside any open application.
6. Scans and enumerates all interactive elements visible on the screen.
"""
from __future__ import annotations

import sys
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
    "name": "vision_interact",
    "description": (
        "بلجن الرؤية والتفاعل المتقدم واتخاذ القرارات الآلية على الشاشة (High-Precision Screen Grounding & Autonomous UI Decision Loop). "
        "يحدد أماكن الأزرار والأيقونات والنصوص بدقة متناهية (Normalized Coordinates 0-1000 مع DPI awareness). "
        "يتيح لـ JARVIS: الضغط، الكتابة، التفاعل مع القوائم، وفحص وقراءة ما هو معروض. "
        "استخدم 'read_and_decide' لفحص الشاشة الحالية أو ما تم فتحه، واتخاذ القرار بالدخول على الروابط والنتائج وقراءة المحتوى والخروج بالقرار والإجابة النهائية."
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
                "description": "وصف مرئي دقيق بالإنجليزية أو العربية للعنصر أو الزر المطلوب (مثال: 'Anti-Gravity icon', 'save button', 'حقل البحث', 'search bar', 'close button')"
            },
            "text": {
                "type": "STRING",
                "description": "النص المطلوب كتابته داخل الحقل عند استخدام action='type_into'"
            },
            "option": {
                "type": "STRING",
                "description": "اسم الخيار المراد اختياره من القائمة المنسدلة عند action='select_option'"
            },
            "question": {
                "type": "STRING",
                "description": "سؤال أو هدف أو استفسار محدد حول محتوى الشاشة أو التطبيق عند action='read_and_decide', 'read_screen' أو 'read_area'"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "مسح النص الموجود قبل الكتابة في الحقل (افتراضي: true)"
            }
        },
        "required": ["action"]
    }
}


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    """Delegate to actions.screen_grounding.screen_grounding_handler."""
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower()

    if not action:
        return "لم يتم تحديد العملية (action) المطلوبة للتفاعل البصري مع الشاشة."

    try:
        from actions.screen_grounding import screen_grounding_handler
        return screen_grounding_handler(
            parameters=params,
            player=player,
            session_memory=session_memory,
        )
    except Exception as e:
        return f"❌ خطأ أثناء تنفيذ عملية الرؤية والتفاعل ({action}): {e}"
