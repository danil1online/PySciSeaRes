#!/usr/bin/env python3
"""Ежедневный синхронизационный скрипт.
Скачивает новые авторефераты из VAK, извлекает публикации и данные руководителя,
сохраняет в БД. Запускается по cron каждый день в 3:00.
"""
import sys
import os
import re
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup logging first
from logging_config import setup_logging
app_logger, sync_logger = setup_logging()

from config import (
    MAX_PUBLICATIONS, SESSION_SECRET_KEY, SCI_SPEC_FILE,
)
from vak_sync.db import init_db, get_db
from vak_sync.vak_api import read_specialties
from vak_sync.memory import (
    get_memory_mb, check_memory, force_gc,
    MAX_MEMORY_MB, CRITICAL_MEMORY_MB, BATCH_SIZE,
)
from vak_sync.pdf import (
    download_autoref, extract_specialty_from_pdf, _read_pdf_full,
    sanitize_filename, find_autoref_pdf_from_page,
)
from extractors.publications import (
    extract_publications_from_pdf_text,
    parse_pub_to_json,
)
from extractors.supervisor import (
    extract_supervisor_from_pdf_text,
)
from extractors.city_org import extract_city_and_org
from search import (
    increment_pub_extract_attempts,
    is_new_defense,
)

logger = sync_logger


