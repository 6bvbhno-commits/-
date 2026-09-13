"""
البوت الرئيسي — يستقبل روابط منتجات وصور، ويرد بأقل سعر أو حالة التوفر.
يستخدم مكتبة python-telegram-bot (الإصدار 20+) + أحدث طبقات الأداء (v6).
"""
import asyncio
import json as _json
import logging
import re as _re
import threading as _threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
import requests as _req
from collections import defaultdict
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer as _HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError, RetryAfter, TimedOut, NetworkError, Conflict
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import price_alerts as _pa
from config import (
    TELEGRAM_BOT_TOKEN,
    MOCK_MODE,
    AMAZON_DOMAIN,
    AFFILIATE_TAG,
    GLOBAL_CONCURRENCY,
    RATE_MAX_PER_USER,
    SKIP_AI_CHAT_UNDER_LOAD,
    LOAD_SHED_ACTIVE_USERS,
    HIGH_LOAD_MODE,
    OFFER_CACHE_MAX,
    CLEAR_CACHE_ON_BOOT,
    HEAVY_POOL_SIZE,
    JSON_LOGS,
    USE_WEBHOOK,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    DASHBOARD_PORT,
    ADMIN_IDS,
    DAILY_DIGEST_ENABLED,
    DAILY_DIGEST_HOUR,
)
from logutil import setup_logging

setup_logging(json_logs=JSON_LOGS)
from amazon_utils import (
    build_affiliate_link,
    build_affiliate_search_link,
    build_affiliate_store_link,
    build_product_image_url,
    clear_offer_cache,
    download_image_bytes,
    enrich_offer_display,
    extract_asin,
    extract_domain,
    extract_product_title,
    fetch_product_image_bytes,
    get_affiliate_tag,
    is_amazon_store_url,
    is_amazon_url,
    resolve_short_link,
    get_lowest_offer,
    format_offer_message,
    format_product_reply_plain,
    tag_amazon_url,
    url_has_our_tag,
)
from amazon_utils import _clean_product_title  # حماية عنوان البطاقة
from vision_utils import (
    search_amazon_by_keywords,
    format_search_results,
    identify_product_from_image,
)

BOT_VERSION = "6.2"

# نص زر تنبيه السعر — واضح للمستخدم
ALERT_BTN_LABEL = "🔔 نبّهني عند انخفاض السعر"

# ─── Rate limiting ────────────────────────────────────────────────────────────
_RATE_WINDOW = 60
_RATE_MAX    = RATE_MAX_PER_USER

# ─── Global backpressure — يحد الطلبات الثقيلة المتزامنة (LLM + scraping) ──
# يُهيَّأ في _post_init بعد بدء event loop
_GLOBAL_SEM: asyncio.Semaphore | None = None
_HEAVY_POOL: ThreadPoolExecutor | None = None
_BG_TASKS: list[asyncio.Task] = []
_user_times: dict[int, list[float]] = defaultdict(list)
_user_last_seen: dict[int, float]   = {}   # آخر نشاط — للتنظيف الدوري

logger = logging.getLogger(__name__)


def _ensure_heavy_pool() -> ThreadPoolExecutor:
    global _HEAVY_POOL
    if _HEAVY_POOL is None:
        _HEAVY_POOL = ThreadPoolExecutor(
            max_workers=HEAVY_POOL_SIZE,
            thread_name_prefix="heavy",
        )
        logging.getLogger(__name__).info("🧵 heavy pool size=%d", HEAVY_POOL_SIZE)
    return _HEAVY_POOL

def _is_rate_limited(user_id: int) -> bool:
    now  = _time.monotonic()
    _user_last_seen[user_id] = now
    buf  = _user_times[user_id]
    buf[:] = [t for t in buf if now - t < _RATE_WINDOW]
    if len(buf) >= _RATE_MAX:
        return True
    buf.append(now)
    return False


def _is_under_load() -> bool:
    """هل عدد المستخدمين النشطين يستدعي تخفيف الحمل؟"""
    return len(_user_last_seen) >= LOAD_SHED_ACTIVE_USERS


class _HeavySlot:
    """مدير خانة طلب ثقيل — يرفض بسرعة إذا كان النظام ممتلئاً."""

    __slots__ = ("_acquired",)

    def __init__(self) -> None:
        self._acquired = False

    async def __aenter__(self) -> bool:
        sem = _GLOBAL_SEM
        if sem is None:
            self._acquired = True
            return True
        try:
            await asyncio.wait_for(sem.acquire(), timeout=2.0)
            self._acquired = True
            return True
        except asyncio.TimeoutError:
            _stat("load_shed")
            return False

    async def __aexit__(self, *_args) -> None:
        if self._acquired and _GLOBAL_SEM is not None:
            _GLOBAL_SEM.release()

# ─── سجل المحادثات لكل مستخدم (آخر 8 رسائل للسياق) ─────────────────────────
_MAX_HISTORY = 8
_user_history: dict[int, list[dict]] = defaultdict(list)

def _add_to_history(user_id: int, role: str, content: str) -> None:
    h = _user_history[user_id]
    h.append({"role": role, "content": content})
    if len(h) > _MAX_HISTORY:
        h[:] = h[-_MAX_HISTORY:]


# ─── تنظيف دوري للذاكرة ──────────────────────────────────────────────────────
_USER_TTL = 2 * 3600   # 2 ساعة عدم نشاط → نحذف من الذاكرة

# ─── إحصاءات صحة البوت ───────────────────────────────────────────────────────
_stats: dict = {
    "requests_total": 0,
    "requests_ok":    0,
    "requests_error": 0,
    "flood_waits":    0,
    "photo_ok":       0,
    "photo_miss":     0,
    "title_fallback": 0,
    "load_shed":      0,
    "coalesce_waits": 0,
    "last_request_ts": 0.0,
}

# ─── Stats HTTP server (port 8766) — يُعرض للـ dashboard ─────────────────────
_STATS_PORT = 8766

