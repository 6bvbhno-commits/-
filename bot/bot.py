"""
البوت الرئيسي — يستقبل روابط منتجات وصور، ويرد بأقل سعر أو حالة التوفر.
يستخدم مكتبة python-telegram-bot (الإصدار 20+) + Claude AI.
"""
import asyncio
import json as _json
import logging
import re as _re
import threading as _threading
import time as _time
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
    MessageHandler,
    ContextTypes,
    filters,
)

import price_alerts as _pa
from config import TELEGRAM_BOT_TOKEN, MOCK_MODE, AMAZON_DOMAIN, AFFILIATE_TAG
from amazon_utils import (
    build_affiliate_link,
    build_product_image_url,
    download_image_bytes,
    extract_asin,
    extract_domain,
    extract_product_title,
    fetch_product_image_bytes,
    resolve_short_link,
    get_lowest_offer,
    format_offer_message,
    format_product_reply_plain,
)
from vision_utils import (
    search_amazon_by_keywords,
    format_search_results,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_VERSION = "3.5"

# نص زر تنبيه السعر — واضح للمستخدم
ALERT_BTN_LABEL = "🔔 نبّهني عند انخفاض السعر"

# ─── Rate limiting ────────────────────────────────────────────────────────────
_RATE_WINDOW = 60
_RATE_MAX    = 30   # رُفع من 20 → 30 لاستيعاب ضغط الحملات التسويقية

# ─── Global backpressure — يحد الطلبات الثقيلة المتزامنة (LLM + scraping) ──
# يُهيَّأ في _post_init بعد بدء event loop
_GLOBAL_SEM: asyncio.Semaphore | None = None
_user_times: dict[int, list[float]] = defaultdict(list)
_user_last_seen: dict[int, float]   = {}   # آخر نشاط — للتنظيف الدوري

def _is_rate_limited(user_id: int) -> bool:
    now  = _time.monotonic()
    _user_last_seen[user_id] = now
    buf  = _user_times[user_id]
    buf[:] = [t for t in buf if now - t < _RATE_WINDOW]
    if len(buf) >= _RATE_MAX:
        return True
    buf.append(now)
    return False

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
    "last_request_ts": 0.0,
}

# ─── Stats HTTP server (port 8766) — يُعرض للـ dashboard ─────────────────────
_STATS_PORT = 8766

