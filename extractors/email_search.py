"""Поиск email авторов публикаций по открытым источникам."""

import re
import os
import time
import json
import requests
import pdfplumber
import io
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from config import (
    LLM_API_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
)

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


def _search_semantic_scholar(title, author="", year=None, limit=5):
    """Ищет публикацию через Semantic Scholar API."""
    query = _normalize_title(title)
    if not query:
        return []

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

    params = {
        "query.title": query,
        "rows": limit,
        "select": "title,DOI,author,link,container-title",
    }

    if author:
        # Extract last name from author string (Russian format: "Фамилия, И.О.")
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
    url = f"{DOI_RESOLVER}{doi}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15,
                         allow_redirects=True)
        if r.status_code == 200:
            return {"resolved_url": r.url, "status": r.status_code}
    except Exception:
        pass
    return None


def _find_pdf_url_from_html(html_url):
    """Извлекает URL PDF из HTML-страницы журнала."""
    if not html_url:
        return None
    try:
        r = requests.get(html_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        # Ищем ссылки с "PDF" в тексте
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if "pdf" in text.lower():
                full_url = urljoin(html_url, href)
                if ".pdf" in href.lower() or "download" in href.lower() or "/files/" in href.lower():
                    return full_url

        # Ищем паттерны в сыром HTML
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
    # Паттерны для DOI
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

    Возвращает: {source_type, url, title, authors, doi, source_name} или None.
    source_name - название источника (Semantic Scholar, Crossref, DOI resolver, autoref)
    """
    pub_title = pub.get('title', '')
    pub_authors = pub.get('authors', '')
    pub_doi = pub.get('doi')
    pub_url = pub.get('url')
    pub_year = pub.get('year')
    
    # Convert authors list to string if needed
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
            # Проверяем, относится ли DOI к этой публикации (по году или авторам)
            resolved = _resolve_doi(doi)
            if resolved and resolved.get('status') == 200:
                # Проверяем совпадение по заголовку
                resolved_title = resolved.get('resolved_title', '')
                if resolved_title and _titles_match(pub_title, resolved_title):
                    return {'source_type': 'html', 'url': resolved['resolved_url'],
                            'title': pub_title, 'authors': pub_authors, 'doi': doi,
                            'source_name': 'DOI из автореферата'}

    # 4. Semantic Scholar
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

    # 5. Crossref
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


def _find_email_with_llm(page_text):
    """Ищет email через LLM."""
    prompt = f"""Найди email автора(ов) на первой странице научной статьи.

Форматы email в российских статьях:
- author1, email1@domain.ru; author2, email2@domain.ru
- 1 Организация, email@domain.ru
- Correspondence: email@domain.ru
- Для связи: email@domain.ru
- E-mail: email@domain.ru

Правила:
1. Ищи email под заголовком и авторами, в сносках (1, 2, 2*), в разделе контактов.
2. Email обычно заканчивается на .ru, .com, .org, .edu
3. Если email найден — верни ТОЛЬКО первый email адрес.
4. Если email не найден — верни ТОЛЬКО: NOT_FOUND
5. Не добавляй никакого другого текста.

Страница статьи:
{page_text[:4000]}
"""

    try:
        r = requests.post(
            LLM_API_URL,
            headers={"Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "Ты — помощник по извлечению email из научных статей."},
                    {"role": "user", "content": prompt},
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


def find_email_for_publication(pub, autoref_text=None):
    """Ищет email для одной публикации.

    Возвращает: {emails: [...], source: {...}} или {emails: [], source: None}
    """
    if not pub:
        return {'emails': [], 'source': None}

    source = _find_source_for_publication(pub, autoref_text)
    if not source:
        return {'emails': [], 'source': None}

    page_text = _fetch_source_text(source)
    if not page_text:
        return {'emails': [], 'source': source}

    # LLM поиск
    llm_result = _find_email_with_llm(page_text)
    if llm_result and llm_result != "NOT_FOUND":
        emails = EMAIL_REGEX.findall(llm_result)
        if emails:
            return {'emails': emails, 'source': source}

    # Regex fallback
    regex_email = find_email_regex(page_text)
    if regex_email:
        return {'emails': [regex_email], 'source': source}

    return {'emails': [], 'source': source}


def find_emails_for_publications(publications, autoref_text=None):
    """Ищет email для списка публикаций.

    Возвращает список: [(pub_data, email_list, source_info), ...]
    email_list — список найденных email для каждой публикации.
    source_info — информация об источнике или None.
    """
    if not publications:
        return []

    results = []
    for i, pub in enumerate(publications):
        try:
            result = find_email_for_publication(pub, autoref_text)
            results.append((pub, result['emails'], result['source']))
            time.sleep(1)  # Пауза между запросами
        except Exception:
            results.append((pub, [], None))

    return results
