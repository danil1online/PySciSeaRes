#!/usr/bin/env python3
"""Ежедневный синхронизационный скрипт.
Скачивает новые авторефераты из VAK, извлекает публикации и данные руководителя,
сохраняет в БД. Запускается по cron каждый день в 3:00.
"""
import sys
import os
import re
import gc
import time
import sqlite3
import resource
import psutil
from datetime import datetime, timedelta

import requests
import pdfplumber
import logging
import io
from bs4 import BeautifulSoup
logging.getLogger("pdfminer").setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    API_BASE, LLM_API_URL, LLM_MODEL, HEADERS, MIN_SIZE,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
    MAX_PUBLICATIONS, AUTOREFS_DIR, LLM_RESPONSES_DIR,
    SCI_SPEC_FILE, SESSION_SECRET_KEY,
)

# Import extraction modules
from extractors.publications import (
    extract_publications_from_pdf_text,
    parse_pub_to_json,
)
from extractors.supervisor import (
    extract_supervisor_from_pdf_text,
)

# ======================== MEMORY CONTROL ========================

# Настройки: 16 ГБ RAM, оставляем 2 ГБ на систему
MAX_MEMORY_MB = 12 * 1024  # 12 ГБ — мягкий лимит
CRITICAL_MEMORY_MB = 14 * 1024  # 14 ГБ — критический, аварийная остановка
BATCH_SIZE = 10  # обработать N объявлений и сбросить память
gc_threshold = 0

def get_memory_mb():
    """Возвращает потребление памяти процесса в МБ."""
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 * 1024)


def check_memory():
    """Проверяет потребление памяти. При превышении — сброс GC."""
    mem = get_memory_mb()
    if mem >= CRITICAL_MEMORY_MB:
        print(f"\n  !!! КРИТИЧЕСКАЯ ПАМЯТЬ: {mem:.0f} МБ, аварийная остановка !!!")
        sys.exit(1)
    if mem >= MAX_MEMORY_MB:
        print(f"\n  Ограничение памяти: {mem:.0f} МБ, сброс GC...")
        force_gc()


def force_gc():
    """Принудительный сбор мусора + очистка кэша SQLite."""
    global gc_threshold
    before = gc.get_count()[0]
    gc.collect()
    gc.collect()
    gc.collect()
    after = gc.get_count()[0]
    print(f"    GC: {before} -> {after} объектов в gen0")


