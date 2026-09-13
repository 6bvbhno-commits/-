"""
سجل مستخدمي البوت — لكل من يتفاعل يُحفظ chat_id لإرسال التنبيهات الجماعية.
نفس قاعدة SQLite: bot/price_data.db
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "price_data.db"
_DB_LOCK = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                chat_id     INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                first_name  TEXT,
                last_seen   INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                blocked     INTEGER DEFAULT 0,
                opt_out     INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_users_active "
            "ON bot_users(blocked, opt_out, last_seen)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  INTEGER NOT NULL,
                kind        TEXT,
                code        TEXT,
                message     TEXT,
                scheduled_for INTEGER,
                status      TEXT,
                sent_ok     INTEGER DEFAULT 0,
                sent_fail   INTEGER DEFAULT 0,
                total       INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wishlist (
                user_id     INTEGER NOT NULL,
                asin        TEXT    NOT NULL,
                domain      TEXT    NOT NULL DEFAULT 'amazon.sa',
                title       TEXT,
                price       TEXT,
                price_val   REAL,
                added_at    INTEGER NOT NULL,
                PRIMARY KEY (user_id, asin)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wishlist_user ON wishlist(user_id, added_at DESC)"
        )


try:
    _init()
except Exception as e:
    logger.error("users_db init فشل: %s", e)


def upsert_user(
    chat_id: int,
    user_id: int,
    *,
    username: str = "",
    first_name: str = "",
) -> None:
    """يسجّل أو يحدّث مستخدماً عند أي تفاعل."""
    if not chat_id or not user_id:
        return
    now = int(time.time())
    try:
        with _DB_LOCK, _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO bot_users
                    (chat_id, user_id, username, first_name, last_seen, created_at, blocked, opt_out)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    username=COALESCE(NULLIF(excluded.username,''), bot_users.username),
                    first_name=COALESCE(NULLIF(excluded.first_name,''), bot_users.first_name),
                    last_seen=excluded.last_seen,
                    blocked=0
                """,
                (chat_id, user_id, (username or "")[:64], (first_name or "")[:64], now, now),
            )
    except Exception as e:
        logger.warning("users_db.upsert_user: %s", e)


def mark_blocked(chat_id: int) -> None:
    try:
        with _DB_LOCK, _get_conn() as conn:
            conn.execute("UPDATE bot_users SET blocked=1 WHERE chat_id=?", (chat_id,))
    except Exception as e:
        logger.warning("users_db.mark_blocked: %s", e)


def set_opt_out(chat_id: int, opt_out: bool = True) -> None:
    try:
        with _DB_LOCK, _get_conn() as conn:
            conn.execute(
                "UPDATE bot_users SET opt_out=? WHERE chat_id=?",
                (1 if opt_out else 0, chat_id),
            )
    except Exception as e:
        logger.warning("users_db.set_opt_out: %s", e)


def count_users() -> dict:
    """إحصاءات سريعة للداشبورد."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM bot_users WHERE blocked=0 AND opt_out=0"
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM bot_users WHERE blocked=1"
            ).fetchone()[0]
        return {"total": total, "active": active, "blocked": blocked}
    except Exception as e:
        logger.warning("users_db.count_users: %s", e)
        return {"total": 0, "active": 0, "blocked": 0}


def get_broadcast_chat_ids(*, after_chat_id: int = 0) -> list[int]:
    """كل chat_id مؤهّل — مع إمكانية الاستئناف بعد after_chat_id."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            if after_chat_id:
                rows = conn.execute(
                    "SELECT chat_id FROM bot_users "
                    "WHERE blocked=0 AND opt_out=0 AND chat_id>? ORDER BY chat_id",
                    (after_chat_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT chat_id FROM bot_users WHERE blocked=0 AND opt_out=0 ORDER BY chat_id"
                ).fetchall()
        return [int(r[0]) for r in rows]
    except Exception as e:
        logger.warning("users_db.get_broadcast_chat_ids: %s", e)
        return []


def import_from_price_alerts() -> int:
    """يستورد chat_id من تنبيهات الأسعار القديمة."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT chat_id, user_id FROM price_alerts
                WHERE active=1 AND chat_id IS NOT NULL
                """
            ).fetchall()
            now = int(time.time())
            n = 0
            for chat_id, user_id in rows:
                conn.execute(
                    """
                    INSERT INTO bot_users
                        (chat_id, user_id, username, first_name, last_seen, created_at, blocked, opt_out)
                    VALUES (?, ?, '', '', ?, ?, 0, 0)
                    ON CONFLICT(chat_id) DO NOTHING
                    """,
                    (int(chat_id), int(user_id or chat_id), now, now),
                )
                n += 1
        return n
    except Exception as e:
        logger.warning("users_db.import_from_price_alerts: %s", e)
        return 0


