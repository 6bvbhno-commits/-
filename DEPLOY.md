# نشر البوت بدون Replit

المشروع جاهز للنشر عبر **Dockerfile** و **railway.toml**.

## Railway (موصى به)

1. افتح: https://railway.com/new
2. سجّل دخول بـ **GitHub**
3. اختر **GitHub Repository** → `6bvbhno-commits/-`
4. افتح المشروع → **Variables** وأضف:
   - `TELEGRAM_BOT_TOKEN` = توكن البوت من @BotFather
   - `AFFILIATE_TAG` = `rashedalhano-21`
   - `AMAZON_DOMAIN` = `amazon.sa`
5. اضغط **Deploy** — Railway يقرأ `Dockerfile` تلقائياً
6. في **Logs** تأكد من ظهور:
   ```
   🔗 Link sample: https://www.amazon.sa/dp/B0GM947WC5/ref=nosim?tag=rashedalhano-21
   ```

## Render (بديل مجاني)

1. افتح: https://dashboard.render.com/select-repo
2. اختر المستودع `6bvbhno-commits/-`
3. Render يقرأ `render.yaml` تلقائياً
4. أضف `TELEGRAM_BOT_TOKEN` في Environment

## ملاحظات

- `bot/run.sh` يشغّل البوت **فقط على Railway** (`RAILWAY_ENVIRONMENT` يُضبط تلقائياً)
- على Render يُضبط `RAILWAY_ENVIRONMENT` يدوياً أو عدّل `run.sh`
- لا تحتاج Replit بعد الآن

## ظهور البوت في بحث تيليجرام

بعد النشر، البوت يضبط تلقائياً: الاسم + الوصف القصير + الأوامر.

فعّل الوضع المضمّن مرة واحدة من @BotFather (يزيد الانتشار جداً):

1. افتح @BotFather → `/mybots` → اختر البوت
2. `Bot Settings` → `Inline Mode` → `Turn on`
3. Placeholder مقترح: `ابحث عن منتج في أمازون...`
4. (اختياري) `/setuserpic` — ضع صورة واضحة للبوت
5. (اختياري) `/setabouttext` — نفس نص الوصف القصير

تأكد في Logs من:
```
✅ AFFILIATE OK | tag=rashedalhano-21
📣 short_description OK
```

## ترقية v6.3

- `/deals` يبقى بعد إعادة النشر (SQLite)
- نصيحة شراء ذكية من تاريخ السعر على البطاقة
- `/compare` مع تنبيه + مفضلة + رسم بياني
- `/fav` → زر 🔔 تنبيه سريع لكل منتج
- بحث بالصورة يدعم إرسال الصورة كملف
- تفعيل التنبيه ما يحذف أزرار الشراء
- `/debug` للمدير فقط (`ADMIN_IDS`)
- توضيح: `/mute` يوقف رسائل العروض فقط — تنبيهات السعر تبقى

## ترقية v6.2

- `/compare رابط1 رابط2` — مقارنة سعر منتجين
- `/fav` — مفضلة حتى 20 منتج + زر ⭐ على البطاقة
- بحث بالصورة 📸 (Vision → أمازون)
- `/stats` للمدير — مستخدمون/طلبات/قاطع SerpAPI
- رسم بياني نصي لتاريخ السعر على بطاقة المنتج

## ترقية v6.1 (تشغيل من الجوال + نمو عضوي)

- `/broadcast كود|عنوان|تفاصيل` للمدير فقط (`ADMIN_IDS`)
- `/mute` و `/unmute` + زر إيقاف على رسائل البث
- أزرار سريعة في `/start` + مشاركة بطاقة المنتج
- شارة خصم % على البطاقة + ملخص يومي 21:00 الرياض (`DAILY_DIGEST_ENABLED`)

```
ADMIN_IDS=123456789
DAILY_DIGEST_ENABLED=true
DAILY_DIGEST_HOUR=21
```

## ترقية v6 (أداء وموثوقية)

- قاطع دائرة SerpAPI + جلسة HTTP مشتركة (Keep-Alive)
- مجمع خيوط ثقيل + سجلات JSON
- أمر `/deals` للعروض الرائجة
- مقارنة السعر مع التاريخ على بطاقة المنتج
- Webhook اختياري: `USE_WEBHOOK=true` + `RAILWAY_PUBLIC_DOMAIN`

## داشبورد سري لأكواد الخصم (مالك البوت فقط)

الداشبورد **لا يعمل** بدون سر قوي. لا يوجد رابط عام ظاهر.

1. في Railway → Variables أضف:
   ```
   DASHBOARD_SECRET=ضع-هنا-سراً-طويلاً-عشوائياً-12حرف-على-الأقل
   ```
   (اختياري) مسار مخصص:
   ```
   DASHBOARD_PATH=my-private-panel-xyz
   ```
2. فعّل **Public Networking** على الخدمة (منفذ `PORT`) حتى تفتح الرابط من المتصفح
3. بعد النشر، ابحث في Logs عن:
   ```
   🔒 داشبورد سري شغّال ... المسار /x...../
   ```
4. افتح: `https://<نطاق-Railway>/<المسار>/`
5. أدخل نفس `DASHBOARD_SECRET` — الجلسة 12 ساعة، محاولات الدخول محدودة

من اللوحة:
- معاينة الرسالة
- **جدولة الساعة 9 م (الرياض)** لكود أمازون
- أو إرسال فوري لكل من ضغط `/start`

⚠️ لا تشارك السر ولا المسار مع أحد. بدون السر الصفحة لا تعمل.
