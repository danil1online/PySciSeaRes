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
from extractors.email_search import find_emails_for_publications
from search import (
    increment_email_search_attempts,
    increment_pub_extract_attempts,
    is_new_defense,
)

# Re-export for backward compatibility
__all__ = [
    'init_db', 'read_specialties', 'download_autoref',
    'extract_specialty_from_pdf', 'sanitize_filename',
    'find_autoref_pdf_from_page',
    'get_memory_mb', 'check_memory',
    'process_advert', 'main',
]


def _extract_publications(conn, adv_id, pdf_path, counters):
    """Извлекает публикации из автореферата.

    Этапы:
    1. JSON-LLM — отправляем последние ~5 страниц в LLM
    2. Regex — ищем раздел публикаций, анализируем
    3. Объединяем результаты, проверяем на дубли по названию
    4. Записываем в БД

    Возвращает: (количество извлечённых публикаций, True если успешно)
    """
    print("\n    Извлечение данных из PDF...", end="")
    counters["extracted"] += 1
    pdf_full_text = None
    try:
        pdf_full_text = _read_pdf_full(pdf_path)

        # JSON-LLM (последние ~5 страниц)
        raw_pubs, found_section, llm_time = extract_publications_from_pdf_text(pdf_full_text)
        struct_pubs = parse_pub_to_json(raw_pubs)

        # Удаляем старые публикации, записываем новые
        c = conn.cursor()
        c.execute("DELETE FROM publications WHERE advert_id = ?", (adv_id,))
        for num, p in enumerate(struct_pubs, 1):
            authors = p["authors"]
            if isinstance(authors, list):
                authors = ", ".join(authors)
            c.execute(
                "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages, email, source_url, source_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (adv_id, num, authors, p["title"], p["journal"], p["year"], p["pages"], "", "", "")
            )
        conn.commit()

        print(f" OK ({len(struct_pubs)} публикаций)", end="")
        if found_section:
            print(f"\n      Раздел: {found_section}")
        if llm_time:
            print(f"\n      LLM время: {llm_time:.1f}с")

        return len(struct_pubs), True

    except Exception as e:
        print(f" Ошибка извлечения: {e}")
        return 0, False
    finally:
        if pdf_full_text:
            del pdf_full_text


def _search_emails(conn, adv_id, pdf_path, counters):
    """Ищет email-адреса для всех публикаций объявления.

    Для каждой публикации:
    - Ищем по открытым источникам (Semantic Scholar, Crossref, DOI)
    - Скачиваем PDF/HTML и извлекаем email (LLM + regex)
    - Записываем email и ссылку в БД

    Возвращает: (найденные email, источники)
    """
    c = conn.cursor()
    c.execute("SELECT pub_number, authors, title, journal, year, pages FROM publications WHERE advert_id = ? ORDER BY pub_number", (adv_id,))
    db_pubs = c.fetchall()
    struct_pubs = [
        {"authors": r[1], "title": r[2], "journal": r[3], "year": r[4], "pages": r[5]}
        for r in db_pubs
    ]

    if not struct_pubs:
        return

    # Читаем PDF если нужно
    pdf_full_text = None
    try:
        pdf_full_text = _read_pdf_full(pdf_path)

        print("\n    Поиск email авторов...", end="")
        email_results = find_emails_for_publications(struct_pubs, pdf_full_text)
        email_count = 0
        source_count = 0

        for i, (pub, emails, source) in enumerate(email_results):
            if emails:
                email_str = "; ".join(emails)
                source_url = source.get('url', '') if source else ''
                source_name = source.get('source_name', '') if source else ''
                c.execute(
                    "UPDATE publications SET email = ?, source_url = ?, source_name = ? WHERE advert_id = ? AND pub_number = ?",
                    (email_str, source_url, source_name, adv_id, i + 1)
                )
                email_count += 1
            elif source:
                source_url = source.get('url', '')
                source_name = source.get('source_name', '')
                c.execute(
                    "UPDATE publications SET source_url = ?, source_name = ? WHERE advert_id = ? AND pub_number = ?",
                    (source_url, source_name, adv_id, i + 1)
                )
                source_count += 1

        conn.commit()

        if email_count:
            print(f" OK ({email_count} email, {source_count} источников)")
        else:
            print(f" (не найдены)")

    except Exception as e:
        print(f" Ошибка поиска email: {e}")
        conn.commit()
    finally:
        if pdf_full_text:
            del pdf_full_text


