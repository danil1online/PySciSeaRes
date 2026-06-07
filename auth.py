import sqlite3
import os
import bcrypt

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "vak.db")


def get_db():
    return sqlite3.connect(DB_PATH)


def init_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()

    # Create default admin if not exists
    c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
    if c.fetchone()[0] == 0:
        pwd = bcrypt.hashpw(os.environ.get("ADMIN_PASSWORD", "admin123").encode("utf-8"), bcrypt.gensalt())
        c.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                  ("admin", pwd.decode("utf-8"), 1))
        conn.commit()
        print("Создан администратор: admin / admin123 (измените пароль!)")

    conn.close()


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password, password_hash):
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_user(username, password, is_admin=False):
    try:
        conn = get_db()
        c = conn.cursor()
        pwd_hash = hash_password(password)
        c.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                  (username, pwd_hash, 1 if is_admin else 0))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_user(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return c.rowcount > 0


def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id")
    users = c.fetchall()
    conn.close()
    return [{"id": u[0], "username": u[1], "is_admin": bool(u[2]), "created_at": u[3]} for u in users]


def login(username, password):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT password_hash, is_admin FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row and check_password(password, row[0]):
        return {"username": username, "is_admin": bool(row[1])}
    return None
