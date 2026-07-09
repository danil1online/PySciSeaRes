#!/usr/bin/env python3
"""Тесты для модуля email_search."""
import os
import sys
import pytest
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["VAK_CACHE_ENABLED"] = "0"


class TestNormalizeTitle:
    def test_basic_title(self):
        from extractors.email_search import _normalize_title
        result = _normalize_title("Test Article Title")
        assert "Test" in result
        assert "Article" in result

    def test_empty_title(self):
        from extractors.email_search import _normalize_title
        result = _normalize_title("")
        assert result == ""

    def test_none_title(self):
        from extractors.email_search import _normalize_title
        result = _normalize_title(None)
        assert result == ""

    def test_truncates_long_title(self):
        from extractors.email_search import _normalize_title
        long_title = "A" * 200
        result = _normalize_title(long_title)
        assert len(result) <= 100


class TestTitlesMatch:
    def test_identical_titles(self):
        from extractors.email_search import _titles_match
        assert _titles_match("Same Title", "Same Title") is True

    def test_different_titles(self):
        from extractors.email_search import _titles_match
        assert _titles_match("Title One", "Completely Different") is False

    def test_empty_titles(self):
        from extractors.email_search import _titles_match
        assert _titles_match("", "Some Title") is False
        assert _titles_match("Some Title", "") is False

    def test_similar_titles(self):
        from extractors.email_search import _titles_match
        t1 = "Machine Learning Approaches for Data Analysis"
        t2 = "Machine Learning in Data Analysis Tasks"
        assert _titles_match(t1, t2) is True


class TestBuildDDGQuery:
    def test_all_fields(self):
        from extractors.email_search import _build_ddg_query
        pub = {
            "title": "Test Article",
            "authors": "Ivanov I.I.",
            "journal": "Test Journal",
            "year": 2024,
            "pages": "45-50",
        }
        query = _build_ddg_query(pub)
        assert "Ivanov" in query
        assert "Test Article" in query
        assert "2024" in query

    def test_minimal_fields(self):
        from extractors.email_search import _build_ddg_query
        pub = {"title": "Simple Title"}
        query = _build_ddg_query(pub)
        assert "Simple Title" in query

    def test_with_suffix(self):
        from extractors.email_search import _build_ddg_query
        pub = {"title": "Test"}
        query = _build_ddg_query(pub, suffix="author")
        assert "author" in query


class TestIsPdfUrl:
    def test_pdf_extension(self):
        from extractors.email_search import _is_pdf_url
        assert _is_pdf_url("http://example.com/article.pdf") is True

    def test_download_path(self):
        from extractors.email_search import _is_pdf_url
        assert _is_pdf_url("http://example.com/download/article") is True

    def test_pdf_path(self):
        from extractors.email_search import _is_pdf_url
        assert _is_pdf_url("http://example.com/pdf/article") is True

    def test_html_url(self):
        from extractors.email_search import _is_pdf_url
        assert _is_pdf_url("http://example.com/article") is False

    def test_empty_url(self):
        from extractors.email_search import _is_pdf_url
        assert _is_pdf_url("") is False
        assert _is_pdf_url(None) is False


class TestIsJournalDomain:
    def test_cyberleninka(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("https://cyberleninka.ru/article/test") is True

    def test_elibrary(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("https://elibrary.ru/item.test") is True

    def test_blocked_wikipedia(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("https://wikipedia.org") is False

    def test_blocked_social(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("https://vk.com/article") is False
        assert _is_journal_domain("https://youtube.com/watch") is False

    def test_blocked_file_sharing(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("https://drive.google.com/file") is False
        assert _is_journal_domain("https://dropbox.com/share") is False

    def test_empty_url(self):
        from extractors.email_search import _is_journal_domain
        assert _is_journal_domain("") is False
        assert _is_journal_domain(None) is False


class TestEmailRegex:
    def test_valid_emails(self):
        from extractors.email_search import EMAIL_REGEX
        assert EMAIL_REGEX.search("test@example.com") is not None
        assert EMAIL_REGEX.search("user.name+tag@domain.ru") is not None

    def test_invalid_emails(self):
        from extractors.email_search import EMAIL_REGEX
        assert EMAIL_REGEX.search("not-an-email") is None
        assert EMAIL_REGEX.search("@nodomain.com") is None


class TestFindEmailRegex:
    def test_single_email(self):
        from extractors.email_search import find_email_regex
        text = "Contact: author@university.ru for questions"
        result = find_email_regex(text)
        assert result == "author@university.ru"

    def test_no_email(self):
        from extractors.email_search import find_email_regex
        text = "No email in this text"
        result = find_email_regex(text)
        assert result is None

    def test_multiple_emails(self):
        from extractors.email_search import find_email_regex
        text = "Emails: first@a.com and second@b.ru"
        result = find_email_regex(text)
        assert result == "first@a.com"


class TestCheckTitleSimilarity:
    def test_identical(self):
        from extractors.email_search import _check_title_similarity
        assert _check_title_similarity("Same Title", "Same Title") is True

    def test_empty_result_title(self):
        from extractors.email_search import _check_title_similarity
        assert _check_title_similarity("Some Title", "") is True

    def test_partial_match(self):
        from extractors.email_search import _check_title_similarity
        t1 = "Machine Learning Methods in Biology"
        t2 = "ML Methods Applied to Biology"
        result = _check_title_similarity(t1, t2)
        assert isinstance(result, bool)


class TestExtractDois:
    def test_standard_doi(self):
        from extractors.email_search import _extract_dois_from_autoref_text
        text = "DOI: 10.1234/test-article"
        dois = _extract_dois_from_autoref_text(text)
        assert "10.1234/test-article" in dois

    def test_url_doi(self):
        from extractors.email_search import _extract_dois_from_autoref_text
        text = "https://doi.org/10.5678/paper"
        dois = _extract_dois_from_autoref_text(text)
        assert "10.5678/paper" in dois

    def test_multiple_dois(self):
        from extractors.email_search import _extract_dois_from_autoref_text
        text = "DOI: 10.1111/a and DOI: 10.2222/b"
        dois = _extract_dois_from_autoref_text(text)
        assert len(dois) == 2

    def test_no_dois(self):
        from extractors.email_search import _extract_dois_from_autoref_text
        text = "No DOIs here"
        dois = _extract_dois_from_autoref_text(text)
        assert len(dois) == 0


class TestFindEmailForPublication:
    def test_empty_pub(self):
        from extractors.email_search import find_email_for_publication
        result = find_email_for_publication(None)
        assert result == {"emails": [], "source": None}

    def test_empty_dict_pub(self):
        from extractors.email_search import find_email_for_publication
        result = find_email_for_publication({})
        assert result == {"emails": [], "source": None}


class TestFindEmailsForPublications:
    def test_empty_list(self):
        from extractors.email_search import find_emails_for_publications
        result = find_emails_for_publications([])
        assert result == []

    def test_none_list(self):
        from extractors.email_search import find_emails_for_publications
        result = find_emails_for_publications(None)
        assert result == []
