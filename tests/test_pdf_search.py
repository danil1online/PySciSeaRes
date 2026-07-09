#!/usr/bin/env python3
"""Тесты поиска PDF-ссылок на HTML-страницах."""
import sys
import os
import http.server
import threading
import pytest
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)
        
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


class TestFindAutorefStrategy4Scoring:
    """Тесты улучшенного фоллбэка (стратегия 4) со скорингом."""

    def test_protocol_falls_to_llm(self, monkeypatch):
        """Когда все кандидаты имеют отрицательный скор, вызывается LLM."""
        def fake_check_pages(url):
            return 5
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        llm_called = []

        def fake_llm(*args, **kwargs):
            llm_called.append(True)
            # LLM возвращает URL автореферата
            return 'https://example.com/avtoref.pdf'

        monkeypatch.setattr('vak_sync.pdf._find_autoref_with_llm', fake_llm)

        # Ни одно слово "автореферат" в тексте — стратегии 1-3 не сработают.
        # "Протокол" и "диссертация" в именах файлов → отрицательный скор → LLM
        html = '''<html><body>
        <p>Дополнительные материалы:</p>
        <a href="https://example.com/протокол_2.pdf">протокол</a>
        <a href="https://example.com/диссертация.pdf">диссертация</a>
        </body></html>'''

        server = MockHTTPServer(19913, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19913/page')
            assert llm_called, "LLM должен быть вызван когда фоллбэк не уверен"
            assert result == 'https://example.com/avtoref.pdf'
        finally:
            server.shutdown()

    def test_autoref_in_filename_wins(self, monkeypatch):
        """PDF с 'автореферат' в имени получает более высокий скор."""
        def fake_check_pages(url):
            return 20
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        # Без LLM — фоллбэк должен выбрать правильный файл сам
        def fake_llm(*args, **kwargs):
            pytest.fail("LLM не должен вызываться — фоллбэк справляется сам")

        monkeypatch.setattr('vak_sync.pdf._find_autoref_with_llm', fake_llm)

        html = '''<html><body>
        <a href="https://example.com/dissertation.pdf">Полный текст</a>
        <a href="https://example.com/avtoref_baronov.pdf">Скачать файл</a>
        </body></html>'''

        server = MockHTTPServer(19914, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19914/page')
            assert 'avtoref' in result, "Должен выбрать файл с авторефератом в имени"
        finally:
            server.shutdown()

    def test_dissertation_scored_down(self, monkeypatch):
        """Файл с 'диссертация' в имени получает отрицательный скор."""
        def fake_check_pages(url):
            return 20
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        # Если диссертация имеет более низкий скор — фоллбэк выбирает другой файл
        html = '''<html><body>
        <a href="https://example.com/materialy.pdf">Ссылка 1</a>
        <a href="https://example.com/текст_диссертации.pdf">Ссылка 2</a>
        </body></html>'''

        server = MockHTTPServer(19915, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19915/page')
            assert 'materialy' in result, "Должен выбрать файл без 'диссертация'"
        finally:
            server.shutdown()

    def test_fio_passed_to_function(self, monkeypatch):
        """ФИО передаётся в функцию — проверяем что не ломает."""
        def fake_check_pages(url):
            return 20
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        html = '''<html><body>
        <a href="https://example.com/avtoref.pdf">Смотреть</a>
        </body></html>'''

        server = MockHTTPServer(19916, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page(
                'http://127.0.0.1:19916/page',
                fio="Иванов И.И.",
                date_defend="2026-06-15"
            )
            assert result is not None
        finally:
            server.shutdown()

    def test_barinov_scenario_many_pdfs(self, monkeypatch):
        """Сценарий Баринава: множество PDF (протоколы, отзывы, диссертация),
        дубликаты URL — должен выбрать автореферат."""
        def fake_check_pages(url):
            if 'avtoref' in url:
                return 36
            if 'protocol' in url:
                return 1
            if 'otziv' in url:
                return 2
            if 'dissert' in url:
                return 200
            return 20
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        # HTML как на странице Баринава: 2 ссылки на автореферат (дубликат),
        # протоколы, отзывы, диссертация
        html = '''<html><body>
        <h3>Автореферат диссертации</h3>
        <a href="https://example.com/protocol_2.pdf">протоколу № 2</a>
        <a href="https://example.com/protocol_8.pdf">протокол № 8</a>
        <a href="https://example.com/conclusion.pdf">Заключение диссертационного совета</a>
        <a href="https://example.com/dissertation_full.pdf">Полный текст</a>
        <a href="https://example.com/dissertation_full.pdf">Диссертация.pdf</a>
        <a href="https://example.com/otziv_konsultant.pdf">Отзыв научного консультанта</a>
        <a href="https://example.com/otziv_konsultant.pdf">Отзыв научного консультанта (2).pdf</a>
        <a href="https://example.com/otziv_opponent_1.pdf">Отзыв 1 официального оппонента</a>
        <a href="https://example.com/otziv_opponent_2.pdf">Отзыв 2 официального оппонента</a>
        <a href="https://example.com/otziv_opponent_3.pdf">Отзыв 3 официального оппонента</a>
        <a href="https://example.com/soglasie_1.pdf">Согласие и сведения 1</a>
        <a href="https://example.com/avtoref.pdf">Автореферат (Дата размещения: 10.02.2026 г.)</a>
        <a href="https://example.com/avtoref.pdf">Автореферат (2).pdf</a>
        <a href="https://example.com/materialy.pdf">Смотреть файл</a>
        </body></html>'''

        server = MockHTTPServer(19917, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page(
                'http://127.0.0.1:19917/page',
                fio="Баринов С.В.",
                date_defend="2026-05-14"
            )
            assert result is not None, "Должен найти автореферат"
            assert 'avtoref' in result, f"Должен выбрать автореферат, получен: {result}"
            assert 'protocol' not in result, f"Не должен выбрать протокол: {result}"
            assert 'otziv' not in result, f"Не должен выбрать отзыв: {result}"
            assert 'dissert' not in result, f"Не должен выбрать диссертацию: {result}"
            assert 'soglasie' not in result, f"Не должен выбрать согласие: {result}"
            assert 'conclusion' not in result, f"Не должен выбрать заключение: {result}"
        finally:
            server.shutdown()

    def test_pdf_without_extension_via_content_type(self, monkeypatch):
        """Ссылка без .pdf расширения, но с текстом 'автореферат' — определяется по Content-Type."""
        # HEAD запрос возвращает application/pdf
        def fake_head(url, **kwargs):
            mock = MagicMock()
            mock.headers = {"Content-Type": "application/pdf"}
            mock.url = url
            return mock

        def fake_check_pages(url):
            return 28

        monkeypatch.setattr('vak_sync.pdf.requests.head', fake_head)
        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        # HTML: ссылка на PDF без .pdf расширения, текст содержит "автореферат"
        html = '''<html><body>
        <p>Зайцева Елена Сергеевна</p>
        <a href="https://example.com/cms_files/p_file/471563267699f417240406">
        Автореферат диссертации Зайцевой Е.С. на соискание ученой степени
        </a>
        </body></html>'''

        server = MockHTTPServer(19920, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page(
                'http://127.0.0.1:19920/page',
                fio="Зайцева Е.С.",
                date_defend="2026-05-13"
            )
            assert result is not None, "Должен найти PDF по Content-Type"
            assert 'p_file/471563267699f417240406' in result
        finally:
            server.shutdown()

    def test_pdf_without_extension_not_pdf(self, monkeypatch):
        """Ссылка без .pdf расширения с текстом 'автореферат', но Content-Type не PDF."""
        def fake_head(url, **kwargs):
            mock = MagicMock()
            mock.headers = {"Content-Type": "text/html"}
            mock.url = url
            return mock

        monkeypatch.setattr('vak_sync.pdf.requests.head', fake_head)

        html = '''<html><body>
        <a href="https://example.com/cms_files/p_file/471563267699f417240406">
        Автореферат диссертации
        </a>
        <a href="https://example.com/avtoref.pdf">Скачать</a>
        </body></html>'''

        def fake_check_pages(url):
            return 20

        monkeypatch.setattr('vak_sync.pdf._check_pdf_page_count', fake_check_pages)

        server = MockHTTPServer(19921, {
            '/page': {'status': 200, 'html': html}
        })
        server.start()

        try:
            result = find_autoref_pdf_from_page('http://127.0.0.1:19921/page')
            assert result is not None
            assert 'avtoref.pdf' in result, "Должен найти PDF с расширением, когда Content-Type не PDF"
        finally:
            server.shutdown()
