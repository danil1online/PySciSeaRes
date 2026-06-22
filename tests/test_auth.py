#!/usr/bin/env python3
"""Тесты для модуля auth."""
import os
import sys
import pytest
import sqlite3
import tempfile
import bcrypt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["VAK_CACHE_ENABLED"] = "0"


@pytest.fixture
def test_db_path():
    """Создаёт временную БД для тестов auth."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "vak.db")
        # Patch DB_PATH temporarily
        import auth as auth_module
        original_db_path = auth_module.DB_PATH
        auth_module.DB_PATH = db_path

        conn = sqlite3.connect(db_path)
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
        conn.close()

        yield db_path

        auth_module.DB_PATH = original_db_path


@pytest.fixture(autouse=True)
def patch_db_path(test_db_path):
    """Автоматически патчит DB_PATH для всех тестов."""
    import auth as auth_module
    original = auth_module.DB_PATH
    auth_module.DB_PATH = test_db_path
    yield
    auth_module.DB_PATH = original


class TestHashPassword:
    def test_hash_returns_string(self):
        from auth import hash_password
        result = hash_password("testpassword")
        assert isinstance(result, str)
        assert len(result) > 50  # bcrypt hash is ~60 chars

    def test_different_passwords_different_hashes(self):
        from auth import hash_password
        h1 = hash_password("password1")
        h2 = hash_password("password2")
        assert h1 != h2

    def test_same_password_different_hashes(self):
        from auth import hash_password
        h1 = hash_password("samepassword")
        h2 = hash_password("samepassword")
        assert h1 != h2  # bcrypt uses random salt


class TestCheckPassword:
    def test_correct_password(self):
        from auth import hash_password, check_password
        pwd = "testpass123"
        h = hash_password(pwd)
        assert check_password(pwd, h) is True

    def test_wrong_password(self):
        from auth import hash_password, check_password
        h = hash_password("correctpassword")
        assert check_password("wrongpassword", h) is False


class TestCreateUser:
    def test_create_user_success(self):
        from auth import create_user
        result = create_user("testuser", "password123")
        assert result is True

    def test_create_user_duplicate(self):
        from auth import create_user
        create_user("testuser", "password123")
        result = create_user("testuser", "password123")
        assert result is False

    def test_create_user_short_password(self):
        from auth import create_user
        result = create_user("testuser", "12345")  # too short
        assert result is True  # auth doesn't validate password length

    def test_create_user_admin(self):
        from auth import create_user, get_all_users
        create_user("adminuser", "password123", is_admin=True)
        users = get_all_users()
        admin = [u for u in users if u["username"] == "adminuser"][0]
        assert admin["is_admin"] is True


class TestLogin:
    def test_login_success(self):
        from auth import create_user, login
        create_user("loginuser", "password123")
        result = login("loginuser", "password123")
        assert result is not None
        assert result["username"] == "loginuser"

    def test_login_wrong_password(self):
        from auth import create_user, login
        create_user("loginuser", "password123")
        result = login("loginuser", "wrongpass")
        assert result is None

    def test_login_nonexistent_user(self):
        from auth import login
        result = login("nonexistent", "password123")
        assert result is None

    def test_login_returns_cluster_id(self):
        from auth import create_user_with_cluster, login
        create_user_with_cluster("clusteruser", "password123", cluster_id=5)
        result = login("clusteruser", "password123")
        assert result["cluster_id"] == 5


class TestDeleteUser:
    def test_delete_existing_user(self):
        from auth import create_user, delete_user
        create_user("deletetest", "password123")
        result = delete_user("deletetest")
        assert result is True

    def test_delete_nonexistent_user(self):
        from auth import delete_user
        result = delete_user("nonexistent_user")
        assert result is False


class TestGetAllUsers:
    def test_empty_users(self):
        from auth import get_all_users
        users = get_all_users()
        assert isinstance(users, list)

    def test_users_after_creation(self):
        from auth import create_user, get_all_users
        create_user("user1", "password1")
        create_user("user2", "password2")
        users = get_all_users()
        usernames = {u["username"] for u in users}
        assert "user1" in usernames
        assert "user2" in usernames


class TestCreateUserWithCluster:
    def test_create_with_cluster(self):
        from auth import create_user_with_cluster
        result = create_user_with_cluster("clusteruser", "password123", cluster_id=3)
        assert result is True

    def test_create_without_cluster(self):
        from auth import create_user_with_cluster
        result = create_user_with_cluster("nocluser", "password123", cluster_id=None)
        assert result is True

    def test_create_with_cluster_duplicate(self):
        from auth import create_user_with_cluster
        create_user_with_cluster("dupuser", "password123", cluster_id=1)
        result = create_user_with_cluster("dupuser", "password123", cluster_id=2)
        assert result is False
