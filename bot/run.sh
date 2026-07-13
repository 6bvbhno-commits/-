#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  Watchdog — مراقبة البوت كل ثانية وإعادة التشغيل الفوري
# ══════════════════════════════════════════════════════════════
cd "$(dirname "$0")"

# ── منع التشغيل المزدوج: إذا لم نكن على Railway، لا تشغّل البوت ──
if [ -z "$RAILWAY_ENVIRONMENT" ]; then
    echo "⛔ البوت معطّل على Replit — يعمل على Railway فقط."
    echo "   لتشغيله هنا، احذف متغير RAILWAY_ENVIRONMENT أو عدّل run.sh"
    exit 0
fi

# ── خدمة واحدة فقط: monorepo ينشر عدة خدمات بنفس Dockerfile ──
if [ -n "$RAILWAY_SERVICE_NAME" ] && [[ "$RAILWAY_SERVICE_NAME" == @workspace/* ]]; then
    echo "⛔ خدمة $RAILWAY_SERVICE_NAME — البوت يعمل على charming-strength فقط."
    exit 0
fi

# ── إعداد Python: استخدم النظام إذا كانت المكتبات متوفرة (Railway)، وإلا أنشئ venv (Replit) ──
if python3 -c "import telegram" 2>/dev/null; then
    echo "✅ المكتبات جاهزة في Python النظام"
else
    VENV="/tmp/bot-venv"
    if [ ! -f "$VENV/bin/python3" ]; then
        echo "⚙️  إنشاء بيئة Python افتراضية..."
        python3 -m venv "$VENV" --clear
        "$VENV/bin/pip" install -r requirements.txt -q
        echo "✅ المكتبات جاهزة"
    fi
    export VIRTUAL_ENV="$VENV"
    export PATH="$VENV/bin:$PATH"
fi

LOG_FILE="/tmp/bot_watchdog.log"
RESTART_COUNT=0
ERROR_COUNT=0
BOT_PID=""

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

start_bot() {
    python3 bot.py &
    BOT_PID=$!
    log "▶️  تشغيل البوت — PID=$BOT_PID (محاولة رقم $((RESTART_COUNT + 1)))"
}

log "═══════════════════════════════════════"
log "🚀 Watchdog بدأ المراقبة"
log "═══════════════════════════════════════"

# شغّل Auto-Healer في الخلفية
if [ -f "auto_healer.sh" ]; then
    bash auto_healer.sh &
    log "🛡️  Auto-Healer شغّال في الخلفية (PID=$!)"
fi

start_bot

while true; do
    sleep 1

    # تحقق إذا كان البوت لا يزال شغّالاً
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
        wait "$BOT_PID" 2>/dev/null
        EXIT_CODE=$?
        RESTART_COUNT=$((RESTART_COUNT + 1))
        ERROR_COUNT=$((ERROR_COUNT + 1))

        log "⚠️  البوت توقف — كود الخروج: $EXIT_CODE | إجمالي إعادة التشغيل: $RESTART_COUNT"

        # إذا تعطّل أكثر من 5 مرات في أقل من 30 ثانية → انتظر 10 ثوانٍ منعاً للحلقة اللانهائية
        if [ "$ERROR_COUNT" -ge 5 ]; then
            log "🔴 تعطّلات متكررة ($ERROR_COUNT) — انتظار 10 ثوانٍ قبل إعادة المحاولة..."
            sleep 10
            ERROR_COUNT=0
        fi

        log "🔄 إعادة تشغيل البوت..."
        start_bot
    fi
done
