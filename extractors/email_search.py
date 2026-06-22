"""Поиск email авторов публикаций по открытым источникам."""

import re
import os
import time
import json
import requests
import pdfplumber
import io
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from config import (
    LLM_API_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
)
from rate_limiter import (
    DDG_LIMITER, SEMANTIC_SCHOLAR_LIMITER, CROSSREF_LIMITER, DOI_LIMITER,
)
from logging_config import get_logger

logger = get_logger("email_search")

SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_API = "https://api.crossref.org/works"
DOI_RESOLVER = "https://doi.org/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def _normalize_title(title):
    """Нормализует заголовок для поиска."""
    if not title:
        return ""
    t = title[:100]
    t = re.sub(r'[^\w\s\-]', ' ', t)
    t = re.sub(r'\s+', '+', t)
    return t


def _titles_match(title1, title2):
    """Проверяет совпадение заголовков."""
    def normalize(t):
        t = t.lower()
        t = re.sub(r'[^\w\s]', ' ', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    n1 = normalize(title1)
    n2 = normalize(title2)
    if not n1 or not n2:
        return False

    words1 = set(n1.split())
    words2 = set(n2.split())

    if len(words1) < 3 or len(words2) < 3:
        return n1 == n2

    overlap = len(words1 & words2)
    ratio = overlap / min(len(words1), len(words2))
    return ratio >= 0.4


def _build_ddg_query(pub, suffix=""):
    """Собирает запрос для DuckDuckGo из полных данных публикации."""
    title = pub.get('title', '')
    authors = pub.get('authors', '')
    journal = pub.get('journal', '')
    year = pub.get('year', '')
    pages = pub.get('pages', '')

    parts = []
    if authors:
        parts.append(authors)
    if title:
        parts.append(title)
    if journal:
        parts.append(journal)
    if year:
        parts.append(str(year))
    if pages:
        parts.append(str(pages))

    query = " ".join(parts)
    if suffix:
        query += " " + suffix
    return query


def _search_duckduckgo(pub, max_results=10):
    """Ищет публикацию через DuckDuckGo полными данными публикации."""
    query = _build_ddg_query(pub)
    # Rate limiting
    if not DDG_LIMITER.allow():
        time.sleep(DDG_LIMITER.wait_time())
    try:
        import ddgs
        with ddgs.DDGS() as ddgs:
            results = list(ddgs.text(query, region='ru-ru', max_results=max_results))
        parsed = []
        for r in results:
            parsed.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "body_text": r.get("body", "") or "",
            })
        return parsed
    except ImportError:
        pass
    except Exception:
        pass
    return []


def _is_pdf_url(url):
    """Проверяет, является ли URL ссылкой на PDF."""
    if not url:
        return False
    lower = url.lower()
    return lower.endswith('.pdf') or '/download/' in lower or '/pdf/' in lower or '/files/' in lower


def _is_journal_domain(url):
    """Проверяет, является ли домен научным/редакционным."""
    if not url:
        return False
    domain = urlparse(url).hostname or ""
    domain = domain.lower()

    blocked_patterns = [
        'wikipedia', 'wiktionary', 'wiki', 'wikimedia',
        'baidu', 'zhihu', 'quora', 'reddit',
        'mathway', 'wolfram', 'calculator', 'convert',
        'avanza', 'vorsorgeforum', 'clinicplusapp', 'allsparks',
        'youtube', 'vk.com', 'facebook', 'twitter', 't.me',
        'slader', 'coursehero', 'studocu', 'chegg',
        'drive.google', 'docs.google', 'dropbox',
        'kwork.ru', 'fb.ru', 'investfuture', 'peredelka38',
        'methodological_terms', 'ht-lab', 'mksegment',
        'kartaslov', 'investfuture', 'if',
    ]
    for pat in blocked_patterns:
        if pat in domain:
            return False

    # Разрешаем научные платформы (не агрегаторы, а источники статей)
    allowed_platforms = {
        'cyberleninka.ru', 'www.cyberleninka.ru',
        'elibrary.ru', 'www.elibrary.ru',
    }
    if domain in allowed_platforms:
        return True

    aggregator_domains = {
        'e-library.ru', 'ncbi.nlm.nih.gov',
    }
    if domain in aggregator_domains:
        return False

    return True


