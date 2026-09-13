"""
تتبع العروض الرائجة — ذاكرة + SQLite حتى تبقى /deals بعد إعادة النشر.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_HOT: OrderedDict[str, dict] = OrderedDict()
_MAX = 40
_DB_PATH = Path(__file__).parent / "price_data.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hot_deals (
                asin       TEXT PRIMARY KEY,
                title      TEXT,
                price      TEXT,
                price_val  REAL,
                domain     TEXT,
                image      TEXT,
                hits       INTEGER NOT NULL DEFAULT 1,
                ts         REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hot_deals_hits ON hot_deals(hits DESC, ts DESC)"
        )


def _row_to_meta(row: sqlite3.Row | tuple) -> dict:
    if isinstance(row, sqlite3.Row):
        return {
            "asin": row["asin"],
            "title": row["title"] or "",
            "price": row["price"] or "",
            "price_val": row["price_val"],
            "domain": row["domain"] or "amazon.sa",
            "image": row["image"] or "",
            "hits": int(row["hits"] or 1),
            "ts": float(row["ts"] or 0),
        }
    return {
        "asin": row[0],
        "title": row[1] or "",
        "price": row[2] or "",
        "price_val": row[3],
        "domain": row[4] or "amazon.sa",
        "image": row[5] or "",
        "hits": int(row[6] or 1),
        "ts": float(row[7] or 0),
    }


def _load_from_db() -> None:
    try:
        with _get_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT asin, title, price, price_val, domain, image, hits, ts "
                "FROM hot_deals ORDER BY hits ASC, ts ASC"
            ).fetchall()
        with _lock:
            _HOT.clear()
            for row in rows:
                meta = _row_to_meta(row)
                asin = (meta.get("asin") or "").upper()
                if asin and len(asin) == 10:
                    _HOT[asin] = meta
            while len(_HOT) > _MAX:
                _HOT.popitem(last=False)
        if rows:
            logger.info("deals_tracker: حُمّل %d عرض من SQLite", len(_HOT))
    except Exception as e:
        logger.warning("deals_tracker._load_from_db: %s", e)


def _persist(asin: str, meta: dict) -> None:
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO hot_deals (asin, title, price, price_val, domain, image, hits, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    title=excluded.title,
                    price=excluded.price,
                    price_val=excluded.price_val,
                    domain=excluded.domain,
                    image=excluded.image,
                    hits=excluded.hits,
                    ts=excluded.ts
                """,
                (
                    asin,
                    meta.get("title") or "",
                    meta.get("price") or "",
                    meta.get("price_val"),
                    meta.get("domain") or "amazon.sa",
                    meta.get("image") or "",
                    int(meta.get("hits") or 1),
                    float(meta.get("ts") or time.time()),
                ),
            )
            # حافظ على أقصى _MAX صفاً
            extras = conn.execute(
                "SELECT asin FROM hot_deals ORDER BY hits ASC, ts ASC"
            ).fetchall()
            if len(extras) > _MAX:
                drop = [r[0] for r in extras[: len(extras) - _MAX]]
                conn.executemany("DELETE FROM hot_deals WHERE asin=?", [(a,) for a in drop])
    except Exception as e:
        logger.warning("deals_tracker._persist: %s", e)


try:
    _init_db()
    _load_from_db()
except Exception as _init_err:
    logger.error("deals_tracker: فشل التهيئة — %s", _init_err)


def record_deal(
    *,
    asin: str,
    title: str,
    price: str = "",
    price_val: float | None = None,
    domain: str = "amazon.sa",
    image: str = "",
) -> None:
    asin = (asin or "").upper().strip()
    if not asin or len(asin) != 10:
        return
    with _lock:
        prev = _HOT.get(asin) or {}
        meta = {
            "asin": asin,
            "title": (title or prev.get("title") or "")[:90],
            "price": (price or prev.get("price") or "")[:40],
            "price_val": price_val if price_val is not None else prev.get("price_val"),
            "domain": domain or prev.get("domain") or "amazon.sa",
            "image": image or prev.get("image") or "",
            "ts": time.time(),
            "hits": int(prev.get("hits", 0)) + 1,
        }
        _HOT[asin] = meta
        _HOT.move_to_end(asin)
        while len(_HOT) > _MAX:
            _HOT.popitem(last=False)
    _persist(asin, meta)


def top_deals(limit: int = 8) -> list[dict]:
    with _lock:
        items = list(_HOT.values())
    items.sort(key=lambda x: (x.get("hits", 0), x.get("ts", 0)), reverse=True)
    return items[:limit]