def _print_and_log(msg, level="INFO"):
    """Выводит сообщение в stdout."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level}: {msg}")


def _search_publication_url(pub, autoref_text):
    """Ищет URL публикации через DuckDuckGo и научные базы."""
    import requests
    from bs4 import BeautifulSoup
    from extractors.email_search import (
        _search_duckduckgo, _analyze_ddg_link, _search_semantic_scholar,
        _search_crossref, _resolve_doi, _titles_match, _find_pdf_url_from_html,
    )

    pub_title = pub.get('title', '')
    pub_authors = pub.get('authors', '')
    pub_year = pub.get('year')

    if isinstance(pub_authors, list):
        pub_authors = ", ".join(pub_authors)

    # 1. Проверяем DOI в тексте автореферата
    if autoref_text:
        doi_patterns = [
            r'doi[.:]\s*(10\.\d+/[^\s\n]+)',
            r'https?://doi\.org/(10\.\d+/[^\s\n]+)',
            r'DOI:\s*(10\.\d+/[^\s\n]+)',
        ]
        import re
        for pattern in doi_patterns:
            matches = re.findall(pattern, autoref_text, re.IGNORECASE)
            for doi in matches:
                doi = doi.strip().rstrip('.;,')
                if doi and _titles_match(pub_title, ''):
                    resolved = _resolve_doi(doi)
                    if resolved and resolved.get('status') == 200:
                        return resolved['resolved_url'], 'DOI resolver'

    # 2. DuckDuckGo поиск
    ddg_results = _search_duckduckgo(pub, max_results=5)
    if ddg_results:
        for ddg in ddg_results:
            source = _analyze_ddg_link(ddg, pub_title, pub_authors, pub_year)
            if source:
                return source['url'], source.get('source_name', 'DuckDuckGo')

    # 3. Semantic Scholar
    ss_results = _search_semantic_scholar(pub_title, pub_authors, pub_year)
    if ss_results:
        for ss in ss_results:
            ss_title = ss.get("title", "")
            if _titles_match(pub_title, ss_title):
                if ss.get("url"):
                    return ss['url'], 'Semantic Scholar'

    # 4. Crossref
    cr_results = _search_crossref(pub_title, pub_authors, pub_year)
    if cr_results:
        for cr in cr_results:
            cr_title = cr.get("title", "")
            if _titles_match(pub_title, cr_title):
                doi = cr.get("DOI")
                if doi:
                    resolved = _resolve_doi(doi)
                    if resolved and resolved.get('status') == 200:
                        return resolved['resolved_url'], 'Crossref'

    return None, None


def _verify_publication_urls(conn, advert_db_id):
    """Проверяет URL публикаций: открывает страницу и сравнивает название и авторов."""
    import requests
    from bs4 import BeautifulSoup

    c = conn.cursor()
    c.execute("SELECT id, pub_number, title, authors, source_url FROM publications WHERE advert_id = ? AND source_url IS NOT NULL AND source_url != ''", (advert_db_id,))
    pubs = c.fetchall()

    if not pubs:
        return 0

    verified_count = 0
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    for pub_id, pub_num, title, authors, source_url in pubs:
        try:
            resp = requests.get(source_url, headers={"User-Agent": user_agent}, timeout=15, allow_redirects=True)
            if resp.status_code != 200:
                _print_and_log(f"Publication {pub_num}: URL returned {resp.status_code}", "WARNING")
                continue

            # Check if it's a PDF
            content_type = resp.headers.get("Content-Type", "").lower()
            if "application/pdf" in content_type:
                _print_and_log(f"Publication {pub_num}: PDF source, skipping HTML check", "DEBUG")
                verified_count += 1
                continue

            # Parse HTML and check title/authors
            soup = BeautifulSoup(resp.text, "lxml")
            page_title = soup.title.string if soup.title else ""

            # Normalize titles for comparison
            def normalize(t):
                t = t.lower()
                t = re.sub(r'[^\w\s]', ' ', t)
                t = re.sub(r'\s+', ' ', t).strip()
                return t

            pub_title_norm = normalize(title)
            page_title_norm = normalize(page_title)

            if not pub_title_norm or not page_title_norm:
                verified_count += 1
                continue

            # Check title match
            pub_words = set(pub_title_norm.split())
            page_words = set(page_title_norm.split())
            overlap = len(pub_words & page_words)
            min_overlap = 3 if len(pub_words) > 5 else 2

            if overlap >= min_overlap:
                verified_count += 1
                _print_and_log(f"Publication {pub_num}: title match verified", "DEBUG")
            else:
                _print_and_log(f"Publication {pub_num}: title mismatch (pub='{title[:50]}', page='{page_title[:50]}')", "WARNING")

        except Exception as e:
            _print_and_log(f"Publication {pub_num}: URL verification error: {e}", "ERROR")

    return verified_count


def _extract_publications(conn, advert_db_id, pdf_path, counters):
    """Извлекает публикации из автореферата и ищет для них URL."""
    _print_and_log("Extracting publications from PDF...")
    counters["extracted"] += 1
    pdf_full_text = None
    try:
        pdf_full_text = _read_pdf_full(pdf_path)

        raw_pubs, found_section, llm_time = extract_publications_from_pdf_text(pdf_full_text)
        struct_pubs = parse_pub_to_json(raw_pubs)

        c = conn.cursor()
        c.execute("DELETE FROM publications WHERE advert_id = ?", (advert_db_id,))
        urls_found = 0
        for num, p in enumerate(struct_pubs, 1):
            authors = p["authors"]
            if isinstance(authors, list):
                authors = ", ".join(authors)

            # Search for publication URL
            url = None
            source_name = ""
            try:
                url, source_name = _search_publication_url(p, pdf_full_text)
                if url:
                    urls_found += 1
                    _print_and_log(f"Publication {num}: found URL - {url[:80]}", "DEBUG")
            except Exception as e:
                _print_and_log(f"Publication {num}: URL search error - {e}", "DEBUG")

            c.execute(
                "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages, email, source_url, source_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (advert_db_id, num, authors, p["title"], p["journal"], p["year"], p["pages"], "", url or "", source_name)
            )
        conn.commit()

        _print_and_log(f"Extracted {len(struct_pubs)} publications (section: {found_section}), {urls_found} URLs found")
        if llm_time:
            _print_and_log(f"LLM time: {llm_time:.1f}s", "DEBUG")

        # Verify publication URLs
        if struct_pubs:
            verified = _verify_publication_urls(conn, advert_db_id)
            _print_and_log(f"Publication URL verification: {verified}/{len(struct_pubs)} verified")

        return len(struct_pubs), True

    except Exception as e:
        _print_and_log(f"Extraction error: {e}", "ERROR")
        return 0, False
    finally:
        if pdf_full_text:
            del pdf_full_text


def process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids):
    """Обрабатывает одно объявление о защите."""
    advert_db_id = advert["id"]
    if advert_db_id in processed_ids:
        return False
    processed_ids.add(advert_db_id)

    from vak_sync.vak_api import get_advert_detail

    fio = advert.get("fio", "Неизвестно")
    date_defend = advert.get("date_defend", "")
    counters["processed"] += 1

    _print_and_log(f"[{advert_db_id[:8]}...] {fio} | {date_defend}")

    detail = get_advert_detail(advert_db_id)
    if not detail:
        _print_and_log("Error getting detail", "ERROR")
        counters["errors"] += 1
        return True

    c = conn.cursor()

    new_defense = is_new_defense(fio, date_defend)
    advert_db_id = detail.get("id")

    try:
        c.execute("""INSERT OR REPLACE INTO adverts (
            id, old_id, date_defend, fio, dissertation_name,
            specialty_cipher, specialty_text,
            council_cipher, defend_org, org_address, org_phone,
            autoref_url, autoref_path, autoref_pdf_url, downloaded,
            city, organization_name
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            advert_db_id,
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
            None,
            None,
            0,
            None,
            None,
        ))
        conn.commit()

        c.execute("SELECT COUNT(*) FROM adverts WHERE id = ?", (advert_db_id,))
        if c.fetchone()[0] == 1:
            counters["new"] += 1
        else:
            counters["updated"] += 1

    except Exception as e:
        _print_and_log(f"DB error: {e}", "ERROR")
        counters["errors"] += 1
        return True

    c.execute("SELECT COUNT(*) FROM publications WHERE advert_id = ?", (advert_db_id,))
    pub_count = c.fetchone()[0]

    c.execute("SELECT autoref_path, autoref_pdf_url, downloaded FROM adverts WHERE id = ?", (advert_db_id,))
    row = c.fetchone()
    autoref_path = row[0] if row else None
    autoref_pdf_url = row[1] if row else None
    autoref_downloaded = row[2] if row else 0

    autoref_url = detail.get("autoref_site")

    if autoref_path and os.path.exists(autoref_path):
        _print_and_log(f"Skipped: existing defense, PDF found ({os.path.getsize(autoref_path) // 1024} KB)")
        counters["skipped"] += 1
        return True

    status_label = "new defense" if new_defense else "existing defense"
    _print_and_log(f"Status: {status_label}")

    if not autoref_url:
        _print_and_log("No autoref URL", "ERROR")
        counters["errors"] += 1
        return True

    save_path, status, resolved_url = download_autoref(
        autoref_url, fio, date_defend, previous_pdf_url=autoref_pdf_url
    )
    if not save_path:
        _print_and_log(f"Autoref: {status}", "ERROR")
        counters["errors"] += 1
        return True

    counters["downloaded"] += 1
    _print_and_log(f"Autoref downloaded ({os.path.getsize(save_path) // 1024} KB)")

    # Extract city and organization from PDF
    city, org_name = extract_city_and_org(save_path)
    if city:
        _print_and_log(f"City extracted: {city}")
    if org_name:
        _print_and_log(f"Organization extracted: {org_name}")

    c.execute("UPDATE adverts SET autoref_path = ?, autoref_pdf_url = ?, downloaded = 1, city = ?, organization_name = ? WHERE id = ?",
              (save_path, resolved_url, advert_db_id, city, org_name))
    conn.commit()

    pdf_cipher, pdf_name = extract_specialty_from_pdf(save_path)
    if pdf_name and not spec_name:
        c.execute("UPDATE adverts SET specialty_text = ? WHERE id = ?",
                  (pdf_name, advert_db_id))
        _print_and_log(f"Specialty from PDF: {pdf_cipher} - {pdf_name}")
    conn.commit()

    extract_count, extract_ok = _extract_publications(conn, advert_db_id, save_path, counters)
    if extract_ok:
        increment_pub_extract_attempts(advert_db_id)
        _print_and_log("Publication extraction attempt #1 completed")

    _print_and_log("Extracting supervisor...")
    try:
        pdf_full_text = _read_pdf_full(save_path)
        sup = extract_supervisor_from_pdf_text(pdf_full_text)
        sup_name = sup.get("supervisor_name")
        sup_work = sup.get("supervisor_work")
        c.execute("UPDATE adverts SET supervisor_name = ?, supervisor_work = ? WHERE id = ?",
                  (sup_name, sup_work, advert_db_id))
        conn.commit()
        if sup_name:
            _print_and_log(f"Supervisor: {sup_name}")
        else:
            _print_and_log("Supervisor not found", "DEBUG")
        del pdf_full_text
    except Exception as e:
        _print_and_log(f"Supervisor extraction error: {e}", "ERROR")

    if counters["processed"] % BATCH_SIZE == 0:
        check_memory()

    return True


