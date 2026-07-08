"""
نظام تنبيهات انخفاض الأسعار — يُخطر المستخدم فور انخفاض سعر منتجه المتابَع.
المخزن: SQLite محلي (bot/price_data.db) — نفس قاعدة price_history.
"""
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH              = Path(__file__).parent / "price_data.db"
_DB_LOCK              = threading.Lock()
MAX_ALERTS_PER_USER   = 10          # الحد الأقصى لكل مستخدم
_MIN_DROP_PCT         = 0.5         # نسبة الانخفاض الدنيا للتنبيه (0.5%)


# ── إعداد الجدول ─────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_alerts_table() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                asin         TEXT    NOT NULL,
                domain       TEXT    NOT NULL DEFAULT 'amazon.sa',
                product_name TEXT,
                target_price REAL    NOT NULL,
                last_known   REAL    NOT NULL,
                created_at   INTEGER NOT NULL,
                notified_at  INTEGER DEFAULT 0,
                active       INTEGER DEFAULT 1
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_active ON price_alerts(active)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_user ON price_alerts(user_id)"
        )


try:
    _init_alerts_table()
except Exception as _e:
    logger.error("price_alerts: فشل إنشاء الجدول — %s", _e)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def count_user_alerts(user_id: int) -> int:
    """عدد التنبيهات النشطة للمستخدم."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM price_alerts WHERE user_id=? AND active=1",
                (user_id,)
            ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning("price_alerts.count_user_alerts: %s", e)
        return 0


def add_alert(
    user_id: int,
    chat_id: int,
    asin: str,
    domain: str,
    product_name: str,
    current_price: float,
) -> str:
    """
    يضيف تنبيهاً جديداً أو يحدّث موجوداً.
    الفحص والإدراج يتمّان داخل نفس القفل لتجنب race condition.
    يُعيد: 'added' | 'updated' | 'limit_reached' | 'error'
    """
    try:
        with _DB_LOCK, _get_conn() as conn:
            # فحص الحد + الإدراج في معاملة واحدة مقفلة
            count_row = conn.execute(
                "SELECT COUNT(*) FROM price_alerts WHERE user_id=? AND active=1",
                (user_id,),
            ).fetchone()
            current_count = count_row[0] if count_row else 0

            existing = conn.execute(
                "SELECT id FROM price_alerts WHERE user_id=? AND asin=? AND active=1",
                (user_id, asin),
            ).fetchone()

            if existing:
                # حدّث التنبيه القائم
                conn.execute(
                    "UPDATE price_alerts SET last_known=?, target_price=?, product_name=? WHERE id=?",
                    (current_price, current_price, (product_name or "")[:100], existing[0]),
                )
                return "updated"

            if current_count >= MAX_ALERTS_PER_USER:
                return "limit_reached"

            conn.execute(
                """INSERT INTO price_alerts
                   (user_id, chat_id, asin, domain, product_name,
                    target_price, last_known, created_at, active)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (
                    user_id, chat_id, asin, domain,
                    (product_name or "")[:100],
                    current_price, current_price, int(time.time()),
                ),
            )
        return "added"
    except Exception as e:
        logger.warning("price_alerts.add_alert: %s", e)
        return "error"


def get_user_alerts(user_id: int) -> list[dict]:
    """تنبيهات المستخدم النشطة مرتّبة من الأحدث."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                """SELECT id, asin, domain, product_name, target_price, last_known, created_at
                   FROM price_alerts
                   WHERE user_id=? AND active=1
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r[0], "asin": r[1], "domain": r[2],
                "product_name": r[3], "target_price": r[4],
                "last_known": r[5], "created_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("price_alerts.get_user_alerts: %s", e)
        return []


def delete_alert(alert_id: int, user_id: int) -> bool:
    """يُلغّي تنبيهاً — مع التحقق من ملكية المستخدم."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            changed = conn.execute(
                "UPDATE price_alerts SET active=0 WHERE id=? AND user_id=?",
                (alert_id, user_id),
            ).rowcount
        return changed > 0
    except Exception as e:
        logger.warning("price_alerts.delete_alert: %s", e)
        return False


def get_all_active() -> list[dict]:
    """كل التنبيهات النشطة — تُستخدم في حلقة الفحص الخلفية."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                """SELECT id, user_id, chat_id, asin, domain,
                          product_name, target_price, last_known
                   FROM price_alerts WHERE active=1"""
            ).fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "chat_id": r[2],
                "asin": r[3], "domain": r[4], "product_name": r[5],
                "target_price": r[6], "last_known": r[7],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("price_alerts.get_all_active: %s", e)
        return []


def update_last_price(alert_id: int, new_price: float, notified: bool = False) -> None:
    """يحدّث آخر سعر معروف — وعند التنبيه يُحدّث السعر المستهدف أيضاً."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            if notified:
                conn.execute(
                    """UPDATE price_alerts
                       SET last_known=?, target_price=?, notified_at=?
                       WHERE id=?""",
                    (new_price, new_price, int(time.time()), alert_id),
                )
            else:
                conn.execute(
                    "UPDATE price_alerts SET last_known=? WHERE id=?",
                    (new_price, alert_id),
                )
    except Exception as e:
        logger.warning("price_alerts.update_last_price: %s", e)


def check_drop(new_price: float, last_known: float) -> bool:
    """هل انخفض السعر بشكل حقيقي (> MIN_DROP_PCT)؟"""
    if last_known <= 0:
        return False
    return new_price < last_known * (1 - _MIN_DROP_PCT / 100)
