#!/usr/bin/env python3
"""Тесты функций daily_sync."""
import os
import sys
import re
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_sync import (
    sanitize_filename,
    read_specialties,
    get_memory_mb,
    force_gc,
    check_memory,
)


class TestSanitizeFilename:
    """Тесты функции sanitization filename."""

    def test_basic_sanitize(self):
        name = "Иванов Иван Иванович"
        result = sanitize_filename(name)
        assert "_" in result
        assert " " not in result

    def test_remove_special_chars(self):
        name = 'Test<>:"/\\|?*'
        result = sanitize_filename(name)
        for ch in '<>:"/\\|?*':
            assert ch not in result

    def test_consecutive_spaces(self):
        name = "Иванов   Иван    Иванович"
        result = sanitize_filename(name)
        assert "   " not in result
        assert "    " not in result

    def test_strip_leading_trailing(self):
        name = "  _test_  "
        result = sanitize_filename(name)
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_max_length(self):
        long_name = "A" * 200
        result = sanitize_filename(long_name)
        assert len(result) <= 100

    def test_empty_string(self):
        result = sanitize_filename("")
        assert result == ""

    def test_unicode(self):
        name = "Цгоев Чермен Аланович"
        result = sanitize_filename(name)
        assert len(result) > 0
        assert "Цгоев" in result or "Цгоев" in result.replace("ё", "е")


class TestReadSpecialties:
    """Тесты функции read_specialties."""

    def test_read_with_separator(self, tmp_path, sample_spec_lines):
        spec_file = tmp_path / "sci_spec.txt"
        spec_file.write_text("\n".join(sample_spec_lines), encoding="utf-8")

        specs = read_specialties(spec_file=str(spec_file))
        assert len(specs) == 3
        assert specs[0][0] == "1.2.1"
        assert "Искусственный" in specs[0][1]
        assert specs[2][1] == ""

    def test_read_missing_file(self, tmp_path):
        missing_file = tmp_path / "missing.txt"
        
        from config import SCI_SPEC_FILE
        import daily_sync
        
        original_file = daily_sync.SCI_SPEC_FILE
        specs = read_specialties(spec_file=str(missing_file))
        assert specs == []

    def test_read_empty_lines(self, tmp_path):
        content = "\n1.2.1 - Test\n\n2.2.2 - Test2\n\n"
        spec_file = tmp_path / "sci_spec.txt"
        spec_file.write_text(content, encoding="utf-8")

        specs = read_specialties(spec_file=str(spec_file))
        assert len(specs) == 2


class TestMemoryControl:
    """Тесты контроля памяти."""

    def test_get_memory_mb(self):
        mem = get_memory_mb()
        assert mem > 0
        assert isinstance(mem, float)

    def test_force_gc(self, capsys):
        force_gc()
        captured = capsys.readouterr()
        assert "GC:" in captured.out

    def test_check_memory_normal(self):
        # Should not raise or exit with normal memory usage
        check_memory()