def process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids):
    """Обрабатывает одно объявление: проверка, скачивание, извлечение.

    Возвращает True если объявление обработано, False если пропущено/ошибка.
    """
    adv_id = advert["id"]
    if adv_id in processed_ids:
        return False
    processed_ids.add(adv_id)

    fio = advert.get("fio", "Неизвестно")
    date_defend = advert.get("date_defend", "")
    counters["processed"] += 1

    print(f"\n  [{adv_id[:8]}...] {fio} | {date_defend}")

    # Get detail
    detail = get_advert_detail(adv_id)
    if not detail:
        print("    Ошибка получения детали")
        counters["errors"] += 1
        return True

    # Save advert
    c = conn.cursor()
    try:
        c.execute("""INSERT OR REPLACE INTO adverts (
            id, old_id, date_defend, fio, dissertation_name,
            specialty_cipher, specialty_text,
            council_cipher, defend_org, org_address, org_phone,
            autoref_url, autoref_path, autoref_pdf_url, downloaded
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            detail.get("id"),
            detail.get("old_id"),
            detail.get("date_defend"),
            detail.get("fio"),
            detail.get("dissertation_name"),
            spec_cipher,
            spec_name,
            detail.get("council_cipher"),
            detail.get("defend_org"),
            detail.get("org_address"),
            detail.get("org_phone"),
            detail.get("autoref_site"),
            None,  # autoref_path — заполняется после скачивания
            None,  # autoref_pdf_url — resolved PDF URL
            0,
        ))
        conn.commit()

        # Check if newly inserted or updated
        c.execute("SELECT COUNT(*) FROM adverts WHERE id = ?", (adv_id,))
        if c.fetchone()[0] == 1:
            counters["new"] += 1
        else:
            counters["updated"] += 1

    except sqlite3.Error as e:
        print(f"    Ошибка БД: {e}")
        counters["errors"] += 1
        return True

    # Check how many publications already exist for this advert
    c.execute("SELECT COUNT(*) FROM publications WHERE advert_id = ?", (adv_id,))
    existing_count = c.fetchone()[0]

    # Check if autoref PDF is already downloaded
    c.execute("SELECT autoref_path, autoref_pdf_url, downloaded FROM adverts WHERE id = ?", (adv_id,))
    row = c.fetchone()
    autoref_path = row[0] if row else None
    autoref_pdf_url = row[1] if row else None
    autoref_downloaded = row[2] if row else 0

    # --- Decision logic ---
    need_download = False
    need_reextract = False

    if existing_count >= 8:
        print(f"    Пропуск: уже есть {existing_count} публикаций в БД")
        counters["skipped"] += 1
        return True

    if existing_count < 5:
        # Мало публикаций (0-4) — скачиваем заново, извлекаем и заменяем
        need_download = True
        need_reextract = True
    elif existing_count < 8:
        # 5-7 публикаций — достаточно, пересчитываем только если PDF не скачан
        if autoref_downloaded == 0 or autoref_path is None:
            need_download = True
            # Не извлекаем публикации заново, только скачиваем PDF

    # Download autoref if needed
    autoref_url = detail.get("autoref_site")
    if need_download:
        save_path, status, resolved_url = download_autoref(
            autoref_url, fio, date_defend, previous_pdf_url=autoref_pdf_url
        )
        if not save_path:
            print(f"    Автореферат: {status}")
            if existing_count < 5:
                counters["errors"] += 1
            return True
        save_path_real = save_path
        counters["downloaded"] += 1
    elif autoref_path:
        save_path_real = autoref_path
        resolved_url = autoref_pdf_url
    else:
        print("    Нет ссылки на автореферат")
        return True

    size_kb = os.path.getsize(save_path_real) // 1024
    if need_download:
        print(f"    Автореферат: скачан ({size_kb} КБ)", end="")
    else:
        print(f"    Автореферат: найден ({size_kb} КБ)", end="")

    # Update path and resolved URL in DB
    c.execute("UPDATE adverts SET autoref_path = ?, autoref_pdf_url = ?, downloaded = 1 WHERE id = ?",
              (save_path_real, resolved_url, adv_id))

    # Extract specialty from PDF first page
    pdf_cipher, pdf_name = extract_specialty_from_pdf(save_path_real)
    if pdf_name and not spec_name:
        c.execute("UPDATE adverts SET specialty_text = ? WHERE id = ?",
                  (pdf_name, adv_id))
        print(f"\n    Specialty из PDF: {pdf_cipher} - {pdf_name}", end="")
    conn.commit()

    # --- Extract publications if needed (only when count < 5) ---
    if need_reextract:
        print("\n    Извлечение данных из PDF...", end="")
        counters["extracted"] += 1
        try:
            pdf_full_text = _read_pdf_full(save_path_real)

            # Extract publications (using extracted text)
            raw_pubs, found_section, llm_time = extract_publications_from_pdf_text(
                pdf_full_text
            )
            struct_pubs = parse_pub_to_json(raw_pubs)
            c.execute("DELETE FROM publications WHERE advert_id = ?", (adv_id,))
            for num, p in enumerate(struct_pubs, 1):
                authors = p["authors"]
                if isinstance(authors, list):
                    authors = ", ".join(authors)
                c.execute(
                    "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages) VALUES (?,?,?,?,?,?,?)",
                    (adv_id, num, authors, p["title"], p["journal"], p["year"], p["pages"])
                )
            conn.commit()
            print(f" OK ({len(struct_pubs)} публикаций)", end="")
            if found_section:
                print(f"\n      Раздел: {found_section}")
            if llm_time:
                print(f"\n      LLM время: {llm_time:.1f}с")

            # Re-check publication count after extraction
            c.execute("SELECT COUNT(*) FROM publications WHERE advert_id = ?", (adv_id,))
            new_count = c.fetchone()[0]
            if new_count >= 5:
                print(f"\n    Восстановлено: было {existing_count}, стало {new_count} публикаций")
            else:
                print(f"\n    Внимание: извлечено всего {new_count} публикаций (было {existing_count})")

            del pdf_full_text

        except Exception as e:
            print(f" Ошибка извлечения: {e}")

    # Always extract supervisor
    print("    Извлечение руководителя...", end="")
    try:
        pdf_full_text = _read_pdf_full(save_path_real)
        sup = extract_supervisor_from_pdf_text(pdf_full_text)
        sup_name = sup.get("supervisor_name")
        sup_work = sup.get("supervisor_work")
        c.execute("UPDATE adverts SET supervisor_name = ?, supervisor_work = ? WHERE id = ?",
                  (sup_name, sup_work, adv_id))
        conn.commit()
        if sup_name:
            print(f" OK ({sup_name})")
        else:
            print(" (не найден)")
        del pdf_full_text
    except Exception as e:
        print(f" Ошибка: {e}")

    # Memory check after each advert
    if counters["processed"] % BATCH_SIZE == 0:
        check_memory()

    return True


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
        print(f"\n  Ошибка чтения PDF {pdf_path}: {e}")
        return ""


# ======================== DATABASE ========================

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(__file__)) + "/instance", exist_ok=True)
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "vak.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS adverts (
        id TEXT PRIMARY KEY,
        old_id INTEGER,
        date_defend TEXT,
        fio TEXT,
        dissertation_name TEXT,
        specialty_cipher TEXT,
        specialty_text TEXT,
        supervisor_name TEXT,
        supervisor_work TEXT,
        council_cipher TEXT,
        defend_org TEXT,
        org_address TEXT,
        org_phone TEXT,
        autoref_url TEXT,
        autoref_path TEXT,
        autoref_pdf_url TEXT,
        downloaded INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Migration: add autoref_pdf_url column if it doesn't exist (for existing DBs)
    try:
        c.execute("ALTER TABLE adverts ADD COLUMN autoref_pdf_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    c.execute("""CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advert_id TEXT NOT NULL,
        pub_number INTEGER NOT NULL,
        authors TEXT,
        title TEXT,
        journal TEXT,
        year INTEGER,
        pages TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (advert_id) REFERENCES adverts(id) ON DELETE CASCADE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    return conn


# ======================== VAK API ========================

def read_specialties():
    specs = []
    if not os.path.exists(SCI_SPEC_FILE):
        print(f"  Файл {SCI_SPEC_FILE} не найден!")
        return specs
    with open(SCI_SPEC_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Format: "1.2.1. - Искусственный интеллект" or just "1.2.1."
            m = re.match(r'^(\S+?)\s*[-—–]\s*(.*)', line)
            if m:
                specs.append((m.group(1).strip(), m.group(2).strip()))
            else:
                specs.append((line, ""))
    print(f"  Загружено {len(specs)} специальностей из sci_spec.txt")
    return specs


def search_adverts(specialty_id, date_from, date_to):
    params = {
        "specialty": specialty_id,
        "date_defend_from": date_from,
        "date_defend_to": date_to,
        "page_size": 100,
    }
    all_results = []
    page = 1
    while True:
        params["page"] = page
        try:
            resp = requests.get(f"{API_BASE}/att/adverts/", params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Ошибка API (страница {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break
        all_results.extend(results)
        print(f"  Страница {page}: {len(all_results)} из {data.get('count', '?')}")

        if data.get("next"):
            page += 1
        else:
            break

    return all_results


def get_advert_detail(advert_id):
    try:
        resp = requests.get(f"{API_BASE}/att/adverts/{advert_id}/", headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Ошибка получения детализации {advert_id}: {e}")
        return {}


# ======================== PDF ========================

def sanitize_filename(name):
    bad_chars = '<>:"/\\|?*'
    for ch in bad_chars:
        name = name.replace(ch, '')
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:100]


def _url_encode_path(url):
    """Кодирует пробелы и опасные ASCII-символы в пути и query части URL.

    Не кодирует неблокальные символы (кириллицу, UTF-8).
    Кодирование безопасного символа: %20 для пробелов, %2F для слэшей и т.д.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)

    def _encode_path(path):
        """Кодирует только ASCII-символы, оставляя UTF-8 как есть."""
        import re
        # Кодируем пробелы как %20, и опасные ASCII символы
        return re.sub(r'[^\x00-\x7F]+', lambda m: m.group(0), path)

    # Простое кодирование пробелов и опасных ASCII символов
    def _safe_encode(s):
        """Кодирует пробелы и опасные символы, но оставляет UTF-8."""
        import re
        # Сначала декодируем уже закодированные символы (на случай повторного вызова)
        from urllib.parse import unquote
        s = unquote(s)
        # Кодируем пробелы
        s = s.replace(' ', '%20')
        return s

    encoded_path = _safe_encode(parsed.path)
    encoded_query = _safe_encode(parsed.query)
    return urlunparse((parsed.scheme, parsed.netloc, encoded_path, parsed.params, encoded_query, parsed.fragment))


def _check_pdf_page_count(url):
    """Проверяет количество страниц в PDF-файле. Возвращает число или None при ошибке."""
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


def find_autoref_pdf_from_page(url, fio="", date_defend=""):
    """Многоуровневый поиск PDF-автореферата на HTML-странице.

    Стратегии (по приоритету):
    1. Ссылка с текстом "автореферат" + .pdf
    2. Ссылка .pdf, рядом в HTML-тексте есть слово "автореферат"
    3. Ссылка с действием "посмотреть"/"скачать" + .pdf
    4. Умный фоллбэк — сортировка кандидатов с учётом имён файлов
    5. LLM-анализ страницы, если фоллбэк не уверен

    PDF с >50 страницами пропускаются (вероятно, полный текст диссертации).

    Аргументы:
        url: URL страницы с ссылками на PDF
        fio: ФИО кандидата наук (для LLM-контекста)
        date_defend: дата защиты (для LLM-контекста)
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  Ошибка загрузки страницы {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    base_url = url

    def resolve_href(href):
        """Резолвит относительные URL."""
        if not href:
            return ""
        if href.startswith("http"):
            return _url_encode_path(href)
        from urllib.parse import urljoin
        return _url_encode_path(urljoin(base_url, href))

    def is_pdf(href):
        """Проверяет, ведёт ли ссылка на PDF по расширению."""
        path = resolve_href(href).split("?")[0].lower()
        return path.endswith(".pdf")

    def check_is_pdf_via_head(url):
        """Проверяет Content-Type через HEAD-запрос (для ссылок без .pdf расширения)."""
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
        """Собирает текст родительских элементов."""
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
        """Собирает текст соседних элементов (предыдущие и следующие 3 узла)."""
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
        """Проверяет PDF на размер страниц (автореферат <=50 стр.)."""
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
    all_pdf_candidates = []  # (url, strategy_name, link_text, page_count)

    for link in links:
        href = link.get("href", "")
        if not href:
            continue

        text = (link.get_text() or "").strip()
        full_url = resolve_href(href)
        link_text_lower = text.lower()

        # Check if this is a PDF
        has_pdf_ext = is_pdf(href)
        is_pdf_content = False

        if has_pdf_ext:
            # Has .pdf extension — check page count
            page_count = _check_pdf_page_count(full_url)
            if page_count is None or page_count > 50:
                continue
            is_pdf_content = True
        else:
            # No .pdf extension — check for "автореферат" in link text
            # If found, do HEAD request to verify Content-Type
            if any(kw in link_text_lower for kw in keywords):
                if check_is_pdf_via_head(full_url):
                    page_count = _check_pdf_page_count(full_url)
                    if page_count is not None and page_count <= 50:
                        is_pdf_content = True

        if not is_pdf_content:
            continue

        # Strategy 1: Link text contains "автореферат" keywords (highest priority)
        if any(kw in link_text_lower for kw in keywords):
            all_pdf_candidates.append((full_url, "s1_keyword_text", text, page_count))
            continue

        # Strategy 2: "автореферат" in surrounding text (parent/sibling)
        ancestor = get_ancestor_text(link)
        siblings = get_sibling_text(link)
        if any(kw in ancestor or kw in siblings for kw in keywords):
            all_pdf_candidates.append((full_url, "s2_context", text, page_count))
            continue

        # Strategy 3: Action words + "автореферат" in surrounding text
        if any(w in link_text_lower for w in action_words):
            if any(kw in ancestor or kw in siblings for kw in keywords):
                all_pdf_candidates.append((full_url, "s3_action_context", text, page_count))
                continue

        # Collect all other PDFs for fallback
        all_pdf_candidates.append((full_url, "s4_fallback", text, page_count))

    if not all_pdf_candidates:
        print(f"    PDF-файлы не найдены")
        return None

    # Strategy 4: Score-based selection (unified for all candidates)
    def autoref_score(item):
        """Score a candidate — higher is better."""
        url, strategy, text, page_count = item
        url_lower = url.lower()
        text_lower = text.lower()
        score = 0

        # Strategy priority: s1 > s2 > s3 > s4
        strategy_scores = {
            "s1_keyword_text": 300,
            "s2_context": 200,
            "s3_action_context": 150,
            "s4_fallback": 0,
        }
        score += strategy_scores.get(strategy, 0)

        # Strong positive: "автореферат" in filename
        if "автореферат" in url_lower:
            score += 100
        if "автореферат" in text_lower:
            score += 50

        # Moderate positive: action words in link text (without context)
        if any(w in text_lower for w in action_words):
            score += 10

        # Strong negative: disqualifying keywords in filename/URL/text
        avoid = ["диссертация", "полный текст", "отзыв", "рецензия",
                 "протокол", "сопроводительн", "согласие", "заключение"]
        if any(w in url_lower or w in text_lower for w in avoid):
            score -= 200

        return score

    all_pdf_candidates.sort(key=autoref_score, reverse=True)

    # Deduplicate by URL — keep only the highest-scoring entry per unique URL
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

    # Print top 3 candidates for debugging
    print(f"    Топ-3 кандидата (скор | стратегия | файл):")
    for i, (url, strat, txt, pc) in enumerate(all_pdf_candidates[:3], 1):
        sc = autoref_score((url, strat, txt, pc))
        fname = url.split("/")[-1].split("?")[0][:60]
        print(f"      [{i}] скор={sc:+d} | {strat} | {fname} ({pc} стр.)")

    # If the best and second-best have very different scores, the best is clearly correct
    # Only use LLM if scores are close or best is suspiciously low
    score_gap = best_score - second_score

    if best_score < -50 or score_gap < 30:
        # Ambiguous or bad — use LLM
        candidates_for_llm = []
        for url, strategy, text, pc in all_pdf_candidates[:10]:
            info = f"{pc} стр."
            candidates_for_llm.append((url, strategy, text, info))

        print(f"    [5] Неоднозначность (скор={best_score}, разрыв={score_gap}) — анализ через LLM...")
        pdf_url = _find_autoref_with_llm(
            str(soup), str(resp.text), base_url,
            candidates_for_llm, fio, date_defend
        )
        if pdf_url:
            pages = _check_pdf_page_count(pdf_url)
            info_str = f"{pages} стр." if pages else "неизвестно"
            print(f"    LLM выбрал: {pdf_url} ({info_str})")
            return pdf_url
    elif best_score < -50:
        print(f"    [4] Наилучший кандидат имеет низкий скор ({best_score}) — LLM...")
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
            print(f"    LLM выбрал: {pdf_url} ({info_str})")
            return pdf_url
    else:
        strategy_name = {"s1_keyword_text": "С1: текст ссылки",
                         "s2_context": "С2: контекст",
                         "s3_action_context": "С3: действие+контекст",
                         "s4_fallback": "С4: фоллбэк"}.get(best_strategy, best_strategy)
        print(f"    [4] {strategy_name}: {best_url} ({best_pages} стр., скор={best_score})")
        return best_url

    return None


def _find_autoref_with_llm(html_snippet, full_html, base_url, candidates_with_info, fio, date_defend):
    """Использует LLM для выбора автореферата из списка PDF-кандидатов.

    Аргументы:
        html_snippet: обрезанный HTML для передачи в LLM
        full_html: полный HTML (для извлечения title)
        base_url: базовый URL страницы
        candidates_with_info: список (url, strategy, text, info)
        fio: ФИО кандидата наук
        date_defend: дата защиты

    Возвращает URL автореферата или None.
    """
    from urllib.parse import urljoin

    # Извлекаем заголовок страницы
    page_title = ""
    try:
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', full_html, re.IGNORECASE)
        if title_match:
            from bs4 import BeautifulSoup as BS2
            page_title = BS2(title_match.group(1), "html.parser").get_text().strip()
    except Exception:
        page_title = full_html[:200]

    # Формируем список кандидатов для LLM
    # Максимум 10 кандидатов (LLM ограничена по контексту)
    max_candidates = min(10, len(candidates_with_info))
    candidate_items = candidates_with_info[:max_candidates]

    # Собираем информацию о каждом кандидате
    candidate_descriptions = []
    for idx, (url, strategy, link_text, info_str) in enumerate(candidate_items, 1):
        # Урезаем текст ссылки до 200 символов
        clean_text = link_text[:200].strip() if link_text else ""
        # Извлекаем имя файла из URL
        filename = url.split("/")[-1].split("?")[0]
        filename = filename.replace("%20", " ").replace("%C3", "И").replace("%23", "#")
        candidate_descriptions.append(
            f"{idx}. URL: {url}\n   Имя файла: {filename}\n"
            f"   Текст ссылки: {clean_text}\n"
            f"   Стратегия: {strategy} | {info_str}"
        )

    candidates_text = "\n\n".join(candidate_descriptions)

    # Обрезаем HTML-контекст, чтобы уместить в лимит
    # Берём содержимое body, обрезая до ~8000 символов
    html_context = ""
    try:
        body_match = re.search(r'<body[^>]*>(.*?)</body>', html_snippet, re.DOTALL | re.IGNORECASE)
        if body_match:
            body_text = body_match.group(1)
            # Убираем HTML-теги
            from bs4 import BeautifulSoup as BS2
            text_only = BS2(body_text, "html.parser").get_text(separator="\n", strip=True)
            html_context = text_only[:8000]
    except Exception:
        html_context = html_snippet[:8000]

    system_prompt = (
        "Ты — помощник по поиску автореферата диссертации на веб-странице. "
        "Тебе предоставлена HTML-страница с ссылками на PDF-файлы. "
        "Твоя задача — найти ссылку именно на АВТОРЕФЕРАТ диссертации. "
        "Автореферат — это краткое изложение диссертации (обычно 15-50 страниц). "
        "НЕ выбирай: полный текст диссертации, отзывы, рецензии, протоколы, "
        "сопроводительные документы, методические указания. "
        "Выбери номер одного кандидата (из списка 1..N), который является авторефератом. "
        "Ответь ТОЛЬКО числом — номер кандидата. Если не можешь определить — верни слово NONE."
    )

    fio_desc = f"ФИО кандидата: {fio}" if fio else ""
    defend_desc = f"Дата защиты: {date_defend}" if date_defend else ""

    user_prompt = (
        f"Страница: {page_title}\n"
        f"{fio_desc}\n"
        f"{defend_desc}\n\n"
        f"Контекст страницы (текст из body):\n{html_context}\n\n"
        f"Список PDF-кандидатов:\n{candidates_text}\n\n"
        f"На какой номер кандидата ведёт ссылка на автореферат?"
    )

    # Вызываем LLM
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

        # Парсим ответ: ищем число от 1 до N
        import re as re2
        num_match = re2.search(r'\b([1-9]|10)\b', answer)
        if num_match:
            chosen_idx = int(num_match.group(1)) - 1
            if 0 <= chosen_idx < len(candidate_items):
                return candidate_items[chosen_idx][0]

        # Если ответ "NONE" или не число — пробуем первый кандидат
        print(f"    LLM ответ: '{answer}', фоллбэк на первый кандидат")
        return candidate_items[0][0]
    except Exception as e:
        print(f"    LLM ошибка: {e}, фоллбэк на первый кандидат")
        return candidate_items[0][0] if candidate_items else None


def download_autoref(autoref_url, fio, date_defend, max_retries=3, previous_pdf_url=None):
    if not autoref_url:
        return None, "Нет ссылки", autoref_url

    # Step 1: Check if URL is direct PDF link or potential PDF download
    resolved_pdf_url = autoref_url
    url_path = autoref_url.split("?")[0].lower()

    if not url_path.endswith('.pdf'):
        # Check if URL looks like a PDF download endpoint (e.g., /avtoreferat.html, /get-file/5144)
        pdf_indicators = ["avtoreferat", "get-file", "download", "file/", "send/", "attachment"]
        is_potential_pdf = any(ind in url_path for ind in pdf_indicators)

        if is_potential_pdf:
            # Try to determine if this is a PDF by checking Content-Type
            print(f"  Проверка потенциального PDF: {autoref_url}...", end="")
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
                    print(f" PDF ({content_type})")
                else:
                    print(f" не PDF ({content_type})")
                    # Step 2: Try to find PDF link on the page
                    print(f"  Поиск PDF на странице {autoref_url}...", end="")
                    resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
                    if not resolved_pdf_url:
                        return None, "Не PDF (ссылка не найдена)", autoref_url
                    print(f" -> {resolved_pdf_url}")
            except Exception as e:
                print(f" Ошибка проверки: {e}")
                # Fallback to finding PDF on page
                print(f"  Поиск PDF на странице {autoref_url}...", end="")
                resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
                if not resolved_pdf_url:
                    return None, "Не PDF (ссылка не найдена)", autoref_url
                print(f" -> {resolved_pdf_url}")
        else:
            # Step 2: Try to find PDF link on the page
            print(f"  Поиск PDF на странице {autoref_url}...", end="")
            resolved_pdf_url = find_autoref_pdf_from_page(autoref_url, fio=fio, date_defend=date_defend)
            if not resolved_pdf_url:
                return None, "Не PDF (ссылка не найдена)", autoref_url
            print(f" -> {resolved_pdf_url}")
    else:
        resolved_pdf_url = autoref_url

    # Check if the resolved URL has changed since last download
    if previous_pdf_url is not None and resolved_pdf_url != previous_pdf_url:
        print(f"\n    Ссылка на автореферат изменилась, скачиваем заново")
        print(f"    Старая: {previous_pdf_url}")
        print(f"    Новая:  {resolved_pdf_url}")
        # Force re-download by setting previous_url to None so we skip cache check
        pass
    elif previous_pdf_url is not None:
        resolved_pdf_url = previous_pdf_url

    date_str = date_defend.replace("-", "_") if date_defend else "unknown"
    filename = f"{sanitize_filename(fio)}_{date_str}.pdf"
    save_path = os.path.join(AUTOREFS_DIR, filename)

    os.makedirs(AUTOREFS_DIR, exist_ok=True)

    if os.path.exists(save_path) and previous_pdf_url is not None and previous_pdf_url == resolved_pdf_url:
        return save_path, "Скачан ранее", resolved_pdf_url

    # Remove old cached file if it exists and URL is unknown or changed
    if os.path.exists(save_path):
        try:
            old_size = os.path.getsize(save_path)
            os.remove(save_path)
            if previous_pdf_url is None:
                print(f"\n    URL старого файла неизвестен, файл удалён ({old_size // 1024} КБ)")
            else:
                print(f"\n    Старый файл удалён ({old_size // 1024} КБ)")
        except Exception:
            pass

    for attempt in range(max_retries):
        try:
            # Ensure the resolved PDF URL is properly encoded
            from urllib.parse import unquote
            decoded_url = unquote(resolved_pdf_url)
            encoded_url = _url_encode_path(decoded_url)
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

            return save_path, "OK", resolved_pdf_url

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if attempt < max_retries - 1:
                print(f"  HTTP {status_code}, повтор...")
            else:
                return None, f"HTTP {status_code}", resolved_pdf_url
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None, str(e), resolved_pdf_url

    return None, "Ошибка скачивания", resolved_pdf_url


def extract_specialty_from_pdf(pdf_path):
    """Извлекает шифр и название специальности из первой страницы PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page = pdf.pages[0]
            text = first_page.extract_text() or ""

        # Normalize special characters
        text = text.replace('\uf02d', '—').replace('\u2013', '—').replace('\u2014', '—')

        # Pattern: "Специальность 2.2.11 — Информационно-измерительные и управляющие системы (технические науки)"
        # или с двоеточием: "Специальность: 2.2.11 — ..."
        m = re.search(
            r'Специальность\s*[:\s]\s*(\S+\.\S+\.\S+)\s*[-—–]\s*([^()\n]+)',
            text
        )
        if m:
            cipher = m.group(1).strip()
            name = re.sub(r'\s+', ' ', m.group(2).strip())
            # Remove guillemets «»
            name = name.replace('«', '').replace('»', '').strip()
            return cipher, name

        # Fallback: look for "Специальность" followed by cipher pattern
        m2 = re.search(
            r'Специальность\s*[:\s]\s*(\S+\.\S+\.\S+)',
            text
        )
        if m2:
            cipher = m2.group(1).strip()
            return cipher, ""

    except Exception as e:
        print(f"  Ошибка извлечения specialty {pdf_path}: {e}")

    return None, None


# ======================== MAIN SYNC ========================

def main():
    global gc_threshold

    print("=" * 70)
    print("ЕЖЕДНЕВНЫЙ СИНХРОНИЗАЦИОННЫЙ СКРИПТ")
    print(f"Дата запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Memory report
    mem = get_memory_mb()
    print(f"Начало: потребление памяти {mem:.0f} МБ / лимит {MAX_MEMORY_MB} МБ")
    print("=" * 70)

    conn = init_db()

    # Dates
    today = datetime.now()
    date_from = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"\nПериод: с {date_from} по {date_to}")

    # Read specialties
    specs = read_specialties()
    if not specs:
        print("Нет специальностей для обработки!")
        conn.close()
        return

    # Counters
    counters = {
        "new": 0,
        "updated": 0,
        "downloaded": 0,
        "extracted": 0,
        "errors": 0,
        "skipped": 0,
        "processed": 0,
    }

    processed_ids = set()

    for spec_idx, (spec_cipher, spec_name) in enumerate(specs):
        print(f"\n{'='*70}")
        print(f"СПЕЦИАЛЬНОСТЬ {spec_idx + 1}/{len(specs)}: {spec_cipher} — {spec_name}")
        print(f"{'='*70}")

        adverts = search_adverts(spec_cipher, date_from, date_to)
        print(f"  Найдено: {len(adverts)}")

        for advert in adverts:
            try:
                process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids)
            except KeyboardInterrupt:
                print("\n\n  !!! Прервано пользователем !!!")
                check_memory()
                raise

        # Специальность обработана — сброс памяти
        print(f"\n  specialty GC...")
        check_memory()

    # Summary
    print(f"\n{'='*70}")
    print("ИТОГИ:")
    print(f"  Новых:           {counters['new']}")
    print(f"  Обновлено:       {counters['updated']}")
    print(f"  Скачано PDF:     {counters['downloaded']}")
    print(f"  Извлечено:       {counters['extracted']}")
    print(f"  Пропущено:       {counters['skipped']}")
    print(f"  Ошибок:          {counters['errors']}")
    final_mem = get_memory_mb()
    print(f"  Память в конце:  {final_mem:.0f} МБ")
    print(f"{'='*70}")

    conn.close()


if __name__ == "__main__":
    main()
