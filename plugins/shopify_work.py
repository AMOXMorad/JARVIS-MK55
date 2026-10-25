"""
plugins/shopify_work.py — Native Shopify Work Shift & Order Automation Plugin for JARVIS Mark LIV.

Automates the exact personal Shopify workflow for AMOX:
1. Opens Shopify Admin in Google Chrome under the Work profile (Mohamed Work).
2. Reads the orders count next to "Orders" on the side of the page and scans for open orders.
3. For each open order:
   a. Clicks the order with Ctrl + Click to open it in a secondary browser tab.
   b. Switches to the order tab (Ctrl + Tab) and uses Gemini Vision to read:
      - Order code (the number next to the '#' symbol)
      - Customer name, phone number, governorate, shipping address
      - Product name, quantity, total price, and notes
   c. Writes the order data into the desktop Orders system (D:\\Work\\orders2\\الطلبات.xlsx & Orders.exe).
   d. Archives the order via the 3-dots menu at the top of the order page ('Archive').
   e. Closes the order tab (Ctrl + W).
   f. Refreshes (F5) the main Shopify orders page and continues until no open orders remain.
4. Enters an intelligent Standby loop to monitor for newly arriving orders.
5. Stops gracefully when the user sends the command "وقف الشفت" (stop_shift).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from actions.screen_processor import _capture_screen, _ensure_desktop
from core import gemini

try:
    from google.genai import types as gtypes
    _GENAI_TYPES_OK = True
except ImportError:
    _GENAI_TYPES_OK = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
    _AUTOMATION_OK = True
except ImportError:
    _AUTOMATION_OK = False

try:
    import openpyxl
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False

# Safe console encoding
for _s in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _s, None)
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PLUGIN = {
    "name": "shopify_work",
    "description": (
        "بلجن إدارة ومراقبة شفت أوردرات متجر شوبيفاي بريفكس في متصفح كروم ببروفايل العمل. "
        "يقوم بفتح الأوردرات الجديدة بـ Ctrl+Click، قراءة تفاصيل العميل والمنتج بكود الطلب (#)، "
        "تسجيلها في برنامج الاوردرات (الطلبات.xlsx) على سطح المكتب، أرشفتها عبر زر الـ 3 نقاط، "
        "والبقاء في وضع الـ Standby لمراقبة الأوردرات الجديدة حتى إصدار أمر 'وقف الشفت'. "
        "الإجراءات: start_shift (بدء الشفت والمراقبة) | stop_shift (إيقاف الشفت) | status (حالة الشفت) | process_once."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start_shift (افتراضي لبدء الشفت) | stop_shift (لإيقاف الشفت) | status (عرض الحالة) | process_once | open_orders"
            },
            "interval": {
                "type": "INTEGER",
                "description": "فترة الانتظار بين كل فحص في وضع الـ Standby بالثواني (افتراضي 60 ثانية)"
            }
        }
    }
}

SHOPIFY_ORDERS_URL = "https://admin.shopify.com/store/prifix1/orders?country=EG"
ORDERS_EXCEL_PATH = Path("D:/Work/orders2/الطلبات.xlsx")
ORDERS_EXE_PATH = Path("D:/Work/orders2/Orders.exe")
ORDERS_LNK_PATH = Path(os.path.expanduser("~/Desktop/Orders.lnk"))

# Global shift state
_SHIFT_RUNNING = False
_SHIFT_THREAD: threading.Thread | None = None
_SHIFT_LOCK = threading.Lock()
_SHIFT_STATS = {
    "started_at": None,
    "orders_processed": 0,
    "last_order_code": None,
    "status": "stopped",  # stopped | running | standby | processing
    "last_check_time": None,
    "log_history": []
}


def _record_order_to_excel(order_data: dict) -> bool:
    """
    Appends the extracted order directly into D:\\Work\\orders2\\الطلبات.xlsx
    matching the exact schema of the desktop Orders system.
    """
    if not _OPENPYXL_OK or not ORDERS_EXCEL_PATH.exists():
        print(f"[ShopifyWork] ⚠️ Excel file or openpyxl missing: {ORDERS_EXCEL_PATH}")
        return False

    try:
        wb = openpyxl.load_workbook(str(ORDERS_EXCEL_PATH))
        ws = wb["الطلبات"] if "الطلبات" in wb.sheetnames else wb.active

        today_str = datetime.datetime.now().strftime("%d/%m/%Y")
        customer_name = str(order_data.get("customer_name") or "").strip()
        phone = str(order_data.get("phone") or "").strip()
        governorate = str(order_data.get("governorate") or "").strip()
        address = str(order_data.get("address") or "").strip()
        qty = int(order_data.get("quantity") or 1)
        product_name = str(order_data.get("product_name") or "").strip()
        price = float(order_data.get("price") or 0.0)
        source = "شوبيفاي"
        delivery_date = None
        status = "لم يتم التنفيذ"
        payment_method = str(order_data.get("payment_method") or "دفع عند الاستلام").strip()
        notes = str(order_data.get("notes") or "").strip() or None
        order_code = str(order_data.get("order_code") or "").replace("#", "").strip()
        recipient = "مراد"

        # Check if order_code already exists to prevent duplicates
        for r in range(max(1, ws.max_row - 30), ws.max_row + 1):
            val = ws.cell(r, 15).value
            if val and str(val).strip() == order_code:
                print(f"[ShopifyWork] ℹ️ Order #{order_code} already exists in Excel, skipping duplicate row.")
                wb.close()
                return True

        row_data = [
            today_str,
            customer_name,
            phone,
            governorate,
            address,
            qty,
            product_name,
            price,
            source,
            delivery_date,
            status,
            payment_method,
            notes,
            None,  # رقم الاذان
            order_code,
            recipient
        ]

        ws.append(row_data)
        wb.save(str(ORDERS_EXCEL_PATH))
        wb.close()
        print(f"[ShopifyWork] ✅ Recorded Order #{order_code} ({customer_name}) into {ORDERS_EXCEL_PATH.name} successfully.")
        return True
    except Exception as e:
        print(f"[ShopifyWork] ❌ Failed to record order to Excel: {e}")
        return False


def _ensure_shopify_open() -> bool:
    """Ensure Chrome with Profile 2 (work) has the Shopify Orders page active and maximized."""
    try:
        from actions.browser_control import _launch_chrome_with_profile
        from actions.computer_control import _maximize_window
        _launch_chrome_with_profile(
            profile_query="work",
            bookmark="shopify",
            url=SHOPIFY_ORDERS_URL
        )
        time.sleep(1.0)
        _maximize_window("chrome")
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"[ShopifyWork] Error ensuring Shopify open: {e}")
        return False


def _scan_for_open_orders() -> dict:
    """
    Captures the Shopify orders page and uses Gemini Vision to detect open/unfulfilled orders.
    Returns dict: {"has_orders": bool, "count": int, "first_order_point": [y, x], "order_id": str}
    """
    _ensure_desktop()
    time.sleep(0.8)
    w, h = pyautogui.size()
    img_bytes, mime = _capture_screen()

    prompt = (
        f"This is a screenshot of the Shopify Admin Orders page in Google Chrome ({w}x{h} px).\n"
        f"Follow these EXACT rules to detect unarchived orders:\n"
        f"1. LOOK AT THE LEFT SIDEBAR MENU: Under 'Home', find the 'Orders' navigation item.\n"
        f"   - Look for the badge/pill next to 'Orders' (e.g. showing a number like 2, 1, 3).\n"
        f"   - If there is NO badge next to 'Orders' (or the number is 0), then ALL orders are archived and written! Reply immediately:\n"
        f'     {{"has_orders": false, "count": 0, "order_id": "", "point": null}}\n'
        f"2. IF THERE IS A BADGE with a number > 0 (e.g. 2):\n"
        f"   - That number is the exact count of pending unarchived orders!\n"
        f"   - The pending unarchived orders are the topmost rows of the table.\n"
        f"   - Find the clickable link of the FIRST order in the table (e.g. #53689).\n"
        f"   - Return its order_id and normalized center coordinates [y, x] from 0 to 1000.\n"
        f"Reply in STRICT JSON format ONLY:\n"
        f"{{\n"
        f'  "has_orders": true,\n'
        f'  "count": <number from the badge next to Orders>,\n'
        f'  "order_id": "<first row order id, e.g. #53689>",\n'
        f'  "point": [<normalized y>, <normalized x>]\n'
        f"}}\n"
    )

    if _GENAI_TYPES_OK:
        part = gtypes.Part.from_bytes(data=img_bytes, mime_type=mime)
    else:
        part = {"inline_data": {"mime_type": mime, "data": img_bytes}}

    try:
        resp = gemini.call([part, prompt], tier=gemini.FAST, timeout_ms=25_000)
        txt = (getattr(resp, "text", None) or "").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return data
    except Exception as e:
        print(f"[ShopifyWork] Vision scan error: {e}")

    return {"has_orders": False, "count": 0, "order_id": "", "point": None}


def _extract_order_details() -> dict:
    """
    Captures the opened Shopify order details page and extracts all order & customer data.
    Also locates the 3-dots ('...') more actions menu button.
    """
    _ensure_desktop()
    time.sleep(1.0)
    w, h = pyautogui.size()
    img_bytes, mime = _capture_screen()

    prompt = (
        f"This is a screenshot of an opened Shopify order details page ({w}x{h} px). "
        f"Extract all required order and customer information accurately: "
        f"1. order_code: The order number right next to the '#' sign (e.g. #1045 -> '1045'). "
        f"2. customer_name: Full customer name from the Customer card on the right. "
        f"3. phone: Customer phone number from the contact card on the right. "
        f"4. governorate: Shipping governorate / city in Egypt (e.g. القاهرة, الجيزة, الاسكندرية, الغردقة, etc.). "
        f"5. address: Full shipping street address and apartment/details. "
        f"6. product_name: Item(s) name listed in the order. "
        f"7. quantity: Total quantity (integer, default 1). "
        f"8. price: Total order price in EGP (numeric, e.g. 3445). "
        f"9. notes: Any customer or order delivery notes. "
        f"10. dots_point: The normalized coordinates [y, x] (0 to 1000) of the '...' (More actions / 3 dots) button near the top header of the order page. "
        f"Reply ONLY in STRICT JSON format:\n"
        f"{{\n"
        f'  "order_code": "...",\n'
        f'  "customer_name": "...",\n'
        f'  "phone": "...",\n'
        f'  "governorate": "...",\n'
        f'  "address": "...",\n'
        f'  "product_name": "...",\n'
        f'  "quantity": 1,\n'
        f'  "price": 0.0,\n'
        f'  "notes": "",\n'
        f'  "dots_point": [y, x]\n'
        f"}}"
    )

    if _GENAI_TYPES_OK:
        part = gtypes.Part.from_bytes(data=img_bytes, mime_type=mime)
    else:
        part = {"inline_data": {"mime_type": mime, "data": img_bytes}}

    try:
        resp = gemini.call([part, prompt], tier=gemini.SMART, timeout_ms=30_000)
        txt = (getattr(resp, "text", None) or "").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return data
    except Exception as e:
        print(f"[ShopifyWork] Order details extraction error: {e}")

    return {}


def _archive_current_order(dots_point: list[float] | None = None) -> bool:
    """Clicks the 3-dots menu on the order page and chooses 'Archive'."""
    w, h = pyautogui.size()
    clicked_dots = False

    if dots_point and len(dots_point) >= 2:
        dy, dx = dots_point[0], dots_point[1]
        px = int((dx / 1000.0) * w) if dx <= 1000 else int(dx)
        py = int((dy / 1000.0) * h) if dy <= 1000 else int(dy)
        pyautogui.click(px, py)
        clicked_dots = True
        time.sleep(0.6)

    if not clicked_dots:
        # Fallback to visual locator
        try:
            from actions.screen_grounding import locate_ui_element
            el = locate_ui_element("3 dots menu or More actions icon button at the top header of Shopify order page")
            if el:
                pyautogui.click(el["cx"], el["cy"])
                clicked_dots = True
                time.sleep(0.6)
        except Exception:
            pass

    # Click Archive option from dropdown menu
    try:
        from actions.screen_grounding import locate_ui_element
        el = locate_ui_element("Archive or Archive order option in the dropdown menu")
        if el:
            pyautogui.click(el["cx"], el["cy"])
        else:
            # Try pressing Down arrow then Enter in menu
            pyautogui.press("down")
            time.sleep(0.1)
            pyautogui.press("enter")
        time.sleep(1.0)
        return True
    except Exception as e:
        print(f"[ShopifyWork] Archive click error: {e}")
        return False


def _process_single_order(order_info: dict, player=None) -> bool:
    """
    Executes the full order processing sequence:
    1. Ctrl+Click on order row.
    2. Switch to order tab.
    3. Extract order details via vision.
    4. Write into Excel / desktop orders.
    5. Archive order via 3-dots menu.
    6. Close tab (Ctrl+W) & reload main page (F5).
    """
    point = order_info.get("point")
    if not point or len(point) < 2:
        print("[ShopifyWork] ⚠️ Missing click point for order row.")
        return False

    w, h = pyautogui.size()
    oy, ox = point[0], point[1]
    px = int((ox / 1000.0) * w) if ox <= 1000 else int(ox)
    py = int((oy / 1000.0) * h) if oy <= 1000 else int(oy)

    # 1. Ctrl + Click to open in new tab
    print(f"[ShopifyWork] 🖱️ Ctrl+Clicking order row at ({px}, {py})...")
    pyautogui.keyDown("ctrl")
    time.sleep(0.1)
    pyautogui.click(px, py)
    time.sleep(0.1)
    pyautogui.keyUp("ctrl")
    time.sleep(0.8)

    # 2. Switch to the newly opened tab
    pyautogui.hotkey("ctrl", "tab")
    time.sleep(2.5)  # allow order page to load

    # 3. Read order details with Gemini Vision
    details = _extract_order_details()
    order_code = details.get("order_code") or order_info.get("order_id", "").replace("#", "")
    customer_name = details.get("customer_name") or "عميل شوبيفاي"

    if player:
        try:
            player.write_log(f"SHOPIFY: قراءة الطلب #{order_code} للعميل '{customer_name}'...")
        except Exception:
            pass

    # 4. Record into Desktop Orders Excel
    saved = _record_order_to_excel(details)

    # Launch or focus Orders.exe on Desktop if shortcut exists
    if ORDERS_LNK_PATH.exists():
        try:
            os.startfile(str(ORDERS_LNK_PATH))
        except Exception:
            pass

    # 5. Archive order via 3-dots menu
    dots_pt = details.get("dots_point")
    _archive_current_order(dots_pt)
    time.sleep(0.8)

    # 6. Close the order tab
    pyautogui.hotkey("ctrl", "w")
    time.sleep(0.8)

    # 7. Reload main Shopify orders page
    pyautogui.press("f5")
    time.sleep(2.5)

    _SHIFT_STATS["orders_processed"] += 1
    _SHIFT_STATS["last_order_code"] = order_code
    msg = f"تم تسجيل وأرشفة الطلب #{order_code} بنجاح ({customer_name})."
    _SHIFT_STATS["log_history"].append(msg)
    if player:
        try:
            player.write_log(f"SHOPIFY: ✅ {msg}")
        except Exception:
            pass

    return True


def _shift_worker_loop(interval: int = 60, player=None):
    """Continuous background shift loop that processes orders and stands by."""
    global _SHIFT_RUNNING
    print(f"[ShopifyWork] 🚀 Shift worker loop started (standby interval: {interval}s).")
    _SHIFT_STATS["status"] = "running"
    _SHIFT_STATS["started_at"] = datetime.datetime.now().strftime("%I:%M %p")

    while _SHIFT_RUNNING:
        try:
            _SHIFT_STATS["last_check_time"] = datetime.datetime.now().strftime("%I:%M %p")
            _ensure_shopify_open()

            # Scan for open orders
            _SHIFT_STATS["status"] = "scanning"
            scan_res = _scan_for_open_orders()

            if scan_res.get("has_orders") and scan_res.get("point"):
                _SHIFT_STATS["status"] = "processing"
                _process_single_order(scan_res, player=player)
                # Continue immediately to process next order without sleeping
                time.sleep(1.0)
                continue

            # No open orders -> Enter Standby
            _SHIFT_STATS["status"] = "standby"
            print(f"[ShopifyWork] 💤 No pending orders found. Standing by for {interval}s...")

            # Sleep in 1-second chunks so stop_shift responds instantly
            for _ in range(interval):
                if not _SHIFT_RUNNING:
                    break
                time.sleep(1.0)

            # Refresh page before next cycle
            if _SHIFT_RUNNING:
                _ensure_shopify_open()
                pyautogui.press("f5")
                time.sleep(2.5)

        except Exception as e:
            print(f"[ShopifyWork] ⚠️ Error in shift loop: {e}")
            time.sleep(5.0)

    _SHIFT_STATS["status"] = "stopped"
    print("[ShopifyWork] 🛑 Shift worker loop ended.")
    if player:
        try:
            player.write_log(f"SHOPIFY: 🛑 تم إيقاف شفت شوبيفاي. إجمالي الطلبات: {_SHIFT_STATS['orders_processed']}")
        except Exception:
            pass


def start_shift(interval: int = 60, player=None) -> str:
    """Starts the automated Shopify shift worker thread."""
    global _SHIFT_RUNNING, _SHIFT_THREAD
    with _SHIFT_LOCK:
        if _SHIFT_RUNNING:
            return (
                f"⚠️ شفت شوبيفاي قيد التشغيل بالفعل حالياً في الخلفية.\n"
                f"• الحالة: {_SHIFT_STATS['status'].upper()}\n"
                f"• الطلبات المكتملة: {_SHIFT_STATS['orders_processed']}\n"
                f"• آخر طلب: #{_SHIFT_STATS['last_order_code'] or 'لا يوجد'}\n"
                f"👉 يمكنك قول 'حالة الشفت' للمتابعة أو 'وقف الشفت' لإنهاء العمل."
            )

        _SHIFT_RUNNING = True
        _SHIFT_THREAD = threading.Thread(
            target=_shift_worker_loop,
            kwargs={"interval": interval, "player": player},
            daemon=True,
            name="ShopifyShiftWorker"
        )
        _SHIFT_THREAD.start()

    return (
        f"🚀 تم بدء شفت شوبيفاي الآلي ومراقبة الأوردرات بنجاح!\n"
        f"• المتجر: بريفكس (Shopify Admin)\n"
        f"• المتصفح: كروم ببروفايل العمل (Mohamed Work)\n"
        f"• آلية العمل: فحص الأوردرات المفتوحة ⬅️ فتح بـ Ctrl+Click ⬅️ قراءة بيانات العميل والكود (#) ⬅️ الحفظ في برنامج الاوردرات ⬅️ الأرشفة من الـ 3 نقاط ⬅️ غلق التاب وتحديث الصفحة ⬅️ وضع الـ Standby التلقائي.\n"
        f"🛑 سيتوقف الشفت فقط عند قولك: 'وقف الشفت'."
    )


def stop_shift() -> str:
    """Stops the running automated Shopify shift loop."""
    global _SHIFT_RUNNING
    with _SHIFT_LOCK:
        if not _SHIFT_RUNNING:
            return "ℹ️ شفت شوبيفاي متوقف حالياً بالفعل."
        _SHIFT_RUNNING = False

    return (
        f"🛑 تم إيقاف شفت شوبيفاي بنجاح!\n"
        f"• إجمالي الأوردرات المعالجة خلال الشفت: {_SHIFT_STATS['orders_processed']}\n"
        f"• آخر كود تم تسجيله: #{_SHIFT_STATS['last_order_code'] or 'لا يوجد'}\n"
        f"• وقت البدء: {_SHIFT_STATS['started_at'] or 'غير محدد'}\n"
        f"• النظام الآن في وضع الاستعداد العادي."
    )


def get_status() -> str:
    """Returns current shift statistics and state."""
    state = "نشط ومستمر (RUNNING)" if _SHIFT_RUNNING else "متوقف (STOPPED)"
    sub_state = _SHIFT_STATS["status"].upper()
    return (
        f"📊 تقرير شفت شوبيفاي الحالي:\n"
        f"• حالة الشفت: **{state}** ({sub_state})\n"
        f"• الأوردرات المعالجة: **{_SHIFT_STATS['orders_processed']}** أوردر\n"
        f"• آخر كود تم تسجيله: #{_SHIFT_STATS['last_order_code'] or 'لا يوجد'}\n"
        f"• وقت البدء: {_SHIFT_STATS['started_at'] or 'لم يبدأ بعد'}\n"
        f"• آخر فحص: {_SHIFT_STATS['last_check_time'] or 'لا يوجد'}"
    )


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    """Entry point invoked by core.plugin_loader."""
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip() or "start_shift"
    interval = int(params.get("interval") or 60)

    # 1. START SHIFT (DEFAULT FOR INTENT)
    if action in ("start_shift", "start", "work", "run_shift", "shift", "ابدأ", "شغل"):
        return start_shift(interval=interval, player=player)

    # 2. STOP SHIFT
    if action in ("stop_shift", "stop", "halt", "end", "وقف", "انهاء", "الغاء"):
        return stop_shift()

    # 3. STATUS REPORT
    if action in ("status", "report", "check", "stats", "حالة"):
        return get_status()

    # 4. PROCESS ONCE (SINGLE SCAN)
    if action in ("process_once", "scan_once", "single"):
        _ensure_shopify_open()
        res = _scan_for_open_orders()
        if res.get("has_orders") and res.get("point"):
            _process_single_order(res, player=player)
            return f"✅ تم فحص ومعالجة الطلب {res.get('order_id')} وأرشفته بنجاح."
        return "ℹ️ تم فحص صفحة شوبيفاي: لا توجد أوردرات مفتوحة حالياً."

    # 5. OPEN ORDERS PAGE ONLY
    if action in ("open_orders", "open", "launch"):
        _ensure_shopify_open()
        return f"✅ تم فتح صفحة أوردرات شوبيفاي في كروم ببروفايل العمل وتكبير الشاشة."

    return f"Unknown shopify_work action: '{action}'. Available: start_shift, stop_shift, status, process_once, open_orders."
