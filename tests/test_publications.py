#!/usr/bin/env python3
"""Тесты извлечения публикаций."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractors.publications import (
    _extract_publications_regex,
    _is_likely_publication,
    _normalize_for_dedup,
    _find_publications_section,
    _merge_publications,
    parse_pub_to_json,
    parse_publication_to_structured,
)


class TestExtractPublicationsRegex:
    """Тесты regex-парсинга публикаций."""

    def test_basic_extraction(self, sample_pub_text):
        pubs = _extract_publications_regex(sample_pub_text)
        assert len(pubs) >= 2

    def test_separated_by_number(self):
        text = "Some intro\n\n1. First publication title that is long enough\n2. Second publication title here\n3. Third publication entry now\n"
        pubs = _extract_publications_regex(text)
        assert len(pubs) == 3

    def test_filter_short_items(self):
        text = "1. Short\n2. A very long publication title that should pass the filter\n"
        pubs = _extract_publications_regex(text)
        assert len(pubs) == 1

    def test_no_pubs(self):
        text = "No numbered items here\n"
        pubs = _extract_publications_regex(text)
        assert pubs == []

    def test_handles_periods(self):
        text = "\n\n1. First publication title here\n2. Second publication entry now\n3. Third publication text please\n"
        pubs = _extract_publications_regex(text)
        assert len(pubs) == 3


class TestIsLikelyPublication:
    """Тесты фильтрации публикаций."""

    def test_real_publication(self):
        text = "Иванов И.И. Метод анализа данных // Журн. анализ данных. - 2025. - Т. 10. - С. 45-50."
        assert _is_likely_publication(text)

    def test_not_introduction(self):
        text = "Во введении обоснована актуальность исследования"
        assert not _is_likely_publication(text)

    def test_not_chapter(self):
        text = "В первой главе рассмотрены основные положения"
        assert not _is_likely_publication(text)

    def test_not_supervisor(self):
        text = "Научный руководитель профессор Сидоров П.А."
        assert not _is_likely_publication(text)

    def test_not_defense(self):
        text = "Защита состояла 15 июня 2026 года"
        assert not _is_likely_publication(text)

    def test_with_preposition(self):
        text = "На основе анализа данных получены результаты"
        assert not _is_likely_publication(text)

    def test_conclusion_with_indicator(self):
        text = "Анализ данных выполнен // Журн. машинное обучение. - 2025."
        assert _is_likely_publication(text)

    def test_conclusion_without_indicator(self):
        text = "Анализ показал высокую эффективность"
        assert not _is_likely_publication(text)

    def test_dissertation_structure(self):
        text = "Диссертация состоит из введения, трех глав и заключения"
        assert not _is_likely_publication(text)


class TestNormalizeForDedup:
    """Тесты нормализации для дедупликации."""

    def test_lowercase(self):
        text = "Test Title"
        result = _normalize_for_dedup(text)
        assert result == "test title"

    def test_remove_dashes(self):
        text = "Test-title"
        result = _normalize_for_dedup(text)
        assert result == "test title"

    def test_remove_punctuation(self):
        text = "Test, title."
        result = _normalize_for_dedup(text)
        assert result == "test title"

    def test_normalize_whitespace(self):
        text = "Test   multiple    spaces"
        result = _normalize_for_dedup(text)
        assert "  " not in result

    def test_truncate(self):
        long_text = "A" * 200
        result = _normalize_for_dedup(long_text)
        assert len(result) <= 120


class TestFindPublicationsSection:
    """Тесты поиска раздела публикаций."""

    def test_found_section(self, sample_pdf_text_with_section):
        idx, found = _find_publications_section(sample_pdf_text_with_section)
        assert idx >= 0
        assert found is not None
        assert "ПУБЛИКАЦИИ" in found or "ОСНОВНЫЕ" in found

    def test_not_found_section(self, sample_pdf_text_no_section):
        idx, found = _find_publications_section(sample_pdf_text_no_section)
        assert idx == -1
        assert found is None

    def test_various_headers(self):
        headers = [
            'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ АВТОРОМ',
            'ОСНОВНЫЕ ПУБЛИКАЦИИ',
            'Публикации автора по теме диссертации',
        ]
        for header in headers:
            text = f"Some text before\n{header}\nSome publications"
            idx, found = _find_publications_section(text)
            assert idx >= 0, f"Header not found: {header}"


class TestMergePublications:
    """Тесты объединения публикаций."""

    def test_regex_only(self):
        regex_pubs = ["Pub 1", "Pub 2", "Pub 3"]
        result = _merge_publications(regex_pubs, [])
        assert len(result) == 3

    def test_llm_only(self):
        regex_pubs = []
        llm_pubs = ["Pub 1", "Pub 2"]
        result = _merge_publications(regex_pubs, llm_pubs)
        assert len(result) == 2

    def test_merge_with_dedup(self):
        regex_pubs = ["Pub 1", "Pub 2", "Pub 3"]
        llm_pubs = ["Pub 2", "Pub 4"]
        result = _merge_publications(regex_pubs, llm_pubs)
        # Should have 4 unique publications (3 regex + 1 new from LLM)
        assert len(result) == 4

    def test_empty(self):
        result = _merge_publications([], [])
        assert result == []


class TestParsePubToJson:
    """Тесты парсинга публикаций в JSON."""

    def test_basic_parsing(self):
        texts = ["Иванов И.И. Метод анализа // Журн. - 2025. - С. 45-50."]
        result = parse_pub_to_json(texts)
        assert len(result) >= 1
        assert "title" in result[0] or "journal" in result[0]

    def test_deduplication(self):
        texts = [
            "Иванов И.И. Метод // Журн. - 2025.",
            "Иванов И.И. Метод // Журн. - 2025.",  # duplicate
        ]
        result = parse_pub_to_json(texts)
        assert len(result) == 1

    def test_multiple_pubs(self):
        texts = [
            "Иванов И.И. Метод // Журн. - 2025. - С. 45.",
            "Петров П.П. Анализ // Научн. - 2024.",
        ]
        result = parse_pub_to_json(texts)
        assert len(result) == 2


class TestParsePublicationToStructured:
    """Тесты парсинга одной публикации."""

    def test_with_slash(self):
        text = "Иванов И.И. Метод // Журн. анализ. - 2025. - С. 45-50."
        result = parse_publication_to_structured(text)
        assert "title" in result
        assert result["year"] == 2025

    def test_without_slash(self):
        text = "Иванов И.И. Метод анализа данных. - 2025."
        result = parse_publication_to_structured(text)
        assert "title" in result or "journal" in result

    def test_year_extraction(self):
        text = "Test publication 2025"
        result = parse_publication_to_structured(text)
        assert result["year"] == 2025

    def test_pages_extraction(self):
        text = "Test publication. - 2025. - С. 45-50."
        result = parse_publication_to_structured(text)
        assert "45" in result["pages"] or "50" in result["pages"]
