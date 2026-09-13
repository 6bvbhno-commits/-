"""
تتبع العروض الرائجة داخل العملية — يغذّي أمر /deals بدون بنية تحتية إضافية.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

_lock = threading.Lock()
# ASIN -> meta (الأحدث أولاً عبر move_to_end)
_HOT: OrderedDict[str, dict] = OrderedDict()
_MAX = 40


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
        _HOT[asin] = {
            "asin": asin,
            "title": (title or "")[:90],
            "price": (price or "")[:40],
            "price_val": price_val,
            "domain": domain,
            "image": image or "",
            "ts": time.time(),
            "hits": (_HOT.get(asin) or {}).get("hits", 0) + 1,
        }
        _HOT.move_to_end(asin)
        while len(_HOT) > _MAX:
            _HOT.popitem(last=False)


def top_deals(limit: int = 8) -> list[dict]:
    with _lock:
        items = list(_HOT.values())
    # رتّب: الأكثر ضربات ثم الأحدث
    items.sort(key=lambda x: (x.get("hits", 0), x.get("ts", 0)), reverse=True)
    return items[:limit]
