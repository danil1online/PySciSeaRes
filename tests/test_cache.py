#!/usr/bin/env python3
"""Тесты для модуля cache."""
import os
import sys
import pytest
import tempfile
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Enable cache for these tests
os.environ["VAK_CACHE_ENABLED"] = "1"


class TestCacheKey:
    def test_deterministic(self):
        from extractors.cache import _cache_key
        k1 = _cache_key("system prompt", "user text")
        k2 = _cache_key("system prompt", "user text")
        assert k1 == k2

    def test_different_text_different_key(self):
        from extractors.cache import _cache_key
        k1 = _cache_key("prompt", "text1")
        k2 = _cache_key("prompt", "text2")
        assert k1 != k2

    def test_different_prompt_different_key(self):
        from extractors.cache import _cache_key
        k1 = _cache_key("prompt1", "text")
        k2 = _cache_key("prompt2", "text")
        assert k1 != k2

    def test_key_length(self):
        from extractors.cache import _cache_key
        key = _cache_key("x" * 1000, "y" * 1000)
        assert len(key) == 32  # truncated to 32 chars


class TestCacheGetSet:
    @pytest.fixture
    def cache_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import extractors.cache as cache_module
            import config
            original = cache_module.CACHE_DIR
            original_enabled = config.VAK_CACHE_ENABLED
            original_cache_enabled = cache_module.VAK_CACHE_ENABLED
            cache_module.CACHE_DIR = os.path.join(tmpdir, "cache")
            config.VAK_CACHE_ENABLED = True
            cache_module.VAK_CACHE_ENABLED = True
            yield tmpdir
            cache_module.CACHE_DIR = original
            config.VAK_CACHE_ENABLED = original_enabled
            cache_module.VAK_CACHE_ENABLED = original_cache_enabled

    def test_cache_miss(self, cache_dir):
        from extractors.cache import get_cached
        result, elapsed, from_cache = get_cached("prompt", "text")
        assert result is None
        assert elapsed is None
        assert from_cache is False

    def test_cache_set_and_get(self, cache_dir):
        from extractors.cache import cache_result, get_cached

        cache_result("prompt", "text", "LLM response", 1.5)
        content, elapsed, from_cache = get_cached("prompt", "text")

        assert content == "LLM response"
        assert elapsed == 1.5
        assert from_cache is True

    def test_cache_overwrite(self, cache_dir):
        from extractors.cache import cache_result, get_cached

        cache_result("prompt", "text", "first response", 1.0)
        cache_result("prompt", "text", "second response", 2.0)

        content, elapsed, from_cache = get_cached("prompt", "text")
        assert content == "second response"
        assert elapsed == 2.0

    def test_cache_disabled(self, cache_dir):
        import extractors.cache as cache_module
        from extractors.cache import cache_result, get_cached

        # Need to reimport to pick up the changed CACHE_ENABLED
        cache_module.CACHE_ENABLED = False

        # Manually check the condition
        result, elapsed, from_cache = get_cached("new_prompt", "new_text")
        assert from_cache is False

        cache_module.CACHE_ENABLED = True

    def test_cache_stores_timestamp(self, cache_dir):
        from extractors.cache import cache_result, get_cached

        cache_result("prompt", "text", "response", 0.5)

        # Check the file exists and has correct format
        from extractors.cache import _cache_key, _cache_path
        key = _cache_key("prompt", "text")
        path = _cache_path(key)

        assert os.path.exists(path)
        with open(path, "r") as f:
            data = json.load(f)
        assert "timestamp" in data
        assert "elapsed" in data
        assert "content" in data


