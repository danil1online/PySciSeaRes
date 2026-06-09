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
            autoref_url, autoref_path, downloaded
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
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

    # Check if enough publications already exist for this advert
    c.execute("SELECT COUNT(*) FROM publications WHERE advert_id = ?", (adv_id,))
    existing_count = c.fetchone()[0]
    if existing_count >= 8:
        print(f"    Пропуск: уже есть {existing_count} публикаций в БД")
        counters["skipped"] += 1
        return True

    # Download autoref
    autoref_url = detail.get("autoref_site")
    if autoref_url:
        save_path, status, _ = download_autoref(autoref_url, fio, date_defend)
        print(f"    Автореферат: {status}", end="")
        if save_path:
            size_kb = os.path.getsize(save_path) // 1024
            print(f" ({size_kb} КБ)", end="")
            counters["downloaded"] += 1
            counters["extracted"] += 1

            # Update path in DB and extract specialty name from PDF
            c.execute("UPDATE adverts SET autoref_path = ?, downloaded = 1 WHERE id = ?",
                      (save_path, adv_id))

            # Extract specialty from PDF first page (single page, minimal memory)
            pdf_cipher, pdf_name = extract_specialty_from_pdf(save_path)
            if pdf_name and not spec_name:
                # Update specialty_text from PDF if sci_spec.txt has no name
                c.execute("UPDATE adverts SET specialty_text = ? WHERE id = ?",
                          (pdf_name, adv_id))
                print(f"\n    Specialty из PDF: {pdf_cipher} - {pdf_name}", end="")
            conn.commit()

            # --- Extract ALL data from PDF in ONE pass ---
            print("\n    Извлечение данных из PDF...", end="")
            try:
                # Open PDF once, read text once, close
                pdf_full_text = _read_pdf_full(save_path)

                # Extract publications (using extracted text)
                raw_pubs, found_section, llm_time = extract_publications_from_pdf_text(
                    pdf_full_text
                )
                struct_pubs = parse_pub_to_json(raw_pubs)
                c.execute("DELETE FROM publications WHERE advert_id = ?", (adv_id,))
                for num, p in enumerate(struct_pubs, 1):
                    c.execute(
                        "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages) VALUES (?,?,?,?,?,?,?)",
                        (adv_id, num, p["authors"], p["title"], p["journal"], p["year"], p["pages"])
                    )
                conn.commit()
                print(f" OK ({len(struct_pubs)} публикаций)", end="")
                if found_section:
                    print(f"\n      Раздел: {found_section}")
                if llm_time:
                    print(f"\n      LLM время: {llm_time:.1f}с")

                # Extract supervisor (using same text, pages 1-3)
                print("    Извлечение руководителя...", end="")
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

            except Exception as e:
                print(f" Ошибка извлечения: {e}")

            # Clear PDF text from memory after processing
            del pdf_full_text

            # Memory check after each advert
            if counters["processed"] % BATCH_SIZE == 0:
                check_memory()
        else:
            print()
    else:
        print("    Нет ссылки на автореферат")

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
        downloaded INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

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


def find_autoref_pdf_from_page(url):
    """Ищет ссылку на PDF-автореферат на веб-странице."""
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
    links = soup.find_all("a")

    keywords = ["автореферат", "авторерат", "авторефер"]
    pdf_keywords = ["pdf", "pdf"]

    for link in links:
        href = link.get("href", "")
        text = (link.get_text() or "").lower()
        if not href:
            continue

        # Check if link text contains "автореферат" (case-insensitive)
        has_keyword = any(kw in text for kw in keywords)
        if not has_keyword:
            continue

        # Resolve relative URL
        if href.startswith("http"):
            full_url = href
        else:
            from urllib.parse import urljoin
            full_url = urljoin(url, href)

        # Check if it's a PDF
        if full_url.lower().endswith(".pdf"):
            return full_url

    return None


def download_autoref(autoref_url, fio, date_defend, max_retries=3):
    if not autoref_url:
        return None, "Нет ссылки", autoref_url

    # Step 1: Check if URL is direct PDF link
    url_path = autoref_url.split("?")[0].lower()
    if not url_path.endswith('.pdf'):
        # Step 2: Try to find PDF link on the page
        print(f"  Поиск PDF на странице {autoref_url}...", end="")
        pdf_url = find_autoref_pdf_from_page(autoref_url)
        if not pdf_url:
            return None, "Не PDF (ссылка не найдена)", autoref_url
        print(f" -> {pdf_url}")
        autoref_url = pdf_url

    date_str = date_defend.replace("-", "_") if date_defend else "unknown"
    filename = f"{sanitize_filename(fio)}_{date_str}.pdf"
    save_path = os.path.join(AUTOREFS_DIR, filename)

    os.makedirs(AUTOREFS_DIR, exist_ok=True)

    if os.path.exists(save_path):
        return save_path, "Скачан ранее", autoref_url

    for attempt in range(max_retries):
        try:
            resp = requests.get(
                autoref_url, stream=True, timeout=120,
                headers={"User-Agent": HEADERS["User-Agent"] if "User-Agent" in HEADERS else "Mozilla/5.0"},
                verify=False,
            )
            resp.raise_for_status()

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > 0 and content_length < MIN_SIZE:
                return None, f"Малый размер ({content_length // 1024} КБ)", autoref_url

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            actual_size = os.path.getsize(save_path)
            if actual_size < 300 * 1024:
                os.remove(save_path)
                return None, f"Слишком маленький файл ({actual_size // 1024} КБ)", autoref_url

            return save_path, "OK", autoref_url

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if attempt < max_retries - 1:
                print(f"  HTTP {status_code}, повтор...")
            else:
                return None, f"HTTP {status_code}", autoref_url
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None, str(e), autoref_url

    return None, "Ошибка скачивания", autoref_url


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
