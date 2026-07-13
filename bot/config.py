"""
إعدادات البوت — عدّل القيم هنا أو استخدم متغيرات البيئة (Environment Variables)
"""
import os

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
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

# مفتاح Google Cloud Vision API (قديم — استُبدل بـ Gemini)
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")

# مفتاح SerpAPI — البديل الجذري للكشط (serpapi.com)
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")

# Replit OpenAI Integration — vision عبر GPT-4o بدون مفتاح خاص
OPENAI_BASE_URL = os.getenv("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
OPENAI_API_KEY  = os.getenv("AI_INTEGRATIONS_OPENAI_API_KEY", "")

# Replit Anthropic Integration — Claude للمحادثة وتحليل الأسعار
ANTHROPIC_BASE_URL = os.getenv("AI_INTEGRATIONS_ANTHROPIC_BASE_URL", "")
ANTHROPIC_API_KEY  = os.getenv("AI_INTEGRATIONS_ANTHROPIC_API_KEY", "")

# DeepSeek API — الطبقة الأولى للذكاء الاصطناعي (V3 للمحادثة، R1 لتحليل الأسعار)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# وضع تجريبي: False = أسعار حقيقية من PA API
MOCK_MODE = False