def log_broadcast(
    *,
    kind: str,
    code: str,
    message: str,
    scheduled_for: int | None,
    status: str,
    sent_ok: int = 0,
    sent_fail: int = 0,
    total: int = 0,
) -> int:
    try:
        with _DB_LOCK, _get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO broadcast_log
                    (created_at, kind, code, message, scheduled_for, status, sent_ok, sent_fail, total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    kind,
                    (code or "")[:64],
                    (message or "")[:4000],
                    scheduled_for,
                    status,
                    sent_ok,
                    sent_fail,
                    total,
                ),
            )
            return int(cur.lastrowid or 0)
    except Exception as e:
        logger.warning("users_db.log_broadcast: %s", e)
        return 0


def update_broadcast_log(
    log_id: int,
    *,
    status: str,
    sent_ok: int = 0,
    sent_fail: int = 0,
    total: int = 0,
) -> None:
    try:
        with _DB_LOCK, _get_conn() as conn:
            conn.execute(
                """
                UPDATE broadcast_log
                SET status=?, sent_ok=?, sent_fail=?, total=?
                WHERE id=?
                """,
                (status, sent_ok, sent_fail, total, log_id),
            )
    except Exception as e:
        logger.warning("users_db.update_broadcast_log: %s", e)


def set_broadcast_checkpoint(log_id: int, last_chat_id: int, sent_ok: int, sent_fail: int) -> None:
    """يحفظ نقطة استئناف للإرسال الجماعي."""
    try:
        with _DB_LOCK, _get_conn() as conn:
            # عمود اختياري — نضيفه إن لم يوجد
            cols = {r[1] for r in conn.execute("PRAGMA table_info(broadcast_log)").fetchall()}
            if "last_chat_id" not in cols:
                conn.execute("ALTER TABLE broadcast_log ADD COLUMN last_chat_id INTEGER DEFAULT 0")
            conn.execute(
                "UPDATE broadcast_log SET last_chat_id=?, sent_ok=?, sent_fail=?, status=? WHERE id=?",
                (last_chat_id, sent_ok, sent_fail, "sending", log_id),
            )
    except Exception as e:
        logger.warning("users_db.set_broadcast_checkpoint: %s", e)


def recent_broadcasts(limit: int = 10) -> list[dict]:
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, kind, code, status, sent_ok, sent_fail, total, scheduled_for
                FROM broadcast_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "kind": r[2],
                "code": r[3],
                "status": r[4],
                "sent_ok": r[5],
                "sent_fail": r[6],
                "total": r[7],
                "scheduled_for": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("users_db.recent_broadcasts: %s", e)
        return []


def _ensure_wishlist() -> None:
    try:
        with _DB_LOCK, _get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wishlist (
                    user_id     INTEGER NOT NULL,
                    asin        TEXT    NOT NULL,
                    domain      TEXT    NOT NULL DEFAULT 'amazon.sa',
                    title       TEXT,
                    price       TEXT,
                    price_val   REAL,
                    added_at    INTEGER NOT NULL,
                    PRIMARY KEY (user_id, asin)
                )
                """
            )
    except Exception as e:
        logger.warning("users_db._ensure_wishlist: %s", e)


def add_favorite(
    user_id: int,
    asin: str,
    *,
    domain: str = "amazon.sa",
    title: str = "",
    price: str = "",
    price_val: float | None = None,
) -> str:
    """يضيف للمفضلة. يرجع: added | updated | limit | error"""
    _ensure_wishlist()
    asin = (asin or "").upper().strip()
    if not user_id or not asin:
        return "error"
    try:
        with _DB_LOCK, _get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM wishlist WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            exists = conn.execute(
                "SELECT 1 FROM wishlist WHERE user_id=? AND asin=?",
                (user_id, asin),
            ).fetchone()
            if not exists and count >= 20:
                return "limit"
            conn.execute(
                """
                INSERT INTO wishlist (user_id, asin, domain, title, price, price_val, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, asin) DO UPDATE SET
                    title=excluded.title,
                    price=excluded.price,
                    price_val=excluded.price_val,
                    domain=excluded.domain,
                    added_at=excluded.added_at
                """,
                (
                    user_id,
                    asin,
                    domain or "amazon.sa",
                    (title or "")[:120],
                    (price or "")[:40],
                    price_val,
                    int(time.time()),
                ),
            )
        return "updated" if exists else "added"
    except Exception as e:
        logger.warning("users_db.add_favorite: %s", e)
        return "error"


def remove_favorite(user_id: int, asin: str) -> bool:
    _ensure_wishlist()
    try:
        with _DB_LOCK, _get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM wishlist WHERE user_id=? AND asin=?",
                (user_id, (asin or "").upper().strip()),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.warning("users_db.remove_favorite: %s", e)
        return False


def list_favorites(user_id: int) -> list[dict]:
    _ensure_wishlist()
    try:
        with _DB_LOCK, _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT asin, domain, title, price, price_val, added_at
                FROM wishlist WHERE user_id=? ORDER BY added_at DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
        return [
            {
                "asin": r[0],
                "domain": r[1],
                "title": r[2],
                "price": r[3],
                "price_val": r[4],
                "added_at": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("users_db.list_favorites: %s", e)
        return []