def _check_title_similarity(pub_title, result_title):
    """Проверяет совпадение заголовков с учётом русских научных статей."""
    if not result_title:
        return True
    if _titles_match(pub_title, result_title):
        return True
    # Мягкая проверка: хотя бы 2 общих слова
    pub_words = set(pub_title.lower().split())
    result_words = set(result_title.lower().split())
    overlap = len(pub_words & result_words)
    # Для длинных заголовков — нужно больше совпадений
    min_overlap = 3 if len(pub_words) > 5 else 2
    return overlap >= min_overlap


def _analyze_ddg_link(result, pub_title, pub_authors, pub_year):
    """Анализирует одну ссылку из DuckDuckGo.

    Возвращает source dict или None.
    """
    url = result.get("url", "")
    if not url:
        return None

    if not _is_journal_domain(url):
        return None

    result_title = result.get("title", "")
    is_pdf = _is_pdf_url(url)

    if is_pdf:
        # Для PDF — проверяем совпадение заголовка
        if result_title and not _check_title_similarity(pub_title, result_title):
            return None
        return {
            'source_type': 'pdf',
            'url': url,
            'title': result_title or pub_title,
            'authors': pub_authors,
            'source_name': 'DuckDuckGo (PDF)',
        }

    # HTML — проверяем совпадение заголовка, ищем PDF на странице
    if result_title and not _check_title_similarity(pub_title, result_title):
        return None
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        pdf_url = _find_pdf_url_from_html(url)
        if pdf_url:
            return {
                'source_type': 'pdf',
                'url': pdf_url,
                'title': result_title or pub_title,
                'authors': pub_authors,
                'source_name': 'DuckDuckGo (PDF)',
            }

        return {
            'source_type': 'html',
            'url': url,
            'title': result_title or pub_title,
            'authors': pub_authors,
            'source_name': 'DuckDuckGo (HTML)',
        }
    except Exception:
        return None


def _search_semantic_scholar(title, author="", year=None, limit=5):
    """Ищет публикацию через Semantic Scholar API."""
    query = _normalize_title(title)
    if not query:
        return []

    # Rate limiting
    if not SEMANTIC_SCHOLAR_LIMITER.allow():
        time.sleep(SEMANTIC_SCHOLAR_LIMITER.wait_time())

    params = {
        "query": query,
        "limit": limit,
        "fields": "title,authors,year,externalIds,url,openAccessPdf",
    }
    if year:
        params["year"] = str(year)

    try:
        r = requests.get(SEMANTIC_SCHOLAR_API, params=params, timeout=15,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            data = r.json()
            return data.get("data", [])
    except Exception:
        pass
    return []


def _search_crossref(title, author="", year=None, limit=3):
    """Ищет публикацию через Crossref API."""
    query = _normalize_title(title)
    if not query:
        return []

    # Rate limiting
    if not CROSSREF_LIMITER.allow():
        time.sleep(CROSSREF_LIMITER.wait_time())

    params = {
        "query.title": query,
        "rows": limit,
        "select": "title,DOI,author,link,container-title",
    }

    if author:
        fam_match = re.match(r'([А-ЯЁA-Z][а-яёa-z]+)', author)
        if fam_match:
            params["query.author"] = fam_match.group(1)

    if year:
        params["filter"] = f"from-pub-date:{year}"

    try:
        r = requests.get(CROSSREF_API, params=params, timeout=15)
        if r.status_code == 200:
            items = r.json().get("message", {}).get("items", [])
            results = []
            for item in items:
                results.append({
                    "title": item.get("title", [""])[0],
                    "DOI": item.get("DOI"),
                    "authors": item.get("author", []),
                    "year": item.get("published-print", {}).get("date-parts", [[0]])[0][0]
                            or item.get("created", {}).get("date-parts", [[0]])[0][0],
                    "links": item.get("link", []),
                    "journal": (item.get("container-title", []) or [""])[0],
                })
            return results
    except Exception:
        pass
    return []


def _resolve_doi(doi):
    """Разрешает DOI через DOI resolver."""
    if not doi:
        return None
    # Rate limiting
    if not DOI_LIMITER.allow():
        time.sleep(DOI_LIMITER.wait_time())
    url = f"{DOI_RESOLVER}{doi}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15,
                         allow_redirects=True)
        if r.status_code == 200:
            return {"resolved_url": r.url, "status": r.status_code}
    except Exception:
        pass
    return None


