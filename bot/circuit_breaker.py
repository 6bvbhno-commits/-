"""
قاطع دائرة بسيط — يحمي SerpAPI وخدمات خارجية من الانهيار المتسلسل عند 429/5xx.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int = 5,
        cooldown_sec: float = 60.0,
    ) -> None:
        self.name = name
        self.fail_threshold = max(1, fail_threshold)
        self.cooldown_sec = max(5.0, cooldown_sec)
        self._fails = 0
        self._opened_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now < self._opened_until:
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._opened_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.fail_threshold:
                self._opened_until = time.monotonic() + self.cooldown_sec
                logger.warning(
                    "⚡ circuit OPEN [%s] fails=%d cooldown=%.0fs",
                    self.name,
                    self._fails,
                    self.cooldown_sec,
                )
                self._fails = 0

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            open_left = max(0.0, self._opened_until - now)
            return {
                "name": self.name,
                "open": open_left > 0,
                "cooldown_left": round(open_left, 1),
                "fails": self._fails,
            }