def main():
    print("=" * 70)
    print("DAILY SYNCHRONIZATION SCRIPT")
    print(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    mem = get_memory_mb()
    _print_and_log(f"Start: memory {mem:.0f} MB / limit {MAX_MEMORY_MB} MB")
    print("=" * 70)

    init_db()
    conn = get_db()

    today = datetime.now()
    date_from = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    _print_and_log(f"Period: {date_from} to {date_to}")

    specs = read_specialties()
    if not specs:
        _print_and_log("No specialties to process!", "ERROR")
        conn.close()
        return

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
        _print_and_log(f"Specialty {spec_idx + 1}/{len(specs)}: {spec_cipher} — {spec_name}")
        print(f"\n{'='*70}")
        print(f"SPECIALTY {spec_idx + 1}/{len(specs)}: {spec_cipher} — {spec_name}")
        print(f"{'='*70}")

        from vak_sync.vak_api import search_adverts
        adverts = search_adverts(spec_cipher, date_from, date_to)
        _print_and_log(f"Found: {len(adverts)}")

        for advert in adverts:
            try:
                process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids)
            except KeyboardInterrupt:
                logger.critical("Interrupted by user")
                check_memory()
                raise

        _print_and_log("Specialty GC...", "DEBUG")
        check_memory()

    print(f"\n{'='*70}")
    print("SUMMARY:")
    print(f"  New:             {counters['new']}")
    print(f"  Updated:         {counters['updated']}")
    print(f"  Downloaded PDF:  {counters['downloaded']}")
    print(f"  Extracted:       {counters['extracted']}")
    print(f"  Skipped:         {counters['skipped']}")
    print(f"  Errors:          {counters['errors']}")
    final_mem = get_memory_mb()
    _print_and_log(f"Final memory: {final_mem:.0f} MB")
    print(f"  Memory end:     {final_mem:.0f} MB")
    print(f"{'='*70}")

    conn.close()


if __name__ == "__main__":
    main()
