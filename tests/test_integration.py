#!/usr/bin/env python3
"""Интеграционные тесты: полный цикл работы системы."""
import sys
import os
import tempfile
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_sync import init_db
from search import search_adverts, get_advert_detail, get_all_specialties


class TestIntegrationSearch:
    """Интеграционные тесты поиска."""

    @pytest.fixture
    def db_with_data(self, tmp_path):
        """Создаёт БД с тестовыми данными."""
        db_path = tmp_path / "vak.db"
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        c.execute("""CREATE TABLE adverts (
            id TEXT PRIMARY KEY, old_id INTEGER, date_defend TEXT,
            fio TEXT, dissertation_name TEXT, specialty_cipher TEXT,
            specialty_text TEXT, supervisor_name TEXT, supervisor_work TEXT,
            council_cipher TEXT, defend_org TEXT, org_address TEXT,
            org_phone TEXT, autoref_url TEXT, autoref_path TEXT,
            autoref_pdf_url TEXT, downloaded INTEGER DEFAULT 0,
            email_search_attempts INTEGER DEFAULT 0,
            pub_extract_attempts INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advert_id TEXT NOT NULL, pub_number INTEGER NOT NULL,
            authors TEXT, title TEXT, journal TEXT,
            year INTEGER, pages TEXT,
            email TEXT, source_url TEXT, source_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (advert_id) REFERENCES adverts(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # Вставляем тестовые данные
        c.execute("""INSERT INTO adverts (id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text)
                     VALUES ('adv-001', 'Иванов И.И.', '2026-06-01', 'Тестовая диссертация 1', '2.2.11.', 'Информатика')""")
        c.execute("""INSERT INTO adverts (id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text)
                     VALUES ('adv-002', 'Петров П.П.', '2026-06-02', 'Тестовая диссертация 2', '2.2.11.', 'Информатика')""")
        c.execute("""INSERT INTO adverts (id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text)
                     VALUES ('adv-003', 'Сидоров С.С.', '2026-05-15', 'Другая диссертация', '1.2.1.', 'Математика')""")

        # Публикации для adv-001
        c.execute("""INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages)
                     VALUES ('adv-001', 1, 'Иванов И.И.', 'Статья 1', 'Журнал А', 2025, '10-20')""")
        c.execute("""INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages)
                     VALUES ('adv-001', 2, 'Иванов И.И., Петров П.П.', 'Статья 2', 'Журнал Б', 2024, '30-40')""")

        # Публикации для adv-002
        c.execute("""INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages)
                     VALUES ('adv-002', 1, 'Петров П.П.', 'Статья 3', 'Журнал В', 2025, '50-60')""")

        conn.commit()
        yield str(db_path)
        conn.close()

    def test_search_all_adverts(self, db_with_data, monkeypatch):
        """Поиск всех объявлений возвращает все записи."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_data))

        result = search_adverts()
        assert result['total'] == 3
        assert len(result['adverts']) == 3

    def test_search_by_specialty(self, db_with_data, monkeypatch):
        """Поиск по специальности фильтрует правильно."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_data))

        result = search_adverts(specialties=['2.2.11.'])
        assert result['total'] == 2
        assert all(a['specialty_cipher'] == '2.2.11.' for a in result['adverts'])

    def test_search_by_query(self, db_with_data, monkeypatch):
        """Поиск по тексту находит совпадения."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_data))

        result = search_adverts(query='Иванов')
        assert result['total'] == 1
        assert result['adverts'][0]['fio'] == 'Иванов И.И.'

    def test_search_by_date_range(self, db_with_data, monkeypatch):
        """Поиск по дате фильтрует правильно."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_data))

        result = search_adverts(date_from='2026-06-01', date_to='2026-06-02')
        assert result['total'] == 2

    def test_advert_pub_count(self, db_with_data, monkeypatch):
        """Подсчёт публикаций верен."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_data))

        result = search_adverts()
        by_id = {a['id']: a for a in result['adverts']}
        assert by_id['adv-001']['pub_count'] == 2
        assert by_id['adv-002']['pub_count'] == 1
        assert by_id['adv-003']['pub_count'] == 0


