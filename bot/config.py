"""
إعدادات البوت — عدّل القيم هنا أو استخدم متغيرات البيئة (Environment Variables)
"""
import os

# توكن البوت — تحصل عليه من @BotFather في تيليجرام
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ضع_توكن_البوت_هنا")

# تاق الأفلييت الخاص بك (استخرجناه من رابطك)
AFFILIATE_TAG = os.getenv("AFFILIATE_TAG", "rashedalhano-21")

# نطاق أمازون المستهدف (بدون www — مطلوب لصحة عنوان PAAPI)
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.sa")

# مفاتيح الوصول لـ PA API v5 الرسمي (تأخذها من حساب الأفلييت الخاص بك)
AMAZON_ACCESS_KEY = os.getenv("AMAZON_ACCESS_KEY", "ضع_مفتاح_Access_Key_هنا")
AMAZON_SECRET_KEY = os.getenv("AMAZON_SECRET_KEY", "ضع_مفتاح_Secret_Key_هنا")

# مفتاح Google Cloud Vision API (اختياري بالبداية — بدونه يشتغل البوت بوضع تجريبي)
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")

# وضع تجريبي: لو True يرجع بيانات وهمية بدل الاتصال بأمازون الحقيقي
# خليه True أول ما تجرب البوت، وحوّله False لما تجهز مفاتيح API الحقيقية
MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"
