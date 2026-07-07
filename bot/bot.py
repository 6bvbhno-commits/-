"""
البوت الرئيسي — يستقبل روابط منتجات وصور، ويرد بأقل سعر أو حالة التوفر.
يستخدم مكتبة python-telegram-bot (الإصدار 20+).
"""
import asyncio
import logging
import time as _time
from collections import defaultdict
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, MOCK_MODE, AMAZON_DOMAIN
from amazon_utils import (
    extract_asin,
    extract_domain,
    resolve_short_link,
    get_lowest_offer,
    format_offer_message,
)
from vision_utils import (
    identify_product_from_image,
    search_amazon_by_keywords,
    format_search_results,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── حماية من الفيضان: 5 طلبات / 60 ثانية لكل مستخدم ───────────────────────
_RATE_WINDOW   = 60    # ثانية
_RATE_MAX      = 10    # أقصى طلبات في النافذة
_user_times: dict[int, list[float]] = defaultdict(list)

def _is_rate_limited(user_id: int) -> bool:
    now  = _time.monotonic()
    buf  = _user_times[user_id]
    buf[:] = [t for t in buf if now - t < _RATE_WINDOW]
    if len(buf) >= _RATE_MAX:
        return True
    buf.append(now)
    return False

# ─── حد تيليجرام للرسائل ────────────────────────────────────────────────────
_MAX_MSG = 4096

async def _reply(update: Update, text: str, parse_mode: str | None = "Markdown") -> None:
    """يرسل رسالة مع تقليم تلقائي إذا تجاوزت حد تيليجرام."""
    if not update.message:
        return
    if len(text) > _MAX_MSG:
        text = text[: _MAX_MSG - 60] + "\n\n_…(تم اختصار الرسالة لطولها)_"
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except TelegramError as e:
        # إذا فشل Markdown، أعد الإرسال نصاً عادياً
        if parse_mode:
            plain = text.replace("*", "").replace("`", "").replace("_", "").replace("\\", "")
            try:
                await update.message.reply_text(plain[:_MAX_MSG])
            except TelegramError as e2:
                logger.error("فشل إرسال الرسالة حتى بدون Markdown: %s", e2)
        else:
            logger.error("فشل إرسال الرسالة: %s", e)


# ─── المعالجات ───────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب — تحتوي على إفصاح الأفلييت الإلزامي."""
    welcome_text = (
        "👋 أهلاً بك!\n\n"
        "🏷️ *وش يسوي هذا البوت؟*\n"
        "يجيب لك *أقل سعر متاح* لأي منتج في أمازون السعودية — من بين كل البائعين — ويعطيك رابط الشراء مباشرة.\n\n"
        "📌 *كيف تستخدمه؟*\n"
        "• أرسل رابط أي منتج من أمازون ← يرد بأرخص سعر الآن\n"
        "• أو صوّر أي منتج ← يتعرف عليه ويدور لك على أفضل سعر له\n\n"
        "ℹ️ روابط الشراء تحتوي على تاق تسويق بالعمولة."
    )
    if MOCK_MODE:
        welcome_text += "\n\n⚠️ *وضع تجريبي مفعّل حاليًا* — الأسعار والنتائج وهمية للاختبار فقط."
    await _reply(update, welcome_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🆘 *المساعدة*\n\n"
        "• أرسل *رابط منتج أمازون* (طويل أو مختصر) ← أرد بأرخص سعر ورابط شراء\n"
        "• أرسل *صورة منتج* ← أتعرف عليه وأبحث له عن أسعار\n\n"
        "💡 *نصائح للصور:*\n"
        "  - وضّح الاسم التجاري في الصورة\n"
        "  - تأكد من الإضاءة الجيدة\n"
        "  - الجهة الأمامية من العبوة أفضل\n\n"
        "📋 *الأوامر:*\n"
        "/start — رسالة الترحيب\n"
        "/help — هذه الرسالة\n\n"
        "⚠️ الأسعار حية وقد تتغير، تحقق دائماً من صفحة المنتج قبل الشراء."
    )
    await _reply(update, help_text)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي رسالة نصية فيها رابط منتج (طويل أو مختصر بأي نطاق)."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if _is_rate_limited(user_id):
        await _reply(
            update,
            "⏳ أرسلت طلبات كثيرة بسرعة. انتظر دقيقة واحدة ثم حاول مجدداً.",
            parse_mode=None,
        )
        return

    text = update.message.text.strip()

    # المحاولة الأولى: استخراج مباشر من الرابط كما هو
    asin = extract_asin(text)
    resolved_url = text

    # لو ما لقينا ASIN، غالبًا رابط مختصر — نحاول نفكه عبر إعادة التوجيه
    if not asin:
        # استخرج الرابط فقط من النص (قد يحتوي النص على كلام + رابط)
        # وأزل الترقيم الزائد من نهاية الرابط
        import re as _re
        _url_match = _re.search(r"https?://\S+", text)
        url_only = _url_match.group(0).rstrip(".,;:!?)\"']}") if _url_match else text

        await _reply(update, "🔗 جاري تتبع الرابط واستخراج التفاصيل...", parse_mode=None)
        try:
            loop = asyncio.get_running_loop()
            resolved_url = await loop.run_in_executor(None, resolve_short_link, url_only)
        except Exception as e:
            logger.error("فشل فك الرابط المختصر: %s", e)
            resolved_url = url_only
        asin = extract_asin(resolved_url)

    domain = extract_domain(resolved_url)

    if not asin:
        await _reply(
            update,
            "⚠️ حتى بعد محاولة فك الرابط، ما قدرت ألقى رقم منتج (ASIN).\n"
            "جرب تفتح الرابط بالمتصفح وتنسخه من شريط العنوان مباشرة.",
            parse_mode=None,
        )
        return

    await _reply(update, "🔎 جاري حساب وفحص السعر الدقيق...", parse_mode=None)

    try:
        loop = asyncio.get_running_loop()
        offer = await loop.run_in_executor(None, get_lowest_offer, asin, domain)
        message = format_offer_message(offer)

        # أضف تاريخ السعر إن وُجد (يحتاج قراءتين على الأقل)
        if offer and not offer.get("blocked"):
            try:
                from price_history import format_history_message
                history = await loop.run_in_executor(None, format_history_message, asin, domain)
                if history:
                    message = message + "\n\n" + history
            except Exception as _ph_err:
                logger.warning("price_history: فشل عرض التاريخ — %s", _ph_err)

    except Exception as e:
        logger.error("خطأ في جلب السعر للـ ASIN %s: %s", asin, e, exc_info=True)
        await _reply(update, "❌ حصل خطأ أثناء البحث عن السعر. حاول مرة ثانية.", parse_mode=None)
        return

    await _reply(update, message)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي صورة يرسلها المستخدم (من الكاميرا أو المعرض أو ملف)."""
    if not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if _is_rate_limited(user_id):
        await _reply(
            update,
            "⏳ أرسلت طلبات كثيرة بسرعة. انتظر دقيقة واحدة ثم حاول مجدداً.",
            parse_mode=None,
        )
        return

    await _reply(update, "📸 جاري تحليل الصورة بالذكاء الاصطناعي...", parse_mode=None)

    try:
        # صورة عادية
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        # ملف صورة (document)
        elif update.message.document and update.message.document.mime_type and \
                update.message.document.mime_type.startswith("image/"):
            photo_file = await update.message.document.get_file()
        else:
            await _reply(update, "❌ أرسل صورة لأتعرف على المنتج.", parse_mode=None)
            return

        # لا نُمرّر URL التيليجرام لأي خدمة خارجية — يحتوي على التوكن
        # Google Lens تحتاج URL عام؛ نتجنبها بتمرير سلسلة فارغة
        photo_bytes = await photo_file.download_as_bytearray()

        # حد الحجم: 8 MB — صور أكبر من هذا نادرة ونرفضها لتوفير الوقت
        if len(photo_bytes) > 8 * 1024 * 1024:
            await _reply(
                update,
                "⚠️ الصورة كبيرة جداً. أرسل صورة أصغر من 8 MB.",
                parse_mode=None,
            )
            return

    except Exception as e:
        logger.error("فشل تحميل الصورة: %s", e)
        await _reply(update, "❌ ما قدرت أحمّل الصورة. حاول مرة ثانية.", parse_mode=None)
        return

    try:
        loop = asyncio.get_running_loop()
        product_name = await loop.run_in_executor(
            None, identify_product_from_image, bytes(photo_bytes), ""
        )
    except Exception as e:
        logger.error("فشل تحليل الصورة: %s", e)
        await _reply(update, "❌ حصل خطأ أثناء تحليل الصورة. حاول مرة ثانية.", parse_mode=None)
        return

    if not product_name:
        await _reply(
            update,
            "❌ ما قدرت أتعرف على المنتج من الصورة.\n"
            "تأكد من وضوح الصورة أو أرسل رابط المنتج مباشرة.",
            parse_mode=None,
        )
        return

    await _reply(
        update,
        f"🔍 تم التعرف عليه: {product_name}\nجاري البحث في أمازون...",
        parse_mode=None,
    )

    try:
        loop = asyncio.get_running_loop()
        offers = await loop.run_in_executor(None, search_amazon_by_keywords, product_name)
    except Exception as e:
        logger.error("فشل البحث في أمازون: %s", e)
        await _reply(update, "❌ حصل خطأ أثناء البحث في أمازون. حاول مرة ثانية.", parse_mode=None)
        return

    message = format_search_results(product_name, offers)
    await _reply(update, message)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي رسالة نصية ما فيها رابط أمازون."""
    if not update.message:
        return
    await _reply(
        update,
        "أرسل لي رابط منتج من أمازون 🔗 أو صورة منتج 📸 عشان أساعدك.\n"
        "اكتب /help لمزيد من التفاصيل.",
        parse_mode=None,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعترض كل استثناء لم يُعالَج ويسجّله، ثم يُبلّغ المستخدم بهدوء."""
    logger.error("استثناء غير متوقع:", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                "⚠️ حصل خطأ غير متوقع. حاول مرة أخرى أو أرسل /start."
            )
        except Exception:
            pass


# ─── نقطة الانطلاق ───────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  لازم تحط توكن البوت في Replit Secrets تحت اسم TELEGRAM_BOT_TOKEN")
        return

    print("=" * 50)
    print(f"📊 MOCK_MODE: {MOCK_MODE}")
    print(f"   {'⚠️  الأسعار وهمية للاختبار' if MOCK_MODE else '🔴 أسعار حقيقية'}")
    print("=" * 50)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)   # معالجة متزامنة لمستخدمين مختلفين
        .build()
    )

    # الأوامر
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help",  help_command))

    # الصور والملفات
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(
        MessageHandler(
            filters.Document.IMAGE,
            handle_photo,
        )
    )

    # الروابط النصية — فلتر الرسائل المحرَّرة حتى لا تُعالَج مرتين
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"https?://\S+") & ~filters.UpdateType.EDITED_MESSAGE,
            handle_link,
        )
    )

    # النصوص العادية
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            handle_unknown,
        )
    )

    # معالج الأخطاء العام
    app.add_error_handler(error_handler)

    print("🚀 البوت شغّال الآن...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
