#!/usr/bin/env python3
"""Ежедневный синхронизационный скрипт.
Скачивает новые авторефераты из VAK, извлекает публикации и данные руководителя,
сохраняет в БД. Запускается по cron каждый день в 3:00.

Модульная структура:
  daily_sync/db.py     — инициализация БД
  daily_sync/vak_api.py — работа с VAK API
  daily_sync/pdf.py     — обработка PDF (скачивание, поиск, парсинг)
  daily_sync/memory.py  — управление памятью
  daily_sync/__init__.py — точка входа (process_advert, main)
"""
import sys
import os
import time
from datetime import datetime, timedelta

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    MAX_PUBLICATIONS, SESSION_SECRET_KEY, SCI_SPEC_FILE,
)

# Import from modules
from vak_sync.db import init_db
from vak_sync.vak_api import read_specialties
from vak_sync.memory import (
    get_memory_mb, check_memory, force_gc,
    MAX_MEMORY_MB, CRITICAL_MEMORY_MB, BATCH_SIZE,
)
from vak_sync.pdf import (
    download_autoref, extract_specialty_from_pdf, _read_pdf_full,
    sanitize_filename, find_autoref_pdf_from_page,
)

# Import extraction modules
from extractors.publications import (
    extract_publications_from_pdf_text,
    parse_pub_to_json,
)
from extractors.supervisor import (
    extract_supervisor_from_pdf_text,
)

# Re-export for backward compatibility
__all__ = [
    'init_db', 'read_specialties', 'download_autoref',
    'extract_specialty_from_pdf', 'sanitize_filename',
    'find_autoref_pdf_from_page',
    'get_memory_mb', 'check_memory',
    'process_advert', 'main',
]


def process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids):
    """Обрабатывает одно объявление: проверка, скачивание, извлечение.

    Возвращает True если объявление обработано, False если пропущено/ошибка.
    """
    adv_id = advert["id"]
    if adv_id in processed_ids:
        return False
    processed_ids.add(adv_id)

    from vak_sync.vak_api import get_advert_detail

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

    except Exception as e:
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


def main():
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

        from vak_sync.vak_api import search_adverts
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