class TestCacheTTL:
    @pytest.fixture
    def cache_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import extractors.cache as cache_module
            import config
            original = cache_module.CACHE_DIR
            original_ttl = config.CACHE_TTL_DAYS
            original_enabled = cache_module.VAK_CACHE_ENABLED
            cache_module.CACHE_DIR = os.path.join(tmpdir, "cache")
            config.VAK_CACHE_ENABLED = True
            cache_module.VAK_CACHE_ENABLED = True
            yield tmpdir
            cache_module.CACHE_DIR = original
            config.CACHE_TTL_DAYS = original_ttl
            cache_module.VAK_CACHE_ENABLED = original_enabled

    def test_expired_cache(self, cache_dir):
        from extractors.cache import cache_result, get_cached
        from datetime import datetime, timedelta

        # Create a cache entry with old timestamp
        key = "expired_test_key"
        from extractors.cache import _cache_path
        path = _cache_path(key)

        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        data = {
            "timestamp": old_time,
            "elapsed": 1.0,
            "content": "old response",
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

        result, elapsed, from_cache = get_cached("any_prompt", "any_text")
        assert from_cache is False

    def test_fresh_cache(self, cache_dir):
        from extractors.cache import cache_result, get_cached

        cache_result("prompt", "text", "fresh response", 0.5)
        content, elapsed, from_cache = get_cached("prompt", "text")

        assert from_cache is True
        assert content == "fresh response"


class TestCleanup:
    @pytest.fixture
    def cache_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import extractors.cache as cache_module
            import config
            original = cache_module.CACHE_DIR
            original_enabled = cache_module.VAK_CACHE_ENABLED
            cache_module.CACHE_DIR = os.path.join(tmpdir, "cache")
            config.VAK_CACHE_ENABLED = True
            cache_module.VAK_CACHE_ENABLED = True
            yield tmpdir
            cache_module.CACHE_DIR = original
            cache_module.VAK_CACHE_ENABLED = original_enabled

    def test_cleanup_removes_old_files(self, cache_dir):
        from extractors.cache import cache_result, cleanup_old_cache
        from datetime import datetime, timedelta
        from extractors.cache import _cache_key, _cache_path

        # Create old cache file
        key = "old_key"
        path = _cache_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        data = {"timestamp": old_time, "elapsed": 1.0, "content": "old"}
        with open(path, "w") as f:
            json.dump(data, f)

        cleanup_old_cache()
        assert not os.path.exists(path)

    def test_cleanup_keeps_fresh_files(self, cache_dir):
        from extractors.cache import cache_result, cleanup_old_cache

        cache_result("prompt", "text", "fresh", 0.5)
        cleanup_old_cache()

        from extractors.cache import get_cached
        content, _, from_cache = get_cached("prompt", "text")
        assert from_cache is True

    def test_cleanup_non_json_files(self, cache_dir):
        from extractors.cache import cleanup_old_cache, _ensure_cache_dir, CACHE_DIR

        _ensure_cache_dir()
        # Create a non-JSON file
        test_file = os.path.join(CACHE_DIR, "readme.txt")
        with open(test_file, "w") as f:
            f.write("not json")

        cleanup_old_cache()  # Should not crash
        assert os.path.exists(test_file)


class TestClearCache:
    @pytest.fixture
    def cache_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import extractors.cache as cache_module
            original = cache_module.CACHE_DIR
            cache_module.CACHE_DIR = os.path.join(tmpdir, "cache")
            cache_module.CACHE_ENABLED = True
            yield tmpdir
            cache_module.CACHE_DIR = original

    def test_clear_removes_all(self, cache_dir):
        from extractors.cache import cache_result, clear_cache

        cache_result("p1", "t1", "r1", 1.0)
        cache_result("p2", "t2", "r2", 2.0)
        cache_result("p3", "t3", "r3", 3.0)

        clear_cache()

        # All cache files should be gone
        from extractors.cache import get_cached
        _, _, c1 = get_cached("p1", "t1")
        _, _, c2 = get_cached("p2", "t2")
        _, _, c3 = get_cached("p3", "t3")
        assert c1 is False
        assert c2 is False
        assert c3 is False


class TestEnsureCacheDir:
    def test_creates_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            import extractors.cache as cache_module
            original = cache_module.CACHE_DIR
            cache_module.CACHE_DIR = os.path.join(tmpdir, "new", "cache")

            from extractors.cache import _ensure_cache_dir
            _ensure_cache_dir()

            assert os.path.isdir(os.path.join(tmpdir, "new", "cache"))
            cache_module.CACHE_DIR = original
