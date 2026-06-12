"""Управление памятью для ежедневного синхронизационного скрипта."""
import gc
import psutil
import os

# Настройки: 16 ГБ RAM, оставляем 2 ГБ на систему
MAX_MEMORY_MB = 12 * 1024  # 12 ГБ — мягкий лимит
CRITICAL_MEMORY_MB = 14 * 1024  # 14 ГБ — критический, аварийная остановка
BATCH_SIZE = 10  # обработать N объявлений и сбросить память
gc_threshold = 0


def get_memory_mb():
    """Возвращает потребление памяти процесса в МБ."""
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 * 1024)


def check_memory():
    """Проверяет потребление памяти. При превышении — сброс GC."""
    mem = get_memory_mb()
    if mem >= CRITICAL_MEMORY_MB:
        print(f"\n  !!! КРИТИЧЕСКАЯ ПАМЯТЬ: {mem:.0f} МБ, аварийная остановка !!!")
        import sys
        sys.exit(1)
    if mem >= MAX_MEMORY_MB:
        print(f"\n  Ограничение памяти: {mem:.0f} МБ, сброс GC...")
        force_gc()


def force_gc():
    """Принудительный сбор мусора + очистка кэша SQLite."""
    global gc_threshold
    before = gc.get_count()[0]
    gc.collect()
    gc.collect()
    gc.collect()
    after = gc.get_count()[0]
    print(f"    GC: {before} -> {after} объектов в gen0")