class _StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stats":
            payload = _json.dumps({
                "version":        BOT_VERSION,
                "high_load_mode": HIGH_LOAD_MODE,
                "active_users":   len(_user_last_seen),
                "global_concurrency": GLOBAL_CONCURRENCY,
                "rate_max_per_user":  _RATE_MAX,
                "requests_total": _stats.get("requests_total", 0),
                "requests_ok":    _stats.get("requests_ok",    0),
                "requests_error": _stats.get("requests_error", 0),
                "flood_waits":    _stats.get("flood_waits",    0),
                "load_shed":      _stats.get("load_shed",      0),
                "photo_ok":       _stats.get("photo_ok", 0),
                "photo_miss":     _stats.get("photo_miss", 0),
                "title_fallback": _stats.get("title_fallback", 0),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass   # صامت — لا نريد logs لكل طلب

def _start_stats_server():
    try:
        srv = _HTTPServer(("127.0.0.1", _STATS_PORT), _StatsHandler)
    except OSError as _e:
        logger.warning("📡 Stats server: تعذّر الربط بـ port %d — %s (البوت يستمر بدونه)", _STATS_PORT, _e)
        return
    t = _threading.Thread(target=srv.serve_forever, daemon=True, name="stats-http")
    t.start()
    logger.info("📡 Stats server شغّال على port %d", _STATS_PORT)

def _stat(key: str, inc: int = 1) -> None:
    _stats[key] = _stats.get(key, 0) + inc
    if key in ("requests_ok", "requests_error"):
        _stats["requests_total"] = _stats.get("requests_total", 0) + 1
    _stats["last_request_ts"] = _time.monotonic()


async def _memory_cleanup_loop() -> None:
    """يُنظّف بيانات المستخدمين غير النشطين كل 30 دقيقة."""
    while True:
        try:
            await asyncio.sleep(1800)
            cutoff = _time.monotonic() - _USER_TTL
            # list() snapshot — يمنع RuntimeError لو تغيّر الـ dict أثناء الـ iteration
            stale  = [uid for uid, t in list(_user_last_seen.items()) if t < cutoff]
            for uid in stale:
                _user_times.pop(uid, None)
                _user_history.pop(uid, None)
                _user_last_seen.pop(uid, None)
            if stale:
                logger.info("memory_cleanup: حُذف %d مستخدم غير نشط", len(stale))
            logger.info(
                "memory_cleanup: %d مستخدم نشط | طلبات=%d ok=%d err=%d floods=%d",
                len(_user_last_seen),
                _stats.get("requests_total", 0),
                _stats.get("requests_ok",    0),
                _stats.get("requests_error", 0),
                _stats.get("flood_waits",    0),
            )
        except asyncio.CancelledError:
            raise   # السماح بإيقاف المهمة عند الإغلاق النظيف
        except Exception as _ce:
            logger.warning("memory_cleanup فشل: %s", _ce)


async def _keep_alive_loop() -> None:
    """
    يُرسل ping لـ API كل 4 دقائق لمنع Replit من إيقاف الخادم.
    حيوي خلال الحملات التسويقية — أي توقف يعني ضياع رسائل المستخدمين.
    """
    import os as _os
    domain = _os.getenv("REPLIT_DEV_DOMAIN", "")
    if not domain:
        logger.info("keep_alive: لا يوجد REPLIT_DEV_DOMAIN — تم تخطي الـ ping")
        return
    url = f"https://{domain}/api/healthz"
    await asyncio.sleep(60)   # انتظر دقيقة بعد البداية
    while True:
        try:
            loop = asyncio.get_running_loop()
            # requests يدعم verify=False بشكل أبسط من httpx داخل Replit
            status = await loop.run_in_executor(
                None,
                lambda: _req.get(url, timeout=10, verify=False).status_code
            )
            logger.info("keep_alive: ping → %s", status)
        except Exception as _e:
            logger.warning("keep_alive: ping فشل — %s", _e)
        await asyncio.sleep(240)   # كل 4 دقائق


async def _health_monitor_loop() -> None:
    """
    يُراقب البوت كل 5 دقائق ويكتشف أي حالة تجمّد.
    إذا مرّت 10 دقائق بدون أي طلب ناجح وكان عدد الأخطاء مرتفعاً → يُسجّل تحذيراً.
    """
    while True:
        try:
            await asyncio.sleep(300)
            now      = _time.monotonic()
            last_req = _stats.get("last_request_ts", 0.0)
            since    = int(now - last_req) if last_req else -1
            total    = _stats.get("requests_total", 0)
            errors   = _stats.get("requests_error", 0)
            floods   = _stats.get("flood_waits",    0)
            err_rate = (errors / total * 100) if total > 0 else 0
            logger.info(
                "📊 health: %d طلب | %.1f%% أخطاء | %d FloodWait | آخر طلب منذ %ds",
                total, err_rate, floods, since,
            )
            if total > 10 and err_rate > 50:
                logger.warning(
                    "🔴 health: نسبة أخطاء عالية %.1f%% — راجع السجلات", err_rate
                )
        except asyncio.CancelledError:
            raise
        except Exception as _he:
            logger.warning("health_monitor فشل: %s", _he)

# ─── ثوابت ───────────────────────────────────────────────────────────────────
_MAX_MSG       = 4096
_SHORT_LINK_RE = _re.compile(
    r"https?://(?:amzn\.to|amzn\.eu|a\.co|link\.amazon|ty\.gl|bit\.ly|tinyurl\.com|t\.co|rb\.gy)/",
    _re.IGNORECASE,
)
_GREETING_RE = _re.compile(
    r"^(?:مرحبا|مرحباً|هلا|اهلا|أهلا|السلام|سلام عليكم|هاي|hi|hello|"
    r"شكرا|شكراً|thanks|مساء|صباح)[\s!.؟?]*$",
    _re.IGNORECASE,
)


def _guess_product_query(text: str) -> str | None:
    """يخمّن أن النص اسم منتج — بدون انتظار الذكاء الاصطناعي."""
    t = text.strip()
    if len(t) < 3 or len(t) > 120:
        return None
    if _GREETING_RE.match(t):
        return None
    if _re.match(r"^(?:من انت|وش البوت|help|مساعدة|/help)[\s?.!]*$", t, _re.IGNORECASE):
        return None
    return t


async def _search_and_deliver_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    product_query: str,
) -> None:
    """يبحث عن منتج بالاسم ويرسل صورة + وصف + أزرار — مثل الرابط."""
    await _typing(update, context)
    loop = asyncio.get_running_loop()

    async with _HeavySlot() as got_slot:
        if not got_slot:
            await _reply(
                update,
                "⏳ البوت مشغول بطلبات كثيرة الآن. انتظر ثوانٍ وحاول مرة أخرى.",
                parse_mode=None,
            )
            return
        try:
            offers = await asyncio.wait_for(
                loop.run_in_executor(None, search_amazon_by_keywords, product_query),
                timeout=25.0,
            )
        except Exception as e:
            logger.error("فشل البحث عن '%s': %s", product_query, e)
            offers = []

    if offers:
        first = offers[0]
        asin = (first.get("asin") or "").strip()
        if asin:
            try:
                offer = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: get_lowest_offer(asin, AMAZON_DOMAIN, ""),
                    ),
                    timeout=25.0,
                )
            except Exception as e:
                logger.warning("get_lowest_offer للبحث فشل: %s", e)
                offer = None
            if not offer:
                offer = {
                    "asin": asin,
                    "title": first.get("title") or product_query,
                    "price": first.get("price"),
                    "image": first.get("image", ""),
                    "blocked": True,
                    "affiliate_link": build_affiliate_link(asin, AMAZON_DOMAIN),
                }
            await _send_product_offer(update, context, asin, AMAZON_DOMAIN, offer, "")
            return

    message, search_url, image_url = format_search_results(product_query, offers or [])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 شوف العروض واطلب ↗", url=search_url),
    ]]) if search_url.startswith("http") else None

    chat_id = update.effective_chat.id
    reply_to = update.message.message_id
    photo_bytes = None
    if image_url:
        photo_bytes = await loop.run_in_executor(None, download_image_bytes, image_url)

    await _send_offer_card(
        context,
        chat_id=chat_id,
        reply_to=reply_to,
        photo_bytes=photo_bytes,
        caption=message,
        reply_markup=kb,
    )
    _stat("requests_ok")

# =============================================================================
# دوال مساعدة
# =============================================================================

