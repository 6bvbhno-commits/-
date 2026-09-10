"""
داشبورد سري لإرسال أكواد خصم أمازون — محمي بـ DASHBOARD_SECRET فقط لصاحب البوت.

الأمان:
  • بدون DASHBOARD_SECRET → الداشبورد لا يشتغل أصلاً
  • مسار مخفي /d/<path>/
  • جلسة موقّعة HMAC بعد إدخال السر
  • X-Robots-Tag: noindex + لا يوجد رابط عام
  • حد محاولات تسجيل الدخول
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import broadcast as _bc
import users_db as _users
from amazon_utils import get_affiliate_tag
from config import (
    DASHBOARD_PATH,
    DASHBOARD_PORT,
    DASHBOARD_SECRET,
)

logger = logging.getLogger(__name__)

_SESSION_TTL = 12 * 3600  # 12 ساعة
_MAX_LOGIN_FAILS = 8
_LOGIN_WINDOW = 900  # 15 دقيقة
_login_fails: dict[str, list[float]] = {}
_server_started = False


def _secret_ok() -> bool:
    return bool(DASHBOARD_SECRET) and len(DASHBOARD_SECRET) >= 12


def _dash_path() -> str:
    """مسار مخفي — من البيئة أو مشتق من السر (لا يُخمن بسهولة)."""
    if DASHBOARD_PATH:
        return DASHBOARD_PATH
    if not _secret_ok():
        return "disabled"
    digest = hashlib.sha256(f"dash:{DASHBOARD_SECRET}".encode()).hexdigest()[:20]
    return f"x{digest}"


def _sign(payload: str) -> str:
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def _make_session_token() -> str:
    exp = int(time.time()) + _SESSION_TTL
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def _valid_session(token: str | None) -> bool:
    if not token or not _secret_ok():
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, sig = parts
    payload = f"{exp_s}.{nonce}"
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    return exp >= int(time.time())


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    # Railway قد يمرّر X-Forwarded-For
    xf = handler.headers.get("X-Forwarded-For", "")
    if xf:
        return xf.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else "unknown"


def _login_allowed(ip: str) -> bool:
    now = time.time()
    buf = _login_fails.setdefault(ip, [])
    buf[:] = [t for t in buf if now - t < _LOGIN_WINDOW]
    return len(buf) < _MAX_LOGIN_FAILS


def _record_login_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.time())


def _html_page(body: str, *, title: str = "لوحة التحكم السرية") -> bytes:
    page = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow,noarchive"/>
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet"/>
<style>
:root {{
  --bg0:#0c1117; --bg1:#141b24; --bg2:#1c2633;
  --line:#2a3647; --text:#e8eef6; --muted:#8b9bb0;
  --accent:#ff9900; --accent2:#ffb84d; --ok:#3dd68c; --danger:#ff6b6b;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; min-height:100vh; color:var(--text);
  font-family:"IBM Plex Sans Arabic",sans-serif;
  background:
    radial-gradient(1200px 600px at 100% -10%, rgba(255,153,0,.16), transparent 55%),
    radial-gradient(900px 500px at -10% 100%, rgba(61,214,140,.08), transparent 50%),
    linear-gradient(165deg, var(--bg0), #0a0e14 40%, var(--bg1));
}}
.wrap {{ max-width:920px; margin:0 auto; padding:28px 18px 60px; }}
.brand {{
  font-size:clamp(1.6rem, 4vw, 2.2rem); font-weight:700; letter-spacing:-.02em;
  margin:0 0 6px;
}}
.brand span {{ color:var(--accent); }}
.sub {{ color:var(--muted); margin:0 0 22px; font-size:.95rem; }}
.grid {{ display:grid; gap:14px; grid-template-columns:repeat(3,1fr); }}
@media (max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} }}
.stat {{
  background:linear-gradient(180deg, var(--bg2), var(--bg1));
  border:1px solid var(--line); border-radius:14px; padding:16px 18px;
}}
.stat b {{ display:block; font-size:1.7rem; font-weight:700; margin-top:4px; }}
.stat small {{ color:var(--muted); }}
.panel {{
  margin-top:18px; background:rgba(20,27,36,.92);
  border:1px solid var(--line); border-radius:16px; padding:20px;
  backdrop-filter: blur(8px);
}}
.panel h2 {{ margin:0 0 14px; font-size:1.15rem; }}
label {{ display:block; color:var(--muted); font-size:.85rem; margin:12px 0 6px; }}
input, textarea, select {{
  width:100%; background:var(--bg0); color:var(--text);
  border:1px solid var(--line); border-radius:10px; padding:12px 14px;
  font:inherit; outline:none;
}}
input:focus, textarea:focus {{ border-color:var(--accent); }}
textarea {{ min-height:110px; resize:vertical; }}
.mono {{ font-family:"JetBrains Mono",monospace; letter-spacing:.04em; }}
.row {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
button, .btn {{
  appearance:none; border:0; cursor:pointer; border-radius:10px;
  padding:12px 18px; font:inherit; font-weight:600;
  background:linear-gradient(135deg, var(--accent), var(--accent2));
  color:#111; transition: transform .12s ease, filter .12s;
}}
button:hover {{ filter:brightness(1.05); transform:translateY(-1px); }}
button.secondary {{ background:var(--bg2); color:var(--text); border:1px solid var(--line); }}
button.danger {{ background:linear-gradient(135deg,#c0392b,#ff6b6b); color:#fff; }}
.preview {{
  margin-top:14px; white-space:pre-wrap; background:#0a0e14;
  border:1px dashed var(--line); border-radius:12px; padding:14px;
  font-size:.92rem; line-height:1.7;
}}
.msg {{ margin-top:12px; padding:10px 12px; border-radius:10px; display:none; }}
.msg.show {{ display:block; }}
.msg.ok {{ background:rgba(61,214,140,.12); color:var(--ok); border:1px solid rgba(61,214,140,.35); }}
.msg.err {{ background:rgba(255,107,107,.12); color:var(--danger); border:1px solid rgba(255,107,107,.35); }}
table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
th, td {{ text-align:right; padding:10px 8px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-weight:600; }}
.lock {{
  max-width:420px; margin:12vh auto; background:var(--bg1);
  border:1px solid var(--line); border-radius:16px; padding:28px 24px;
}}
.lock p {{ color:var(--muted); margin:8px 0 18px; }}
.badge {{
  display:inline-block; padding:3px 8px; border-radius:999px; font-size:.75rem;
  background:rgba(255,153,0,.15); color:var(--accent); border:1px solid rgba(255,153,0,.35);
}}
.hint {{ color:var(--muted); font-size:.8rem; margin-top:8px; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
    return page.encode("utf-8")


def _login_html(error: str = "") -> bytes:
    err = f'<div class="msg err show">{error}</div>' if error else ""
    body = f"""
