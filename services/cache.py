"""services/cache.py — Потокобезопасный TTL / LRU кэш для защиты от утечек памяти.

Используется для:
- _business_phone_cache (кеш бизнес-номеров Zernio)
- _notification_throttle (троттлинг уведомлений владельцу)
- _rate_limit_history (скользящее окно rate limiting медиа)
"""

import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Кэш с ограничением максимального размера (LRU) и времени жизни (TTL)."""

    def __init__(self, maxsize: int = 1000, ttl: float = 300.0) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be > 0")
        self.maxsize = maxsize
        self.ttl = float(ttl)
        self._data: OrderedDict[Any, tuple[T, float]] = OrderedDict()
        self._lock = Lock()

    def _evict_expired(self, now: float) -> None:
        """Удаляет протухшие записи с начала очереди."""
        expired_keys = [k for k, (_, expire_at) in self._data.items() if expire_at <= now]
        for k in expired_keys:
            self._data.pop(k, None)

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            now = time.monotonic()
            if key not in self._data:
                return default
            val, expire_at = self._data[key]
            if expire_at <= now:
                self._data.pop(key, None)
                return default
            self._data.move_to_end(key)
            return val

    def set(self, key: Any, value: T, ttl: float | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            item_ttl = float(ttl) if ttl is not None else self.ttl
            expire_at = now + item_ttl

            if key in self._data:
                self._data[key] = (value, expire_at)
                self._data.move_to_end(key)
                return

            self._evict_expired(now)

            while len(self._data) >= self.maxsize:
                self._data.popitem(last=False)

            self._data[key] = (value, expire_at)

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            now = time.monotonic()
            item = self._data.pop(key, None)
            if item is None:
                return default
            val, expire_at = item
            if expire_at <= now:
                return default
            return val

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            return len(self._data)

    def __getitem__(self, key: Any) -> T:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: Any, value: T) -> None:
        self.set(key, value)
