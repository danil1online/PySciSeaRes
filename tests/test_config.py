#!/usr/bin/env python3
"""Тесты конфигурации."""
import os
from config import (
    BASE_DIR, DB_PATH, AUTOREFS_DIR, LLM_RESPONSES_DIR, SCI_SPEC_FILE,
    API_BASE, LLM_API_URL, LLM_MODEL,
    MIN_SIZE, MAX_PUBLICATIONS, SYNC_DAYS,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
    SESSION_SECRET_KEY,
)


def test_base_dir():
    assert BASE_DIR is not None
    assert os.path.isabs(BASE_DIR)


def test_db_path():
    assert DB_PATH.endswith("vak.db")
    assert "instance" in DB_PATH


def test_autorefs_dir():
    assert AUTOREFS_DIR.endswith("autorefs")


def test_llm_responses_dir():
    assert LLM_RESPONSES_DIR.endswith("llm_responses")


def test_sci_spec_file():
    assert SCI_SPEC_FILE.endswith("sci_spec.txt")


def test_api_base():
    assert API_BASE == "https://vak.gisnauka.ru/api"


def test_llm_api_url():
    assert LLM_API_URL.startswith("http://")
    assert "chat/completions" in LLM_API_URL


def test_llm_model():
    assert LLM_MODEL == "qwen3.5-4b"


def test_min_size():
    assert MIN_SIZE == 200 * 1024
    assert MIN_SIZE > 0


def test_max_publications():
    assert MAX_PUBLICATIONS == 30
    assert MAX_PUBLICATIONS > 0


def test_sync_days():
    assert SYNC_DAYS == 30


def test_llm_timeout():
    assert LLM_TIMEOUT == 120
    assert LLM_TIMEOUT > 0


def test_llm_max_tokens():
    assert LLM_MAX_TOKENS == 2000
    assert LLM_MAX_TOKENS > 0


def test_llm_temperature():
    assert LLM_TEMPERATURE == 0.1
    assert 0 <= LLM_TEMPERATURE <= 1


def test_session_secret_key():
    assert SESSION_SECRET_KEY is not None
    assert isinstance(SESSION_SECRET_KEY, str)
    assert len(SESSION_SECRET_KEY) > 0