<div class="lock">
  <div class="badge">سري · مالك البوت فقط</div>
  <h1 class="brand" style="margin-top:12px">دخول <span>الداشبورد</span></h1>
  <p>أدخل السر من Railway Variables — لا يُشارك مع أحد.</p>
  <form method="POST" action="login">
    <label>DASHBOARD_SECRET</label>
    <input class="mono" type="password" name="secret" autocomplete="current-password" required autofocus/>
    {err}
    <div class="row"><button type="submit">دخول آمن</button></div>
  </form>
  <p class="hint">المسار مخفي · الجلسة 12 ساعة · محاولات الدخول محدودة</p>
</div>"""
    return _html_page(body, title="دخول سري")


def _dashboard_html(stats: dict, jobs: list, logs: list, riyadh: str) -> bytes:
    tag = get_affiliate_tag()
    jobs_rows = "".join(
        f"<tr><td>{j.get('run_at_riyadh','')}</td><td class='mono'>{j.get('code','')}</td>"
        f"<td>{j.get('status','')}</td>"
        f"<td><button class='secondary' onclick=\"cancelJob('{j.get('id','')}')\">إلغاء</button></td></tr>"
        for j in jobs
        if j.get("status") == "scheduled"
    ) or "<tr><td colspan='4'>لا يوجد إرسال مجدول</td></tr>"
    log_rows = "".join(
        f"<tr><td>{l.get('id')}</td><td class='mono'>{l.get('code') or '—'}</td>"
        f"<td>{l.get('status')}</td><td>{l.get('sent_ok',0)}/{l.get('total',0)}</td></tr>"
        for l in logs
    ) or "<tr><td colspan='4'>لا سجل بعد</td></tr>"

    body = f"""
