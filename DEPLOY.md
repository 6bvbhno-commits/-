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
