#!/usr/bin/env python3
"""Тесты поиска PDF-ссылок на HTML-страницах."""
import sys
import os
import http.server
import threading
from unittest.mock import patch, MagicMock
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_sync import find_autoref_pdf_from_page


class MockHTTPServer:
    """Mock HTTP server for testing."""
    
    def __init__(self, port, routes):
        self.port = port
        self.routes = routes
        self.server = None
        self.thread = None
    
    def start(self):
        routes = self.routes  # Capture in closure
        
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                route = routes.get(self.path, {'status': 404, 'html': '<html><body>Not found</body></html>'})
                status = route.get('status', 200)
                html = route.get('html', '')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))
            
            def log_message(self, format, *args):
                pass
        
        self.server = http.server.HTTPServer(('127.0.0.1', self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    
    def shutdown(self):
        if self.server:
            self.server.shutdown()


class TestFindAutorefPdfFromPage:
    """Тесты поиска PDF-автореферата на странице."""

    def test_strategy1_direct_link_text(self, monkeypatch):
        """Стратегия 1: текст ссылки содержит 'автореферат'."""
        def fake_check_pages(url):
            return 15  # valid page count
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/avtoref.pdf">Автореферат</a>
        </body></html>'''
        
        server = MockHTTPServer(19901, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19901/page')
            assert 'avtoref.pdf' in result
        finally:
            server.shutdown()

    def test_strategy2_context_in_text(self, monkeypatch):
        """Стратегия 2: 'автореферат' в соседнем тексте."""
        def fake_check_pages(url):
            return 25
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <p>Автореферат диссертации: <a href="https://example.com/file.pdf">Посмотреть файл</a></p>
        </body></html>'''
        
        server = MockHTTPServer(19902, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19902/page')
            assert 'file.pdf' in result
        finally:
            server.shutdown()

    def test_strategy3_action_words(self, monkeypatch):
        """Стратегия 3: action слова + PDF."""
        def fake_check_pages(url):
            return 30
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <div>
            <span>Автореферат: </span>
            <a href="https://example.com/download.pdf">Скачать файл</a>
        </div>
        </body></html>'''
        
        server = MockHTTPServer(19903, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19903/page')
            assert 'download.pdf' in result
        finally:
            server.shutdown()

    def test_strategy4_fallback_pdf(self, monkeypatch):
        """Стратегия 4: фоллбэк на любой PDF."""
        def fake_check_pages(url):
            return 40
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/any.pdf">Ссылка</a>
        </body></html>'''
        
        server = MockHTTPServer(19904, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19904/page')
            assert 'any.pdf' in result
        finally:
            server.shutdown()

    def test_no_pdf_found(self, monkeypatch):
        """Нет PDF на странице."""
        def fake_check_pages(url):
            return 10
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/page.html">Страница</a>
        </body></html>'''
        
        server = MockHTTPServer(19905, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19905/page')
            assert result is None
        finally:
            server.shutdown()

    def test_relative_url_resolution(self, monkeypatch):
        """Тест резолва относительных URL."""
        def fake_check_pages(url):
            return 20
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <p>Автореферат: <a href="/files/avtoref.pdf">Скачать</a></p>
        </body></html>'''
        
        server = MockHTTPServer(19906, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19906/page')
            assert result is not None
            assert '127.0.0.1:19906' in result or 'avtoref.pdf' in result
        finally:
            server.shutdown()

    def test_no_links(self, monkeypatch):
        """Нет ссылок на странице."""
        def fake_check_pages(url):
            return 10
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <p>Просто текст</p>
        </body></html>'''
        
        server = MockHTTPServer(19907, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19907/page')
            assert result is None
        finally:
            server.shutdown()

    def test_http_error(self, monkeypatch):
        """HTTP ошибка."""
        def fake_check_pages(url):
            return 10
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '<html><body>Error</body></html>'
        
        server = MockHTTPServer(19908, {
            '/page': {'status': 404, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19908/page')
            # Should return None or handle the error gracefully
            assert result is None or '404' in str(result)
        finally:
            server.shutdown()


class TestFindAutorefPdfFromPageMock:
    """Тесты с моками для сложных сценариев."""

    def test_request_error(self):
        """Ошибка при запросе к странице."""
        with patch('requests.get', side_effect=Exception("Network error")):
            result = find_autoref_pdf_from_page('http://example.com/page')
            assert result is None

    def test_empty_response(self):
        """Пустой ответ."""
        mock_response = MagicMock()
        mock_response.text = ''
        
        with patch('requests.get', return_value=mock_response):
            result = find_autoref_pdf_from_page('http://example.com/page')
            assert result is None

    def test_mixed_urls(self, monkeypatch):
        """Смешанные URL (абсолютные и относительные)."""
        def fake_check_pages(url):
            return 30  # valid
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://external.com/external.pdf">Внешняя</a>
        <a href="/local/local.pdf">Локальная</a>
        </body></html>'''
        
        mock_response = MagicMock()
        mock_response.text = html
        
        with patch('requests.get', return_value=mock_response):
            result = find_autoref_pdf_from_page('http://example.com/page')
            # Should return first PDF found (strategy 4)
            assert result is not None
            assert result.endswith('.pdf')

    def test_large_pdf_skipped(self, monkeypatch):
        """Большой PDF (>50 стр.) пропускается."""
        call_count = [0]
        def fake_check_pages(url):
            call_count[0] += 1
            return 100  # too large
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/avtoref.pdf">Автореферат</a>
        </body></html>'''
        
        server = MockHTTPServer(19909, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19909/page')
            assert result is None, "Большой PDF должен быть пропущен"
        finally:
            server.shutdown()

    def test_alternative_pdf_found(self, monkeypatch):
        """Если первый PDF большой, находится второй."""
        def fake_check_pages(url):
            if 'large' in url:
                return 100  # too large
            return 20  # valid
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/large.pdf">Ссылка 1</a>
        <a href="https://example.com/valid.pdf">Автореферат</a>
        </body></html>'''
        
        server = MockHTTPServer(19910, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19910/page')
            assert 'valid.pdf' in result, "Должен найтись валидный PDF"
        finally:
            server.shutdown()

    def test_boundary_50_pages(self, monkeypatch):
        """PDF ровно 50 страниц — допустимый."""
        def fake_check_pages(url):
            return 50
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/boundary.pdf">Автореферат</a>
        </body></html>'''
        
        server = MockHTTPServer(19911, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19911/page')
            assert 'boundary.pdf' in result, "50 страниц должно быть OK"
        finally:
            server.shutdown()

    def test_boundary_51_pages(self, monkeypatch):
        """PDF 51 страница — пропускается."""
        def fake_check_pages(url):
            return 51
        monkeypatch.setattr('daily_sync._check_pdf_page_count', fake_check_pages)
        
        html = '''<html><body>
        <a href="https://example.com/toobig.pdf">Автореферат</a>
        </body></html>'''
        
        server = MockHTTPServer(19912, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()
        
        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19912/page')
            assert result is None, "51 страница должна быть пропущена"
        finally:
            server.shutdown()
