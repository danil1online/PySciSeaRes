"""Rate limiting для внешних API-запросов.

Использует sliding window алгоритм для ограничения частоты запросов.
Поддерживает несколько независимых лимитеров для разных API.
"""
import time
import threading
from collections import deque


class RateLimiter:
    """Sliding window rate limiter.

    Ограничивает количество запросов за заданный временной window.

    Example:
        limiter = RateLimiter(max_requests=30, window_seconds=60)
        if limiter.allow():
            make_api_request()
        else:
            wait_and_retry()
    """

    def __init__(self, max_requests, window_seconds):
        """
        Args:
            max_requests: Максимум запросов за window
            window_seconds: Размер окна в секундах
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps = deque()
        self._lock = threading.Lock()

    def _cleanup(self):
        """Удаляет старые записи за пределами window."""
        cutoff = time.time() - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def allow(self):
        """Проверяет, можно ли сделать запрос.

        Returns:
            bool: True если запрос разрешён
        """
        with self._lock:
            self._cleanup()
            if len(self._timestamps) < self.max_requests:
                self._timestamps.append(time.time())
                return True
            return False

    def wait_time(self):
        """Возвращает время ожидания до следующего разрешения.

        Returns:
            float: Секунд до следующего разрешения (0 если можно сразу)
        """
        with self._lock:
            self._cleanup()
            if len(self._timestamps) < self.max_requests:
                return 0.0
            # Ждём пока самый старый запрос выйдет из window
            oldest = self._timestamps[0]
            return max(0.0, (oldest + self.window_seconds) - time.time())

    def wait(self):
        """Блокирует до разрешения запроса."""
        while not self.allow():
            wait = self.wait_time()
            if wait > 0:
                time.sleep(wait)


class RateLimiterRegistry:
    """Registry для нескольких rate limiter'ов по именам."""

    def __init__(self):
        self._limiters = {}
        self._lock = threading.Lock()

    def get(self, name, max_requests, window_seconds=60):
        """Получить или создать rate limiter по имени.

        Args:
            name: Уникальное имя (например, "vak_api", "ddg")
            max_requests: Макс запросов за window
            window_seconds: Размер окна в секундах

        Returns:
            RateLimiter
        """
        with self._lock:
            if name not in self._limiters:
                self._limiters[name] = RateLimiter(max_requests, window_seconds)
            return self._limiters[name]

    def get_or_create(self, name):
        """Получить существующий или создать с дефолтными значениями."""
        with self._lock:
            if name not in self._limiters:
                # Default: 30 requests per minute
                self._limiters[name] = RateLimiter(30, 60)
            return self._limiters[name]


# Глобальный registry
_registry = RateLimiterRegistry()


def get_limiter(name, max_requests=30, window_seconds=60):
    """Получить rate limiter по имени.

    Args:
        name: Имя лимитера
        max_requests: Макс запросов за window
        window_seconds: Размер окна в секундах

    Returns:
        RateLimiter
    """
    return _registry.get(name, max_requests, window_seconds)


# Предопределённые лимитеры
VAK_API_LIMITER = get_limiter("vak_api", max_requests=30, window_seconds=60)
DDG_LIMITER = get_limiter("ddg", max_requests=10, window_seconds=60)
SEMANTIC_SCHOLAR_LIMITER = get_limiter("semantic_scholar", max_requests=20, window_seconds=60)
CROSSREF_LIMITER = get_limiter("crossref", max_requests=15, window_seconds=60)
DOI_LIMITER = get_limiter("doi", max_requests=20, window_seconds=60)
