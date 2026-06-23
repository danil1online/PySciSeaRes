"""Кэширование LLM-ответов."""
import json
import hashlib
import os
import time
from datetime import datetime, timedelta

from config import LLM_RESPONSES_DIR, CACHE_TTL_DAYS, VAK_CACHE_ENABLED
from logging_config import get_logger

logger = get_logger("cache")

CACHE_DIR = os.path.join(LLM_RESPONSES_DIR, "cache")


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_key(system_prompt, user_text):
    """Генерирует ключ кэша из system prompt и user text."""
    raw = f"{system_prompt}\n---\n{user_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_path(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def get_cached(system_prompt, user_text):
    """Получить кэшированный ответ LLM."""
    if not VAK_CACHE_ENABLED:
        return None, None, False
    _ensure_cache_dir()
    key = _cache_key(system_prompt, user_text)
    path = _cache_path(key)

    if not os.path.exists(path):
        return None, None, False

    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)

        cached_at = datetime.fromisoformat(cached["timestamp"])
        if datetime.now() - cached_at > timedelta(days=CACHE_TTL_DAYS):
            os.remove(path)
            return None, None, False

        logger.debug(f"Cache hit for key {key[:8]}... ({cached['elapsed']:.0f}s)")
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
    if not VAK_CACHE_ENABLED:
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
        logger.debug(f"Cache saved for key {key[:8]}... ({elapsed:.0f}s)")
    except OSError:
        logger.error(f"Failed to save cache for key {key[:8]}...")


def cleanup_old_cache():
    """Удалить просроченные записи кэша."""
    _ensure_cache_dir()
    cutoff = datetime.now() - timedelta(days=CACHE_TTL_DAYS)
    removed = 0
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
                removed += 1
        except (json.JSONDecodeError, OSError):
            pass
    if removed:
        logger.info(f"Cleaned up {removed} expired cache files")


def clear_cache():
    """Очистить весь кэш (для тестов)."""
    _ensure_cache_dir()
    count = 0
    for filename in os.listdir(CACHE_DIR):
        if filename.endswith(".json"):
            try:
                os.remove(os.path.join(CACHE_DIR, filename))
                count += 1
            except OSError:
                pass
    if count:
        logger.info(f"Cleared {count} cache files")
