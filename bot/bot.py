"""
البوت الرئيسي — يستقبل روابط منتجات وصور، ويرد بأقل سعر أو حالة التوفر.
يستخدم مكتبة python-telegram-bot (الإصدار 20+).
"""
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, MOCK_MODE
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
# ملاحظة: identify_product_from_image و search_amazon_by_keywords الآن متزامنتان
# ويجب تشغيلهما عبر run_in_executor

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب — تحتوي على إفصاح الأفلييت الإلزامي."""
    welcome_text = (
        "👋 أهلاً بك!\n\n"
        "أرسل لي رابط أي منتج من أمازون، وبعطيك السعر الدقيق والمحدث له.\n"
        "أو أرسل صورة لمنتج، وبدور لك عليه في أمازون وأقولك إذا متوفر أو لا.\n\n"
        "ℹ️ روابط الشراء في هذا البوت تحتوي على رابط تسويق بالعمولة الخاص بي."
    )
    if MOCK_MODE:
        welcome_text += "\n\n⚠️ *وضع تجريبي مفعّل حاليًا* — الأسعار والنتائج وهمية للاختبار فقط."

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي رسالة نصية فيها رابط منتج (طويل أو مختصر بأي نطاق)."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # المحاولة الأولى: استخراج مباشر من الرابط كما هو
    asin = extract_asin(text)
    resolved_url = text

    # لو ما لقينا ASIN، غالبًا رابط مختصر — نحاول نفكه عبر إعادة التوجيه
    if not asin:
        await update.message.reply_text("🔗 جاري تتبع الرابط واستخراج التفاصيل...")
        try:
            loop = asyncio.get_event_loop()
            resolved_url = await loop.run_in_executor(None, resolve_short_link, text)
        except Exception as e:
            logger.error("Failed to resolve short link: %s", e)
            resolved_url = text
        asin = extract_asin(resolved_url)

    domain = extract_domain(resolved_url)

    if not asin:
        await update.message.reply_text(
            "⚠️ حتى بعد محاولة فك الرابط، ما قدرت ألقى رقم منتج (ASIN) واضح.\n"
            "جرب تفتح الرابط بالمتصفح وتنسخه من شريط العنوان مباشرة بدل زر المشاركة."
        )
        return

    await update.message.reply_text("🔎 جاري حساب وفحص السعر الدقيق من السيرفر...")

    try:
        loop = asyncio.get_event_loop()
        offer = await loop.run_in_executor(None, get_lowest_offer, asin, domain)
        message = format_offer_message(offer)
    except Exception as e:
        logger.error("Failed to fetch offer for ASIN %s: %s", asin, e)
        await update.message.reply_text(
            "❌ حصل خطأ أثناء البحث عن السعر. حاول مرة ثانية."
        )
        return

    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي صورة يرسلها المستخدم (من الكاميرا أو المعرض — نفس المعالجة)."""
    if not update.message or not update.message.photo:
        return

    await update.message.reply_text("📸 جاري تحليل الصورة بالذكاء الاصطناعي...")

    try:
        photo_file = await update.message.photo[-1].get_file()  # أعلى دقة متاحة
        photo_bytes = await photo_file.download_as_bytearray()
    except Exception as e:
        logger.error("Failed to download photo: %s", e)
        await update.message.reply_text("❌ ما قدرت أحمّل الصورة. حاول مرة ثانية.")
        return

    try:
        loop = asyncio.get_event_loop()
        product_name = await loop.run_in_executor(
            None, identify_product_from_image, bytes(photo_bytes)
        )
    except Exception as e:
        logger.error("Image analysis failed: %s", e)
        await update.message.reply_text("❌ حصل خطأ أثناء تحليل الصورة. حاول مرة ثانية.")
        return

    if not product_name:
        await update.message.reply_text(
            "❌ ما قدرت أتعرف على المنتج من الصورة.\n"
            "تأكد من وضوح الصورة أو أرسل رابط المنتج مباشرة."
        )
        return

    await update.message.reply_text(
        f"🔍 تم التعرف عليه: {product_name}\nجاري البحث في أمازون..."
    )

    try:
        loop = asyncio.get_event_loop()
        offers = await loop.run_in_executor(
            None, search_amazon_by_keywords, product_name
        )
    except Exception as e:
        logger.error("Amazon search failed: %s", e)
        await update.message.reply_text("❌ حصل خطأ أثناء البحث في أمازون. حاول مرة ثانية.")
        return

    message = format_search_results(product_name, offers)
    try:
        await update.message.reply_text(message, parse_mode="Markdown")
    except Exception as e:
        logger.warning("Markdown send failed, retrying plain: %s", e)
        # fallback: أرسل بدون تنسيق إذا فيه أحرف خاصة ما اتهربت
        plain = message.replace("*", "").replace("`", "").replace("_", "").replace("\\", "")
        await update.message.reply_text(plain)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي رسالة نصية ما فيها رابط أمازون واضح."""
    await update.message.reply_text(
        "أرسل لي رابط منتج من أمازون 🔗 عشان أساعدك."
    )


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  لازم تحط توكن البوت الحقيقي في Replit Secrets تحت اسم TELEGRAM_BOT_TOKEN")
        return

    # طباعة واضحة لحالة الإعدادات الحالية عشان تتأكد بسرعة بدون ما تدور بالملفات
    print("=" * 50)
    print(f"📊 MOCK_MODE (وضع الأسعار التجريبي): {MOCK_MODE}")
    print(f"   {'⚠️  الأسعار وهمية للاختبار' if MOCK_MODE else '🔴 يحاول يجيب أسعار حقيقية (يحتاج API معتمد)'}")
    print("=" * 50)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"https?://\S+"), handle_link)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    print("🚀 البوت شغّال الآن... اضغط Ctrl+C للإيقاف")
    app.run_polling()


if __name__ == "__main__":
    main()
