import json
import urllib.parse
import urllib3
import requests

# Disable insecure request warning for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLUGIN = {
    "name": "obsidian",
    "description": (
        "Obsidian note-taking integration. Use this plugin when the user wants to:\n"
        "- Read a note from Obsidian (action='read', path='folder/note.md')\n"
        "- Write or append to a note (action='write', path='...', content='...', mode='replace'/'append'/'prepend')\n"
        "- Search in their Obsidian vault (action='search', query='...')\n"
        "- List notes in a folder (action='list', path='folder')\n"
        "- Read or append to today's Daily Note (action='daily', mode='read'/'append')\n"
        "- Delete a note (action='delete', path='...')\n"
        "- Sync Jarvis memory to/from Obsidian (action='sync_memory', direction='to_obsidian'/'from_obsidian')\n"
        "- Check Obsidian connection status (action='status')\n"
        "Do NOT use web_search or file_processor for Obsidian notes.\n"
        "Arabic triggers: 'افتح نوتة', 'اكتب في أوبسيديان', 'ابحث في الـ vault', 'سينك الذاكرة لأوبسيديان'"
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "The action to perform"},
            "path": {"type": "STRING"},
            "content": {"type": "STRING"},
            "query": {"type": "STRING"},
            "mode": {"type": "STRING"},
            "direction": {"type": "STRING"},
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "obsidian",
    "title": "Obsidian Integration",
    "fields": [
        {"key": "api_key", "label": "API Key", "type": "password"},
        {"key": "port", "label": "Port (default 27123)", "type": "text"},
        {"key": "use_https", "label": "Use HTTPS (port 27124)", "type": "checkbox"},
    ]
}

class _ObsidianClient:
    DEFAULT_PORT = 27123
    
    def __init__(self):
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config("obsidian") or {}
        self.api_key = cfg.get("api_key", "").strip()
        port_raw = cfg.get("port", self.DEFAULT_PORT)
        try:
            port = int(port_raw)
        except (ValueError, TypeError):
            port = self.DEFAULT_PORT
        use_https = str(cfg.get("use_https", "")).lower() in ("true", "1", "yes")
        proto = "https" if use_https else "http"
        self.base_url = f"{proto}://localhost:{port}"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "text/markdown",
        }
    
    def is_configured(self) -> bool:
        return bool(self.api_key)
    
    def not_configured_msg(self) -> str:
        return (
            "❌ Obsidian غير مُعدَّن. قم بتثبيت plugin 'Local REST API' في Obsidian، "
            "ثم اذهب لإعدادات جرفيس → Obsidian Integration واحفظ الـ API key."
        )
    
    def request(self, method: str, path: str, content: str = None,
                 extra_headers: dict = None, params: dict = None):
        """Makes HTTP request. Returns (status_code, response_text_or_dict).
        Raises ConnectionError if Obsidian is not running."""
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self.base_url}{path}"
        kwargs = {"headers": headers, "timeout": 5, "verify": False}
        if params:
            kwargs["params"] = params
        if content is not None:
            kwargs["data"] = content.encode("utf-8")
        resp = requests.request(method, url, **kwargs)
        return resp.status_code, resp