async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يُرسل مؤشر "يكتب..." لإشعار المستخدم فوراً."""
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        pass


async def _reply(
    update: Update,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup=None,
) -> None:
    """يرسل رسالة — يعالج FloodWait وMarkdown تلقائياً (يدعم callback أيضاً)."""
    msg = update.effective_message
    if not msg:
        return
    if len(text) > _MAX_MSG:
        text = text[: _MAX_MSG - 60] + "\n\n_…(تم اختصار الرسالة)_"
    for attempt in range(4):
        try:
            await msg.reply_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
            return
        except RetryAfter as e:
            wait = min(int(e.retry_after) + 1, 30)
            logger.warning("Telegram FloodWait %ds (محاولة %d/3)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
                logger.warning("Telegram network خطأ، إعادة المحاولة: %s", e)
            else:
                logger.error("Telegram network فشل نهائي: %s", e)
                return
        except TelegramError:
            if parse_mode:
                plain = text.replace("*","").replace("`","").replace("_","").replace("\\","")
                try:
                    await msg.reply_text(plain[:_MAX_MSG], reply_markup=reply_markup)
                except TelegramError as e2:
                    logger.error("فشل إرسال الرسالة: %s", e2)
            return


# حد أقصى لطول تسمية الصورة في تيليجرام
_MAX_CAPTION = 1024


async def _send_offer_card(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    reply_to: int,
    photo_bytes: bytes | None,
    caption: str,
    reply_markup=None,
) -> None:
    """صورة + نص + أزرار في رسالة واحدة — أو نص فقط إن تعذّر تحميل الصورة."""
    cap = caption[:_MAX_CAPTION]
    if photo_bytes:
        photo_file = BytesIO(photo_bytes)
        photo_file.name = "product.jpg"
        for attempt in range(2):
            try:
                photo_file.seek(0)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    reply_to_message_id=reply_to,
                    photo=photo_file,
                    caption=cap,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
                return
            except TelegramError as e:
                logger.warning("صورة+نص فشلت: %s", e)
                if attempt == 0:
                    await asyncio.sleep(1)
        # محاولة أخيرة: صورة بدون كابشن ثم رسالة نصية
        try:
            photo_file.seek(0)
            await context.bot.send_photo(
                chat_id=chat_id,
                reply_to_message_id=reply_to,
                photo=photo_file,
                parse_mode=None,
            )
        except TelegramError as e:
            logger.warning("صورة بدون كابشن فشلت: %s", e)
    await context.bot.send_message(
        chat_id=chat_id,
        reply_to_message_id=reply_to,
        text=cap[:_MAX_MSG],
        parse_mode=None,
        reply_markup=reply_markup,
    )


async def _reply_photo(
    update: Update,
    photo_url: str,
    caption: str,
    parse_mode: str | None = "Markdown",
    reply_markup=None,
) -> bool:
    """يرسل صورة المنتج مع النص — يحمّل الصورة أولاً لأن تيليجرام يرفض روابط أمازون."""
    if not update.message or not photo_url or not photo_url.startswith("http"):
        return False
    cap = caption if len(caption) <= _MAX_CAPTION else caption[: _MAX_CAPTION - 20] + "\n\n_…_"
    loop = asyncio.get_running_loop()
    photo_bytes = await loop.run_in_executor(None, download_image_bytes, photo_url)
    if not photo_bytes:
        logger.warning("تعذّر تحميل صورة المنتج: %s", photo_url[:80])
        return False

    photo_file = BytesIO(photo_bytes)
    for attempt in range(3):
        try:
            photo_file.seek(0)
            await update.message.reply_photo(
                photo=photo_file,
                caption=cap,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return True
        except RetryAfter as e:
            wait = min(int(e.retry_after) + 1, 30)
            logger.warning("Telegram FloodWait (photo) %ds", wait)
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.warning("إرسال الصورة فشل شبكياً: %s", e)
                return False
        except TelegramError as e:
            logger.info("تعذّر إرسال الصورة (%s)", e)
            return False
    return False


# =============================================================================
# المعالجات
# =============================================================================

def _track_user(update: Update) -> None:
    """يسجّل المستخدم لاستقبال تنبيهات أكواد الخصم."""
    try:
        import users_db as _users
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user:
            return
        _users.upsert_user(
            chat.id,
            user.id,
            username=user.username or "",
            first_name=user.first_name or "",
        )
    except Exception as e:
        logger.warning("track_user: %s", e)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب مع إفصاح الأفلييت الإلزامي + أزرار سريعة."""
    try:
        _track_user(update)
        user_id = update.effective_user.id if update.effective_user else 0
        _user_history[user_id].clear()

        # deep-link: /start p_ASIN
        args = context.args or []
        if args and str(args[0]).startswith("p_") and len(args[0]) >= 12:
            asin = str(args[0])[2:12].upper()
            if _re.fullmatch(r"[A-Z0-9]{10}", asin):
                await _typing(update, context)
                offer = None
                try:
                    loop = asyncio.get_running_loop()
                    offer = await loop.run_in_executor(
                        None, lambda: get_lowest_offer(asin, AMAZON_DOMAIN, "")
                    )
                except Exception as e:
                    logger.warning("deep-link offer: %s", e)
                await _send_product_offer(update, context, asin, AMAZON_DOMAIN, offer, "")
                return

        bot_user = context.bot.username or ""
        share_url = (
            f"https://t.me/share/url?url=https%3A%2F%2Ft.me%2F{bot_user}"
            f"&text=%D8%A8%D9%88%D8%AA%20%D8%A3%D8%B3%D8%B9%D8%A7%D8%B1%20%D8%A3%D9%85%D8%A7%D8%B2%D9%88%D9%86"
            if bot_user else ""
        )

        welcome_text = (
            "👋 *أهلاً في بوت أسعار أمازون السعودية!*\n\n"
            "🔥 *وفّر فلوسك* — أقل سعر + صورة + زر شراء مباشر\n\n"
            "📌 *كيف تستخدمه؟*\n"
            "• 🔗 أرسل *رابط منتج* أو *رابط متجر*\n"
            "• 💬 اكتب *اسم منتج* ← أبحث لك فوراً\n"
            "• 🔎 في أي محادثة اكتب `@" + (bot_user or "bot") + "` ثم اسم المنتج\n\n"
            "🔔 *تنبيه انخفاض السعر:*\n"
            f"اضغط *{ALERT_BTN_LABEL.replace('🔔 ', '')}* — وأرسلك إشعار أول ما ينزل!\n"
            "📋 تنبيهاتك: /myalerts\n\n"
            f"ℹ️ _روابط الشراء بعمولة Associates (`{get_affiliate_tag()}`)._"
        )
        if MOCK_MODE:
            welcome_text += "\n\n⚠️ *وضع تجريبي* — الأسعار وهمية."

        rows = [
            [
                InlineKeyboardButton("🔥 عروض الآن", callback_data="q:deals"),
                InlineKeyboardButton("🔔 تنبيهاتي", callback_data="q:alerts"),
            ],
            [
                InlineKeyboardButton("🆘 مساعدة", callback_data="q:help"),
                InlineKeyboardButton("🔕 إيقاف التنبيهات", callback_data="q:mute"),
            ],
        ]
        if share_url:
            rows.append([InlineKeyboardButton("📤 شارك البوت", url=share_url)])
        rows.append([InlineKeyboardButton(
            "🛒 عروض أمازون.sa",
            url=build_affiliate_search_link("", AMAZON_DOMAIN),
        )])
        kb = InlineKeyboardMarkup(rows)
        await _reply(update, welcome_text, reply_markup=kb)
    except Exception as _e:
        logger.error("start_command فشل: %s", _e, exc_info=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_user = context.bot.username or "البوت"
        help_text = (
            "🆘 *المساعدة*\n\n"
            "• 🔗 *رابط أمازون / متجر* ← صورة + أقل سعر + زر شراء\n"
            "• 💬 *اسم منتج* ← بحث فوري\n"
            f"• 🔥 /deals ← الأكثر طلباً الآن\n"
            f"• ⚖️ /compare رابط1 رابط2 ← مقارنة سعر\n"
            f"• ⭐ /fav ← مفضلتك\n"
            f"• 📸 أرسل *صورة منتج* ← أتعرف عليه وأبحث\n"
            f"• 🔎 اكتب `@{bot_user}` في أي شات وابحث\n"
            f"• {ALERT_BTN_LABEL} ← إشعار عند نزول السعر\n\n"
            "📋 *الأوامر:* /start · /myalerts · /share · /version\n\n"
            f"🏷️ تاق العمولة: `{get_affiliate_tag()}`\n"
            "⚠️ _تحقق من السعر على أمازون قبل الشراء._"
        )
        await _reply(update, help_text)
    except Exception as _e:
        logger.error("help_command فشل: %s", _e, exc_info=True)


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إيقاف رسائل أكواد الخصم والملخص اليومي."""
    chat = update.effective_chat
    if not chat:
        return
    import users_db as _users
    _users.set_opt_out(chat.id, True)
    await _reply(
        update,
        "🔕 تم إيقاف تنبيهات العروض وأكواد الخصم.\n"
        "تقدر ترجعها بأي وقت: /unmute",
        parse_mode=None,
    )


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    import users_db as _users
    _users.set_opt_out(chat.id, False)
    await _reply(
        update,
        "🔔 رجّعنا تنبيهات العروض وأكواد الخصم.\n"
        "لإيقافها: /mute",
        parse_mode=None,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    للمدير فقط من الجوال:
      /broadcast كود|عنوان|تفاصيل
    مثال:
      /broadcast SAVE20|كود خصم اليوم|على الإلكترونيات حتى منتصف الليل
    """
    uid = update.effective_user.id if update.effective_user else 0
    if not _is_admin(uid):
        await _reply(update, "⛔ هذا الأمر للمدير فقط.", parse_mode=None)
        return
    raw = " ".join(context.args or []).strip()
    if not raw or "|" not in raw:
        await _reply(
            update,
            "📢 الصيغة:\n"
            "`/broadcast الكود|العنوان|تفاصيل اختيارية`\n\n"
            "مثال:\n"
            "`/broadcast SAVE20|كود خصم أمازون|ينتهي الليلة`",
        )
        return
    parts = [p.strip() for p in raw.split("|")]
    code = parts[0]
    title = parts[1] if len(parts) > 1 else "كود خصم أمازون اليوم"
    extra = parts[2] if len(parts) > 2 else ""
    await _reply(update, f"⏳ جاري إرسال الكود `{code}` للجميع…", parse_mode="Markdown")
    import broadcast as _bc
    result = await _bc.send_discount_broadcast(
        code=code, title=title, extra=extra, when_label="الحين"
    )
    if result.get("ok"):
        await _reply(
            update,
            f"✅ تم الإرسال\n"
            f"نجاح: {result.get('sent_ok', 0)}\n"
            f"فشل: {result.get('sent_fail', 0)}\n"
            f"الإجمالي: {result.get('total', 0)}",
            parse_mode=None,
        )
    else:
        await _reply(update, f"❌ فشل: {result.get('error', 'unknown')}", parse_mode=None)


async def handle_quick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أزرار شاشة /start السريعة."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    await query.answer()

    # نعيد استخدام نفس Update مع message للرد
    if data == "q:deals":
        await deals_command(update, context)
        return
    if data == "q:alerts":
        await myalerts_command(update, context)
        return
    if data == "q:help":
        await help_command(update, context)
        return
    if data == "q:mute":
        chat_id = query.message.chat_id if query.message else 0
        import users_db as _users
        _users.set_opt_out(chat_id, True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if query.message:
            await query.message.reply_text(
                "🔕 تم إيقاف تنبيهات العروض.\nللتفعيل مرة ثانية: /unmute",
            )
        return
    if data == "q:unmute":
        chat_id = query.message.chat_id if query.message else 0
        import users_db as _users
        _users.set_opt_out(chat_id, False)
        if query.message:
            await query.message.reply_text("🔔 تم تفعيل تنبيهات العروض من جديد.")
        return


async def handle_mute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زر إيقاف التنبيهات من رسائل البث."""
    query = update.callback_query
    if not query or not query.message:
        return
    data = query.data or ""
    chat_id = query.message.chat_id
    import users_db as _users
    if data == "bc:mute":
        _users.set_opt_out(chat_id, True)
        await query.answer("تم إيقاف التنبيهات", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔔 تفعيل التنبيهات", callback_data="bc:unmute")],
            ]))
        except Exception:
            pass
    elif data == "bc:unmute":
        _users.set_opt_out(chat_id, False)
        await query.answer("تم التفعيل", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await query.answer()


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رابط مشاركة البوت — يزيد الانتشار والظهور."""
    bot_user = context.bot.username or ""
    if not bot_user:
        await _reply(update, "⚠️ ما قدرت أجيب يوزر البوت حالياً.", parse_mode=None)
        return
    link = f"https://t.me/{bot_user}"
    share = (
        f"https://t.me/share/url?url={link}"
        "&text=%D8%A8%D9%88%D8%AA%20%D8%A3%D8%B3%D8%B9%D8%A7%D8%B1%20%D8%A3%D9%85%D8%A7%D8%B2%D9%88%D9%86%20"
        "%D8%A7%D9%84%D8%B3%D8%B9%D9%88%D8%AF%D9%8A%D8%A9%20%F0%9F%94%A5"
    )
    text = (
        "📤 *شارك البوت مع أصحابك*\n\n"
        f"رابط البوت: `{link}`\n\n"
        "كل مشاركة تساعد يظهر البوت أكثر في بحث تيليجرام 🔍"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 مشاركة سريعة", url=share)],
        [InlineKeyboardButton("فتح البوت", url=link)],
    ])
    await _reply(update, text, reply_markup=kb)


async def deals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض أكثر المنتجات طلباً الآن داخل البوت."""
    from deals_tracker import top_deals

    _track_user(update)
    deals = top_deals(8)
    if not deals:
        await _reply(
            update,
            "🔥 ما فيه عروض رائجة مسجّلة للحين.\n"
            "أرسل رابط أو اسم منتج — وبصير قائمة /deals تتعبّى تلقائياً.",
            parse_mode=None,
        )
        return

    lines = ["🔥 *الأكثر طلباً الآن*\n"]
    rows = []
    for i, d in enumerate(deals, 1):
        title = (d.get("title") or d["asin"])[:50]
        price = d.get("price") or ""
        link = build_affiliate_link(d["asin"], d.get("domain") or AMAZON_DOMAIN)
        lines.append(f"*{i}.* {title}")
        if price:
            lines.append(f"   💰 {price}")
        lines.append("")
        rows.append([InlineKeyboardButton(f"🛒 {i}. اشتري", url=link)])

    await _reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


def _parse_asin_or_link(token: str) -> tuple[str | None, str]:
    """يستخرج ASIN والنطاق من رابط أو كود ASIN خام."""
    token = (token or "").strip().rstrip(".,;:!?)\"}'")
    if not token:
        return None, AMAZON_DOMAIN
    if _re.fullmatch(r"[A-Za-z0-9]{10}", token):
        return token.upper(), AMAZON_DOMAIN
    asin = extract_asin(token)
    domain = extract_domain(token) if is_amazon_url(token) else AMAZON_DOMAIN
    return asin, domain or AMAZON_DOMAIN


async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مقارنة منتجين: /compare رابط1 رابط2 أو ASIN1 ASIN2"""
    _track_user(update)
    args = context.args or []
    text = update.effective_message.text if update.effective_message else ""
    if len(args) < 2:
        # حاول التقاط رابطين من النص
        urls = _re.findall(r"https?://\S+", text or "")
        if len(urls) >= 2:
            args = urls[:2]
        else:
            await _reply(
                update,
                "⚖️ *المقارنة*\n\n"
                "أرسل:\n"
                "`/compare رابط1 رابط2`\n"
                "أو:\n"
                "`/compare ASIN1 ASIN2`\n\n"
                "مثال:\n"
                "`/compare B0XXXXXXX1 B0XXXXXXX2`",
            )
            return

    a1, d1 = _parse_asin_or_link(args[0])
    a2, d2 = _parse_asin_or_link(args[1])
    if not a1 or not a2:
        await _reply(update, "⚠️ ما قدرت أقرأ المنتجين. أرسل رابطين أو كودين ASIN.", parse_mode=None)
        return

    await _typing(update, context)
    loop = asyncio.get_running_loop()

    async with _HeavySlot() as got:
        if not got:
            await _reply(update, "⏳ البوت مشغول — حاول بعد ثوانٍ.", parse_mode=None)
            return
        try:
            o1, o2 = await asyncio.wait_for(
                asyncio.gather(
                    loop.run_in_executor(None, lambda: get_lowest_offer(a1, d1, "")),
                    loop.run_in_executor(None, lambda: get_lowest_offer(a2, d2, "")),
                ),
                timeout=30.0,
            )
        except Exception as e:
            logger.warning("compare failed: %s", e)
            await _reply(update, "❌ فشلت المقارنة. حاول مرة ثانية.", parse_mode=None)
            return

    def _row(label: str, offer: dict | None, asin: str) -> tuple[str, float | None, str]:
        if not offer:
            return f"• {label}: غير متاح", None, build_affiliate_link(asin, AMAZON_DOMAIN)
        title = (offer.get("title") or asin)[:45]
        price = offer.get("price") or "—"
        pval = offer.get("price_val")
        link = offer.get("affiliate_link") or build_affiliate_link(asin, offer.get("domain") or AMAZON_DOMAIN)
        return f"• *{label}:* {title}\n  💰 {price}", pval, link

    l1, p1, u1 = _row("أ", o1, a1)
    l2, p2, u2 = _row("ب", o2, a2)
    verdict = ""
    if p1 and p2:
        if p1 < p2:
            verdict = f"\n✅ الأرخص: *أ* بفرق `{p2 - p1:.2f}` SAR"
        elif p2 < p1:
            verdict = f"\n✅ الأرخص: *ب* بفرق `{p1 - p2:.2f}` SAR"
        else:
            verdict = "\n⚖️ نفس السعر تقريباً"

    msg = f"⚖️ *مقارنة سريعة*\n\n{l1}\n\n{l2}{verdict}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 اشتري أ", url=u1), InlineKeyboardButton("🛒 اشتري ب", url=u2)],
    ])
    await _reply(update, msg, reply_markup=kb)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إحصاءات سريعة للمدير فقط."""
    uid = update.effective_user.id if update.effective_user else 0
    if not _is_admin(uid):
        await _reply(update, "⛔ هذا الأمر للمدير فقط.", parse_mode=None)
        return
    import users_db as _users
    from deals_tracker import top_deals
    uc = _users.count_users()
    try:
        from serpapi_utils import serpapi_circuit_status
        circ = serpapi_circuit_status()
    except Exception:
        circ = {}
    deals = top_deals(3)
    deals_txt = "\n".join(
        f"  {i}. {(d.get('title') or d['asin'])[:40]}" for i, d in enumerate(deals, 1)
    ) or "  —"
    msg = (
        f"📊 *إحصاءات البوت v{BOT_VERSION}*\n\n"
        f"• مستخدمون: `{uc.get('total', 0)}` (نشط بث: `{uc.get('active', 0)}`)\n"
        f"• طلبات: `{_stats.get('requests_total', 0)}` · ✅ `{_stats.get('requests_ok', 0)}` · ❌ `{_stats.get('requests_error', 0)}`\n"
        f"• صور: ✅ `{_stats.get('photo_ok', 0)}` · ❌ `{_stats.get('photo_miss', 0)}`\n"
        f"• تخفيف حمل: `{_stats.get('load_shed', 0)}` · FloodWait: `{_stats.get('flood_waits', 0)}`\n"
        f"• قاطع SerpAPI: `{'مفتوح' if circ.get('open') else 'سليم'}`\n"
        f"• نشطون الآن: `{len(_user_last_seen)}`\n\n"
        f"🔥 الأكثر طلباً:\n{deals_txt}"
    )
    await _reply(update, msg)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض المفضلة /fav"""
    import users_db as _users
    from amazon_utils import build_affiliate_link as _bal

    _track_user(update)
    uid = update.effective_user.id if update.effective_user else 0
    items = _users.list_favorites(uid)
    if not items:
        await _reply(
            update,
            "⭐ ما عندك منتجات بالمفضلة.\n"
            "افتح أي منتج واضغط «⭐ للمفضلة».",
            parse_mode=None,
        )
        return
    lines = [f"⭐ *مفضلتك ({len(items)}/20)*\n"]
    rows = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or it["asin"])[:45]
        price = it.get("price") or ""
        lines.append(f"*{i}.* {title}")
        if price:
            lines.append(f"   💰 {price}")
        lines.append("")
        link = _bal(it["asin"], it.get("domain") or AMAZON_DOMAIN)
        rows.append([
            InlineKeyboardButton(f"🛒 {i}", url=link),
            InlineKeyboardButton("🗑️", callback_data=f"favdel:{it['asin']}"),
        ])
    await _reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def handle_fav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إضافة/حذف من المفضلة."""
    query = update.callback_query
    if not query or not query.message:
        return
    data = query.data or ""
    uid = query.from_user.id if query.from_user else 0
    import users_db as _users

    if data.startswith("favdel:"):
        asin = data.split(":", 1)[1].upper()
        _users.remove_favorite(uid, asin)
        await query.answer("تم الحذف من المفضلة", show_alert=False)
        # أعد رسم القائمة
        fake = update
        await favorites_command(fake, context)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("fav:"):
        asin = data.split(":", 1)[1].upper()
        domain = (context.user_data or {}).get(f"pdomain_{asin}", AMAZON_DOMAIN)
        title = (context.user_data or {}).get(f"ptitle_{asin}", asin)
        result = _users.add_favorite(uid, asin, domain=domain, title=title)
        if result == "limit":
            await query.answer("وصلت للحد 20 — احذف من /fav", show_alert=True)
        elif result in ("added", "updated"):
            await query.answer("⭐ تمت الإضافة للمفضلة", show_alert=False)
        else:
            await query.answer("فشل الحفظ", show_alert=True)
        return
    await query.answer()


async def _send_product_offer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    asin: str,
    domain: str,
    offer: dict | None,
    source_url: str,
) -> None:
    """صورة + وصف + أزرار — مع ضمانات ضد اختفاء الاسم/الصورة."""
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    reply_to = update.message.message_id

    fallback_title = extract_product_title(source_url, asin)
    loop = asyncio.get_running_loop()

    # إثراء إلزامي: SerpAPI → ويدجت → OG → slug
    offer = await loop.run_in_executor(
        None,
        lambda: enrich_offer_display(offer, asin, domain, source_url),
    )

    used_fallback_title = (offer.get("title") or "") in ("منتج من أمازون", "")
    if used_fallback_title:
        _stat("title_fallback")

    if context.user_data is None:
        context.user_data = {}
    context.user_data[f"pdomain_{asin}"] = domain

    affiliate_url = build_affiliate_link(asin, domain)
    if not url_has_our_tag(affiliate_url):
        logger.error("AFFILIATE_TAG_MISSING على رابط الشراء: %s", affiliate_url)
        affiliate_url = build_affiliate_link(asin, domain)
    buy_btn = InlineKeyboardButton("🛒 اشتري الآن ↗", url=affiliate_url)

    price_val = offer.get("price_val") if offer else None
    price_int = int(float(price_val) * 100) if price_val else 0
    cb_data = f"al:{asin}:{price_int}"
    context.user_data[f"ptitle_{asin}"] = str(offer.get("title", ""))[:80]

    bot_user = context.bot.username or ""
    title_q = _re.sub(r"\s+", " ", str(offer.get("title") or asin)[:80])
    share_text = f"{title_q}\n{affiliate_url}"
    from urllib.parse import quote
    share_url = (
        f"https://t.me/share/url?url={quote(affiliate_url, safe='')}"
        f"&text={quote(title_q, safe='')}"
    )
    if bot_user:
        # يفتح البوت مباشرة على المنتج لمن يضغط المشاركة داخل التيليجرام
        deep = f"https://t.me/{bot_user}?start=p_{asin}"
        share_url = (
            f"https://t.me/share/url?url={quote(deep, safe='')}"
            f"&text={quote(share_text[:180], safe='')}"
        )

    fav_cb = f"fav:{asin}"
    kb = InlineKeyboardMarkup([
        [buy_btn],
        [InlineKeyboardButton(ALERT_BTN_LABEL, callback_data=cb_data)],
        [
            InlineKeyboardButton("⭐ للمفضلة", callback_data=fav_cb),
            InlineKeyboardButton("📤 شارك", url=share_url),
        ],
    ])

    message = format_product_reply_plain(
        offer,
        fallback_title=fallback_title,
        asin=asin,
        version=BOT_VERSION,
        domain=domain,
    )
    # حماية نهائية: لا تعرض كود ASIN كاسم أبداً
    display_title = (offer.get("title") or "").strip()
    if (
        not display_title
        or display_title.upper() == asin.upper()
        or _re.fullmatch(r"[A-Z0-9]{10}", display_title.upper())
        or _re.fullmatch(rf"منتج\s*{_re.escape(asin)}", display_title, flags=_re.I)
    ):
        display_title = _clean_product_title(fallback_title, asin) or "منتج من أمازون"
        offer["title"] = display_title
        message = format_product_reply_plain(
            offer,
            fallback_title=fallback_title,
            asin=asin,
            version=BOT_VERSION,
            domain=domain,
        )
        logger.error("CARD_GUARD: استُبدل كود/عنوان ضعيف للـ ASIN %s → %s", asin, display_title)

    photo_bytes = await loop.run_in_executor(
        None, fetch_product_image_bytes, asin, domain, offer, source_url
    )
    if not photo_bytes and (offer.get("image") or "").startswith("http"):
        photo_bytes = await loop.run_in_executor(
            None, download_image_bytes, offer["image"]
        )

    if photo_bytes:
        _stat("photo_ok")
    else:
        _stat("photo_miss")
        logger.error(
            "PHOTO_MISS ASIN=%s title=%s image=%s",
            asin,
            (offer.get("title") or "")[:60],
            (offer.get("image") or "")[:80],
        )

    await _send_offer_card(
        context,
        chat_id=chat_id,
        reply_to=reply_to,
        photo_bytes=photo_bytes,
        caption=message,
        reply_markup=kb,
    )
    try:
        from deals_tracker import record_deal
        record_deal(
            asin=asin,
            title=str(offer.get("title") or fallback_title or ""),
            price=str(offer.get("price") or ""),
            price_val=offer.get("price_val"),
            domain=domain,
            image=str(offer.get("image") or ""),
        )
    except Exception:
        pass
    _stat("requests_ok")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي رسالة نصية فيها رابط منتج أمازون."""
    if not update.message or not update.message.text:
        return

    _track_user(update)
    user_id = update.effective_user.id if update.effective_user else 0
    if _is_rate_limited(user_id):
        await _reply(update, "⏳ أرسلت طلبات كثيرة. انتظر قليلاً ثم حاول.", parse_mode=None)
        return

    await _typing(update, context)

    text = update.message.text.strip()
    _url_match = _re.search(r"https?://\S+", text)
    url_only   = _url_match.group(0).rstrip(".,;:!?)\"']}") if _url_match else text

    asin         = extract_asin(url_only)
    resolved_url = url_only

    if not asin:
        is_short = bool(_SHORT_LINK_RE.match(url_only))
        if is_short:
            await _reply(update, "🔗 جاري تتبع الرابط...", parse_mode=None)
        try:
            loop = asyncio.get_running_loop()
            resolved_url = await loop.run_in_executor(None, resolve_short_link, url_only)
        except Exception as e:
            logger.error("فشل فك الرابط: %s", e)
            resolved_url = url_only
        asin = extract_asin(resolved_url)

    domain = extract_domain(resolved_url)

    if not asin:
        # رابط متجر / صفحة عروض / أي أمازون بدون ASIN → تاق عمولة + زر فتح
        if is_amazon_url(resolved_url) or is_amazon_store_url(resolved_url):
            tagged = build_affiliate_store_link(resolved_url, domain)
            store_title = "متجر / صفحة عروض أمازون"
            if is_amazon_store_url(resolved_url):
                store_title = "🏪 متجر أمازون — عروض مختارة"
            msg = (
                f"{store_title}\n\n"
                "✨ فتحت لك الرابط بتاق العمولة الخاص فينا.\n"
                "اضغط الزر تحت للتصفح والشراء 👇"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 افتح العروض ↗", url=tagged),
            ]])
            await _reply(update, msg, parse_mode=None, reply_markup=kb)
            logger.info("STORE_LINK tagged | tag=%s | %s", get_affiliate_tag(), tagged[:120])
            _stat("requests_ok")
            return

        await _reply(
            update,
            "⚠️ ما قدرت أستخرج رقم المنتج من هذا الرابط.\n"
            "جرّب تفتح الرابط في المتصفح وانسخه من شريط العنوان مباشرة.",
            parse_mode=None,
        )
        return

    await _typing(update, context)
    offer = None

    async with _HeavySlot() as got_slot:
        if not got_slot:
            await _reply(
                update,
                "⏳ البوت مشغول بطلبات كثيرة الآن. انتظر ثوانٍ وحاول مرة أخرى.",
                parse_mode=None,
            )
            return
        try:
            loop = asyncio.get_running_loop()
            try:
                offer = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: get_lowest_offer(asin, domain, resolved_url),
                    ),
                    timeout=25.0,
                )
            except asyncio.TimeoutError:
                logger.warning("get_lowest_offer timeout للـ ASIN %s", asin)
                offer = {
                    "blocked": True,
                    "affiliate_link": build_affiliate_link(asin, domain),
                    "title": extract_product_title(resolved_url, asin),
                }
        except Exception as e:
            logger.error("خطأ في جلب السعر للـ ASIN %s: %s", asin, e, exc_info=True)
            await _reply(update, "❌ حصل خطأ أثناء البحث. حاول مرة ثانية.", parse_mode=None)
            return

    await _send_product_offer(update, context, asin, domain, offer, resolved_url)


