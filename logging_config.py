"""Конфигурация логирования для проекта vak-adverts-list.

Создаёт логгер с.handlers:
  - logs/app.log — все логи приложения
  - logs/sync.log — только логи синхронизации (daily_sync)
  - logs/error.log — только ошибки и критические
  - Console handler — вывод в stderr
"""
import os
import logging
from logging.handlers import RotatingFileHandler

from config import LOG_DIR


def setup_logging(
    app_level=logging.INFO,
    sync_level=logging.INFO,
    max_bytes=10 * 1024 * 1024,  # 10 MB
    backup_count=5,
):
    """Настраивает логирование для всего приложения.

    Вызывается один раз при старте (app.py или daily_sync.py).

    Args:
        app_level: Уровень логирования для основного приложения
        sync_level: Уровень логирования для синхронизации
        max_bytes: Максимальный размер файла лога
        backup_count: Количество ротаций
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    # Форматтер
    detailed_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    simple_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Консольный handler (stderr для всех)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(simple_fmt)

    # Основной логгер приложения
    app_logger = logging.getLogger("vak")
    app_logger.setLevel(app_level)
    app_logger.addHandler(console_handler)

    # Файл: app.log (все логи приложения)
    app_file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    app_file_handler.setLevel(app_level)
    app_file_handler.setFormatter(detailed_fmt)
    app_logger.addHandler(app_file_handler)

    # Файл: error.log (только ошибки)
    error_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "error.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_fmt)
    app_logger.addHandler(error_handler)

    # Синхронизационный логгер (потомок vak.sync)
    sync_logger = logging.getLogger("vak.sync")
    sync_logger.setLevel(sync_level)
    sync_logger.propagate = False

    # Файл: sync.log
    sync_file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "sync.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    sync_file_handler.setLevel(sync_level)
    sync_file_handler.setFormatter(detailed_fmt)
    sync_logger.addHandler(sync_file_handler)

    # Файл: error.log — также для sync ошибок
    sync_error_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "error.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    sync_error_handler.setLevel(logging.ERROR)
    sync_error_handler.setFormatter(detailed_fmt)
    sync_logger.addHandler(sync_error_handler)

    # Консоль для sync — WARNING+
    sync_console = logging.StreamHandler()
    sync_console.setLevel(logging.WARNING)
    sync_console.setFormatter(simple_fmt)
    sync_logger.addHandler(sync_console)

    # Фильтр: только WARNING+ для urllib3 (чтобы не загромождать)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return app_logger, sync_logger


def get_logger(name):
    """Получить логгер по имени.

    Args:
        name: Имя логгера (например, "vak.search", "vak.pdf")

    Returns:
        logging.Logger
    """
    return logging.getLogger(f"vak.{name}")
