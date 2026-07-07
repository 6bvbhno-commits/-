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

# مفتاح Google Cloud Vision API (اختياري — بدونه يشتغل البوت بوضع تجريبي للصور)
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")

# وضع تجريبي: False = أسعار حقيقية من PA API
MOCK_MODE = False
