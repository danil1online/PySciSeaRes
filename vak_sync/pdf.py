"""Обработка PDF-файлов: скачивание, парсинг, поиск авторефератов."""
import os
import re
import io
import time

import requests
import pdfplumber
from bs4 import BeautifulSoup

from config import LLM_API_URL, LLM_MODEL, HEADERS, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE, MIN_SIZE, AUTOREFS_DIR
from logging_config import get_logger

logger = get_logger("pdf")


# ======================== UTILITY ========================

def sanitize_filename(name):
    bad_chars = '<>:"/\\|?*'
    for ch in bad_chars:
        name = name.replace(ch, '')
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:100]


def _url_encode_path(url):
    """Кодирует пробелы и опасные ASCII-символы в пути и query части URL."""
    from urllib.parse import urlparse, urlunparse, unquote
    parsed = urlparse(url)

    def _safe_encode(s):
        s = unquote(s)
        s = s.replace(' ', '%20')
        return s

    encoded_path = _safe_encode(parsed.path)
    encoded_query = _safe_encode(parsed.query)
    return urlunparse((parsed.scheme, parsed.netloc, encoded_path, parsed.params, encoded_query, parsed.fragment))


# ======================== PDF CHECK ========================

def _check_pdf_page_count(url):
    """Проверяет количество страниц в PDF-файле."""
    try:
        url = _url_encode_path(url)
        resp = requests.get(
            url,
            headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")},
            timeout=30,
        )
        resp.raise_for_status()
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def _read_pdf_full(pdf_path):
    """Считывает весь текст PDF в строку. Закрывает файл."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                parts.append(t)
            return "\n".join(parts)
    except Exception as e:
        logger.error(f"Error reading PDF {pdf_path}: {e}")
        return ""


# ======================== PDF SEARCH ========================

def find_autoref_pdf_from_page(url, fio="", date_defend=""):
    """Многоуровневый поиск PDF-автореферата на HTML-странице."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error loading page {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    base_url = url

    def resolve_href(href):
        if not href:
            return ""
        if href.startswith("http"):
            return _url_encode_path(href)
        from urllib.parse import urljoin
        return _url_encode_path(urljoin(base_url, href))

    def is_pdf(href):
        path = resolve_href(href).split("?")[0].lower()
        return path.endswith(".pdf")

    def check_is_pdf_via_head(url):
        try:
            resp = requests.head(
                url,
                headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")},
                timeout=15,
                allow_redirects=True,
            )
            content_type = resp.headers.get("Content-Type", "").lower()
            return "application/pdf" in content_type
        except Exception:
            return False

    def get_ancestor_text(element, max_depth=5):
        text = ""
        for i in range(max_depth):
            parent = element.parent
            if parent is None:
                break
            text = parent.get_text() + " " + text
            if parent.name in ("body", "html"):
                break
        return text.lower()

    def get_sibling_text(element):
        parts = []
        current = element.previous_sibling
        for _ in range(5):
            if current is None:
                break
            t = current.get_text() if hasattr(current, 'get_text') else str(current)
            parts.append(t)
            current = current.previous_sibling
        current = element.next_sibling
        for _ in range(5):
            if current is None:
                break
            t = current.get_text() if hasattr(current, 'get_text') else str(current)
            parts.append(t)
            current = current.next_sibling
        return " ".join(parts).lower()

    def _is_valid_pdf(url_to_check):
        pages = _check_pdf_page_count(url_to_check)
        if pages is None:
            return False, "Не удалось определить размер PDF"
        if pages > 50:
            return False, f"Слишком большой PDF ({pages} стр., автореферат должен быть <=50)"
        return True, f"{pages} стр."

    keywords = {"автореферат", "авторерат", "авторефер",
                "дипломная", "магистерская", "кандидат", "доктор"}
    action_words = {"посмотреть", "посмотреть файл", "скачать", "скачать файл",
                    "открыть", "открыть файл", "загрузить", "загрузить файл",
                    "download", "view", "open", "click"}

    links = soup.find_all("a")
    all_pdf_candidates = []

    for link in links:
        href = link.get("href", "")
        if not href:
            continue

        text = (link.get_text() or "").strip()
        full_url = resolve_href(href)
        link_text_lower = text.lower()

        has_pdf_ext = is_pdf(href)
        is_pdf_content = False

        if has_pdf_ext:
            page_count = _check_pdf_page_count(full_url)
            if page_count is None or page_count > 50:
                continue
            is_pdf_content = True
        else:
            if any(kw in link_text_lower for kw in keywords):
                if check_is_pdf_via_head(full_url):
                    page_count = _check_pdf_page_count(full_url)
                    if page_count is not None and page_count <= 50:
                        is_pdf_content = True

        if not is_pdf_content:
            continue

        if any(kw in link_text_lower for kw in keywords):
            all_pdf_candidates.append((full_url, "s1_keyword_text", text, page_count))
            continue

        ancestor = get_ancestor_text(link)
        siblings = get_sibling_text(link)
        if any(kw in ancestor or kw in siblings for kw in keywords):
            all_pdf_candidates.append((full_url, "s2_context", text, page_count))
            continue

        if any(w in link_text_lower for w in action_words):
            if any(kw in ancestor or kw in siblings for kw in keywords):
                all_pdf_candidates.append((full_url, "s3_action_context", text, page_count))
                continue

        all_pdf_candidates.append((full_url, "s4_fallback", text, page_count))

    if not all_pdf_candidates:
        logger.debug("No PDF files found")
        return None

    def autoref_score(item):
        url, strategy, text, page_count = item
        url_lower = url.lower()
        text_lower = text.lower()
        score = 0

        strategy_scores = {
            "s1_keyword_text": 300,
            "s2_context": 200,
            "s3_action_context": 150,
            "s4_fallback": 0,
        }
        score += strategy_scores.get(strategy, 0)

        if "автореферат" in url_lower:
            score += 100
        if "автореферат" in text_lower:
            score += 50

        if any(w in text_lower for w in action_words):
            score += 10

        avoid = ["диссертация", "полный текст", "отзыв", "рецензия",
                 "протокол", "сопроводительн", "согласие", "заключение"]
        if any(w in url_lower or w in text_lower for w in avoid):
            score -= 200

        return score

    all_pdf_candidates.sort(key=autoref_score, reverse=True)

    # Deduplicate by URL
    seen_urls = set()
    unique_candidates = []
    for item in all_pdf_candidates:
        url = item[0]
        if url not in seen_urls:
            seen_urls.add(url)
            unique_candidates.append(item)
    all_pdf_candidates = unique_candidates

    best_url, best_strategy, best_text, best_pages = all_pdf_candidates[0]
    best_score = autoref_score(all_pdf_candidates[0])
    second_score = autoref_score(all_pdf_candidates[1]) if len(all_pdf_candidates) > 1 else 0

    logger.debug(f"Top candidates: {[(autoref_score((u,s,t,p)), s, u.split('/')[-1][:40]) for u,s,t,p in all_pdf_candidates[:3]]}")

    score_gap = best_score - second_score

    if best_score < -50 or score_gap < 30:
        candidates_for_llm = []
        for url, strategy, text, pc in all_pdf_candidates[:10]:
            info = f"{pc} стр."
            candidates_for_llm.append((url, strategy, text, info))

        logger.debug(f"Ambiguity (score={best_score}, gap={score_gap}) — LLM analysis...")
        pdf_url = _find_autoref_with_llm(
            str(soup), str(resp.text), base_url,
            candidates_for_llm, fio, date_defend
        )
        if pdf_url:
            pages = _check_pdf_page_count(pdf_url)
            info_str = f"{pages} стр." if pages else "неизвестно"
            logger.info(f"LLM selected: {pdf_url} ({info_str})")
            return pdf_url
    elif best_score < -50:
        candidates_for_llm = []
        for url, strategy, text, pc in all_pdf_candidates[:10]:
            info = f"{pc} стр."
            candidates_for_llm.append((url, strategy, text, info))

        pdf_url = _find_autoref_with_llm(
            str(soup), str(resp.text), base_url,
            candidates_for_llm, fio, date_defend
        )
        if pdf_url:
            pages = _check_pdf_page_count(pdf_url)
            info_str = f"{pages} стр." if pages else "неизвестно"
            logger.info(f"LLM selected: {pdf_url} ({info_str})")
            return pdf_url
    else:
        strategy_name = {"s1_keyword_text": "S1: link text",
                         "s2_context": "S2: context",
                         "s3_action_context": "S3: action+context",
                         "s4_fallback": "S4: fallback"}.get(best_strategy, best_strategy)
        logger.debug(f"{strategy_name}: {best_url} ({best_pages} pages, score={best_score})")
        return best_url

    return None


