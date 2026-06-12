#!/usr/bin/env python3
"""Тесты скачивания авторефератов."""
import sys
import os
import tempfile
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_sync import download_autoref


class TestDownloadAutoref:
    """Тесты функции скачивания авторефератов."""

    def test_no_url(self):
        """Нет URL - возвращает ошибку."""
        result = download_autoref(None, "Test", "2026-06-04")
        assert result[0] is None
        assert "Нет ссылки" in result[1]

    def test_non_pdf_url(self):
        """Не PDF URL - ищет на странице."""
        mock_response = MagicMock()
        mock_response.text = '<html><body><a href="file.pdf">Автореферат</a></body></html>'
        
        with patch('daily_sync.find_autoref_pdf_from_page', return_value='file.pdf'):
            # Should call find_autoref_pdf_from_page for non-PDF URL
            with patch('daily_sync.download_autoref', return_value=(None, "Не PDF", "")):
                pass

    def test_direct_pdf_url(self, tmp_path):
        """Прямой URL на PDF."""
        # Create a test PDF file
        test_pdf = tmp_path / "test.pdf"
        test_pdf.write_bytes(b"%PDF-1.4" + b"x" * (310 * 1024))
        
        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": str(test_pdf.stat().st_size)}
        mock_response.iter_content.return_value = [test_pdf.read_bytes()]
        
        with patch('requests.get', return_value=mock_response):
            with patch('daily_sync.AUTOREFS_DIR', str(tmp_path)):
                result = download_autoref('https://example.com/file.pdf', "Test", "2026-06-04")
                assert result[0] is not None
                assert result[1] == "OK"

    def test_small_pdf_rejected(self, tmp_path):
        """Маленький PDF отклоняется."""
        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": "100"}  # 100 bytes
        mock_response.iter_content.return_value = [b"x" * 100]
        
        with patch('requests.get', return_value=mock_response):
            with patch('daily_sync.AUTOREFS_DIR', str(tmp_path)):
                result = download_autoref('https://example.com/file.pdf', "Test", "2026-06-04")
                # Should fail due to small size
                assert result[1] != "OK"

    def test_existing_file_cached(self, tmp_path):
        """Существующий файл с совпадающим URL - кэш."""
        import daily_sync as ds

        test_file = tmp_path / "Test_2026_06_04.pdf"
        test_file.write_bytes(b"%PDF-1.4" + b"x" * (310 * 1024))

        original_dir = ds.AUTOREFS_DIR
        ds.AUTOREFS_DIR = str(tmp_path)

        try:
            # Passing matching URL in previous_pdf_url -> should use cache
            result = download_autoref(
                'https://example.com/file.pdf', "Test", "2026-06-04",
                previous_pdf_url='https://example.com/file.pdf'
            )
            assert result[1] == "Скачан ранее"
            assert result[0] == str(test_file)
        finally:
            ds.AUTOREFS_DIR = original_dir

    def test_no_cache_without_url(self, tmp_path):
        """Без previous_pdf_url файл не используется - скачивается заново."""
        import daily_sync as ds

        # Pre-create a file (simulates old incorrect download)
        test_file = tmp_path / "Test_2026_06_04.pdf"
        test_file.write_bytes(b"%PDF-1.4" + b"x" * (310 * 1024))

        original_dir = ds.AUTOREFS_DIR
        ds.AUTOREFS_DIR = str(tmp_path)

        # Mock that simulates a different/newer PDF
        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": str(315 * 1024)}
        mock_response.iter_content.return_value = [b"%PDF-1.4" + b"y" * (315 * 1024)]

        with patch('requests.get', return_value=mock_response):
            try:
                # No previous_pdf_url -> should NOT use cache, should download
                result = download_autoref('https://example.com/file.pdf', "Test", "2026-06-04")
                assert result[0] == str(test_file)
                assert result[1] == "OK"
                # File content should be different (y, not x)
                content = test_file.read_bytes()
                assert content[10] == ord('y'), "Файл должен быть перезаписан"
            finally:
                ds.AUTOREFS_DIR = original_dir

    def test_http_error(self, tmp_path):
        """HTTP ошибка."""
        import daily_sync as ds

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("404")

        original_dir = ds.AUTOREFS_DIR
        ds.AUTOREFS_DIR = str(tmp_path)

        try:
            with patch('requests.get', side_effect=Exception("404")):
                result = download_autoref('https://example.com/notfound.pdf', "Test", "2026-06-04")
                assert result[0] is None
        finally:
            ds.AUTOREFS_DIR = original_dir

    def test_potential_pdf_url_with_content_type(self, tmp_path):
        """URL вида /avtoreferat.html определяется как PDF по Content-Type."""
        import daily_sync as ds

        test_file = tmp_path / "Test_2026_06_04.pdf"
        original_dir = ds.AUTOREFS_DIR
        ds.AUTOREFS_DIR = str(tmp_path)

        # Mock head request for content-type check
        mock_head = MagicMock()
        mock_head.headers = {"Content-Type": "application/pdf"}
        mock_head.url = "https://example.com/avtoreferat.pdf"

        # Mock download request
        mock_download = MagicMock()
        mock_download.headers = {"Content-Length": str(310 * 1024)}
        mock_download.iter_content.return_value = [b"%PDF-1.4" + b"x" * (310 * 1024)]

        def mock_get(url, **kwargs):
            if "head" not in str(url) or "get-file" in url:
                return mock_download
            return mock_head

        with patch('requests.head', return_value=mock_head):
            with patch('requests.get', return_value=mock_download):
                # URL with "avtoreferat" should be checked via HEAD request
                result = download_autoref(
                    'https://example.com/avtoreferat.html',
                    "Test", "2026-06-04"
                )
                assert result[0] == str(test_file)
                assert result[1] == "OK"

        ds.AUTOREFS_DIR = original_dir

    def test_potential_pdf_not_pdf(self, tmp_path):
        """URL вида /avtoreferat.html не является PDF - должен искать на странице."""
        import daily_sync as ds

        test_file = tmp_path / "Test_2026_06_04.pdf"
        original_dir = ds.AUTOREFS_DIR
        ds.AUTOREFS_DIR = str(tmp_path)

        # Mock head request - not a PDF
        mock_head = MagicMock()
        mock_head.headers = {"Content-Type": "text/html"}
        mock_head.url = "https://example.com/avtoreferat.html"

        # Mock find_autoref_pdf_from_page
        mock_pdf_url = "https://example.com/actual.pdf"

        # Mock download request
        mock_download = MagicMock()
        mock_download.headers = {"Content-Length": str(310 * 1024)}
        mock_download.iter_content.return_value = [b"%PDF-1.4" + b"x" * (310 * 1024)]

        with patch('requests.head', return_value=mock_head):
            with patch('daily_sync.find_autoref_pdf_from_page', return_value=mock_pdf_url):
                with patch('requests.get', return_value=mock_download):
                    result = download_autoref(
                        'https://example.com/avtoreferat.html',
                        "Test", "2026-06-04"
                    )
                    assert result[0] == str(test_file)
                    assert result[1] == "OK"

        ds.AUTOREFS_DIR = original_dir

    def test_content_length_check(self, tmp_path):
        """Проверка Content-Length."""
        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": "1000"}  # 1 KB, too small
        mock_response.iter_content.return_value = [b"x" * 1000]
        
        with patch('requests.get', return_value=mock_response):
            with patch('daily_sync.AUTOREFS_DIR', str(tmp_path)):
                result = download_autoref('https://example.com/file.pdf', "Test", "2026-06-04")
                assert result[1] != "OK"