def process_advert(conn, advert, spec_cipher, spec_name, counters, processed_ids):
    """Обрабатывает одно объявление о защите.

    Логика:
    1) НОВАЯ защита (ФИО + дата защиты не в БД):
       - Сохраняем объявление, скачиваем автореферат
       - Извлекаем публикации (JSON-LLM → regex → merge)
       - Записываем в БД, отмечаем 1 попытку извлечения
       - Ищем email-адреса для всех публикаций
       - Записываем email/ссылки, отмечаем 1 попытку поиска

    2) СУЩЕСТВУЮЩАЯ защита (ФИО + дата защиты уже в БД):
       - Загружаем имеющиеся данные
       - Если >= 5 публикаций: проверяем попытки поиска email
         - >= 2 попыток — пропускаем
         - 1 попытка, есть email — пропускаем
         - 1 попытка, нет email — ищем email
       - Если < 5 публикаций: проверяем попытки извлечения
         - >= 2 попыток — пропускаем
         - 0-1 попыток — извлекаем публикации заново

    Возвращает True если обработано, False если пропущено.
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

    c = conn.cursor()

    # Проверяем: новая защита или существующая (по ФИО + дата защиты)
    new_defense = is_new_defense(fio, date_defend)

    # Сохраняем/обновляем объявление
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
            None,
            None,
            0,
        ))
        conn.commit()

        c.execute("SELECT COUNT(*) FROM adverts WHERE id = ?", (adv_id,))
        if c.fetchone()[0] == 1:
            counters["new"] += 1
        else:
            counters["updated"] += 1

    except Exception as e:
        print(f"    Ошибка БД: {e}")
        counters["errors"] += 1
        return True

    # Проверяем текущее состояние
    c.execute("SELECT COUNT(*) FROM publications WHERE advert_id = ?", (adv_id,))
    pub_count = c.fetchone()[0]

    c.execute("SELECT autoref_path, autoref_pdf_url, downloaded FROM adverts WHERE id = ?", (adv_id,))
    row = c.fetchone()
    autoref_path = row[0] if row else None
    autoref_pdf_url = row[1] if row else None
    autoref_downloaded = row[2] if row else 0

    autoref_url = detail.get("autoref_site")

    # === НОВАЯ ЗАЩИТА ===
    if new_defense:
        print("    Статус: новая защита")

        # Скачиваем автореферат
        if not autoref_url:
            print("    Нет ссылки на автореферат")
            counters["errors"] += 1
            return True

        save_path, status, resolved_url = download_autoref(
            autoref_url, fio, date_defend, previous_pdf_url=autoref_pdf_url
        )
        if not save_path:
            print(f"    Автореферат: {status}")
            counters["errors"] += 1
            return True

        counters["downloaded"] += 1
        print(f"    Автореферат: скачан ({os.path.getsize(save_path) // 1024} КБ)")

        # Обновляем путь в БД
        c.execute("UPDATE adverts SET autoref_path = ?, autoref_pdf_url = ?, downloaded = 1 WHERE id = ?",
                  (save_path, resolved_url, adv_id))
        conn.commit()

        # Извлекаем specialty из PDF
        pdf_cipher, pdf_name = extract_specialty_from_pdf(save_path)
        if pdf_name and not spec_name:
            c.execute("UPDATE adverts SET specialty_text = ? WHERE id = ?",
                      (pdf_name, adv_id))
            print(f"\n    Specialty из PDF: {pdf_cipher} - {pdf_name}")
        conn.commit()

        # Извлекаем публикации (JSON-LLM → regex → merge)
        extract_count, extract_ok = _extract_publications(conn, adv_id, save_path, counters)
        if extract_ok:
            increment_pub_extract_attempts(adv_id)
            print(f"\n    Попытка извлечения #1 завершена")

        # Ищем email-адреса
        if pub_count + extract_count > 0:
            increment_email_search_attempts(adv_id)
            _search_emails(conn, adv_id, save_path, counters)

    # === СУЩЕСТВУЮЩАЯ ЗАЩИТА ===
    else:
        print("    Статус: существующая защита")
        autoref_newly_downloaded = False

        # Проверяем, скачан ли автореферат
        if not autoref_path:
            autoref_newly_downloaded = True
            # Автореферат не скачан — скачиваем
            if not autoref_url:
                print("    Нет ссылки на автореферат")
                counters["errors"] += 1
                return True

            save_path, status, resolved_url = download_autoref(
                autoref_url, fio, date_defend, previous_pdf_url=autoref_pdf_url
            )
            if not save_path:
                print(f"    Автореферат: {status}")
                counters["errors"] += 1
                return True

            counters["downloaded"] += 1
            print(f"    Автореферат: скачан ({os.path.getsize(save_path) // 1024} КБ)")

            c.execute("UPDATE adverts SET autoref_path = ?, autoref_pdf_url = ?, downloaded = 1 WHERE id = ?",
                      (save_path, resolved_url, adv_id))
            conn.commit()
        else:
            save_path = autoref_path
            resolved_url = autoref_pdf_url
            print(f"    Автореферат: найден ({os.path.getsize(save_path) // 1024} КБ)")

        # Обновляем specialty из PDF
        pdf_cipher, pdf_name = extract_specialty_from_pdf(save_path)
        if pdf_name:
            c.execute("UPDATE adverts SET specialty_text = ? WHERE id = ?",
                      (pdf_name, adv_id))
            conn.commit()

        # === ЛОГИКА ДЛЯ СУЩЕСТВУЮЩЕЙ ЗАЩИТЫ ===
        # Ищем email, если PDF был скачан в этом запуске
        if autoref_newly_downloaded and pub_count > 0:
            increment_email_search_attempts(adv_id)
            _search_emails(conn, adv_id, save_path, counters)

    # === Извлечение руководителя (всегда) ===
    print("    Извлечение руководителя...", end="")
    try:
        pdf_full_text = _read_pdf_full(save_path)
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