def _try_cyberleninka_pdf(html_url):
    """Пытается получить PDF с cyberleninka.ru по шаблону URL + /pdf."""
    if not html_url or 'cyberleninka.ru' not in html_url:
        return None
    # cyberleninka: https://cyberleninka.ru/article/n/... -> https://cyberleninka.ru/article/n/.../pdf
    if html_url.endswith('/pdf'):
        return html_url
    return html_url.rstrip('/') + '/pdf'


def _find_pdf_url_from_html(html_url):
    """Извлекает URL PDF из HTML-страницы журнала."""
    if not html_url:
        return None

    domain = urlparse(html_url).hostname or ""
    
    # Специальная обработка cyberleninka.ru
    if 'cyberleninka.ru' in domain:
        pdf_url = _try_cyberleninka_pdf(html_url)
        if pdf_url:
            try:
                r = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=30)
                if r.status_code == 200 and 'application/pdf' in r.headers.get("Content-Type", ""):
                    return pdf_url
            except Exception:
                pass
        return None

    try:
        r = requests.get(html_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        # Паттерны OJS (Open Journal Systems) — /article/view/XXX/YYY
        ojs_patterns = ['/article/view/', '/article/download/', '/article/viewfile/']
        
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            
            if "pdf" in text.lower() or "скачать" in text.lower() or "download" in text.lower():
                full_url = urljoin(html_url, href)
                # Расширяем проверку: .pdf, download, files, OJS-паттерны, viewfile
                if (".pdf" in href.lower() or 
                    "download" in href.lower() or 
                    "/files/" in href.lower() or
                    "/viewfile/" in href.lower() or
                    any(p in href.lower() for p in ojs_patterns)):
                    return full_url

        patterns = [
            r'["\x27](https?://[^\s"\x27]+\.pdf)',
            r'["\x27](/article/download/[^\s"\x27]+)',
            r'["\x27](/files/article/[^\s"\x27]+\.pdf)',
        ]
        for pattern in patterns:
            match = re.search(pattern, r.text)
            if match:
                url_candidate = match.group(1)
                if url_candidate.startswith("http"):
                    domain_match = re.match(r'https?://([^/]+)', url_candidate)
                    if domain_match:
                        domain = domain_match.group(1)
                        domain_check = domain.replace('.pdf', '')
                        if '.' not in domain_check:
                            return urljoin(html_url, '/' + domain + url_candidate[len(domain_match.group(0)):])
                    return url_candidate
                return urljoin(html_url, url_candidate)
    except Exception:
        pass
    return None


def _extract_dois_from_autoref_text(text):
    """Извлекает все DOI из текста автореферата."""
    doi_patterns = [
        r'doi[.:]\s*(10\.\d+/[^\s\n]+)',
        r'https?://doi\.org/(10\.\d+/[^\s\n]+)',
        r'DOI:\s*(10\.\d+/[^\s\n]+)',
    ]

    dois = []
    for pattern in doi_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for doi in matches:
            doi = doi.strip().rstrip('.;,')
            if doi and doi not in dois:
                dois.append(doi)

    return dois


def _find_source_for_publication(pub, autoref_text=None):
    """Ищет источник для публикации.

    Приоритет:
    1. DOI из текста публикации
    2. URL из текста публикации
    3. DOI из текста автореферата
    4. DuckDuckGo (поиск по полным данным, анализ первых 10 ссылок)
    5. Semantic Scholar
    6. Crossref
    7. DOI resolver

    Возвращает: {source_type, url, title, authors, doi, source_name} или None.
    """
    pub_title = pub.get('title', '')
    pub_authors = pub.get('authors', '')
    pub_doi = pub.get('doi')
    pub_url = pub.get('url')
    pub_year = pub.get('year')

    if isinstance(pub_authors, list):
        pub_authors = ", ".join(pub_authors)

    # 1. DOI из текста публикации
    if pub_doi:
        resolved = _resolve_doi(pub_doi)
        if resolved and resolved.get('status') == 200:
            return {'source_type': 'html', 'url': resolved['resolved_url'],
                    'title': pub_title, 'authors': pub_authors, 'doi': pub_doi,
                    'source_name': 'DOI resolver'}

    # 2. URL из текста публикации
    if pub_url:
        return {'source_type': 'html', 'url': pub_url,
                'title': pub_title, 'authors': pub_authors, 'doi': pub_doi,
                'source_name': 'автореферат'}

    # 3. Ищем DOI в тексте автореферата
    if autoref_text and not pub_doi:
        dois = _extract_dois_from_autoref_text(autoref_text)
        for doi in dois:
            resolved = _resolve_doi(doi)
            if resolved and resolved.get('status') == 200:
                resolved_title = resolved.get('resolved_title', '')
                if resolved_title and _titles_match(pub_title, resolved_title):
                    return {'source_type': 'html', 'url': resolved['resolved_url'],
                            'title': pub_title, 'authors': pub_authors, 'doi': doi,
                            'source_name': 'DOI из автореферата'}

    # 4. DuckDuckGo — поиск по полным данным публикации
    ddg_results = _search_duckduckgo(pub, max_results=10)
    if ddg_results:
        for ddg in ddg_results:
            source = _analyze_ddg_link(ddg, pub_title, pub_authors, pub_year)
            if source:
                return source

    # 5. Semantic Scholar
    ss_results = _search_semantic_scholar(pub_title, pub_authors, pub_year)
    if ss_results:
        for ss in ss_results:
            ss_title = ss.get("title", "")
            if _titles_match(pub_title, ss_title):
                result = {
                    'title': ss_title,
                    'authors': ", ".join(
                        f"{a.get('given','')} {a.get('family','')}"
                        for a in ss.get("authors", [])
                    ),
                    'doi': ss.get("externalIds", {}).get("DOI"),
                    'source_name': 'Semantic Scholar',
                }
                pdf_info = ss.get("openAccessPdf")
                if pdf_info and pdf_info.get("url"):
                    result['source_type'] = 'pdf'
                    result['url'] = pdf_info['url']
                    return result
                if ss.get("url"):
                    result['source_type'] = 'html'
                    result['url'] = ss['url']
                    pdf_url = _find_pdf_url_from_html(ss['url'])
                    if pdf_url:
                        result['source_type'] = 'pdf'
                        result['url'] = pdf_url
                    return result

    # 6. Crossref
    cr_results = _search_crossref(pub_title, pub_authors, pub_year)
    if cr_results:
        for cr in cr_results:
            cr_title = cr.get("title", "")
            if _titles_match(pub_title, cr_title):
                doi = cr.get("DOI")
                result = {
                    'title': cr_title,
                    'authors': ", ".join(
                        f"{a.get('given','')} {a.get('family','')}"
                        for a in cr.get("authors", [])
                    ),
                    'doi': doi,
                    'source_name': 'Crossref',
                }
                if doi:
                    resolved = _resolve_doi(doi)
                    if resolved and resolved.get('status') == 200:
                        result['source_type'] = 'html'
                        result['url'] = resolved['resolved_url']
                        return result
                for link in cr.get("links", []):
                    if "pdf" in link.get("url", "").lower():
                        result['source_type'] = 'pdf'
                        result['url'] = link['url']
                        return result

    return None


def _fetch_pdf_first_page(pdf_url):
    """Скачивает PDF и извлекает текст первой страницы."""
    if not pdf_url:
        return None
    try:
        r = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=60)
        if r.status_code != 200:
            return None

        pdf = pdfplumber.open(io.BytesIO(r.content))
        text = pdf.pages[0].extract_text() or ""
        pdf.close()
        return text
    except Exception:
        return None