# =============================================================================
# معالج أزرار التنبيهات (Inline Keyboard Callbacks)
# =============================================================================

def _mdv2(text: str) -> str:
    """يهرّب النص لـ MarkdownV2 — ضروري لكل محتوى ديناميكي."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def handle_alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج نقرات أزرار تنبيهات الأسعار."""
    try:
        await _handle_alert_callback_inner(update, context)
    except Exception as _e:
        logger.error("handle_alert_callback فشل: %s", _e, exc_info=True)

async def _handle_alert_callback_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer("جاري تفعيل التنبيه…", show_alert=False)

    data   = query.data or ""
    uid    = query.from_user.id if query.from_user else 0
    cid    = query.message.chat_id

    # ── حذف تنبيه ────────────────────────────────────────────────────────────
    if data.startswith("al_del:"):
        try:
            alert_id = int(data.split(":")[1])
        except (IndexError, ValueError):
            return
        ok = _pa.delete_alert(alert_id, uid)
        # أعد بناء قائمة التنبيهات المحدّثة
        remaining = _pa.get_user_alerts(uid)
        if not remaining:
            try:
                await query.edit_message_text(
                    "✅ تم الحذف.\n\n📭 ما عندك تنبيهات نشطة.",
                    parse_mode=None,
                )
            except Exception:
                pass
            return
        # أعد رسم الرسالة مع الأزرار المحدّثة
        text, keyboard = _build_myalerts_content(remaining)
        try:
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception:
            pass
        return

    # ── إضافة تنبيه ──────────────────────────────────────────────────────────
    if data.startswith("al:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        _, req_asin, price_str = parts[0], parts[1], parts[2]
        req_domain = (context.user_data or {}).get(f"pdomain_{req_asin}", AMAZON_DOMAIN)
        try:
            current_price = int(price_str) / 100
        except ValueError:
            return

        product_name = context.user_data.get(f"ptitle_{req_asin}", "")
        result = _pa.add_alert(uid, cid, req_asin, req_domain, product_name, current_price)

        # أزل الزر من الرسالة الأصلية
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if result in ("added", "updated"):
            verb = "تم تحديث" if result == "updated" else "تم تفعيل"
            if current_price <= 0:
                price_line = "راح أحدد السعر الحالي أول ما يتوفر وأنبّهك عند أي انخفاض"
            else:
                price_line = f"راح أنبّهك لما ينزل عن `{current_price:.2f} SAR`"
            await query.message.reply_text(
                f"✅ *{verb} تنبيه انخفاض السعر!*\n\n"
                f"📦 {product_name[:60] or req_asin}\n"
                f"🔔 {price_line}\n\n"
                f"📋 تنبيهاتك: /myalerts",
                parse_mode="Markdown",
            )
        elif result == "limit_reached":
            await query.message.reply_text(
                f"⚠️ وصلت للحد الأقصى ({_pa.MAX_ALERTS_PER_USER} تنبيهات).\n"
                f"احذف تنبيهاً قديماً أولاً: /myalerts",
                parse_mode=None,
            )
        else:
            await query.message.reply_text("❌ فشل حفظ التنبيه. حاول مرة أخرى.", parse_mode=None)


# =============================================================================
# Inline Mode — يظهر البوت عند الكتابة @username في أي محادثة (اكتشاف أقوى)
# =============================================================================

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بحث منتجات من الوضع المضمّن — ينشر نتائج بروابط عمولة."""
    from telegram import (
        InlineQueryResultArticle,
        InputTextMessageContent,
        InlineKeyboardMarkup as _IKM,
        InlineKeyboardButton as _IKB,
    )
    from vision_utils import search_amazon_by_keywords

    iq = update.inline_query
    if not iq:
        return

    q = (iq.query or "").strip()
    results = []

    if len(q) < 2:
        home = build_affiliate_search_link("", AMAZON_DOMAIN)
        results.append(
            InlineQueryResultArticle(
                id="hint",
                title="اكتب اسم منتج للبحث في أمازون.sa",
                description="مثال: سماعات بلوتوث · iPhone · قهوة",
                input_message_content=InputTextMessageContent(
                    f"🔍 ابحث في بوت أسعار أمازون\n🛒 {home}"
                ),
                reply_markup=_IKM([[_IKB("🛒 أمازون.sa", url=home)]]),
            )
        )
        await iq.answer(results, cache_time=10, is_personal=True)
        return

    if _is_rate_limited(iq.from_user.id if iq.from_user else 0):
        await iq.answer([], cache_time=5, is_personal=True)
        return

    loop = asyncio.get_running_loop()
    offers = []
    try:
        async with _HeavySlot() as got:
            if got:
                offers = await asyncio.wait_for(
                    loop.run_in_executor(None, search_amazon_by_keywords, q),
                    timeout=12.0,
                ) or []
    except Exception as e:
        logger.warning("inline search فشل: %s", e)

    if not offers:
        search_url = build_affiliate_search_link(q, AMAZON_DOMAIN)
        results.append(
            InlineQueryResultArticle(
                id="search",
                title=f"ابحث عن «{q[:40]}» في أمازون",
                description="اضغط للإرسال — رابط بعمولة",
                input_message_content=InputTextMessageContent(
                    f"🔍 نتائج «{q}» على أمازون السعودية\n🛒 {search_url}"
                ),
                reply_markup=_IKM([[_IKB("🛒 شوف العروض ↗", url=search_url)]]),
            )
        )
        await iq.answer(results, cache_time=20, is_personal=True)
        return

    for i, item in enumerate(offers[:8]):
        asin = (item.get("asin") or "").strip().upper()
        title = (item.get("title") or q)[:80]
        price = (item.get("price") or "").strip()
        if asin:
            link = build_affiliate_link(asin, AMAZON_DOMAIN)
        else:
            link = tag_amazon_url(item.get("link") or "", AMAZON_DOMAIN) or build_affiliate_search_link(q)
        desc = price or "اضغط للشراء من أمازون.sa"
        body = f"📦 {title}\n"
        if price:
            body += f"💰 {price}\n"
        body += f"🛒 {link}"
        results.append(
            InlineQueryResultArticle(
                id=f"p{i}-{asin or i}",
                title=title[:60],
                description=desc[:80],
                input_message_content=InputTextMessageContent(body),
                reply_markup=_IKM([[_IKB("🛒 اشتري الآن ↗", url=link)]]),
            )
        )

    await iq.answer(results, cache_time=30, is_personal=True)
    _stat("requests_ok")


# =============================================================================
# أمر /myalerts
# =============================================================================

def _esc_md(text: str) -> str:
    """يهرّب أحرف Markdown v1 في النص الديناميكي."""
    for ch in r"_*`[":
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_myalerts_content(alerts: list[dict]) -> tuple[str, list]:
    """يبني نص رسالة + مصفوفة أزرار لقائمة التنبيهات."""
    text = (
        f"🔔 *تنبيهاتك النشطة ({len(alerts)} من {_pa.MAX_ALERTS_PER_USER})*\n\n"
        f"_أرسل لك إشعار فور نزول السعر عن أي منتج تتابعه:_\n\n"
    )
    keyboard = []
    for i, a in enumerate(alerts, 1):
        raw_name = (a.get("product_name") or a["asin"])[:40]
        name = _esc_md(raw_name)          # هرّب الأحرف الخاصة
        price = f"{a['last_known']:.2f}"
        text += (
            f"*{i}.* 📦 {name}\n"
            f"   💰 أنبّهك إذا نزل عن `{price} SAR`\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ حذف رقم {i}",
                callback_data=f"al_del:{a['id']}",
            )
        ])
    return text, keyboard


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض حالة مفاتيح API المتاحة — للتشخيص فقط (لا يكشف القيم)."""
    import os
    from config import (
        get_deepseek_api_key,
        SERPAPI_KEY,
        ANTHROPIC_API_KEY,
        TELEGRAM_BOT_TOKEN,
    )
    from serpapi_utils import serpapi_available, serpapi_circuit_status
    from paapi_utils import paapi_available

    def _status(val: str) -> str:
        return "✅ متوفر" if val else "❌ غير موجود"

    deepseek_key = get_deepseek_api_key()
    railway_svc = os.getenv("RAILWAY_SERVICE_NAME", "—")
    circ = serpapi_circuit_status()

    msg = (
        f"🔧 *حالة البوت v{BOT_VERSION}*\n\n"
        f"• إصدار الكود: `{BOT_VERSION}`\n"
        f"• خدمة Railway: `{railway_svc}`\n"
        f"• SerpAPI (سعر+صورة): {'✅' if serpapi_available() else '❌'}\n"
        f"• قاطع SerpAPI: `{'مفتوح ⚡' if circ.get('open') else 'سليم'} "
        f"({circ.get('cooldown_left', 0)}s)`\n"
        f"• PA API (سعر+صورة): {'✅' if paapi_available() else '❌'}\n"
        f"• DeepSeek (نص): {_status(deepseek_key)}\n"
        f"• Anthropic: {_status(ANTHROPIC_API_KEY)}\n"
        f"• Telegram: {_status(TELEGRAM_BOT_TOKEN)}\n"
        f"• تاق العمولة: `{get_affiliate_tag()}`\n"
        f"• عيّنة رابط: `{build_affiliate_link('B0GM947WC5', AMAZON_DOMAIN)}`\n"
        f"• صور ناجحة: `{_stats.get('photo_ok', 0)}`\n"
        f"• صور ناقصة: `{_stats.get('photo_miss', 0)}`\n"
        f"• FloodWait: `{_stats.get('flood_waits', 0)}`\n"
        f"• تخفيف حمل: `{_stats.get('load_shed', 0)}`\n"
        f"• مستخدمون نشطون: `{len(_user_last_seen)}`\n"
        f"• concurrency: `{GLOBAL_CONCURRENCY}`\n\n"
        "💡 _بدون SerpAPI أو PA API على Railway يظهر الرابط والصورة فقط بدون سعر حي._"
    )
    await _reply(update, msg)


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import os
    svc = os.getenv("RAILWAY_SERVICE_NAME", "local")
    await _reply(
        update,
        f"🆔 *إصدار البوت:* `{BOT_VERSION}`\n"
        f"🤖 *الخدمة:* `{svc}`\n\n"
        "إذا ما تشوف هذا الإصدار — Railway ما نشر التحديث بعد.",
    )


async def myalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض تنبيهات المستخدم النشطة مع أزرار الحذف."""
    user_id = update.effective_user.id if update.effective_user else 0
    alerts  = _pa.get_user_alerts(user_id)

    if not alerts:
        await _reply(
            update,
            "📭 ما عندك تنبيهات نشطة.\n\n"
            "🔗 أرسل رابط منتج أمازون\n"
            "ثم اضغط زر *نبّهني عند انخفاض السعر* — وأرسل لك إشعار فور نزول السعر!",
        )
        return

    text, keyboard = _build_myalerts_content(alerts)
    await _reply(update, text, reply_markup=InlineKeyboardMarkup(keyboard))


# =============================================================================
# حلقة فحص التنبيهات الخلفية (كل 30 دقيقة)
# =============================================================================

async def _send_alert_notification(app, alert: dict, offer: dict, new_price: float) -> bool:
    """يرسل إشعار انخفاض السعر — صورة المنتج + نص تحفيزي."""
    saving  = alert["last_known"] - new_price
    pct     = saving / alert["last_known"] * 100
    raw_name = (alert.get("product_name") or alert["asin"])[:60]
    safe_name = raw_name.replace("*", "").replace("_", "").replace("`", "")
    link     = build_affiliate_link(alert["asin"], alert["domain"])
    caption = (
        f"🔔 *انخفض السعر!*\n\n"
        f"📦 {safe_name}\n"
        f"💰 `{new_price:.2f} SAR` (كان `{alert['last_known']:.2f}`)\n"
        f"✅ وفّرت `{saving:.2f} SAR` ({pct:.0f}%)\n\n"
        f"👇 اضغط «اشتري الآن» قبل ما يرتفع"
    )
    buy_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 اشتري الآن ↗", url=link),
    ]])
    image_url = build_product_image_url(alert["asin"], alert["domain"], offer)
    loop = asyncio.get_running_loop()
    photo_bytes = await loop.run_in_executor(None, download_image_bytes, image_url)
    try:
        if photo_bytes:
            await app.bot.send_photo(
                chat_id=alert["chat_id"],
                photo=BytesIO(photo_bytes),
                caption=caption[:_MAX_CAPTION],
                parse_mode="Markdown",
                reply_markup=buy_kb,
            )
        elif image_url:
            await app.bot.send_photo(
                chat_id=alert["chat_id"],
                photo=image_url,
                caption=caption[:_MAX_CAPTION],
                parse_mode="Markdown",
                reply_markup=buy_kb,
            )
        else:
            await app.bot.send_message(
                chat_id=alert["chat_id"],
                text=caption + f"\n\n🛒 {link}",
                parse_mode="Markdown",
                reply_markup=buy_kb,
            )
        return True
    except TelegramError:
        try:
            plain = caption.replace("*", "").replace("`", "")
            await app.bot.send_message(
                chat_id=alert["chat_id"],
                text=plain + f"\n\n🛒 {link}",
                parse_mode=None,
                reply_markup=buy_kb,
            )
            return True
        except Exception as se:
            logger.warning("alert_loop: فشل إرسال تنبيه — %s", se)
            return False


async def _price_alert_check_loop(app) -> None:
    """
    كل 30 دقيقة: يجلب أسعار المنتجات المتابَعة ويُرسل تنبيهات عند الانخفاض.
    يُجمّع ASINs الفريدة لتجنب الطلبات المكررة.
    """
    await asyncio.sleep(120)   # انتظر دقيقتين بعد البدء
    while True:
        try:
            alerts = _pa.get_all_active()
            if alerts:
                logger.info("alert_loop: فحص %d تنبيه نشط...", len(alerts))

                # تجميع ASINs الفريدة
                unique: dict[tuple, dict | None] = {}
                for a in alerts:
                    key = (a["asin"], a["domain"])
                    if key not in unique:
                        unique[key] = None

                loop = asyncio.get_running_loop()

                # جلب الأسعار مع تأخير بسيط بين الطلبات
                for asin_key in unique:
                    req_asin, req_domain = asin_key
                    try:
                        offer = await loop.run_in_executor(
                            None, get_lowest_offer, req_asin, req_domain
                        )
                        unique[asin_key] = offer
                    except Exception as fe:
                        logger.warning("alert_loop: فشل جلب ASIN %s — %s", req_asin, fe)
                    await asyncio.sleep(2)   # تأخير بين الطلبات لتجنب الحجب

                # مطابقة النتائج مع التنبيهات
                sent = 0
                for alert in alerts:
                    key   = (alert["asin"], alert["domain"])
                    offer = unique.get(key)

                    if not offer or offer.get("blocked") or offer.get("stale"):
                        continue

                    new_price = offer.get("price_val")
                    if not new_price:
                        continue

                    if alert["last_known"] <= 0:
                        _pa.update_last_price(alert["id"], new_price, notified=False)
                        continue

                    if _pa.check_drop(new_price, alert["last_known"]):
                        ok = await _send_alert_notification(app, alert, offer, new_price)
                        if ok:
                            _pa.update_last_price(alert["id"], new_price, notified=True)
                            sent += 1
                            logger.info(
                                "alert_loop: ✅ تنبيه أُرسل — user=%d ASIN=%s %.2f→%.2f SAR",
                                alert["user_id"], alert["asin"],
                                alert["last_known"], new_price,
                            )
                    else:
                        _pa.update_last_price(alert["id"], new_price, notified=False)

                if sent:
                    logger.info("alert_loop: أُرسل %d تنبيه في هذه الدورة", sent)

        except Exception as e:
            logger.error("_price_alert_check_loop فشل: %s", e, exc_info=True)

        await asyncio.sleep(1800)   # كل 30 دقيقة


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """صورة منتج → تعرّف بالرؤية → بحث أمازون."""
    if not update.message or not update.message.photo:
        return

    _track_user(update)
    user_id = update.effective_user.id if update.effective_user else 0
    if _is_rate_limited(user_id):
        await _reply(update, "⏳ أرسلت طلبات كثيرة. انتظر قليلاً ثم حاول.", parse_mode=None)
        return

    await _typing(update, context)
    await _reply(update, "📸 جاري التعرف على المنتج من الصورة…", parse_mode=None)

    photo = update.message.photo[-1]
    loop = asyncio.get_running_loop()
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        bio = BytesIO()
        await tg_file.download_to_memory(bio)
        image_bytes = bio.getvalue()
    except Exception as e:
        logger.warning("photo download: %s", e)
        await _reply(update, "⚠️ ما قدرت أحمّل الصورة. أرسل رابط المنتج بدلها.", parse_mode=None)
        return

    async with _HeavySlot() as got:
        if not got:
            await _reply(update, "⏳ البوت مشغول — حاول بعد لحظات.", parse_mode=None)
            return
        try:
            name = await asyncio.wait_for(
                loop.run_in_executor(None, identify_product_from_image, image_bytes),
                timeout=35.0,
            )
        except Exception as e:
            logger.warning("vision identify: %s", e)
            name = None

    if not name:
        await _reply(
            update,
            "🤔 ما تعرّفت على المنتج بوضوح.\n"
            "جرّب صورة أوضح أو أرسل *رابط أمازون* / *اسم المنتج*.",
        )
        return

    await _reply(update, f"🔎 لقيته شكله: *{name[:80]}*\nجاري البحث في أمازون…")
    await _search_and_deliver_product(update, context, name)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نص عادي: اسم منتج → بحث فوري بصورة وسعر. تحية → رد قصير."""
    if not update.message or not update.message.text:
        return

    _track_user(update)
    user_id = update.effective_user.id if update.effective_user else 0
    if _is_rate_limited(user_id):
        await _reply(update, "⏳ أرسلت طلبات كثيرة. انتظر قليلاً ثم حاول.", parse_mode=None)
        return

    text = update.message.text.strip()
    _add_to_history(user_id, "user", text)

    if _GREETING_RE.match(text):
        await _reply(
            update,
            f"👋 هلا!\n\n"
            "اكتب *اسم منتج* أو أرسل *رابط أمازون* — أجيبك بالصورة والأزرار فوراً.",
        )
        return

    loop = asyncio.get_running_loop()
    product_query = None
    if not (SKIP_AI_CHAT_UNDER_LOAD and _is_under_load()):
        try:
            from claude_utils import extract_product_intent
            product_query = await loop.run_in_executor(None, extract_product_intent, text)
        except Exception as e:
            logger.warning("extract_product_intent فشل: %s", e)
    elif _is_under_load():
        logger.info("load_shed: تخطّي extract_product_intent — %d مستخدم نشط", len(_user_last_seen))
        _stat("load_shed")

    if not product_query:
        product_query = _guess_product_query(text)

    if product_query:
        await _search_and_deliver_product(update, context, product_query)
        _add_to_history(user_id, "assistant", product_query[:200])
        return

    if SKIP_AI_CHAT_UNDER_LOAD and _is_under_load():
        if len(text) >= 3:
            await _search_and_deliver_product(update, context, text)
        else:
            await _reply(update, "📝 اكتب اسم المنتج أو أرسل رابط أمازون.", parse_mode=None)
        return

    async with _HeavySlot() as got_slot:
        if not got_slot:
            if len(text) >= 3:
                await _search_and_deliver_product(update, context, text)
            else:
                await _reply(
                    update,
                    "⏳ البوت مشغول. اكتب اسم منتج أو أرسل رابط أمازون.",
                    parse_mode=None,
                )
            return
        try:
            from claude_utils import chat_response
            history  = _user_history[user_id][:-1]
            response = await asyncio.wait_for(
                loop.run_in_executor(None, chat_response, text, history),
                timeout=15.0,
            )
        except Exception as e:
            logger.warning("chat_response فشل: %s", e)
            response = None

    if response:
        await _reply(update, response, parse_mode=None)
        _add_to_history(user_id, "assistant", response)
        _stat("requests_ok")
        return

    # آخر محاولة: ابحث بالنص نفسه
    if len(text) >= 3:
        await _search_and_deliver_product(update, context, text)
        return

    await _reply(update, "📝 اكتب اسم المنتج أو أرسل رابط أمازون.", parse_mode=None)
    _stat("requests_ok")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        err = context.error

        # FloodWait — تيليجرام يطلب انتظاراً
        if isinstance(err, RetryAfter):
            _stat("flood_waits")
            logger.warning("Telegram FloodWait %ds — error_handler", err.retry_after)
            await asyncio.sleep(min(int(err.retry_after) + 1, 30))
            return

        # Conflict — نسخة أخرى تعمل: اطرد المنافس بشكل غير متزامن ثم ارجع للـ polling
        if isinstance(err, Conflict):
            logger.warning("⚡ تعارض: أطرد النسخة المنافسة...")
            _kick = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
            loop = asyncio.get_running_loop()
            try:
                r1 = await loop.run_in_executor(
                    None,
                    lambda: _req.post(f"{_kick}/setWebhook", json={"url": "https://example.com/kick"}, timeout=8)
                )
                if not r1.json().get("ok"):
                    logger.warning("setWebhook أعاد: %s", r1.text)
                await asyncio.sleep(0.8)
                r2 = await loop.run_in_executor(
                    None,
                    lambda: _req.post(f"{_kick}/deleteWebhook", json={"drop_pending_updates": False}, timeout=8)
                )
                if not r2.json().get("ok"):
                    logger.warning("deleteWebhook أعاد: %s", r2.text)
                logger.info("✅ تم طرد المنافس — Polling يستأنف")
            except Exception as _ce:
                logger.warning("طرد المنافس فشل: %s", _ce)
                await asyncio.sleep(3)
            return

        # أخطاء شبكة عابرة — لا داعي لرسالة
        if isinstance(err, (TimedOut, NetworkError)):
            logger.warning("Telegram network خطأ عابر: %s", err)
            return

        _stat("requests_error")
        logger.error("استثناء غير متوقع: %s", err, exc_info=err)

        if isinstance(update, Update) and update.message:
            try:
                await update.message.reply_text(
                    "⚠️ حصل خطأ غير متوقع. حاول مرة أخرى أو أرسل /start."
                )
            except Exception:
                pass

    except Exception as _ef:
        # error_handler نفسه فشل — نسجّل فقط ولا نرفع
        logger.critical("error_handler نفسه فشل: %s", _ef, exc_info=True)


# =============================================================================
# نقطة الانطلاق
# =============================================================================

async def _post_init(application) -> None:
    """يُشغَّل بعد بدء التطبيق — يبدأ مهام الخلفية."""
    global _GLOBAL_SEM
    _GLOBAL_SEM = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    loop = asyncio.get_running_loop()
    pool = _ensure_heavy_pool()
    loop.set_default_executor(pool)
    logger.info(
        "🚀 وضع الضغط: HIGH_LOAD=%s | concurrency=%d | rate/user=%d | cache=%d | pool=%d",
        HIGH_LOAD_MODE,
        GLOBAL_CONCURRENCY,
        RATE_MAX_PER_USER,
        OFFER_CACHE_MAX,
        HEAVY_POOL_SIZE,
    )
    # ملف تيليجرام — وصف قصير + أوامر → ظهور أقوى في البحث
    try:
        from telegram_profile import apply_telegram_profile
        await apply_telegram_profile(application)
    except Exception as e:
        logger.warning("تطبيق ملف تيليجرام فشل: %s", e)

    # تحقق تاق العمولة عند الإقلاع
    sample = build_affiliate_link("B0GM947WC5", AMAZON_DOMAIN)
    store_sample = tag_amazon_url(
        "https://www.amazon.sa/stores/page/A0A6CA9D-152E-403D-8AAF-96570B0152AB?_encoding=UTF8&tag=other-21"
    )
    if not url_has_our_tag(sample) or not url_has_our_tag(store_sample):
        logger.error("🔴 AFFILIATE CHECK FAILED — tag=%s sample=%s", get_affiliate_tag(), sample)
    else:
        logger.info("✅ AFFILIATE OK | tag=%s | product=%s", get_affiliate_tag(), sample)
        logger.info("✅ STORE TAG OK | %s", store_sample)

    # سجل المستخدمين + استيراد من تنبيهات الأسعار
    try:
        import users_db as _users
        imported = _users.import_from_price_alerts()
        logger.info("👥 users_db: استيراد %d من تنبيهات الأسعار | نشط=%s", imported, _users.count_users())
    except Exception as e:
        logger.warning("users_db import: %s", e)

    # بث أكواد الخصم + داشبورد سري
    try:
        import broadcast as _bc
        from dashboard import start_dashboard_server
        from config import USE_WEBHOOK as _uw
        _bc.set_application(application)
        _BG_TASKS.append(asyncio.create_task(_bc.scheduler_loop()))
        _BG_TASKS.append(asyncio.create_task(_bc.daily_digest_loop()))
        loop = asyncio.get_running_loop()
        if _uw:
            logger.warning("🔒 الداشبورد متوقف مؤقتاً لأن USE_WEBHOOK=true (نفس المنفذ)")
        else:
            dash_path = start_dashboard_server(loop)
            if dash_path:
                logger.info("🔒 افتح الداشبورد السري على المسار %s (بعد إدخال السر فقط)", dash_path)
    except Exception as e:
        logger.warning("dashboard/broadcast init: %s", e)

    cleared = 0
    if CLEAR_CACHE_ON_BOOT:
        cleared = clear_offer_cache()
        if cleared:
            logger.info("🧹 مُسح كاش العروض عند الإقلاع (%d إدخال)", cleared)
    else:
        logger.info("🧊 الكاش محفوظ بين إعادة التشغيل (CLEAR_CACHE_ON_BOOT=false)")
    try:
        from serpapi_utils import serpapi_available, serpapi_circuit_status
        if not serpapi_available():
            logger.error(
                "⚠️ SERPAPI_KEY غير موجود — الاسم/الصورة/السعر قد يضعفون على Railway"
            )
        else:
            logger.info("✅ SerpAPI جاهز + قاطع دائرة %s", serpapi_circuit_status())
    except Exception as e:
        logger.warning("فحص SerpAPI عند الإقلاع فشل: %s", e)
    logger.info("🛡️ حماية البطاقة: عنوان إلزامي + CDN صورة + TTL قصير للعروض الناقصة")
    _start_stats_server()
    for coro in (
        _memory_cleanup_loop(),
        _health_monitor_loop(),
        _price_alert_check_loop(application),
        _keep_alive_loop(),
    ):
        _BG_TASKS.append(asyncio.create_task(coro))
    logger.info("✅ مهام الخلفية بدأت: تنظيف + صحة + تنبيهات + keep-alive + heavy pool")


async def _post_shutdown(application) -> None:
    """إيقاف نظيف — يلغي المهام ويغلق الـ pools."""
    logger.info("🛑 إيقاف نظيف...")
    for t in _BG_TASKS:
        t.cancel()
    if _BG_TASKS:
        await asyncio.gather(*_BG_TASKS, return_exceptions=True)
    global _HEAVY_POOL
    if _HEAVY_POOL is not None:
        _HEAVY_POOL.shutdown(wait=False, cancel_futures=True)
        _HEAVY_POOL = None
    try:
        from http_client import close_http_session
        close_http_session()
    except Exception:
        pass
    logger.info("🛑 تم الإيقاف")


def main():
    import os as _os
    from config import get_deepseek_api_key, get_gemini_api_key

    _svc = _os.getenv("RAILWAY_SERVICE_NAME", "")
    _bot_svc = _os.getenv("BOT_SERVICE_NAME", "charming-strength")
    if _svc and _svc != _bot_svc:
        print(f"⛔ خدمة {_svc} — البوت يعمل على {_bot_svc} فقط.")
        return

    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  حط توكن البوت في Replit Secrets تحت اسم TELEGRAM_BOT_TOKEN")
        return

    print("=" * 50)
    print(f"🆔 Bot version: {BOT_VERSION}")
    print(f"📊 MOCK_MODE: {MOCK_MODE}")
    print(f"   {'⚠️  أسعار وهمية' if MOCK_MODE else '🔴 أسعار حقيقية'}")
    print(f"🔗 Affiliate tag: {AFFILIATE_TAG}")
    print(f"🔗 Link sample:   {build_affiliate_link('B0GM947WC5', AMAZON_DOMAIN)}")
    _store_ex = tag_amazon_url(
        "https://www.amazon.sa/stores/page/A0A6CA9D-152E-403D-8AAF-96570B0152AB?_encoding=UTF8"
    )
    print(f"🔗 Store sample:  {_store_ex}")
    if get_affiliate_tag() != "rashedalhano-21":
        print(f"⚠️  AFFILIATE_TAG={get_affiliate_tag()} (متوقع rashedalhano-21 إن كان حسابك)")
    print("=" * 50)

    import os as _os
    from config import get_deepseek_api_key

    _svc = _os.getenv("RAILWAY_SERVICE_NAME", "local")
    _deepseek = get_deepseek_api_key()
    print(f"🤖 Railway service: {_svc}")
    print(f"🔑 DEEPSEEK_API_KEY: {'✅ (' + str(len(_deepseek)) + ' حرف)' if _deepseek else '❌ غير موجود'}")
    print(f"🔑 TELEGRAM_BOT_TOKEN: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}")

    _DEV_DOMAIN  = _os.getenv("REPLIT_DEV_DOMAIN", "")
    _WEBHOOK_URL = f"https://{_DEV_DOMAIN}/api/tgwh" if _DEV_DOMAIN else ""
    _BOT_PORT    = 8765
    _URL_PATH    = "/tgwh"

    # ── ضبط اتصال قوي يتحمّل تذبذب الشبكة بدون توقف ──────────────────────
    _pool_size = 512 if HIGH_LOAD_MODE else 256
    _updates_pool = 64 if HIGH_LOAD_MODE else 32
    _req_general = HTTPXRequest(
        connection_pool_size=_pool_size,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    # طلب get_updates (polling): read_timeout أطول من long-polling نفسه
    _req_updates = HTTPXRequest(
        connection_pool_size=_updates_pool,
        connect_timeout=15.0,
        read_timeout=40.0,          # أطول من poll timeout عشان ما يقطع الاتصال
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .request(_req_general)
        .get_updates_request(_req_updates)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("share",     share_command))
    app.add_handler(CommandHandler("deals",     deals_command))
    app.add_handler(CommandHandler("compare",   compare_command))
    app.add_handler(CommandHandler("fav",       favorites_command))
    app.add_handler(CommandHandler("favorites", favorites_command))
    app.add_handler(CommandHandler("stats",     stats_command))
    app.add_handler(CommandHandler("myalerts",  myalerts_command))
    app.add_handler(CommandHandler("mute",      mute_command))
    app.add_handler(CommandHandler("unmute",    unmute_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("debug",     debug_command))
    app.add_handler(CommandHandler("version",   version_command))
    app.add_handler(CallbackQueryHandler(handle_alert_callback, pattern=r"^al[_:]"))
    app.add_handler(CallbackQueryHandler(handle_quick_callback, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(handle_mute_callback, pattern=r"^bc:"))
    app.add_handler(CallbackQueryHandler(handle_fav_callback, pattern=r"^fav"))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"https?://\S+") & ~filters.UpdateType.EDITED_MESSAGE,
            handle_link,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            handle_text,
        )
    )
    app.add_error_handler(error_handler)

    print("🚀 البوت شغّال الآن...")

    import os as _os2
    _public = (
        _os2.getenv("RAILWAY_PUBLIC_DOMAIN")
        or _os2.getenv("RAILWAY_STATIC_URL")
        or ""
    ).strip().removeprefix("https://").removeprefix("http://")

    if USE_WEBHOOK and _public:
        # Webhook أحدث وأسرع من long-polling — يحتاج نطاقاً عاماً
        wh_url = f"https://{_public}/{WEBHOOK_PATH}"
        print(f"🌐 Webhook mode → {wh_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=DASHBOARD_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=wh_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=-1,
        )
        return

    # ── طرد أي جلسة polling منافسة (Railway وغيرها) ──────────────────────────
    _kick_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    _dummy    = "https://example.com/kick"
    try:
        r1 = _req.post(f"{_kick_url}/setWebhook",    json={"url": _dummy}, timeout=10)
        if not r1.json().get("ok"):
            print(f"⚠️  setWebhook: {r1.text}")
        _time.sleep(0.8)
        r2 = _req.post(f"{_kick_url}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
        if not r2.json().get("ok"):
            print(f"⚠️  deleteWebhook: {r2.text}")
        _time.sleep(0.2)
        print("✅ طردت أي نسخة منافسة — Polling كل ثانية يبدأ الآن")
    except Exception as _ke:
        print(f"⚠️  تعذّر الطرد: {_ke}")

    if USE_WEBHOOK and not _public:
        print("⚠️  USE_WEBHOOK=true لكن لا يوجد RAILWAY_PUBLIC_DOMAIN — الرجوع لـ polling")

    app.run_polling(
        poll_interval=1.0,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
