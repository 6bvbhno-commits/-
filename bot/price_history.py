"""
نظام تتبّع الأسعار الذاتي — يُسجّل سعر كل منتج تلقائياً ويعرض تاريخه كرسم بياني نصي.
المخزن: SQLite محلي (bot/price_data.db)
"""
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── إعداد قاعدة البيانات ────────────────────────────────────────────────────
_DB_PATH  = Path(__file__).parent / "price_data.db"
_DB_LOCK  = threading.Lock()
_MAX_DAYS = 90   # نحتفظ بآخر 90 يوم فقط
_SPARK    = "▁▂▃▄▅▆▇█"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                asin        TEXT    NOT NULL,
                domain      TEXT    NOT NULL,
                price_val   REAL    NOT NULL,
                seller_name TEXT,
                ts          INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_asin_domain ON price_history(asin, domain)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ts ON price_history(ts)"
        )


try:
    _init_db()
except Exception as _init_err:
    logger.error("price_history: فشل إنشاء قاعدة البيانات — %s", _init_err)

# آخر مرة نُنظّف كل قاعدة البيانات (نفعلها مرة كل 24 ساعة فقط)
_last_global_cleanup: float = 0.0


def _global_cleanup() -> None:
    """يحذف كل السجلات الأقدم من 90 يوماً من جميع المنتجات (مرة/24 ساعة)."""
    global _last_global_cleanup
    now = time.time()
    if now - _last_global_cleanup < 86400:
        return
    _last_global_cleanup = now
    cutoff = int(now) - _MAX_DAYS * 86400
    try:
        with _DB_LOCK, _get_conn() as conn:
            deleted = conn.execute(
                "DELETE FROM price_history WHERE ts < ?", (cutoff,)
            ).rowcount
        if deleted:
            logger.info("price_history: حُذف %d سجل قديم", deleted)
    except Exception as e:
        logger.warning("price_history._global_cleanup فشل: %s", e)


# ── التسجيل ─────────────────────────────────────────────────────────────────

def record_price(asin: str, domain: str, price_val: float, seller_name: str = "") -> None:
    """يُسجّل سعراً جديداً — يُجري تنظيفاً شاملاً مرة يومياً."""
    _global_cleanup()
    now    = int(time.time())
    cutoff = now - _MAX_DAYS * 86400
    try:
        with _DB_LOCK, _get_conn() as conn:
            # لا تُسجّل نفس السعر مرتين خلال 30 دقيقة للـ ASIN نفسه
            row = conn.execute(
                "SELECT ts, price_val FROM price_history "
                "WHERE asin=? AND domain=? ORDER BY ts DESC LIMIT 1",
                (asin, domain),
            ).fetchone()
            if row and (now - row[0]) < 1800 and abs(row[1] - price_val) < 0.01:
                return  # تكرار في أقل من 30 دقيقة → تجاهل

            conn.execute(
                "INSERT INTO price_history (asin, domain, price_val, seller_name, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (asin, domain, price_val, seller_name, now),
            )
            # تنظيف سجلات هذا المنتج تحديداً
            conn.execute(
                "DELETE FROM price_history WHERE asin=? AND domain=? AND ts < ?",
                (asin, domain, cutoff),
            )
    except Exception as e:
        logger.warning("price_history.record_price فشل: %s", e)


# ── الجلب والتحليل ──────────────────────────────────────────────────────────

def get_history(asin: str, domain: str, days: int = 30) -> list[dict]:
    """يُعيد سجلات السعر للأيام الماضية مرتبةً من الأقدم للأحدث."""
    cutoff = int(time.time()) - days * 86400
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                "SELECT price_val, seller_name, ts FROM price_history "
                "WHERE asin=? AND domain=? AND ts>=? ORDER BY ts ASC",
                (asin, domain, cutoff),
            ).fetchall()
        return [{"price_val": r[0], "seller_name": r[1], "ts": r[2]} for r in rows]
    except Exception as e:
        logger.warning("price_history.get_history فشل: %s", e)
        return []


def _sparkline(prices: list[float]) -> str:
    """يُنشئ خطاً بيانياً نصياً من قائمة أسعار."""
    if not prices:
        return ""
    lo, hi = min(prices), max(prices)
    span = hi - lo or 1
    # أخذ آخر 30 نقطة كحد أقصى
    sample = prices[-30:]
    bars = []
    for p in sample:
        idx = round((p - lo) / span * 7)
        bars.append(_SPARK[max(0, min(7, idx))])
    return "".join(bars)


def _trend_arrow(prices: list[float]) -> str:
    """📈 / 📉 / ➡️ بحسب الاتجاه بين النصف الأول والثاني."""
    if len(prices) < 4:
        return ""
    mid  = len(prices) // 2
    avg1 = sum(prices[:mid])  / mid
    avg2 = sum(prices[mid:])  / (len(prices) - mid)
    diff = (avg2 - avg1) / avg1 * 100 if avg1 else 0
    if diff > 3:
        return "📈 ارتفع"
    if diff < -3:
        return "📉 انخفض"
    return "➡️ مستقر"


# ── الرسالة الجاهزة ─────────────────────────────────────────────────────────

def format_history_message(asin: str, domain: str) -> str:
    """
    يُعيد قسم تاريخ السعر جاهزاً للإلحاق برسالة تيليجرام.
    يُعيد نصاً فارغاً إذا كانت البيانات غير كافية (أقل من قراءتين).
    """
    records = get_history(asin, domain, days=90)
    if len(records) < 2:
        return ""

    prices = [r["price_val"] for r in records]
    lo     = min(prices)
    hi     = max(prices)
    avg    = sum(prices) / len(prices)
    curr   = prices[-1]
    spark  = _sparkline(prices)
    trend  = _trend_arrow(prices)
    count  = len(records)

    # حساب عدد الأيام منذ أول تسجيل
    days_span = max(1, (records[-1]["ts"] - records[0]["ts"]) // 86400)

    # تمييز السعر الحالي: أدنى سعر؟
    tag = ""
    if abs(curr - lo) < 0.01:
        tag = " 🟢 *أدنى سعر سُجّل — وقت ممتاز للشراء!*"
    elif abs(curr - hi) < 0.01:
        tag = " 🔴 (أعلى سعر)"

    lines = [
        f"📊 *تاريخ السعر — آخر {days_span} يوم ({count} قراءة):*",
        f"`{spark}`",
        f"• الأدنى: `{lo:.2f} SAR` • الأعلى: `{hi:.2f} SAR`",
        f"• المتوسط: `{avg:.2f} SAR`",
    ]
    if trend:
        lines.append(f"• الاتجاه: {trend}{tag}")
    else:
        lines.append(f"• السعر الحالي: `{curr:.2f} SAR`{tag}")

    return "\n".join(lines)
