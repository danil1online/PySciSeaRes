"""Инициализация и управление базой данных."""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "vak.db")


def get_db():
    """Получить соединение с БД."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Инициализирует БД: создаёт таблицы и индексы."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Migration: add autoref_pdf_url column if it doesn't exist (for existing DBs)
    try:
        c.execute("ALTER TABLE adverts ADD COLUMN autoref_pdf_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add email column to publications if it doesn't exist (for existing DBs)
    try:
        c.execute("ALTER TABLE publications ADD COLUMN email TEXT")
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

    # Enable WAL mode for better concurrent read performance
    c.execute("PRAGMA journal_mode=WAL")

    # Indexes for search performance
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_adverts_specialty ON adverts(specialty_cipher)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_date_defend ON adverts(date_defend)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_fio ON adverts(fio)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_dissertation ON adverts(dissertation_name)",
        "CREATE INDEX IF NOT EXISTS idx_adverts_cipher ON adverts(specialty_cipher, date_defend)",
        "CREATE INDEX IF NOT EXISTS idx_publications_advert ON publications(advert_id)",
        "CREATE INDEX IF NOT EXISTS idx_publications_year ON publications(year)",
    ]
    for idx_sql in indexes:
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    return conn
