#!/usr/bin/env python3
"""
Скрипт поиска email авторов публикаций по открытым источникам.

Для каждой публикации из автореферата:
1. Извлекает публикации из текста PDF
2. Ищет публикацию в открытых источниках (Semantic Scholar, Crossref, DOI)
3. Скачивает PDF/HTML страницы
4. Извлекает текст первой страницы
5. Ищет email с помощью LLM

Использует LLM: http://195.133.13.56:1234/v1/chat/completions (qwen3.5-4b)
"""

import re
import os
import sys
import json
import time
import requests
import pdfplumber
import io
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin

from config import LLM_API_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE

# ============================================================
# Конфигурация
# ============================================================

PDF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Avtoreferat(316).pdf")
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_API = "https://api.crossref.org/works"
CYBERLENINKA_API = "https://cyberleninka.ru/search/json"
DOI_RESOLVER = "https://doi.org/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


# ============================================================
# 1. Извлечение и нормализация текста из PDF
# ============================================================

def normalize_pdf_text(pdf_path):
    """Извлекает текст из последних 5 страниц PDF и нормализует."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            start_page = max(0, len(pdf.pages) - 5)
            raw_pages = []
            for i in range(start_page, len(pdf.pages)):
                text = pdf.pages[i].extract_text() or ""
                raw_pages.append(text)

        full_text = "\n\n".join(raw_pages)
        print(f"Извлечено {len(raw_pages)} страниц, {len(full_text)} символов")

        # Заменяем все одиночные \n на пробелы
        full_text = re.sub(r'\n+', ' ', full_text)

        # Склеиваем URL, НО НЕ DOI
        full_text = re.sub(r'(https?://(?!doi\.org/)[^\s]+?)\s*-\s+(\S)', r'\1-\2', full_text)
        full_text = re.sub(r'(https?://(?!doi\.org/)[^\s]+?)\s+(\S)', r'\1\2', full_text)

        # Для DOI: убираем пробел после doi.org/
        full_text = re.sub(r'(doi\.org/)\s+(10\.\d+)', r'\1\2', full_text)

        # Убираем пробел перед дефисом в диапазонах страниц: 385- 396 → 385-396
        full_text = re.sub(r'(\d)\s+(-\s+\d)', r'\1\2', full_text)

        # Убираем лишние пробелы
        full_text = re.sub(r'  +', ' ', full_text)

        # Разделяем DOI от следующего номера публикации: 404 4. → 404 4.
        full_text = re.sub(r'(10\.\d+/[^\s]+?)(\d+[\.\)]\s)', r'\1 \2', full_text)

        return full_text
    except Exception as e:
        print(f"Ошибка чтения PDF: {e}")
        return None


# ============================================================
# 2. Парсинг публикаций из нормализованного текста
# ============================================================

def parse_publications(text):
    """Парсит список публикаций из нормализованного текста."""
    publications = []

    # Находим начало раздела публикаций
    markers = [
        'СПИСОК ОПУБЛИКОВАННЫХ ПО ТЕМЕ ДИССЕРТАЦИИ РАБОТ',
        'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ ПО ТЕМЕ ДИССЕРТАЦИИ',
        'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ',
        'СПИСОК ПУБЛИКАЦИЙ',
    ]
    start_idx = -1
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0:
            if start_idx < 0 or idx < start_idx:
                start_idx = idx

    if start_idx < 0:
        print("  Раздел публикаций не найден")
        return []

    section = text[start_idx:]
    print(f"  Раздел публикаций: {len(section)} символов")

    # Разделяем по: номер + точка/скобка + пробел + не-цифра
    # Гляда Ahead: следующий номер или конец строки
    pattern = r'(\d+)[\.\)]\s+([^0-9][^\n]*?)(?=(?:\s+\d[\.\)]\s)|$)'
    matches = list(re.finditer(pattern, section, re.DOTALL))

    for m in matches:
        num = int(m.group(1))
        content = m.group(2).strip()

        # Фильтры
        if num > 20:
            continue
        if len(content) < 20:
            continue
        if content.startswith('В журналах') or content.startswith('Статьи и материалы'):
            continue
        if content.startswith('https://doi.org/'):
            continue
        # Ослоки предыдущей публикации (начинаются с тире или "С.")
        if content.startswith('–') or content.startswith('С.'):
            if publications:
                publications[-1] = (publications[-1][0], publications[-1][1] + ' ' + content)
            continue

        publications.append((num, content))

    print(f"  Распознано публикаций: {len(publications)}")

    # Парсим каждую публикацию в структурированный формат
    result = []
    for num, content in publications:
        pub = parse_single_publication(num, content)
        result.append(pub)

    return result


def parse_single_publication(num, content):
    """Парсит одну публикацию в структурированный словарь."""
    # Извлекаем DOI
    doi_match = re.search(r'doi\.org/\s*(10\.\d+/[^\s\n]+)', content, re.IGNORECASE)
    if not doi_match:
        doi_match = re.search(r'doi[.:]\s*(10\.\d+/[^\s\n]+)', content, re.IGNORECASE)
    doi = doi_match.group(1).strip().rstrip('.;,') if doi_match else None

    # Извлекаем URL (не DOI!)
    url_match = re.search(r'(https?://(?!doi\.org/)[^\s\n,;)]+)', content)
    url = url_match.group(1) if url_match else None

    # Извлекаем год
    year_match = re.search(r'(?:—|–|,)\s*(20\d{2})\b', content[-150:])
    year = int(year_match.group(1)) if year_match else None

    # Извлекаем авторов и заголовок
    authors = ""
    title = ""

    # Формат: "Фамилия, И.О. Название / Авторы // Журнал"
    slash_idx = content.find(" / ")
    if slash_idx > 0:
        before_slash = content[:slash_idx].strip()
        author_match = re.match(r'([А-ЯЁA-Z][а-яёa-z]+,\s*[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.?)', before_slash)
        if author_match:
            authors = author_match.group(1).strip()
            title = before_slash[author_match.end():].strip()
        else:
            title = before_slash
    else:
        double_slash_idx = content.find(" // ")
        if double_slash_idx > 0:
            title = content[:double_slash_idx].strip()
        else:
            title = content[:250].strip()

    title = re.sub(r'\s+', ' ', title).strip()

    # Извлекаем журнал
    journal = ""
    if " // " in content:
        after = content[content.find(" // ") + 4:]
        journal_match = re.match(r'([^\.,—–-]+)', after.strip())
        if journal_match:
            journal = journal_match.group(1).strip()

    return {
        'number': num,
        'title': title,
        'authors': authors,
        'journal': journal,
        'year': year,
        'doi': doi,
        'url': url,
    }


# ============================================================
# 3. Поиск публикаций в открытых источниках
# ============================================================

def search_semantic_scholar(title, author="", year=None, limit=5):
    """Ищет публикацию через Semantic Scholar API."""
    query = title[:100]
    query = re.sub(r'[^\w\s\-]', ' ', query)
    query = re.sub(r'\s+', '+', query)

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
    except Exception as e:
        print(f"    Semantic Scholar ошибка: {e}")
    return []


def search_crossref(title, author="", year=None, limit=3):
    """Ищет публикацию через Crossref API."""
    query = title[:80]
    query = re.sub(r'[^\w\s\-]', ' ', query)
    query = re.sub(r'\s+', '+', query)

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
    except Exception as e:
        print(f"    Crossref ошибка: {e}")
    return []


def search_cyberleninka(title, author="", year=None, limit=5):
    """Ищет публикацию через КиберЛенинку."""
    query = title[:100]
    query = re.sub(r'[^\w\s\-]', ' ', query)
    query = re.sub(r'\s+', ' ', query)

    params = {
        "limit": limit,
        "title": query,
        "sort": "citations",
    }
    if year:
        params["from_year"] = str(year)

    try:
        r = requests.get(CYBERLENINKA_API, params=params, timeout=15,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", {}).get("items", [])
            results = []
            for item in items:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "authors": item.get("authors", []),
                    "year": item.get("year"),
                    "doi": item.get("doi"),
                    "pdf_url": item.get("pdf_url", ""),
                })
            return results
    except Exception as e:
        print(f"    КиберЛенинка ошибка: {e}")
    return []


def resolve_doi(doi):
    """Разрешает DOI через DOI resolver."""
    url = f"{DOI_RESOLVER}{doi}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15,
                         allow_redirects=True)
        return {"resolved_url": r.url, "status": r.status_code}
    except Exception as e:
        print(f"    DOI resolver ошибка: {e}")
        return None


def find_pdf_url_from_html(html_url):
    """Извлекает URL PDF из HTML-страницы журнала."""
    try:
        r = requests.get(html_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        # Ищем ссылки с "PDF" в тексте (любой регистр)
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if "pdf" in text.lower():
                full_url = urljoin(html_url, href)
                # Проверяем по href
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
                # Проверяем, что это валидный абсолютный URL
                if url_candidate.startswith("http"):
                    # Извлекаем домен
                    domain_match = re.match(r'https?://([^/]+)', url_candidate)
                    if domain_match:
                        domain = domain_match.group(1)
                        # Убираем .pdf из конца домена для проверки
                        domain_check = domain
                        if domain_check.endswith('.pdf'):
                            domain_check = domain_check[:-4]
                        # Если домен не содержит точки (после очистки) — это не валидный домен
                        if '.' not in domain_check:
                            return urljoin(html_url, '/' + domain + url_candidate[len(domain_match.group(0)):])
                    return url_candidate
                return urljoin(html_url, url_candidate)
    except Exception as e:
        print(f"    Поиск PDF URL ошибка: {e}")
    return None


def find_source_for_publication(pub):
    """Ищет источник для публикации.

    Возвращает: {source_type, url, title, authors, doi} или None.
    """
    pub_title = pub['title']
    pub_authors = pub['authors']
    pub_doi = pub.get('doi')
    pub_url = pub.get('url')
    pub_year = pub.get('year')

    print(f"  Поиск: {pub_title[:60]}...")

    # 1. DOI из текста
    if pub_doi:
        print(f"  DOI в тексте: {pub_doi}")
        resolved = resolve_doi(pub_doi)
        if resolved and resolved['status'] == 200:
            return {'source_type': 'html', 'url': resolved['resolved_url'],
                    'title': pub_title, 'authors': pub_authors, 'doi': pub_doi}

    # 2. URL из текста
    if pub_url:
        print(f"  URL в тексте: {pub_url[:60]}")
        return {'source_type': 'html', 'url': pub_url,
                'title': pub_title, 'authors': pub_authors, 'doi': pub_doi}

    # 3. Semantic Scholar
    print("  Semantic Scholar...")
    ss_results = search_semantic_scholar(pub_title, pub_authors, pub_year)
    if ss_results:
        for ss in ss_results:
            ss_title = ss.get("title", "")
            if _titles_match(pub_title, ss_title):
                print(f"  Найдено: {ss_title[:80]}")
                result = {
                    'title': ss_title,
                    'authors': ", ".join(
                        f"{a.get('given','')} {a.get('family','')}"
                        for a in ss.get("authors", [])
                    ),
                    'doi': ss.get("externalIds", {}).get("DOI"),
                }
                pdf_info = ss.get("openAccessPdf")
                if pdf_info and pdf_info.get("url"):
                    result['source_type'] = 'pdf'
                    result['url'] = pdf_info['url']
                    return result
                if ss.get("url"):
                    result['source_type'] = 'html'
                    result['url'] = ss['url']
                    pdf_url = find_pdf_url_from_html(ss['url'])
                    if pdf_url:
                        result['source_type'] = 'pdf'
                        result['url'] = pdf_url
                    return result

    # 4. Crossref
    print("  Crossref...")
    cr_results = search_crossref(pub_title, pub_authors, pub_year)
    if cr_results:
        for cr in cr_results:
            cr_title = cr.get("title", "")
            if _titles_match(pub_title, cr_title):
                print(f"  Найдено: {cr_title[:80]}")
                doi = cr.get("DOI")
                result = {
                    'title': cr_title,
                    'authors': ", ".join(
                        f"{a.get('given','')} {a.get('family','')}"
                        for a in cr.get("authors", [])
                    ),
                    'doi': doi,
                }
                if doi:
                    resolved = resolve_doi(doi)
                    if resolved and resolved['status'] == 200:
                        result['source_type'] = 'html'
                        result['url'] = resolved['resolved_url']
                        return result
                for link in cr.get("links", []):
                    if "pdf" in link.get("url", "").lower():
                        result['source_type'] = 'pdf'
                        result['url'] = link['url']
                        return result

    # 5. КиберЛенинка (российская научная библиотека)
    print("  КиберЛенинка...")
    cl_results = search_cyberleninka(pub_title, pub_authors, pub_year)
    if cl_results:
        for cl in cl_results:
            cl_title = cl.get("title", "")
            if _titles_match(pub_title, cl_title):
                print(f"  Найдено: {cl_title[:80]}")
                result = {
                    'title': cl_title,
                    'authors': ", ".join(cl.get("authors", [])),
                    'doi': cl.get("doi"),
                }
                # PDF URL из КиберЛенинки
                if cl.get("pdf_url"):
                    result['source_type'] = 'pdf'
                    result['url'] = cl['pdf_url']
                    return result
                if cl.get("url"):
                    result['source_type'] = 'html'
                    result['url'] = cl['url']
                    pdf_url = find_pdf_url_from_html(cl['url'])
                    if pdf_url:
                        result['source_type'] = 'pdf'
                        result['url'] = pdf_url
                    return result

    print("  Не найдено")
    return None


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


# ============================================================
# 4. Скачивание и извлечение текста первой страницы
# ============================================================

def fetch_pdf_first_page(pdf_url):
    """Скачивает PDF и извлекает текст первой страницы."""
    try:
        r = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=60)
        if r.status_code != 200:
            print(f"  Ошибка скачивания: {r.status_code}")
            return None

        pdf = pdfplumber.open(io.BytesIO(r.content))
        text = pdf.pages[0].extract_text() or ""
        pdf.close()
        return text
    except Exception as e:
        print(f"  Ошибка чтения PDF: {e}")
        return None


def fetch_source_text(source):
    """Извлекает текст первой страницы из источника."""
    source_type = source.get('source_type', 'html')
    url = source.get('url', '')

    if source_type == 'pdf':
        return fetch_pdf_first_page(url)
    else:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                return None

            soup = BeautifulSoup(r.text, "lxml")
            pdf_url = find_pdf_url_from_html(url)
            if pdf_url:
                print(f"  Найден PDF: {pdf_url[:80]}...")
                return fetch_pdf_first_page(pdf_url)

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
        except Exception as e:
            print(f"  Ошибка HTML: {e}")
            return None


# ============================================================
# 5. Поиск email с помощью LLM
# ============================================================

EMAIL_LLM_PROMPT = """Найди email автора(ов) на первой странице научной статьи.

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
{page_text}
"""


def find_email_with_llm(page_text):
    """Ищет email через LLM."""
    prompt = EMAIL_LLM_PROMPT.format(page_text=page_text[:4000])

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
    except Exception as e:
        print(f"  LLM ошибка: {e}")
        return None


def find_email_regex(page_text):
    """Быстрый поиск email через regex."""
    emails = EMAIL_REGEX.findall(page_text)
    return emails[0] if emails else None


# ============================================================
# 6. Основной процесс
# ============================================================

def process_publication(pub, idx, total):
    """Обрабатывает одну публикацию."""
    print(f"\n{'='*60}")
    print(f"Публикация {idx}/{total}: #{pub['number']}")
    print(f"Заголовок: {pub['title'][:80]}")
    print(f"Авторы: {pub['authors'][:60]}")
    if pub.get('journal'):
        print(f"Журнал: {pub['journal'][:60]}")
    if pub.get('year'):
        print(f"Год: {pub['year']}")

    source = find_source_for_publication(pub)
    if not source:
        print("  → Источник не найден")
        return {**pub, 'found': False, 'email': None, 'source': None, 'error': 'source_not_found'}

    print(f"\n  Источник: {source['source_type']}")
    print(f"  URL: {source['url'][:100]}")

    print("  Извлечение текста...")
    page_text = fetch_source_text(source)
    if not page_text:
        print("  → Текст не извлечён")
        return {**pub, 'found': True, 'email': None, 'source': source, 'error': 'no_text'}

    print(f"  Текст: {len(page_text)} символов")

    print("  LLM поиск email...")
    llm_result = find_email_with_llm(page_text)
    if llm_result and llm_result != "NOT_FOUND":
        emails = EMAIL_REGEX.findall(llm_result)
        if emails:
            print(f"  ✓ Email (LLM): {emails[0]}")
            return {**pub, 'found': True, 'email': emails[0], 'source': source, 'error': None}

    print("  Regex поиск email...")
    regex_email = find_email_regex(page_text)
    if regex_email:
        print(f"  ✓ Email (regex): {regex_email}")
        return {**pub, 'found': True, 'email': regex_email, 'source': source, 'error': None}

    print("  Email не найден")
    return {**pub, 'found': True, 'email': None, 'source': source, 'error': 'no_email'}


def main():
    print("=" * 60)
    print("Поиск email авторов публикаций")
    print("=" * 60)

    print("\n[1/4] Извлечение текста из PDF...")
    text = normalize_pdf_text(PDF_PATH)
    if not text:
        print("Ошибка чтения PDF")
        sys.exit(1)

    print("\n[2/4] Парсинг публикаций...")
    publications = parse_publications(text)
    if not publications:
        print("Публикации не найдены")
        sys.exit(1)

    for pub in publications:
        print(f"  #{pub['number']}: {pub['title'][:60]}")

    print("\n[3/4] Поиск источников и email...")
    results = []
    for i, pub in enumerate(publications, 1):
        result = process_publication(pub, i, len(publications))
        results.append(result)
        time.sleep(1)

    print("\n" + "=" * 60)
    print("[4/4] ИТОГИ")
    print("=" * 60)

    found_count = 0
    email_count = 0

    for r in results:
        status = "НАЙДЕН" if r['found'] else "НЕ НАЙДЕН"
        email = r.get('email') or "—"
        print(f"\n  #{r['number']} [{status}]")
        print(f"    Заголовок: {r['title'][:70]}")
        print(f"    Email: {email}")
        if r.get('source'):
            print(f"    Источник: {r['source']['url'][:80]}")

        if r['found']:
            found_count += 1
        if r.get('email'):
            email_count += 1

    print(f"\n{'='*60}")
    print(f"Всего: {len(results)} | Источников: {found_count} | Email: {email_count}")
    print(f"{'='*60}")

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_search_result.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nСохранено: {output_path}")

    return results


if __name__ == "__main__":
    main()