def _fetch_pdf_last_page(pdf_url):
    """Скачивает PDF и извлекает текст последней страницы."""
    if not pdf_url:
        return None
    try:
        r = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=60)
        if r.status_code != 200:
            return None

        pdf = pdfplumber.open(io.BytesIO(r.content))
        text = pdf.pages[-1].extract_text() or ""
        pdf.close()
        return text
    except Exception:
        return None


def _fetch_source_text(source):
    """Извлекает текст первой страницы из источника."""
    source_type = source.get('source_type', 'html')
    url = source.get('url', '')

    if source_type == 'pdf':
        return _fetch_pdf_first_page(url)
    else:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                return None

            soup = BeautifulSoup(r.text, "lxml")
            pdf_url = _find_pdf_url_from_html(url)
            if pdf_url:
                return _fetch_pdf_first_page(pdf_url)

            for script in soup(["script", "style"]):
                script.decompose()

            text = soup.get_text(separator="\n")
            lines = text.split("\n")
            relevant = []
            for line in lines:
                line = line.strip()
                if line and len(line) > 2:
                    relevant.append(line)
                    if len("\n".join(relevant)) > 5000:
                        break

            return "\n".join(relevant)
        except Exception:
            return None


def _is_editorial_email(email, page_text):
    """Проверяет, является ли email email редакции/платформы, а не автора."""
    if not email or not page_text:
        return False

    email_lower = email.lower()
    local_part = email_lower.split('@')[0] if '@' in email_lower else email_lower
    domain = email_lower.split('@')[-1] if '@' in email_lower else ''

    # Редакционные/сервисные префиксы — точно не автор
    editorial_prefixes = [
        'info', 'support', 'editorial', 'redaction', 'admin', 'post',
        'mail', 'helpdesk', 'noreply', 'no-reply', 'webmaster',
        'subscription', 'subscribe', 'help', 'service', 'office',
        'contact', 'secretariat', 'sekretariat', 'otdel', 'rej',
        'redaktor', 'publisher', 'journal',
        'vestnik', 'reforma-knd', 'knd', 'red', 'press',
        'media', 'news', 'edit', 'main', 'org',
        'anna', 'info2', 'red2',
    ]
    if local_part in editorial_prefixes:
        return True

    # Проверяем, начинается ли local_part с редакционного префикса
    for prefix in editorial_prefixes:
        if local_part.startswith(prefix + '-') or local_part.startswith(prefix + '_'):
            return True
        if local_part == prefix:
            return True

    # Домены платформ — точно не email авторов
    editorial_domains = [
        'elibrary.ru', 'cyberleninka.ru', 'ncbi.nlm.nih.gov',
        'e-library.ru',
    ]
    for ed_domain in editorial_domains:
        if domain == ed_domain or domain.endswith('.' + ed_domain):
            return True

    # Контекстная проверка: если email рядом со словами редакции — это не автор
    page_lower = page_text.lower()
    idx = page_lower.find(email_lower)
    if idx == -1:
        return False

    context = page_lower[max(0, idx - 300):idx + len(email_lower) + 300]

    editorial_words = [
        'редакция', 'journal', 'подписка', 'издатель', 'publisher',
        'editorial', 'editor-in-chief', 'главный редактор',
        'редакционная коллег', 'board of editors',
        'журнал', 'издание', 'periodical',
        'редакционный совет', 'editorial board',
    ]
    for word in editorial_words:
        if word in context:
            author_words = ['e-mail', 'email', 'университет', 'институт',
                          'correspondence', 'для связи', 'contact',
                          'author', 'автор', 'адрес', 'address',
                          'доцент', 'профессор', 'к.т.н', 'д.т.н',
                          'аспирант', 'студент', 'dr.', 'ph.d',
                          'кандидат', 'доктор']
            has_author_context = any(aw in context for aw in author_words)
            if not has_author_context:
                return True

    return False


