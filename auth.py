"""Авторизация, управление пользователями и кластерами."""
import os
import sqlite3
import bcrypt

from config import DB_PATH, DEFAULT_ADMIN_USERNAME
from logging_config import get_logger

logger = get_logger("auth")


def get_db():
    return sqlite3.connect(DB_PATH)


def init_users():
    """Инициализирует таблицу users и создаёт админа по умолчанию."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        cluster_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()

    try:
        c.execute("ALTER TABLE users ADD COLUMN cluster_id INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
    if c.fetchone()[0] == 0:
        pwd = bcrypt.hashpw(
            os.environ.get("ADMIN_PASSWORD", "admin123").encode("utf-8"),
            bcrypt.gensalt()
        )
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (DEFAULT_ADMIN_USERNAME, pwd.decode("utf-8"), 1)
        )
        conn.commit()
        logger.warning(f"Создан администратор: {DEFAULT_ADMIN_USERNAME} / admin123 (измените пароль!)")

    conn.close()


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password, password_hash):
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_user(username, password, is_admin=False):
    """Создаёт нового пользователя."""
    try:
        conn = get_db()
        c = conn.cursor()
        pwd_hash = hash_password(password)
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, pwd_hash, 1 if is_admin else 0)
        )
        conn.commit()
        logger.info(f"Пользователь создан: {username}")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"Пользователь уже существует: {username}")
        return False
    except Exception as e:
        logger.error(f"Ошибка создания пользователя {username}: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_user(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    if deleted:
        logger.info(f"Пользователь удалён: {username}")
    return deleted


def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, is_admin, cluster_id, created_at FROM users ORDER BY id")
    users = c.fetchall()
    conn.close()
    return [
        {"id": u[0], "username": u[1], "is_admin": bool(u[2]), "cluster_id": u[3], "created_at": u[4]}
        for u in users
    ]


def login(username, password):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT password_hash, is_admin, cluster_id FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row and check_password(password, row[0]):
        logger.info(f"Успешный вход: {username}")
        return {"username": username, "is_admin": bool(row[1]), "cluster_id": row[2]}
    logger.warning(f"Неудачная попытка входа: {username}")
    return None


def get_user_by_username(username):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, username, password_hash, is_admin, cluster_id, created_at FROM users WHERE username = ?",
        (username,)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0], "username": row[1], "is_admin": bool(row[3]),
            "cluster_id": row[4], "created_at": row[5]
        }
    return None


def create_user_with_cluster(username, password, is_admin=False, cluster_id=None):
    """Создаёт пользователя с привязкой к кластеру."""
    try:
        conn = get_db()
        c = conn.cursor()
        pwd_hash = hash_password(password)
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin, cluster_id) VALUES (?, ?, ?, ?)",
            (username, pwd_hash, 1 if is_admin else 0, cluster_id)
        )
        conn.commit()
        logger.info(f"Пользователь создан с кластером: {username} cluster_id={cluster_id}")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"Пользователь уже существует: {username}")
        return False
    except Exception as e:
        logger.error(f"Ошибка создания пользователя {username}: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