def _read(client, parameters):
    path = parameters.get("path", "")
    if not path: return "❌ الـ path مطلوب لعملية read"
    path = urllib.parse.quote(path, safe='/')
    status, resp = client.request("GET", f"/vault/{path}")
    if status == 200: return resp.text
    elif status == 404: return f"❌ الملف غير موجود: {path}"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _write(client, parameters):
    path = parameters.get("path", "")
    content = parameters.get("content", "")
    mode = parameters.get("mode", "replace")
    if not path or content == "": return "❌ الـ path والـ content مطلوبين"
    path = urllib.parse.quote(path, safe='/')
    
    if mode == "append":
        status, resp = client.request("PATCH", f"/vault/{path}", content=content, extra_headers={"Operation": "append"})
    elif mode == "prepend":
        status, resp = client.request("PATCH", f"/vault/{path}", content=content, extra_headers={"Operation": "prepend"})
    else: # replace
        status, resp = client.request("PUT", f"/vault/{path}", content=content)
        
    if status in (200, 201, 204): return f"✅ تم الحفظ في: {path} (mode={mode})"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _search(client, parameters):
    query = parameters.get("query", "")
    if not query: return "❌ الـ query مطلوب للبحث"
    status, resp = client.request("POST", "/search/simple/", params={"query": query, "contextLength": 100})
    if status == 200:
        res = resp.json()
        if not res: return "لم يتم العثور على نتائج."
        out = []
        for item in res:
            fn = item.get("filename", "")
            matches = item.get("matches", [])
            for m in matches:
                ctx = m.get("context", "")
                out.append(f"- **{fn}**: ...{ctx}...")
        return "\n".join(out)
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _list(client, parameters):
    path = parameters.get("path", "")
    path = urllib.parse.quote(path, safe='/')
    status, resp = client.request("GET", f"/vault/{path}/")
    if status == 200:
        res = resp.json()
        files = res.get("files", [])
        if not files: return "المجلد فارغ."
        return "\n".join(files)
    elif status == 404: return f"❌ المجلد غير موجود: {path}"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _daily(client, parameters):
    mode = parameters.get("mode", "read")
    content = parameters.get("content", "")
    if mode == "append":
        status, resp = client.request("PATCH", "/periodic/daily/", content=content, extra_headers={"Operation": "append"})
        if status in (200, 201, 204): return "✅ تم الإضافة للـ Daily Note"
    else:
        status, resp = client.request("GET", "/periodic/daily/")
        if status == 200: return resp.text
        
    if status == 404: return "❌ Daily Note غير موجود (ربما لم يتم إنشاؤه اليوم)"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _delete(client, parameters):
    path = parameters.get("path", "")
    if not path: return "❌ الـ path مطلوب لعملية الحذف"
    path = urllib.parse.quote(path, safe='/')
    status, resp = client.request("DELETE", f"/vault/{path}")
    if status in (200, 204): return f"✅ تم الحذف: {path}"
    elif status == 404: return f"❌ الملف غير موجود: {path}"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _sync_memory(client, parameters):
    direction = parameters.get("direction", "to_obsidian")
    from memory.memory_manager import load_memory
    path = urllib.parse.quote("Jarvis/Memory/jarvis_memory.md", safe='/')
    
    if direction == "from_obsidian":
        status, resp = client.request("GET", f"/vault/{path}")
        if status == 200: return resp.text
        elif status == 404: return "❌ ملف الذاكرة غير موجود في أوبسيديان."
        else: return f"❌ خطأ {status}: {resp.text[:200]}"
    else: # to_obsidian
        mem = load_memory()
        
        lines = ["# 🧠 Jarvis Memory"]
        import datetime
        now_str = datetime.datetime.now().strftime("%Y-%m-%d")
        lines.append(f"*آخر تحديث: {now_str}*\n")
        
        for category, items in mem.items():
            if not isinstance(items, dict) or not items:
                continue
            
            icon = "📝"
            if category == "identity": icon = "🪪"
            elif category == "preferences": icon = "⚙️"
            elif category == "projects": icon = "🚀"
            elif category == "relationships": icon = "👥"
            elif category == "wishes": icon = "✨"
            elif category == "notes": icon = "🗒️"
            
            lines.append(f"## {icon} {category.capitalize()}")
            for k, v in items.items():
                val = v.get("value", "") if isinstance(v, dict) else str(v)
                lines.append(f"- **{k}**: {val}")
            lines.append("")
        
        content = "\n".join(lines)
        status, resp = client.request("PUT", f"/vault/{path}", content=content)
        if status in (200, 201, 204): return "✅ تم مزامنة الذاكرة إلى أوبسيديان."
        else: return f"❌ خطأ {status}: {resp.text[:200]}"

def _status(client):
    status, resp = client.request("GET", "/")
    if status == 200:
        try:
            res = resp.json()
            version = res.get("status", "") or res.get("version", "unknown")
            return f"✅ متصل بأوبسيديان! (Version: {version})"
        except ValueError:
            return "✅ متصل بأوبسيديان!"
    elif status == 401: return "❌ API key خاطئ — تحقق من الإعدادات"
    else: return f"❌ خطأ {status}: {resp.text[:200]}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").strip().lower()
    client = _ObsidianClient()
    
    if action != "status" and not client.is_configured():
        return client.not_configured_msg()
    
    try:
        if action == "read": return _read(client, parameters)
        elif action == "write": return _write(client, parameters)
        elif action == "search": return _search(client, parameters)
        elif action == "list": return _list(client, parameters)
        elif action == "daily": return _daily(client, parameters)
        elif action == "delete": return _delete(client, parameters)
        elif action == "sync_memory": return _sync_memory(client, parameters)
        elif action == "status": return _status(client)
        else:
            return f"❌ action غير معروف: '{action}'. المتاح: read, write, search, list, daily, delete, sync_memory, status"
    except Exception as e:
        if "ConnectionRefused" in type(e).__name__ or "Connection" in type(e).__name__:
            return "❌ Obsidian غير مفتوح أو الـ Local REST API plugin غير مفعّل."
        return f"❌ خطأ في Obsidian plugin: {e}"