def _find_email_with_llm(page_text):
    """Ищет email автора через LLM."""
    system_prompt = (
        "Ты — эксперт по извлечению контактной информации авторов из научных статей. "
        "Твоя задача — найти email(ы) КОНКРЕТНОГО автора(ов) статьи. "
        "НЕ возвращай email редакции журнала, НЕ возвращай email платформы (elibrary, cyberleninka), "
        "НЕ возвращай email издательства."
    )

    user_prompt = f"""Найди email автора(ов) на первой странице научной статьи.

ВАЖНЕЙШЕЕ правило: верни email ИМЕННО автора статьи, а НЕ журнала/платформы.

Где искать (приоритет):
1. Сразу под именем автора — "e-mail: name@domain.ru"
2. В сносках у фамилии: "Иванов И.И.1* ... 1 Университет, email@uni.ru"
3. Раздел "Контактная информация", "Correspondence", "Для связи"

Форматы:
- "e-mail: author@domain.ru"
- "E-mail: author@domain.ru"
- "Email: author@domain.ru"

Отбрасывай:
- Email на доменах elibrary.ru, cyberleninka.ru, jcement.ru, vestnik.*, journal-доменах
- Email рядом со словами "редакция", "журнал", "подписка", "издатель"

Если найден email автора — верни ТОЛЬКО его.
Если не найден — верни ТОЛЬКО: NOT_FOUND
Без комментариев.

Статья:
{page_text[:4000]}
"""

    try:
        r = requests.post(
            LLM_API_URL,
            headers={"Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 100,
                "temperature": 0.1,
            },
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json()["choices"][0]["message"]["content"].strip()
        return result
    except Exception:
        return None


def find_email_regex(page_text):
    """Быстрый поиск email через regex."""
    emails = EMAIL_REGEX.findall(page_text)
    return emails[0] if emails else None


def _search_page_for_email(page_text, pub_authors):
    """Ищет email в тексте страницы (LLM + regex)."""
    if not page_text:
        return []

    # LLM поиск
    llm_result = _find_email_with_llm(page_text)
    if llm_result and llm_result != "NOT_FOUND":
        emails = EMAIL_REGEX.findall(llm_result)
        if emails:
            for email in emails:
                if not _is_editorial_email(email, page_text):
                    return [email]

    # Regex fallback
    all_emails = EMAIL_REGEX.findall(page_text)
    for email in all_emails:
        if not _is_editorial_email(email, page_text):
            return [email]

    return []


def find_email_for_publication(pub, autoref_text=None):
    """Ищет email для одной публикации.

    Для PDF — ищет на первой и последней странице.
    Возвращает: {emails: [...], source: {...}} или {emails: [], source: None}
    """
    pub_title = pub.get('title', '')[:50] if pub else ''
    logger.debug(f"Searching email for publication: {pub_title}...")
    if not pub:
        return {'emails': [], 'source': None}

    source = _find_source_for_publication(pub, autoref_text)
    if not source:
        return {'emails': [], 'source': None}

    pub_authors = pub.get('authors', '')
    source_type = source.get('source_type', 'html')

    # Для PDF — проверяем первую и последнюю страницу
    if source_type == 'pdf':
        pdf_url = source.get('url', '')
        first_page = _fetch_pdf_first_page(pdf_url)
        if first_page:
            emails = _search_page_for_email(first_page, pub_authors)
            if emails:
                return {'emails': emails, 'source': source}
        
        last_page = _fetch_pdf_last_page(pdf_url)
        if last_page:
            emails = _search_page_for_email(last_page, pub_authors)
            if emails:
                return {'emails': emails, 'source': source}

        return {'emails': [], 'source': source}

    # Для HTML — извлекаем текст и ищем email
    page_text = _fetch_source_text(source)
    if not page_text:
        return {'emails': [], 'source': source}

    emails = _search_page_for_email(page_text, pub_authors)
    if emails:
        return {'emails': emails, 'source': source}

    return {'emails': [], 'source': source}


def find_emails_for_publications(publications, autoref_text=None):
    """Ищет email для списка публикаций.

    Возвращает список: [(pub_data, email_list, source_info), ...]
    """
    if not publications:
        return []

    logger.info(f"Searching emails for {len(publications)} publications")
    results = []
    for i, pub in enumerate(publications):
        try:
            result = find_email_for_publication(pub, autoref_text)
            emails_found = len(result['emails'])
            if emails_found:
                logger.debug(f"Publication {i+1}: found {emails_found} email(s)")
            results.append((pub, result['emails'], result['source']))
            time.sleep(1)
        except Exception:
            results.append((pub, [], None))

    return results
