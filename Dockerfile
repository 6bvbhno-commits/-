FROM python:3.11-slim

WORKDIR /app

# تثبيت المكتبات أولاً (cache layer)
COPY bot/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي الملفات
COPY . .

CMD ["bash", "bot/run.sh"]
