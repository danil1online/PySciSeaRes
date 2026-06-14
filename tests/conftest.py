#!/usr/bin/env python3
"""Pytest fixtures для тестирования."""
import os
import sys
import shutil
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AUTOREFS_DIR, DB_PATH, BASE_DIR

# Disable LLM cache during tests to avoid stale cached responses
os.environ["VAK_CACHE_ENABLED"] = "0"


@pytest.fixture
def temp_db():
    """Создаёт временную БД с таблицами."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "vak.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS adverts (
            id TEXT PRIMARY KEY,
            old_id INTEGER,
            date_defend TEXT,
            fio TEXT,
            dissertation_name TEXT,
            specialty_cipher TEXT,
            specialty_text TEXT,
            supervisor_name TEXT,
            supervisor_work TEXT,
            council_cipher TEXT,
            defend_org TEXT,
            org_address TEXT,
            org_phone TEXT,
            autoref_url TEXT,
            autoref_path TEXT,
            autoref_pdf_url TEXT,
            downloaded INTEGER DEFAULT 0,
            email_search_attempts INTEGER DEFAULT 0,
            pub_extract_attempts INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advert_id TEXT NOT NULL,
            pub_number INTEGER NOT NULL,
            authors TEXT,
            title TEXT,
            journal TEXT,
            year INTEGER,
            pages TEXT,
            email TEXT,
            source_url TEXT,
            source_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (advert_id) REFERENCES adverts(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        yield conn
        conn.close()


@pytest.fixture
def sample_advert():
    """Пример объявления."""
    return {
        "id": "test-id-123",
        "fio": "Иванов Иван Иванович",
        "date_defend": "2026-06-04",
    }


@pytest.fixture
def sample_detail():
    """Пример детализации объявления."""
    return {
        "id": "test-id-123",
        "old_id": 12345,
        "fio": "Иванов Иван Иванович",
        "date_defend": "2026-06-04",
        "dissertation_name": "Тестовая диссертация",
        "council_cipher": "Д 123.456.78",
        "defend_org": "Тестовый университет",
        "org_address": "г. Москва",
        "org_phone": "+7 123 456 78 90",
        "autoref_site": "https://example.com/autoref.pdf",
    }


@pytest.fixture
def empty_advert():
    """Пустое объявление."""
    return {}


@pytest.fixture
def sample_spec_lines():
    """Примеры строк sci_spec.txt."""
    return [
        "1.2.1 - Искусственный интеллект",
        "2.2.11 — Информатика",
        "3.3.3",
    ]


@pytest.fixture
def sample_pub_text():
    """Пример текста с публикациями."""
    return """ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ

1. Иванов И.И. Метод анализа данных // Журн. анализ данных. - 2025. - Т. 10. - С. 45-50.
2. Петров П.П. Исследование нейросетей. - 2024. - № 5. - С. 12-20.
"""


@pytest.fixture
def sample_supervisor_text():
    """Пример текста с информацией о руководителе."""
    return """Научный руководитель: Сидоров Петр Алексеевич,
доктор технических наук, профессор,
кафедра информационных систем ФГБОУ ВО "Тестовый университет"
"""


@pytest.fixture
def sample_pdf_text_with_section():
    """Текст PDF с разделом публикаций."""
    return """ВВЕДЕНИЕ
Актуальность исследования...

ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ
1. Иванов И.И. Анализ данных // Журн. анализ. - 2025. - № 1.
2. Петров П.П. Нейросети. - 2024. - Т. 5.
3. Сидоров С.С. Машинное обучение. - 2023.
4. Козлов К.К. Глубокое обучение. - 2022.
5. Новиков Н.Н. Обработка данных. - 2021.

ЗАКЛЮЧЕНИЕ
Результаты работы...
"""


@pytest.fixture
def sample_pdf_text_no_section():
    """Текст PDF без раздела публикаций."""
    return """ВВЕДЕНИЕ
Актуальность исследования...

ГЛАВА 1. Обзор литературы...

ГЛАВА 2. Методы...

ЗАКЛЮЧЕНИЕ
Результаты работы...
"""
