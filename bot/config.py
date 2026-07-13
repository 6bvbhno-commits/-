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

# تاق الأفلييت الخاص بك
AFFILIATE_TAG = os.getenv("AFFILIATE_TAG", "rashedalhano-21")

# نطاق أمازون المستهدف (بدون www — مطلوب لصحة عنوان PAAPI)
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.sa")

# مفاتيح الوصول لـ PA API v5 الرسمي — تُسحب تلقائياً من Replit Secrets
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
