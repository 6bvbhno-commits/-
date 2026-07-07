#!/bin/bash
# Watchdog: يُشغّل البوت ويُعيد تشغيله تلقائياً عند أي تعطّل — بعد ثانية واحدة
cd "$(dirname "$0")"

RESTART_COUNT=0

while true; do
    echo "▶️  [$(date '+%H:%M:%S')] تشغيل البوت (محاولة رقم $((RESTART_COUNT + 1)))..."
    python3 bot.py
    EXIT_CODE=$?
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "⚠️  [$(date '+%H:%M:%S')] البوت توقف (كود الخروج: $EXIT_CODE) — إعادة التشغيل بعد ثانية..."
    sleep 1
done