class _StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stats":
            payload = _json.dumps({
                "active_users":   len(_user_last_seen),
                "requests_total": _stats.get("requests_total", 0),
                "requests_ok":    _stats.get("requests_ok",    0),
                "requests_error": _stats.get("requests_error", 0),
                "flood_waits":    _stats.get("flood_waits",    0),
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

    async with (_GLOBAL_SEM or asyncio.Semaphore(8)):
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
    """يرسل رسالة — يعالج FloodWait وMarkdown تلقائياً."""
    if not update.message:
        return
    if len(text) > _MAX_MSG:
        text = text[: _MAX_MSG - 60] + "\n\n_…(تم اختصار الرسالة)_"
    for attempt in range(4):
        try:
            await update.message.reply_text(
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
                    await update.message.reply_text(plain[:_MAX_MSG], reply_markup=reply_markup)
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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب مع إفصاح الأفلييت الإلزامي."""
    try:
        user_id = update.effective_user.id if update.effective_user else 0
        _user_history[user_id].clear()   # بداية محادثة جديدة

        welcome_text = (
            "👋 *أهلاً في بوت الأسعار — وفّر فلوسك على أمازون!*\n\n"
            "📌 *كيف تستخدمه؟*\n"
            "• 🔗 أرسل *رابط منتج* ← صورة + أقل سعر + زر شراء\n"
            "• 💬 اكتب *اسم منتج* ← أبحث لك فوراً\n\n"
            "🔔 *تنبيه انخفاض السعر:*\n"
            f"اضغط زر *{ALERT_BTN_LABEL.replace('🔔 ', '')}* — وأرسلك إشعار أول ما ينزل السعر!\n"
            "📋 تنبيهاتك: /myalerts\n\n"
            "ℹ️ _روابط الشراء تحتوي على تاق تسويق بالعمولة._"
        )
        if MOCK_MODE:
            welcome_text += "\n\n⚠️ *وضع تجريبي* — الأسعار وهمية."
        await _reply(update, welcome_text)
    except Exception as _e:
        logger.error("start_command فشل: %s", _e, exc_info=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        help_text = (
            "🆘 *المساعدة*\n\n"
            "• 🔗 *رابط أمازون* ← صورة + أقل سعر + زر شراء\n"
            "• 💬 *اسم منتج* ← بحث فوري\n"
            f"• {ALERT_BTN_LABEL} ← إشعار عند نزول السعر\n\n"
            "📋 *الأوامر:* /start · /myalerts · /version\n\n"
            "⚠️ _تحقق من السعر على أمازون قبل الشراء._"
        )
        await _reply(update, help_text)
    except Exception as _e:
        logger.error("help_command فشل: %s", _e, exc_info=True)


async def _send_product_offer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    asin: str,
    domain: str,
    offer: dict | None,
    source_url: str,
) -> None:
    """صورة + وصف + أزرار في رسالة واحدة — رد على رسالة المستخدم في نفس المحادثة."""
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    reply_to = update.message.message_id

    fallback_title = extract_product_title(source_url, asin)
    if offer is None:
        offer = {"blocked": True, "affiliate_link": build_affiliate_link(asin, domain)}
    else:
        offer = dict(offer)

    # إثراء الاسم/الصورة من SerpAPI بحث إذا ناقصين
    weak_title = not (offer.get("title") or "").strip() or (offer.get("title") or "").strip().upper() == asin.upper()
    weak_image = not (offer.get("image") or "").startswith("http")
    if weak_title or weak_image:
        try:
            from serpapi_utils import search_items, serpapi_available
            if serpapi_available():
                loop = asyncio.get_running_loop()
                results = await loop.run_in_executor(
                    None, lambda: search_items(asin, domain=domain, max_results=5)
                )
                for item in results or []:
                    link = (item.get("link") or "")
                    item_asin = (item.get("asin") or "").upper()
                    if item_asin == asin.upper() or asin.upper() in link.upper() or not item_asin:
                        if weak_title and item.get("title"):
                            offer["title"] = item["title"]
                            weak_title = False
                        if weak_image and (item.get("image") or "").startswith("http"):
                            offer["image"] = item["image"]
                            weak_image = False
                        if item.get("seller_name") and not offer.get("seller_name"):
                            offer["seller_name"] = item["seller_name"]
                        if not weak_title and not weak_image:
                            break
        except Exception as e:
            logger.warning("إثراء SerpAPI فشل: %s", e)

    if not (offer.get("title") or "").strip() and fallback_title:
        offer["title"] = fallback_title
    if not (offer.get("title") or "").strip():
        offer["title"] = f"منتج {asin}"

    if context.user_data is None:
        context.user_data = {}
    context.user_data[f"pdomain_{asin}"] = domain

    affiliate_url = build_affiliate_link(asin, domain)
    buy_btn = InlineKeyboardButton("🛒 اشتري الآن ↗", url=affiliate_url)

    price_val = offer.get("price_val") if offer else None
    price_int = int(float(price_val) * 100) if price_val else 0
    cb_data = f"al:{asin}:{price_int}"
    if offer.get("title"):
        context.user_data[f"ptitle_{asin}"] = str(offer.get("title", ""))[:80]
    kb = InlineKeyboardMarkup([
        [buy_btn],
        [InlineKeyboardButton(ALERT_BTN_LABEL, callback_data=cb_data)],
    ])

    message = format_product_reply_plain(
        offer,
        fallback_title=fallback_title,
        asin=asin,
        version=BOT_VERSION,
    )

    loop = asyncio.get_running_loop()
    photo_bytes = await loop.run_in_executor(
        None, fetch_product_image_bytes, asin, domain, offer, source_url
    )
    if not photo_bytes and (offer.get("image") or "").startswith("http"):
        photo_bytes = await loop.run_in_executor(
            None, download_image_bytes, offer["image"]
        )

    await _send_offer_card(
        context,
        chat_id=chat_id,
        reply_to=reply_to,
        photo_bytes=photo_bytes,
        caption=message,
        reply_markup=kb,
    )
    _stat("requests_ok")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي رسالة نصية فيها رابط منتج أمازون."""
    if not update.message or not update.message.text:
        return

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
        await _reply(
            update,
            "⚠️ ما قدرت أستخرج رقم المنتج من هذا الرابط.\n"
            "جرّب تفتح الرابط في المتصفح وانسخه من شريط العنوان مباشرة.",
            parse_mode=None,
        )
        return

    await _typing(update, context)
    offer = None

    async with (_GLOBAL_SEM or asyncio.Semaphore(8)):
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
    from serpapi_utils import serpapi_available
    from paapi_utils import paapi_available

    def _status(val: str) -> str:
        return "✅ متوفر" if val else "❌ غير موجود"

    deepseek_key = get_deepseek_api_key()
    railway_svc = os.getenv("RAILWAY_SERVICE_NAME", "—")

    msg = (
        f"🔧 *حالة البوت v{BOT_VERSION}*\n\n"
        f"• إصدار الكود: `{BOT_VERSION}`\n"
        f"• خدمة Railway: `{railway_svc}`\n"
        f"• SerpAPI (سعر+صورة): {'✅' if serpapi_available() else '❌'}\n"
        f"• PA API (سعر+صورة): {'✅' if paapi_available() else '❌'}\n"
        f"• DeepSeek (نص): {_status(deepseek_key)}\n"
        f"• Anthropic: {_status(ANTHROPIC_API_KEY)}\n"
        f"• Telegram: {_status(TELEGRAM_BOT_TOKEN)}\n\n"
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


async def handle_unsupported_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يرفض الصور ويوجّه المستخدم للرابط أو اسم المنتج."""
    if not update.message:
        return
    await _reply(
        update,
        "📸 أرسل *رابط منتج أمازون* — أجيبك بالصورة والسعر والأزرار فوراً.",
        parse_mode=None,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نص عادي: اسم منتج → بحث فوري بصورة وسعر. تحية → رد قصير."""
    if not update.message or not update.message.text:
        return

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
    try:
        from claude_utils import extract_product_intent
        product_query = await loop.run_in_executor(None, extract_product_intent, text)
    except Exception as e:
        logger.warning("extract_product_intent فشل: %s", e)

    if not product_query:
        product_query = _guess_product_query(text)

    if product_query:
        await _search_and_deliver_product(update, context, product_query)
        _add_to_history(user_id, "assistant", product_query[:200])
        return

    async with (_GLOBAL_SEM or asyncio.Semaphore(8)):
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
    _GLOBAL_SEM = asyncio.Semaphore(8)   # حد أقصى 8 طلب ثقيل متزامن
    _start_stats_server()
    asyncio.create_task(_memory_cleanup_loop())
    asyncio.create_task(_health_monitor_loop())
    asyncio.create_task(_price_alert_check_loop(application))
    asyncio.create_task(_keep_alive_loop())
    logger.info("✅ مهام الخلفية بدأت: تنظيف الذاكرة + مراقبة الصحة + تنبيهات الأسعار + keep-alive")


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
    # طلبات عامة: pool كبير + مهلات متوازنة
    _req_general = HTTPXRequest(
        connection_pool_size=256,   # يتحمّل عدد كبير من الطلبات المتزامنة
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    # طلب get_updates (polling): read_timeout أطول من long-polling نفسه
    _req_updates = HTTPXRequest(
        connection_pool_size=32,
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
        .build()
    )

    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("myalerts",  myalerts_command))
    app.add_handler(CommandHandler("debug",     debug_command))
    app.add_handler(CommandHandler("version",   version_command))
    app.add_handler(CallbackQueryHandler(handle_alert_callback, pattern=r"^al[_:]"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_unsupported_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_unsupported_photo))
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

    # ── طرد أي جلسة polling منافسة (Railway وغيرها) ──────────────────────────
    # setWebhook يقطع أي polling نشط فوراً، deleteWebhook يعيد الحالة نظيفة.
    # drop_pending_updates=False ← Telegram يحتفظ بالرسائل خلال الانقطاع ويسلمها عند العودة.
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

    app.run_polling(
        poll_interval=1.0,
        timeout=30,                   # long-polling — أقل من read_timeout (40s)
        bootstrap_retries=-1,         # محاولات لا نهائية وقت الإقلاع — لا يستسلم عند تذبذب الشبكة
        drop_pending_updates=False,   # ← لا نحذف رسائل — Telegram يحتفظ بها ويسلمها فور عودتنا
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
