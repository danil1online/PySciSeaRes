"""Управление памятью для ежедневного синхронизационного скрипта."""
import gc
import psutil
import os

from config import MAX_MEMORY_MB, CRITICAL_MEMORY_MB, BATCH_SIZE
from logging_config import get_logger

logger = get_logger("memory")

gc_threshold = 0


def get_memory_mb():
    """Возвращает потребление памяти процесса в МБ."""
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 * 1024)


def check_memory():
    """Проверяет потребление памяти. При превышении — сброс GC."""
    mem = get_memory_mb()
    if mem >= CRITICAL_MEMORY_MB:
        logger.critical(f"CRITICAL MEMORY: {mem:.0f} MB, exiting...")
        import sys
        sys.exit(1)
    if mem >= MAX_MEMORY_MB:
        logger.warning(f"Memory limit: {mem:.0f} MB, GC...")
        force_gc()


def force_gc():
    """Принудительный сбор мусора + очистка кэша SQLite."""
    global gc_threshold
    before = gc.get_count()[0]
    gc.collect()
    gc.collect()
    gc.collect()
    after = gc.get_count()[0]
    logger.debug(f"GC: {before} -> {after} objects in gen0")