def _find_autoref_with_llm(html_snippet, full_html, base_url, candidates_with_info, fio, date_defend):
    """Использует LLM для выбора автореферата из списка PDF-кандидатов."""
    from urllib.parse import urljoin

    page_title = ""
    try:
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', full_html, re.IGNORECASE)
        if title_match:
            from bs4 import BeautifulSoup as BS2
            page_title = BS2(title_match.group(1), "html.parser").get_text().strip()
    except Exception:
        page_title = full_html[:200]

    max_candidates = min(10, len(candidates_with_info))
    candidate_items = candidates_with_info[:max_candidates]

    candidate_descriptions = []
    for idx, (url, strategy, link_text, info_str) in enumerate(candidate_items, 1):
        clean_text = link_text[:200].strip() if link_text else ""
        filename = url.split("/")[-1].split("?")[0]
        filename = filename.replace("%20", " ").replace("%C3", "И").replace("%23", "#")
        candidate_descriptions.append(
            f"{idx}. URL: {url}\n   Filename: {filename}\n"
            f"   Link text: {clean_text}\n"
            f"   Strategy: {strategy} | {info_str}"
        )

    candidates_text = "\n\n".join(candidate_descriptions)

    html_context = ""
    try:
        body_match = re.search(r'<body[^>]*>(.*?)</body>', html_snippet, re.DOTALL | re.IGNORECASE)
        if body_match:
            body_text = body_match.group(1)
            from bs4 import BeautifulSoup as BS2
            text_only = BS2(body_text, "html.parser").get_text(separator="\n", strip=True)
            html_context = text_only[:8000]
    except Exception:
        html_context = html_snippet[:8000]

    system_prompt = (
        "You are an assistant searching for a dissertation abstract (автореферат) on a web page. "
        "Find the link to the ABSTRACT (usually 15-50 pages). "
        "DO NOT select: full dissertation text, reviews, protocols, cover letters. "
        "Return ONLY the number of the candidate (1..N) that is the abstract. "
        "If unsure — return NONE."
    )

    fio_desc = f"Candidate: {fio}" if fio else ""
    defend_desc = f"Defense date: {date_defend}" if date_defend else ""

    user_prompt = (
        f"Page: {page_title}\n"
        f"{fio_desc}\n"
        f"{defend_desc}\n\n"
        f"Page context (text from body):\n{html_context}\n\n"
        f"Candidates list:\n{candidates_text}\n\n"
        f"Which candidate number is the abstract?"
    )

    try:
        resp = requests.post(
            LLM_API_URL,
            headers=HEADERS,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 10,
                "temperature": 0.0,
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()

        num_match = re.search(r'\b([1-9]|10)\b', answer)
        if num_match:
            chosen_idx = int(num_match.group(1)) - 1
            if 0 <= chosen_idx < len(candidate_items):
                return candidate_items[chosen_idx][0]

        logger.warning(f"LLM answer: '{answer}', fallback to first candidate")
        return candidate_items[0][0]
    except Exception as e:
        logger.error(f"LLM error: {e}, fallback to first candidate")
        return candidate_items[0][0] if candidate_items else None


# ======================== DOWNLOAD ========================

def download_autoref(autoref_url, fio, date_defend, max_retries=3, previous_pdf_url=None):
    """Скачивает автореферат из VAK."""
    if not autoref_url:
        return None, "Нет ссылки", autoref_url

    resolved_pdf_url = autoref_url
    url_path = autoref_url.split("?")[0].lower()

    if not url_path.endswith('.pdf'):
        pdf_indicators = ["avtoreferat", "get-file", "download", "file/", "send/", "attachment"]
        is_potential_pdf = any(ind in url_path for ind in pdf_indicators)

        if is_potential_pdf:
            logger.debug(f"Checking potential PDF: {autoref_url}...")
            try:
                from urllib.parse import unquote
                decoded_url = unquote(autoref_url)
                encoded_url = _url_encode_path(decoded_url)
                resp = requests.head(
                    encoded_url,
                    headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")},
                    timeout=30,
                    allow_redirects=True,
                )
                content_type = resp.headers.get("Content-Type", "").lower()
                if "application/pdf" in content_type:
                    resolved_pdf_url = resp.url
                    logger.debug(f"PDF detected ({content_type})")
                else:
                    logger.debug(f"Not PDF ({content_type}), searching page...")
                    resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
                    if not resolved_pdf_url:
                        return None, "Не PDF (ссылка не найдена)", autoref_url
            except Exception as e:
                logger.error(f"Check error: {e}, searching page...")
                resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
                if not resolved_pdf_url:
                    return None, "Не PDF (ссылка не найдена)", autoref_url
        else:
            logger.debug(f"Searching PDF on page {autoref_url}...")
            resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
            if not resolved_pdf_url:
                return None, "Не PDF (ссылка не найдена)", autoref_url
    else:
        resolved_pdf_url = autoref_url

    if previous_pdf_url is not None and resolved_pdf_url != previous_pdf_url:
        logger.info(f"PDF URL changed, downloading again")
        logger.debug(f"  Old: {previous_pdf_url}")
        logger.debug(f"  New: {resolved_pdf_url}")
    elif previous_pdf_url is not None:
        resolved_pdf_url = previous_pdf_url

    date_str = date_defend.replace("-", "_") if date_defend else "unknown"
    filename = f"{sanitize_filename(fio)}_{date_str}.pdf"
    save_path = os.path.join(AUTOREFS_DIR, filename)

    os.makedirs(AUTOREFS_DIR, exist_ok=True)

    if os.path.exists(save_path):
        return save_path, "Скачан ранее", resolved_pdf_url

    for attempt in range(max_retries):
        try:
            from urllib.parse import unquote
            decoded_url = unquote(resolved_pdf_url)
            encoded_url = _url_encode_path(decoded_url)

            # NOTE: verify=False is used because VAK API may have self-signed certs
            resp = requests.get(
                encoded_url, stream=True, timeout=120,
                headers={"User-Agent": HEADERS["User-Agent"] if "User-Agent" in HEADERS else "Mozilla/5.0"},
                verify=False,
            )
            resp.raise_for_status()

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > 0 and content_length < MIN_SIZE:
                return None, f"Малый размер ({content_length // 1024} КБ)", resolved_pdf_url

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            actual_size = os.path.getsize(save_path)
            if actual_size < 200 * 1024:
                os.remove(save_path)
                return None, f"Слишком маленький файл ({actual_size // 1024} КБ)", resolved_pdf_url

            logger.info(f"Downloaded: {filename} ({actual_size // 1024} KB)")
            return save_path, "OK", resolved_pdf_url

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if attempt < max_retries - 1:
                logger.warning(f"HTTP {status_code}, retry...")
            else:
                return None, f"HTTP {status_code}", resolved_pdf_url
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None, str(e), resolved_pdf_url

    return None, "Ошибка скачивания", resolved_pdf_url


# ======================== SPECIALTY EXTRACTION ========================

def extract_specialty_from_pdf(pdf_path):
    """Извлекает шифр и название специальности из первой страницы PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page = pdf.pages[0]
            text = first_page.extract_text() or ""

        text = text.replace('\uf02d', '—').replace('\u2013', '—').replace('\u2014', '—')

        m = re.search(
            r'Специальность\s*[:\s]\s*(\S+\.\S+\.\S+)\s*[-—–]\s*([^()\n]+)',
            text
        )
        if m:
            cipher = m.group(1).strip()
            name = re.sub(r'\s+', ' ', m.group(2).strip())
            name = name.replace('«', '').replace('»', '').strip()
            return cipher, name

        m2 = re.search(
            r'Специальность\s*[:\s]\s*(\S+\.\S+\.\S+)',
            text
        )
        if m2:
            cipher = m2.group(1).strip()
            return cipher, ""

    except Exception as e:
        logger.error(f"Specialty extraction error {pdf_path}: {e}")

    return None, None