class TestIntegrationDetail:
    """Интеграционные тесты детальной информации."""

    @pytest.fixture
    def db_with_detail(self, tmp_path):
        """Создаёт БД с полными тестовыми данными."""
        db_path = tmp_path / "vak_detail.db"
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        c.execute("""CREATE TABLE adverts (
            id TEXT PRIMARY KEY, old_id INTEGER, date_defend TEXT,
            fio TEXT, dissertation_name TEXT, specialty_cipher TEXT,
            specialty_text TEXT, supervisor_name TEXT, supervisor_work TEXT,
            council_cipher TEXT, defend_org TEXT, org_address TEXT,
            org_phone TEXT, autoref_url TEXT, autoref_path TEXT,
            autoref_pdf_url TEXT, downloaded INTEGER DEFAULT 0,
            email_search_attempts INTEGER DEFAULT 0,
            pub_extract_attempts INTEGER DEFAULT 0,
            city TEXT, organization_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advert_id TEXT NOT NULL, pub_number INTEGER NOT NULL,
            authors TEXT, title TEXT, journal TEXT,
            year INTEGER, pages TEXT,
            email TEXT, source_url TEXT, source_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (advert_id) REFERENCES adverts(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""INSERT INTO adverts (id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text, supervisor_name, council_cipher, defend_org, autoref_url)
                     VALUES ('adv-100', 'Тестов И.Т.', '2026-06-10', 'Полная диссертация', '2.2.11.', 'Информатика', 'Руководов Р.Р.', 'Д 123.456', 'МГУ', 'https://example.com/avtoref.pdf')""")
        c.execute("""INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages)
                     VALUES ('adv-100', 1, 'Тестов И.Т.', 'Первая статья', 'Журнал X', 2025, '1-10')""")
        c.execute("""INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages)
                     VALUES ('adv-100', 2, 'Тестов И.Т., Коллега К.К.', 'Вторая статья', 'Журнал Y', 2024, '11-20')""")

        conn.commit()
        yield str(db_path)
        conn.close()

    def test_get_advert_detail(self, db_with_detail, monkeypatch):
        """Получение детальной информации."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_detail))

        detail = get_advert_detail('adv-100')
        assert detail is not None
        assert detail['fio'] == 'Тестов И.Т.'
        assert detail['supervisor_name'] == 'Руководов Р.Р.'
        assert len(detail['publications']) == 2
        assert detail['publications'][0]['title'] == 'Первая статья'

    def test_get_nonexistent_advert(self, db_with_detail, monkeypatch):
        """Получение несуществующего объявления возвращает None."""
        import search
        monkeypatch.setattr(search, 'DB_PATH', str(db_with_detail))

        detail = get_advert_detail('nonexistent')
        assert detail is None


class TestIntegrationDBInit:
    """Интеграционные тесты инициализации БД."""

    def test_init_db_creates_tables(self, tmp_path):
        """init_db создаёт все таблицы и индексы."""
        os.makedirs(tmp_path / 'instance', exist_ok=True)
        test_db = tmp_path / 'instance' / 'vak.db'

        # Пatches DB_PATH для vak_sync.db
        import vak_sync.db as vak_db
        original_path = vak_db.DB_PATH
        vak_db.DB_PATH = str(test_db)

        try:
            conn = init_db()

            # Проверяем, что WAL режим включён
            c = conn.cursor()
            c.execute("PRAGMA journal_mode")
            mode = c.fetchone()[0]
            assert mode == 'wal', f"Ожидался WAL режим, получено: {mode}"

            # Проверяем наличие таблиц
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in c.fetchall()]
            assert 'adverts' in tables
            assert 'publications' in tables
            assert 'users' in tables

            # Проверяем наличие индексов
            c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
            indexes = [row[0] for row in c.fetchall()]
            assert 'idx_adverts_specialty' in indexes
            assert 'idx_adverts_date_defend' in indexes
            assert 'idx_publications_advert' in indexes

            conn.close()
        finally:
            vak_db.DB_PATH = original_path


class TestIntegrationQASQLValidation:
    """Интеграционные тесты валидации SQL из QA."""

    def test_safe_select_query(self):
        """Безопасный SELECT-запрос проходит валидацию."""
        from qa import _validate_sql
        valid, result = _validate_sql("SELECT fio FROM adverts")
        assert valid is True
        assert 'LIMIT' in result.upper()

    def test_dangerous_drop_query(self):
        """DROP-запрос отклоняется."""
        from qa import _validate_sql
        valid, result = _validate_sql("DROP TABLE adverts")
        assert valid is False

    def test_dangerous_delete_query(self):
        """DELETE-запрос отклоняется."""
        from qa import _validate_sql
        valid, result = _validate_sql("DELETE FROM adverts")
        assert valid is False

    def test_excessive_limit(self):
        """Запрос с LIMIT > 100 отклоняется."""
        from qa import _validate_sql
        valid, result = _validate_sql("SELECT * FROM adverts LIMIT 1000")
        assert valid is False

    def test_insert_query_rejected(self):
        """INSERT-запрос отклоняется."""
        from qa import _validate_sql
        valid, result = _validate_sql("INSERT INTO adverts VALUES ('x','x')")
        assert valid is False
