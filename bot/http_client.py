"""
عميل HTTP مشترك — إعادة استخدام الاتصالات (Keep-Alive) بدل فتح جلسة لكل طلب.
"""
from __future__ import annotations

import logging
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_session: requests.Session | None = None


def get_http_session() -> requests.Session:
    """جلسة requests واحدة مشتركة مع pool وretries خفيفة."""
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        s = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            backoff_factor=0.4,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; PriceBot/6.0; +https://t.me/)"
            ),
            "Accept-Encoding": "gzip, deflate, br",
        })
        _session = s
        logger.info("🌐 HTTP session pool جاهز (32/64)")
        return _session


def close_http_session() -> None:
    global _session
    with _lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
            _session = None
