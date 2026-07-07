#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  Auto-Healer — يراقب البوت ويعالج المشاكل تلقائياً
# ══════════════════════════════════════════════════════════════════
HEAL_LOG="/tmp/auto_healer.log"
ITERATION=0
ERROR_WINDOW=0
WINDOW_START=$(date +%s)

log() {
    local msg="[$(date '+%H:%M:%S')] HEALER: $1"
    echo "$msg"
    echo "$msg" >> "$HEAL_LOG"
}

latest_log() {
    ls -t /tmp/logs/Telegram_Bot_*.log 2>/dev/null | head -1
}

count_errors() {
    local f
    f=$(latest_log)
    [ -z "$f" ] && echo "0" && return
    local n
    n=$(tail -30 "$f" 2>/dev/null | grep -ciE "ERROR|exception|Traceback|فشل نهائي|timeout" 2>/dev/null)
    echo "${n:-0}"
}

last_error() {
    local f
    f=$(latest_log)
    [ -z "$f" ] && echo "" && return
    tail -30 "$f" 2>/dev/null | grep -iE "ERROR|exception|Traceback" | tail -1
}

is_bot_alive() {
    pgrep -f "python3 bot.py" > /dev/null 2>&1
}

log "═══════════════════════════════════════"
log "🛡️  Auto-Healer بدأ المراقبة"
log "═══════════════════════════════════════"

while true; do
    sleep 1
    ITERATION=$((ITERATION + 1))

    # ── تحقق من حياة البوت ──────────────────────────────────────
    if ! is_bot_alive; then
        log "⚠️  البوت غير نشط — watchdog سيعيد تشغيله"
        sleep 3
        continue
    fi

    # ── فحص أخطاء كل 10 ثوانٍ ──────────────────────────────────
    if [ $((ITERATION % 10)) -eq 0 ]; then
        NOW=$(date +%s)
        ERRORS=$(count_errors)
        ERRORS=$((ERRORS + 0))   # تأكيد رقمي

        # إعادة ضبط النافذة كل 60 ثانية
        ELAPSED=$((NOW - WINDOW_START))
        if [ "$ELAPSED" -ge 60 ]; then
            [ "$ERROR_WINDOW" -gt 0 ] && log "📊 نافذة 60s: $ERROR_WINDOW خطأ"
            ERROR_WINDOW=0
            WINDOW_START=$NOW
        fi
        ERROR_WINDOW=$((ERROR_WINDOW + ERRORS))

        if [ "$ERRORS" -gt 5 ]; then
            LAST_ERR=$(last_error)
            log "🔴 أخطاء كثيرة ($ERRORS/30سطر): $LAST_ERR"
        fi

        if [ "$ERROR_WINDOW" -gt 20 ]; then
            log "🚨 تحذير: $ERROR_WINDOW خطأ في 60 ثانية!"
            ERROR_WINDOW=0
        fi
    fi

    # ── إحصاء كل 5 دقائق ────────────────────────────────────────
    if [ $((ITERATION % 300)) -eq 0 ]; then
        BOT_PID=$(pgrep -f "python3 bot.py" | head -1)
        if [ -n "$BOT_PID" ]; then
            MEM=$(ps -o rss= -p "$BOT_PID" 2>/dev/null | awk '{print $1+0}')
            MEM_MB=$((MEM / 1024))
            log "💚 حي | PID=$BOT_PID | RAM=${MEM_MB}MB | iter=$ITERATION"
            if [ "$MEM_MB" -gt 400 ]; then
                log "⚠️  ذاكرة عالية ${MEM_MB}MB"
            fi
        fi
    fi
done