<div class="wrap">
  <div class="badge">لوحة سرية · تاق العمولة {tag}</div>
  <h1 class="brand">تنبيهات <span>أكواد الخصم</span></h1>
  <p class="sub">توقيت الرياض الآن: <b class="mono">{riyadh}</b> — جاهز لجدولة 9:00 م</p>

  <div class="grid">
    <div class="stat"><small>مستخدمون يستقبلون</small><b>{stats.get('active',0)}</b></div>
    <div class="stat"><small>إجمالي المسجّلين</small><b>{stats.get('total',0)}</b></div>
    <div class="stat"><small>محظورون / أوقفوا</small><b>{stats.get('blocked',0)}</b></div>
  </div>

  <div class="panel">
    <h2>🔥 إرسال كود خصم أمازون</h2>
    <label>الكود</label>
    <input id="code" class="mono" placeholder="مثال: SAVE20" />
    <label>العنوان</label>
    <input id="title" value="كود خصم أمازون اليوم" />
    <label>تفاصيل إضافية (اختياري)</label>
    <textarea id="extra" placeholder="نسبة الخصم، الأقسام المشمولة، ينتهي منتصف الليل..."></textarea>
    <label>رابط الزر (اختياري — افتراضي أمازون بتاقك)</label>
    <input id="btnurl" placeholder="https://www.amazon.sa/..." />

    <div class="row">
      <button onclick="preview()">معاينة</button>
      <button class="secondary" onclick="scheduleNine()">جدولة الساعة 9 م (الرياض)</button>
      <button class="danger" onclick="sendNow()">إرسال الآن لكل المستخدمين</button>
    </div>
    <div id="flash" class="msg"></div>
    <div id="preview" class="preview" style="display:none"></div>
    <p class="hint">الإرسال يذهب فقط للمستخدمين الذين فتحوا البوت (/start). الزر يحمل تاق العمولة {tag}.</p>
  </div>

  <div class="panel">
    <h2>⏰ المجدول</h2>
    <table><thead><tr><th>الوقت (الرياض)</th><th>الكود</th><th>الحالة</th><th></th></tr></thead>
    <tbody>{jobs_rows}</tbody></table>
  </div>

  <div class="panel">
    <h2>📜 آخر الإرسالات</h2>
    <table><thead><tr><th>#</th><th>الكود</th><th>الحالة</th><th>نجاح/الكل</th></tr></thead>
    <tbody>{log_rows}</tbody></table>
  </div>
