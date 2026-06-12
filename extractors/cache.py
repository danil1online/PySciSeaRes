"""Кэширование LLM-ответов.

Ключ кэша: hash(system_prompt + user_prompt_text).
Хранение: JSON-файл в LLM_RESPONSES_DIR/cache/.
Срок хранения: 7 дней (конфигурируется).
"""
import json
import hashlib
import os
import time
from datetime import datetime, timedelta

from config import LLM_RESPONSES_DIR

CACHE_DIR = os.path.join(LLM_RESPONSES_DIR, "cache")
CACHE_TTL_DAYS = 7
CACHE_ENABLED = os.environ.get("VAK_CACHE_ENABLED", "1") == "1"


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_key(system_prompt, user_text):
    """Генерирует ключ кэша из system prompt и user text."""
    raw = f"{system_prompt}\n---\n{user_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_path(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def get_cached(system_prompt, user_text):
    """Получить кэшированный ответ LLM.

    Возвращает (content, elapsed_time, from_cache) или (None, None, False).
    """
    if not CACHE_ENABLED:
        return None, None, False
    _ensure_cache_dir()
    key = _cache_key(system_prompt, user_text)
    path = _cache_path(key)

    if not os.path.exists(path):
        return None, None, False

    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)

        # Проверка TTL
        cached_at = datetime.fromisoformat(cached["timestamp"])
        if datetime.now() - cached_at > timedelta(days=CACHE_TTL_DAYS):
            os.remove(path)
            return None, None, False

        return cached["content"], cached["elapsed"], True
    except (json.JSONDecodeError, KeyError, OSError):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return None, None, False


def cache_result(system_prompt, user_text, content, elapsed):
    """Сохранить ответ LLM в кэш."""
    if not CACHE_ENABLED:
        return
    _ensure_cache_dir()
    key = _cache_key(system_prompt, user_text)
    path = _cache_path(key)

    data = {
        "timestamp": datetime.now().isoformat(),
        "elapsed": elapsed,
        "content": content,
    }

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def cleanup_old_cache():
    """Удалить просроченные записи кэша."""
    _ensure_cache_dir()
    cutoff = datetime.now() - timedelta(days=CACHE_TTL_DAYS)

    for filename in os.listdir(CACHE_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached_at = datetime.fromisoformat(cached["timestamp"])
            if cached_at < cutoff:
                os.remove(path)
        except (json.JSONDecodeError, OSError):
            pass


def clear_cache():
    """Очистить весь кэш (для тестов)."""
    _ensure_cache_dir()
    for filename in os.listdir(CACHE_DIR):
        if filename.endswith(".json"):
            try:
                os.remove(os.path.join(CACHE_DIR, filename))
            except OSError:
                pass
