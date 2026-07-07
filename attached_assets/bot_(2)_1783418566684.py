"""
البوت الرئيسي — يستقبل روابط منتجات وصور، ويرد بأقل سعر أو حالة التوفر.
يستخدم مكتبة python-telegram-bot (الإصدار 20+).
"""
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب — تحتوي على إفصاح الأفلييت الإلزامي."""
    welcome_text = (
        "👋 أهلاً بك!\n\n"
        "أرسل لي رابط أي منتج من أمازون، وبعطيك أقل سعر متاح له.\n"
        "أو أرسل صورة لمنتج، وبدور لك عليه في أمازون وأقولك إذا متوفر أو لا.\n\n"
        "ℹ️ روابط الشراء في هذا البوت تحتوي على رابط تسويق بالعمولة الخاص بي."
    )
    if MOCK_MODE:
        welcome_text += "\n\n⚠️ *وضع تجريبي مفعّل حاليًا* — الأسعار والنتائج وهمية للاختبار فقط."

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي رسالة نصية فيها رابط منتج (طويل أو مختصر بأي نطاق)."""
    text = update.message.text.strip()

    # المحاولة الأولى: استخراج مباشر من الرابط كما هو
    asin = extract_asin(text)
    resolved_url = text

    # لو ما لقينا ASIN، غالبًا رابط مختصر — نحاول نفكه عبر إعادة التوجيه
    if not asin:
        await update.message.reply_text("🔗 جاري فك الرابط المختصر...")
        resolved_url = resolve_short_link(text)
        asin = extract_asin(resolved_url)

    domain = extract_domain(resolved_url)

    if not asin:
        await update.message.reply_text(
            "⚠️ حتى بعد محاولة فك الرابط، ما قدرت ألقى رقم منتج (ASIN) واضح.\n"
            "جرب تفتح الرابط بالمتصفح وتنسخه من شريط العنوان مباشرة بدل زر المشاركة."
        )
        return

    await update.message.reply_text("🔎 جاري البحث عن أقل سعر...")

    offer = get_lowest_offer(asin, domain=domain)
    message = format_offer_message(offer)
    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أي صورة يرسلها المستخدم (من الكاميرا أو المعرض — نفس المعالجة)."""
    await update.message.reply_text("📸 جاري تحليل الصورة...")

    photo_file = await update.message.photo[-1].get_file()  # أعلى دقة متاحة
    photo_bytes = await photo_file.download_as_bytearray()

    keywords = identify_product_from_image(bytes(photo_bytes))

    if not keywords:
        await update.message.reply_text(
            "❌ ما قدرت أتعرف على محتوى الصورة. جرب صورة أوضح فيها المنتج بشكل مباشر."
        )
        return

    results = search_amazon_by_keywords(keywords)
    message = format_search_results(results)
    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي رسالة نصية ما فيها رابط أمازون واضح."""
    await update.message.reply_text(
        "أرسل لي رابط منتج من أمازون 🔗 أو صورة 📸 عشان أساعدك."
    )


def main():
    if TELEGRAM_BOT_TOKEN == "ضع_توكن_البوت_هنا":
        print("⚠️  لازم تحط توكن البوت الحقيقي في config.py أو متغير البيئة TELEGRAM_BOT_TOKEN")
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