</div>
<script>
const base = location.pathname.replace(/\\/?$/, '/');
function flash(text, ok) {{
  const el = document.getElementById('flash');
  el.textContent = text;
  el.className = 'msg show ' + (ok ? 'ok' : 'err');
}}
function payload() {{
  return {{
    code: document.getElementById('code').value.trim(),
    title: document.getElementById('title').value.trim(),
    extra: document.getElementById('extra').value.trim(),
    button_url: document.getElementById('btnurl').value.trim(),
  }};
}}
async function api(path, body) {{
  const res = await fetch(base + path, {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    credentials: 'same-origin',
    body: JSON.stringify(body || {{}}),
  }});
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}}
async function preview() {{
  try {{
    const d = await api('api/preview', payload());
    const el = document.getElementById('preview');
    el.style.display = 'block';
    el.textContent = d.preview + '\\n\\n🔗 ' + d.button_url;
    flash('معاينة جاهزة — لم يُرسل شيء', true);
  }} catch (e) {{ flash(e.message, false); }}
}}
async function scheduleNine() {{
  const p = payload();
  if (!p.code) return flash('اكتب الكود أولاً', false);
  if (!confirm('جدولة الإرسال الساعة 9 مساءً بتوقيت الرياض؟')) return;
  try {{
    const d = await api('api/schedule', {{...p, hour: 21, minute: 0}});
    flash('تمت الجدولة: ' + d.run_at_riyadh + ' (باقي ' + d.seconds_left + ' ث)', true);
    setTimeout(() => location.reload(), 900);
  }} catch (e) {{ flash(e.message, false); }}
}}
async function sendNow() {{
  const p = payload();
  if (!p.code) return flash('اكتب الكود أولاً', false);
  if (!confirm('إرسال الآن لكل المستخدمين؟ لا يمكن التراجع.')) return;
  try {{
    flash('جاري الإرسال...', true);
    const d = await api('api/send', p);
    flash('تم: نجاح ' + d.sent_ok + ' / فشل ' + d.sent_fail + ' من ' + d.total, true);
    setTimeout(() => location.reload(), 1200);
  }} catch (e) {{ flash(e.message, false); }}
}}
async function cancelJob(id) {{
  try {{
    await api('api/cancel', {{ job_id: id }});
    location.reload();
  }} catch (e) {{ flash(e.message, false); }}
}}
</script>"""
    return _html_page(body)


class _DashHandler(BaseHTTPRequestHandler):
    server_version = "SecretDash/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # لا نسجّل query/secret
        logger.info("dash %s %s", self.command, urlparse(self.path).path)

    def _send(self, code: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict, headers: dict | None = None) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8", headers)

    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        out: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _authed(self) -> bool:
        return _valid_session(self._cookies().get("dash_session"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 100_000:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20_000:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        qs = parse_qs(raw, keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in qs.items()}

    def _route(self) -> tuple[str, str]:
        """يرجع (prefix, rest) حيث prefix هو المسار السري."""
        path = urlparse(self.path).path
        base = "/" + _dash_path()
        if path.rstrip("/") == base:
            return _dash_path(), ""
        if path.startswith(base + "/"):
            return _dash_path(), path[len(base) + 1 :].strip("/")
        return "", path.strip("/")

    def do_GET(self) -> None:
        # صحة عامة — بدون كشف أي شيء عن الداشبورد
        if urlparse(self.path).path in ("/healthz", "/health"):
            self._json(200, {"status": "ok"})
            return

        if not _secret_ok():
            self._send(404, b"Not Found", "text/plain")
            return

        prefix, rest = self._route()
        if prefix != _dash_path():
            # أي مسار آخر = 404 صامت (ما فيش تلميح بوجود داشبورد)
            self._send(404, b"Not Found", "text/plain")
            return

        if rest in ("", "index", "index.html"):
            if not self._authed():
                self._send(200, _login_html(), "text/html; charset=utf-8")
                return
            stats = _users.count_users()
            jobs = _bc.list_jobs()
            logs = _users.recent_broadcasts(12)
            riyadh = _bc.riyadh_now().strftime("%Y-%m-%d %H:%M")
            self._send(200, _dashboard_html(stats, jobs, logs, riyadh), "text/html; charset=utf-8")
            return

        if rest == "api/status":
            if not self._authed():
                self._json(401, {"error": "غير مصرح"})
                return
            self._json(
                200,
                {
                    "users": _users.count_users(),
                    "jobs": _bc.list_jobs(),
                    "logs": _users.recent_broadcasts(10),
                    "riyadh": _bc.riyadh_now().strftime("%Y-%m-%d %H:%M"),
                    "tag": get_affiliate_tag(),
                },
            )
            return

        self._send(404, b"Not Found", "text/plain")

    def do_POST(self) -> None:
        if not _secret_ok():
            self._send(404, b"Not Found", "text/plain")
            return

        prefix, rest = self._route()
        if prefix != _dash_path():
            self._send(404, b"Not Found", "text/plain")
            return

        # تسجيل الدخول
        if rest == "login":
            ip = _client_ip(self)
            if not _login_allowed(ip):
                self._send(
                    429,
                    _login_html("محاولات كثيرة — انتظر ربع ساعة"),
                    "text/html; charset=utf-8",
                )
                return
            form = self._read_form()
            secret = (form.get("secret") or "").strip()
            if not hmac.compare_digest(secret, DASHBOARD_SECRET):
                _record_login_fail(ip)
                self._send(401, _login_html("السر غير صحيح"), "text/html; charset=utf-8")
                return
            token = _make_session_token()
            cookie = (
                f"dash_session={token}; Path=/{_dash_path()}; HttpOnly; SameSite=Strict; Max-Age={_SESSION_TTL}"
            )
            # Secure فقط خلف HTTPS (Railway)
            if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
                cookie += "; Secure"
            self.send_response(303)
            self.send_header("Location", f"/{_dash_path()}/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.end_headers()
            return

        if not self._authed():
            self._json(401, {"error": "انتهت الجلسة — أعد الدخول"})
            return

        data = self._read_json()

        if rest == "api/preview":
            code = (data.get("code") or "").strip()
            msg = _bc.format_discount_message(
                code=code or "XXXX",
                title=(data.get("title") or "كود خصم أمازون اليوم"),
                extra=(data.get("extra") or ""),
                when_label="اليوم",
            )
            url = (data.get("button_url") or "").strip() or _bc.default_button_url()
            self._json(200, {"preview": msg, "button_url": url, "tag": get_affiliate_tag()})
            return

        if rest == "api/schedule":
            code = (data.get("code") or "").strip()
            if not code:
                self._json(400, {"error": "الكود مطلوب"})
                return
            hour = int(data.get("hour") or 21)
            minute = int(data.get("minute") or 0)

            async def _sched():
                return await _bc.schedule_discount_broadcast(
                    code=code,
                    title=(data.get("title") or "كود خصم أمازون اليوم"),
                    extra=(data.get("extra") or ""),
                    button_url=(data.get("button_url") or ""),
                    hour=hour,
                    minute=minute,
                )

            try:
                result = _run_coro(_sched())
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if rest == "api/send":
            code = (data.get("code") or "").strip()
            if not code:
                self._json(400, {"error": "الكود مطلوب"})
                return

            async def _send():
                return await _bc.send_discount_broadcast(
                    code=code,
                    title=(data.get("title") or "كود خصم أمازون اليوم"),
                    extra=(data.get("extra") or ""),
                    button_url=(data.get("button_url") or ""),
                    when_label="الحين",
                )

            try:
                result = _run_coro(_send())
                if not result.get("ok"):
                    self._json(400, result)
                else:
                    self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if rest == "api/cancel":
            job_id = (data.get("job_id") or "").strip()

            async def _cancel():
                return await _bc.cancel_job(job_id)

            try:
                ok = _run_coro(_cancel())
                self._json(200 if ok else 404, {"ok": ok})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})


_loop_holder: dict = {}


def _run_coro(coro):
    """يشغّل coroutine على event loop البوت من خيط HTTP."""
    loop = _loop_holder.get("loop")
    if loop is None:
        raise RuntimeError("event loop غير جاهز")
    fut = asyncio_run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=600)


def asyncio_run_coroutine_threadsafe(coro, loop):
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, loop)


def start_dashboard_server(loop) -> str | None:
    """
    يبدأ خادم HTTP على PORT.
    • /healthz عام (لـ Railway)
    • الداشبورد فقط إن وُجد DASHBOARD_SECRET قوي — على مسار مخفي
    """
    global _server_started
    if _server_started:
        return f"/{_dash_path()}/" if _secret_ok() else None

    _loop_holder["loop"] = loop
    port = DASHBOARD_PORT

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), _DashHandler)
    except OSError as e:
        logger.error("تعذّر تشغيل HTTP على المنفذ %s: %s", port, e)
        return None

    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="secret-dashboard")
    t.start()
    _server_started = True

    if not _secret_ok():
        logger.warning(
            "🔒 الداشبورد السري مُعطّل — ضع DASHBOARD_SECRET (≥12 حرف) في Railway Variables"
        )
        logger.info("❤️  healthz على 0.0.0.0:%s/healthz", port)
        return None

    path = _dash_path()
    logger.info(
        "🔒 داشبورد سري شغّال على 0.0.0.0:%s المسار /%s/ (يتطلب السر — لك وحدك)",
        port,
        path,
    )
    logger.info("🔒 الرابط: https://<railway-domain>/%s/ ← أدخل DASHBOARD_SECRET", path)
    return f"/{path}/"
