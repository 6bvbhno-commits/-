"""
إعدادات البوت — عدّل القيم هنا أو استخدم متغيرات البيئة (Environment Variables)
"""
import os


def _first_env(*names: str) -> str:
    """يرجع أول متغير بيئة موجود وغير فارغ."""
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return ""


def get_gemini_api_key() -> str:
    """مفتاح Gemini — يُقرأ وقت التشغيل."""
    return _first_env(
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GEMINI_KEY",
    )


def get_deepseek_api_key() -> str:
    """مفتاح DeepSeek — يُقرأ وقت التشغيل."""
    return _first_env("DEEPSEEK_API_KEY")


def get_openai_vision_config() -> tuple[str, str]:
    """(base_url, api_key) لـ ChatGPT/OpenAI Vision على Railway أو Replit."""
    api_key = _first_env(
        "OPENAI_API_KEY",
        "CHATGPT_API_KEY",
        "AI_INTEGRATIONS_OPENAI_API_KEY",
    )
    base_url = _first_env(
        "OPENAI_BASE_URL",
        "AI_INTEGRATIONS_OPENAI_BASE_URL",
    ) or "https://api.openai.com/v1"
    return base_url.rstrip("/"), api_key

# توكن البوت — تحصل عليه من @BotFather في تيليجرام
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# تاق الأفلييت الخاص بك — افتراضي ثابت: rashedalhano-21
# لا تغيّره إلا عبر AFFILIATE_TAG في Railway إذا احتجت حساباً آخر
_DEFAULT_AFFILIATE_TAG = "rashedalhano-21"
AFFILIATE_TAG = (os.getenv("AFFILIATE_TAG") or _DEFAULT_AFFILIATE_TAG).strip() or _DEFAULT_AFFILIATE_TAG

# نطاق أمازون المستهدف (بدون www — مطلوب لصحة عنوان PAAPI)
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.sa")

# هوية البوت في تيليجرام (تظهر في البحث وملف البوت)
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "بوت أسعار أمازون")
BOT_SHORT_DESCRIPTION = os.getenv(
    "BOT_SHORT_DESCRIPTION",
    "أرخص أسعار أمازون السعودية 🔥 أرسل رابط أو اسم منتج — صورة + سعر + تنبيه انخفاض. وفّر فلوسك!",
)
BOT_DESCRIPTION = os.getenv(
    "BOT_DESCRIPTION",
    (
        "بوت أسعار أمازون السعودية — اكتشف أرخص سعر قبل ما تشتري.\n\n"
        "✨ أرسل رابط منتج أمازون ← صورة + أقل سعر + زر شراء بعمولة\n"
        "🔍 اكتب اسم المنتج ← بحث فوري في أمازون.sa\n"
        "🔔 نبّهني عند انخفاض السعر — إشعار تلقائي\n"
        "🏪 يدعم روابط المتاجر (Stores) وصفحات العروض\n\n"
        "كلمات مفتاحية: أسعار أمازون، أرخص سعر، عروض أمازون السعودية، "
        "تنبيه انخفاض السعر، مقارنة أسعار، أمازون.sa\n\n"
        "روابط الشراء تحتوي على تاق تسويق بالعمولة (Associates)."
    ),
)

# مفاتيح الوصول لـ PA API v5 الرسمي
# LWA (Login with Amazon) — من ملف credentials CSV (Credential Id + Secret)
AMAZON_LWA_CLIENT_ID = os.getenv("AMAZON_LWA_CLIENT_ID", "")
AMAZON_LWA_CLIENT_SECRET = os.getenv("AMAZON_LWA_CLIENT_SECRET", "")
# AWS SigV4 — احتياطي (Access Key + Secret Key القديمة)
AMAZON_ACCESS_KEY = os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY = os.getenv("AMAZON_SECRET_KEY")

# مفتاح Gemini API (مجاني من Google AI Studio) — للتعرف على المنتجات في الصور
GEMINI_API_KEY = get_gemini_api_key()

# مفتاح Google Cloud Vision API (قديم — استُبدل بـ Gemini)
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")

# مفتاح SerpAPI — البديل الجذري للكشط (serpapi.com)
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")

# Replit/OpenAI — يُقرأ أيضاً عبر get_openai_vision_config() للصور
_openai_base, _openai_key = get_openai_vision_config()
OPENAI_BASE_URL = _openai_base
OPENAI_API_KEY  = _openai_key

