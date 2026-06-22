#!/usr/bin/env python3
"""Тесты для Flask-маршрутов app.py."""
import os
import sys
import pytest
import json
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["VAK_CACHE_ENABLED"] = "0"


@pytest.fixture
def app_client():
    """Создаёт тестовый клиент Flask с временной БД."""
    from app import app

    with tempfile.TemporaryDirectory() as tmpdir:
        import auth as auth_module
        import search as search_module
        import qa as qa_module
        import vak_sync.db as db_module

        original_paths = {
            "auth": auth_module.DB_PATH,
            "search": search_module.DB_PATH,
            "qa": qa_module.DB_PATH,
            "vak_sync.db": db_module.DB_PATH,
        }

        test_db = os.path.join(tmpdir, "vak.db")
        auth_module.DB_PATH = test_db
        search_module.DB_PATH = test_db
        qa_module.DB_PATH = test_db
        db_module.DB_PATH = test_db

        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret-key"

        from daily_sync import init_db
        init_db()

        with app.test_client() as client:
            yield client

        auth_module.DB_PATH = original_paths["auth"]
        search_module.DB_PATH = original_paths["search"]
        qa_module.DB_PATH = original_paths["qa"]
        db_module.DB_PATH = original_paths["vak_sync.db"]


class TestAuthRoutes:
    def test_login_page_get(self, app_client):
        resp = app_client.get("/login")
        assert resp.status_code == 200

    def test_login_success(self, app_client):
        resp = app_client.post("/login", data={
            "username": "admin",
            "password": "admin123",
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_login_wrong_password(self, app_client):
        resp = app_client.post("/login", data={
            "username": "admin",
            "password": "wrongpassword",
        })
        assert resp.status_code == 200

    def test_logout(self, app_client):
        app_client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = app_client.get("/logout", follow_redirects=True)
        assert resp.status_code == 200

    def test_register_page_get(self, app_client):
        resp = app_client.get("/register")
        assert resp.status_code == 200

    def test_register_success(self, app_client):
        resp = app_client.post("/register", data={
            "username": "newuser1",
            "password": "password1",
            "password_confirm": "password1",
            "cluster_id": "0",
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_register_password_mismatch(self, app_client):
        resp = app_client.post("/register", data={
            "username": "newuser2",
            "password": "password1",
            "password_confirm": "password2",
            "cluster_id": "0",
        })
        assert resp.status_code == 200

    def test_register_invalid_username(self, app_client):
        resp = app_client.post("/register", data={
            "username": "user name!",
            "password": "password1",
            "password_confirm": "password1",
            "cluster_id": "0",
        })
        assert resp.status_code == 200


class TestSearchRoutes:
    def _login(self, client):
        return client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_search_page(self, app_client):
        self._login(app_client)
        resp = app_client.get("/search")
        assert resp.status_code == 200

    def test_api_search_empty(self, app_client):
        self._login(app_client)
        resp = app_client.get("/api/search")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "adverts" in data
        assert "total" in data

    def test_api_search_with_params(self, app_client):
        self._login(app_client)
        resp = app_client.get("/api/search?page=1&query=test")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "adverts" in data

    def test_export_excel(self, app_client):
        self._login(app_client)
        resp = app_client.get("/export")
        assert resp.status_code == 200

    def test_save_search(self, app_client):
        self._login(app_client)
        resp = app_client.post("/api/save_search",
                               data=json.dumps({"specialties": ["1.2.1"], "query": "test"}),
                               content_type="application/json")
        assert resp.status_code == 200

    def test_clear_search(self, app_client):
        self._login(app_client)
        app_client.post("/api/save_search",
                        data=json.dumps({"specialties": ["1.2.1"]}),
                        content_type="application/json")
        resp = app_client.post("/api/clear_search")
        assert resp.status_code == 200


class TestDetailRoutes:
    def _login(self, client):
        return client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_detail_not_found(self, app_client):
        self._login(app_client)
        resp = app_client.get("/detail/nonexistent-id")
        assert resp.status_code == 404


class TestQARoutes:
    def _login(self, client):
        return client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_qa_page(self, app_client):
        self._login(app_client)
        resp = app_client.get("/qa")
        assert resp.status_code == 200

    def test_api_qa_empty_question(self, app_client):
        self._login(app_client)
        resp = app_client.post("/api/qa",
                               data=json.dumps({"question": ""}),
                               content_type="application/json")
        assert resp.status_code == 400

    def test_api_qa_no_auth(self, app_client):
        resp = app_client.post("/api/qa",
                               data=json.dumps({"question": "test"}),
                               content_type="application/json")
        assert resp.status_code in (302, 200)


class TestAdminRoutes:
    def _login_admin(self, client):
        return client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_admin_page(self, app_client):
        self._login_admin(app_client)
        resp = app_client.get("/admin")
        assert resp.status_code == 200

    def test_admin_create_user(self, app_client):
        self._login_admin(app_client)
        resp = app_client.post("/admin/create", data={
            "username": "testadminuser",
            "password": "password1",
            "cluster_id": "",
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_admin_delete_user(self, app_client):
        self._login_admin(app_client)
        app_client.post("/admin/create", data={
            "username": "deleteme",
            "password": "password1",
        })
        resp = app_client.post("/admin/delete/deleteme", follow_redirects=True)
        assert resp.status_code == 200


class TestDocsRoute:
    def _login(self, client):
        return client.post("/login", data={"username": "admin", "password": "admin123"})

    def test_docs_page(self, app_client):
        self._login(app_client)
        resp = app_client.get("/docs")
        assert resp.status_code == 200

    def test_docs_requires_auth(self, app_client):
        resp = app_client.get("/docs")
        assert resp.status_code in (302, 200)


class TestIndexRedirect:
    def test_index_redirects_to_search(self, app_client):
        resp = app_client.get("/", follow_redirects=True)
        assert resp.status_code == 200
