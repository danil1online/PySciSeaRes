"""Инициализация и управление базой данных."""
import os
import sqlite3

from config import DB_PATH
from logging_config import get_logger

logger = get_logger("db")


def get_db():
    """Получить соединение с БД."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Инициализирует БД: создаёт таблицы, индексы и пользователей."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # Initialize users (creates default admin if not exists)
    try:
        from auth import init_users as _init_users
        _init_users()
    except Exception as e:
        logger.warning(f"Could not init users: {e}")

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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Migration: add columns if they don't exist
    migrations = [
        ("adverts", "autoref_pdf_url TEXT"),
        ("publications", "email TEXT"),
        ("publications", "source_url TEXT"),
        ("publications", "source_name TEXT"),
        ("adverts", "email_search_attempts INTEGER DEFAULT 0"),
        ("adverts", "pub_extract_attempts INTEGER DEFAULT 0"),
        ("users", "cluster_id INTEGER"),
        ("adverts", "city TEXT"),
        ("adverts", "organization_name TEXT"),
    ]
    for table, column_sql in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
        except sqlite3.OperationalError:
            pass  # column already exists

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
        cluster_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("PRAGMA journal_mode=WAL")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_adverts_specialty ON adverts(specialty_cipher)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_date_defend ON adverts(date_defend)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_fio ON adverts(fio)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_dissertation ON adverts(dissertation_name)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_cipher ON adverts(specialty_cipher, date_defend)",
        "CREATE INDEX IF NOT EXISTS idx_publications_advert ON publications(advert_id)",
        "CREATE INDEX IF NOT EXISTS idx_publications_year ON publications(year)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_city ON adverts(city)",
    ]
    for idx_sql in indexes:
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    logger.debug(f"Database initialized: {DB_PATH}")
    return conn