# Replit Anthropic Integration — Claude للمحادثة وتحليل الأسعار
ANTHROPIC_BASE_URL = os.getenv("AI_INTEGRATIONS_ANTHROPIC_BASE_URL", "")
ANTHROPIC_API_KEY  = os.getenv("AI_INTEGRATIONS_ANTHROPIC_API_KEY", "")

# DeepSeek API — الطبقة الأولى للذكاء الاصطناعي
DEEPSEEK_API_KEY = get_deepseek_api_key()

# وضع تجريبي: False = أسعار حقيقية من PA API
MOCK_MODE = False


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    """يقرأ عدداً صحيحاً من متغير البيئة مع حدود آمنة."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ── إعدادات تحمل الضغط العالي (حملات / زيارات كثيرة) ───────────────────────
# يمكن ضبطها من Railway Variables بدون تعديل الكود
HIGH_LOAD_MODE = _env_bool("HIGH_LOAD_MODE", True)

# طلبات ثقيلة متزامنة (SerpAPI + scraping + LLM)
GLOBAL_CONCURRENCY = _env_int("GLOBAL_CONCURRENCY", 16 if HIGH_LOAD_MODE else 8, minimum=4, maximum=32)

# حد الطلبات لكل مستخدم في الدقيقة
RATE_MAX_PER_USER = _env_int("RATE_MAX_PER_USER", 40 if HIGH_LOAD_MODE else 30, minimum=10, maximum=120)

# حجم كاش الأسعار (ASIN → عرض)
OFFER_CACHE_MAX = _env_int("OFFER_CACHE_MAX", 2500 if HIGH_LOAD_MODE else 500, minimum=100, maximum=10_000)

# طلبات SerpAPI/كشط متزامنة
SCRAPE_CONCURRENCY = _env_int("SCRAPE_CONCURRENCY", 14 if HIGH_LOAD_MODE else 10, minimum=2, maximum=30)

# عند الضغط: تخطّي محادثة AI واستخدم البحث المباشر فقط
SKIP_AI_CHAT_UNDER_LOAD = _env_bool("SKIP_AI_CHAT_UNDER_LOAD", HIGH_LOAD_MODE)

# عدد المستخدمين النشطين الذي يُفعّل وضع تخفيف الحمل
LOAD_SHED_ACTIVE_USERS = _env_int("LOAD_SHED_ACTIVE_USERS", 80, minimum=20, maximum=500)

# ── ترقية v6: أداء وموثوقية ────────────────────────────────────────────────
# TTL كاش العروض (ثوانٍ)
CACHE_TTL_FULL = _env_int("CACHE_TTL_FULL", 3 * 3600, minimum=60, maximum=24 * 3600)
CACHE_TTL_WEAK = _env_int("CACHE_TTL_WEAK", 10 * 60, minimum=30, maximum=3600)
CLEAR_CACHE_ON_BOOT = _env_bool("CLEAR_CACHE_ON_BOOT", False)

# حجم مجمع الخيوط للأعمال الثقيلة (SerpAPI / scrape / LLM)
HEAVY_POOL_SIZE = _env_int("HEAVY_POOL_SIZE", 24 if HIGH_LOAD_MODE else 12, minimum=4, maximum=64)

# قاطع دائرة SerpAPI
SERPAPI_CIRCUIT_FAILS = _env_int("SERPAPI_CIRCUIT_FAILS", 5, minimum=2, maximum=30)
SERPAPI_CIRCUIT_COOLDOWN = _env_int("SERPAPI_CIRCUIT_COOLDOWN", 90, minimum=15, maximum=600)

# سجلات JSON لـ Railway
JSON_LOGS = _env_bool("JSON_LOGS", True)

# Webhook اختياري (أحدث من long-polling) — يحتاج Public Networking
USE_WEBHOOK = _env_bool("USE_WEBHOOK", False)
WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or "tgwh").strip().strip("/") or "tgwh"
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()

# ── داشبورد سري (أكواد الخصم) — لا يُفعَّل بدون سر قوي ─────────────────────
# مثال: DASHBOARD_SECRET=خيط-طويل-عشوائي-جداً
DASHBOARD_SECRET = (os.getenv("DASHBOARD_SECRET") or "").strip()
# مسار مخفي — لا تشاركه؛ الافتراضي عشوائي ثابت مشتق من السر عند التشغيل
DASHBOARD_PATH = (os.getenv("DASHBOARD_PATH") or "").strip().strip("/")
# منفذ الداشبورد (Railway يستخدم PORT عادةً)
DASHBOARD_PORT = _env_int("PORT", _env_int("DASHBOARD_PORT", 8080, minimum=1, maximum=65535), minimum=1, maximum=65535)
