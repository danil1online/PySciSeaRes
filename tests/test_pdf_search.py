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

    def test_strategy1_direct_link_text(self):
        """Стратегия 1: текст ссылки содержит 'автореферат'."""
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

    def test_strategy2_context_in_text(self):
        """Стратегия 2: 'автореферат' в соседнем тексте."""
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

    def test_strategy3_action_words(self):
        """Стратегия 3: action слова + PDF."""
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

    def test_strategy4_fallback_pdf(self):
        """Стратегия 4: фоллбэк на любой PDF."""
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

    def test_no_pdf_found(self):
        """Нет PDF на странице."""
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

    def test_relative_url_resolution(self):
        """Тест резолва относительных URL."""
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

    def test_no_links(self):
        """Нет ссылок на странице."""
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

    def test_http_error(self):
        """HTTP ошибка."""
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

    def test_mixed_urls(self):
        """Смешанные URL (абсолютные и относительные)."""
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
