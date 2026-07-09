import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime
import sqlite3
import pdfplumber
import re
import os


from config import API_BASE, LLM_API_URL, HEADERS, DOWNLOAD_HEADERS, MIN_SIZE
LLM_HEADERS = {"Content-Type": "application/json"}
AUTOREFS_DIR = "autorefs"
LLM_MAX_TOKENS = 2000
PUB_EXTRACT_SYSTEM_PROMPT = (
    "Ты — помощник по извлечению библиографических данных из авторефератов диссертаций. "
    "Твоя задача — извлечь список всех публикаций автора по теме исследования из текста "
    "раздела 'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ'. "
    "Верни КАЖДУЮ публикацию на отдельной строке. "
    "Верни ТОЛЬКО список публикаций, без заголовков, без комментариев, без нумерации. "
    "Каждая публикация должна содержать: название, авторов, источник (журнал/сборник), год, номер, страницы. "
    "Убери ВСЕ дубликаты. Если публикация встречается несколько раз — оставь один раз. "
    "Если раздел публикаций не найден — напиши только: НЕ НАЙДЕН"
)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS adverts (
        id TEXT PRIMARY KEY,
        old_id INTEGER,
        date_defend TEXT,
        fio TEXT,
        dissertation_name TEXT,
        publication_date TEXT,
        num_and_date_version TEXT,
        dissertation_type TEXT,
        specialty_text TEXT,
        second_specialty TEXT,
        branch TEXT,
        council_cipher TEXT,
        defend_org TEXT,
        org_address TEXT,
        org_phone TEXT,
        card_site TEXT,
        autoref_site TEXT,
        fulltext_site TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advert_id TEXT NOT NULL,
        publication_number INTEGER NOT NULL,
        text TEXT NOT NULL,
        FOREIGN KEY (advert_id) REFERENCES adverts(id) ON DELETE CASCADE
    )""")

    conn.commit()
    return conn


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
        resp = requests.get(f"{API_BASE}/att/adverts/", params=params, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            break

        all_results.extend(results)
        print(f"  Загружено страниц: {page} ({len(all_results)} из {data.get('count', '?')})")

        if data.get("next"):
            page += 1
        else:
            break

    return all_results


def get_advert_detail(advert_id):
    resp = requests.get(f"{API_BASE}/att/adverts/{advert_id}/", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def save_advert_to_db(conn, advert_data):
    c = conn.cursor()
    try:
        c.execute("""INSERT OR REPLACE INTO adverts (
            id, old_id, date_defend, fio, dissertation_name,
            publication_date, num_and_date_version, dissertation_type,
            specialty_text, second_specialty, branch, council_cipher,
            defend_org, org_address, org_phone, card_site, autoref_site, fulltext_site
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            advert_data["id"],
            advert_data.get("old_id"),
            advert_data.get("date_defend"),
            advert_data.get("fio"),
            advert_data.get("dissertation_name"),
            advert_data.get("publication_date"),
            advert_data.get("num_and_date_version"),
            advert_data.get("dissertation_type"),
            advert_data.get("specialty"),
            advert_data.get("second_specialty"),
            advert_data.get("branch"),
            advert_data.get("council_cipher"),
            advert_data.get("defend_org"),
            advert_data.get("org_address"),
            advert_data.get("org_phone"),
            advert_data.get("card_site"),
            advert_data.get("autoref_site"),
            advert_data.get("fulltext_site"),
        ))
        conn.commit()
    except sqlite3.Error as e:
        print(f"  Ошибка записи в БД: {e}")


def _send_to_llm(text):
    """Отправить текст в LLM для извлечения публикаций."""
    prompt = (
        "Извлеки из текста ТОЛЬКО реальные научные публикации автора. "
        "Публикация — это точное цитирование: ФИО автора, название работы, журнал/источник, год, том/номер, страницы. "
        "ВАЖНО: НЕ генерируй и не придумывай публикации! "
        "Если публикация не указана явно в тексте — НЕ включай её. "
        "НЕ придумывай DOI, названия журналов, номера страниц — если их нет в тексте, не создавай их. "
        "НЕ включай: выводы диссертации, описание структуры, задачи, методы, результаты, "
        "информацию о руководителе/оппонентах, апробацию, списки условных обозначений. "
        "Верни ТОЛЬКО нумерованный список цитирований, которые буквально присутствуют в тексте. "
        "Если публикаций нет — напиши: НЕ НАЙДЕНЫ\n\nТекст:\n\n" + text
    )

    resp = requests.post(
        LLM_API_URL,
        headers=LLM_HEADERS,
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": 0.1,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _extract_publications_regex(pub_text):
    """Regex-извлечение публикаций из текстового блока."""
    # Split by numbered items: newline + digit(s) + period + space
    raw_items = re.split(r'(?<=\n)\d+\.\s', pub_text)

    publications = []
    for raw in raw_items[1:]:  # skip first (header before first number)
        if not raw.strip():
            continue
        # Normalize whitespace (join lines, remove extra spaces/hyphens at line ends)
        text = raw.strip()
        text = re.sub(r'\s*\-\s*\n\s*', ' ', text)  # fix hyphenation
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 20:
            publications.append(text)
    return publications


def _is_likely_publication(text):
    """Оценить, похож ли текст на библиографическую публикацию."""
    low = text.lower().strip()

    # Чёткие признаки НЕ-публикации
    not_pub_patterns = [
        "основные положения", "диссертация состоит", "во введении обоснован",
        "в первой главе", "все вышеуказанн", "в результате обзора",
        "научный проект", "внедрен в учебн", "докладывались на",
        "обзор гидрометеорологич", "процессов поддерживаются",
        "списив условных обозначен", "введение, 4 глав",
        "актуальност диссертационн", "положения вынесены на",
        "научный руководитель", "научный советник", "оппоненты",
        "защита состоял", "диссертация доступн",
        "введение в диссертационн",
    ]
    if any(p in low for p in not_pub_patterns):
        return False

    # Предлоги/союзы в начале — не публикации
    prep_starts = ["на основе", "на основании", "в ходе", "в рамках", "по результатам",
                   "по данным", "в связи", "в соответствии", "в течение",
                   "результаты диссертации", "результаты исследования",
                   "результаты работы", "результаты анализа",
                   "положения вынесены", "тезисы доложены",
                   "на привлечении", "на расчетах", "на выявлении",
                   "на построении", "на разработке", "на оценке",
    ]
    if any(low.startswith(p) for p in prep_starts):
        return False

    # Не публикации — это выводы/результаты, начинающиеся с глаголов/существительных
    conclusion_starts = [
        "анализ", "разработ", "провед", "синтезир", "исслед", "получен",
        "представл", "доказан", "определен", "установлен", "выполнен",
        "решен", "сформулирован", "обоснован", "выявлен", "определен",
        "создан", "разработана", "разработано", "разработаны",
        "представлен", "полученн", "доложен", "внедрен",
        "обобщен", "систематизир", "классифицир", "проанализир",
        "сравнен", "проверен", "проверенн", "оценен",
        "методы", "использование", "подход", "подходы",
        "подход требует", "методы инспекции", "методы анализа",
    ]
    words = text[:80].lower().split()
    first_word = words[0] if words else ""
    if any(first_word.startswith(c) for c in conclusion_starts):
        # Строгие признаки публикации
        strong_indicators = ["//", "журн", "конференц", "пат. ", "пат.",
                            "свидетельств", "мбд", "web of science", "вак", "scopus"]
        # Слабые признаки (год, том, страница)
        weak_indicators = [
            r"\b20\d{2}\b",     # год 20xx
            r"\b20\d{2}\.",     # год 20xx.
            r" т\.\s*\d",       # т. 1
            r"\bс\.\s*\d",      # с. 5
            r"№\s*\d",          # № 1
            r"стр\.?\s*\d",     # стр. 5 или стр 5
            r"doi:\s*10\.",     # DOI
        ]
        has_strong = any(ind.lower() in low for ind in strong_indicators)
        has_weak = any(re.search(ind, low) for ind in weak_indicators)
        if not has_strong and not has_weak:
            return False
    return True


def _parse_llm_publications(response_text):
    """Очистить и дедупликровать публикации из ответа LLM."""
    if not response_text:
        return []

    low = response_text.lower()
    if any(w in low for w in ["не найден", "не вижу", "предоставьте", "скопируйте"]):
        return []

    lines = response_text.strip().split("\n")

    entries = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'^\d+\.\s', stripped):
            if current:
                entries.append(current)
            current = stripped
        else:
            if current and len(stripped) > 10:
                current += " " + stripped
    if current:
        entries.append(current)

    publications = []
    seen = set()
    for entry in entries:
        cleaned = re.sub(r'^\d+\.\s*', '', entry).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        if not cleaned or len(cleaned) < 20:
            continue
        if any(w in cleaned for w in ["ОСНОВНЫЕ ПУБЛИКАЦИИ", "Публикации. Основные"]):
            continue
        if not _is_likely_publication(cleaned):
            continue
        norm = _normalize_for_dedup(cleaned)
        if norm in seen:
            continue
        seen.add(norm)
        publications.append(cleaned)

    return publications


def _normalize_for_dedup(text):
    """Нормализовать текст для сравнения (дедупликация)."""
    t = text.lower()
    # Remove all dashes/en-dashes/em-dashes
    t = t.replace("\u2010", "").replace("\u2011", "").replace("\u2012", "")
    t = t.replace("\u2013", "").replace("\u2014", "").replace("\u2015", "")
    t = t.replace("-", "")
    # Normalize all whitespace to single space
    t = re.sub(r'\s+', ' ', t).strip()
    # Remove punctuation
    t = re.sub(r'[.,;:()—–—]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:120]  # Compare first 120 chars


def _merge_publications(regex_pubs, llm_pubs):
    """Объединить результаты regex и LLM: добавить из LLM те, которых нет в regex."""
    if not regex_pubs and not llm_pubs:
        return []

    if regex_pubs and len(regex_pubs) >= 3:
        base = {_normalize_for_dedup(p) for p in regex_pubs}
        merged = list(regex_pubs)
        for lp in llm_pubs:
            norm = _normalize_for_dedup(lp)
            if norm not in base:
                merged.append(lp)
                base.add(norm)
        return merged

    if llm_pubs and len(llm_pubs) > len(regex_pubs):
        return llm_pubs

    return regex_pubs


def extract_publications_from_pdf(pdf_path):
    """Извлечь список публикаций из PDF автореферата через LLM + regex."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            # Read all pages to find publications section
            for page in pdf.pages:
                t = page.extract_text() or ""
                text += "\n" + t

        # Try to find the specific publications section header
        # Priority: publications list at end > main publications section
        pub_headers = [
            "СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ АВТОРОМ",
            "СПИСОК ОПУБЛИКОВАННЫХ АВТОРОМ ДИССЕРТАЦИИ",
            "СПИСОК ПУБЛИКАЦИЙ",
            "СПИСОК ОПУБЛИКОВАННЫХ РАБОТ",
            "ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ",
            "ОСНОВНЫЕ ПУБЛИКАЦИИ",
            "ПУБЛИКАЦИИ ПО ТЕМЕ ДИССЕРТАЦИИ",
            "Публикации автора по теме диссертации",
            "Основные публикации по теме исследования",
            "Апробация работы и публикации",
            "Апробация и публикации",
            "СПИСОК ОСНОВНЫХ ПУБЛИКАЦИЙ",
            "Основные публикации",
            "Публикации в изданиях",
            "Публикации по теме диссертации",
        ]

        idx = -1
        for header in pub_headers:
            i = text.find(header)
            if i >= 0:
                idx = i
                break

        # Fallback: search for "Публикац" if specific header not found
        # Also check for "СПИСОК" which often appears at the end
        if idx < 0:
            list_idx = text.find("СПИСОК ОПУБЛИКОВАННЫХ")
            pub_idx = text.find("Публикац")
            # Prefer the last publications section (often the full list)
            if list_idx >= 0:
                idx = list_idx
            elif pub_idx >= 0:
                idx = pub_idx
        if idx < 0:
            return []

        # Regex extraction on publications section (limited to avoid conclusions)
        pub_text = text[idx:idx + 4000]
        regex_pubs = _extract_publications_regex(pub_text)

        # LLM extraction on a manageable chunk
        llm_pubs = []
        try:
            print("  Запрос к LLM для извлечения публикаций...")
            llm_section = text[idx:idx + 3500]
            response = _send_to_llm(llm_section)
            llm_pubs = _parse_llm_publications(response)
            # Sanity check: real dissertations typically have 5-20 publications
            # LLM hallucinations often produce 20+ fake entries
            if len(llm_pubs) > 30:
                print(f"  Подозрение на галлюцинацию LLM ({len(llm_pubs)} публикаций), уменьшаем до 20")
                llm_pubs = llm_pubs[:20]
        except Exception as e:
            print(f"  Ошибка LLM: {e}")

        # Merge results
        publications = _merge_publications(regex_pubs, llm_pubs)
        print(f"  Извлечено публикаций: {len(publications)}")
        return publications

    except requests.exceptions.Timeout:
        print("  Таймаут при запросе к LLM")
        return []
    except requests.exceptions.ConnectionError:
        print("  Ошибка подключения к LLM-серверу")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"  HTTP-ошибка LLM: {e}")
        return []
    except Exception as e:
        print(f"  Ошибка извлечения публикаций: {e}")
        return []


def sanitize_filename(name):
    """Убрать недопустимые символы из имени файла."""
    bad_chars = '<>:"/\\|?*'
    for ch in bad_chars:
        name = name.replace(ch, '')
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:80]


def download_autoref(autoref_url, fio, max_retries=3):
    """Скачать автореферат. Вернуть (путь_к_файлу, статус)."""
    if not autoref_url:
        return None, "Нет ссылки"

    # Проверить .pdf по URL
    url_path = autoref_url.split("?")[0].lower()
    if not url_path.endswith('.pdf'):
        return None, "Не PDF"

    filename = sanitize_filename(fio) + ".pdf"
    save_path = os.path.join(AUTOREFS_DIR, filename)

    os.makedirs(AUTOREFS_DIR, exist_ok=True)

    # Если уже скачан — вернуть
    if os.path.exists(save_path):
        return save_path, "Скачан ранее"

    for attempt in range(max_retries):
        try:
            resp = requests.get(autoref_url, stream=True, timeout=120, headers=DOWNLOAD_HEADERS, verify=False)
            resp.raise_for_status()

            # Проверить Content-Length до скачивания
            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length < MIN_SIZE:
                return None, "Малый размер (<500 КБ)"

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            actual_size = os.path.getsize(save_path)
            if actual_size < MIN_SIZE:
                os.remove(save_path)
                return None, f"Малый размер ({actual_size // 1024} КБ)"

            return save_path, "OK"

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if attempt < max_retries - 1:
                print(f"  HTTP {status_code}, повтор...")
            else:
                return None, f"HTTP {status_code}"
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  Повторная попытка {attempt + 1}/{max_retries}...")
            else:
                return None, str(e)

    return None, "Ошибка скачивания"


def main():
    conn = init_db()

    # --- Ввод специальности ---
    print("=" * 70)
    print("Поиск диссертаций о защите на vak.gisnauka.ru")
    print("=" * 70)
    print()
    print("Список специальностей:")

    resp = requests.get(f"{API_BASE}/att/specialty-list/", headers=HEADERS)
    resp.raise_for_status()
    specialties = resp.json()

    for i, s in enumerate(specialties, 1):
        print(f"  {i:>4}. {s['id']} - {s['name']}")

    print()
    spec_input = input("Введите номер или id специальности: ").strip()

    if spec_input.isdigit():
        idx = int(spec_input) - 1
        if 0 <= idx < len(specialties):
            specialty_id = specialties[idx]["id"]
        else:
            specialty_id = spec_input
    else:
        specialty_id = spec_input

    print(f"Специальность: {specialty_id}\n")

    # --- Ввод периода ---
    while True:
        date_from_str = input("Дата защиты от (дд.мм.гггг): ").strip()
        try:
            datetime.strptime(date_from_str, "%d.%m.%Y")
            break
        except ValueError:
            print("Неверный формат даты! Используйте дд.мм.гггг")

    while True:
        date_to_str = input("Дата защиты до (дд.мм.гггг): ").strip()
        try:
            datetime.strptime(date_to_str, "%d.%m.%Y")
            break
        except ValueError:
            print("Неверный формат даты! Используйте дд.мм.гггг")

    date_from = datetime.strptime(date_from_str, "%d.%m.%Y").strftime("%Y-%m-%d")
    date_to = datetime.strptime(date_to_str, "%d.%m.%Y").strftime("%Y-%m-%d")

    print(f"\nПоиск диссертаций: {specialty_id}, период с {date_from} по {date_to}...")

    adverts = search_adverts(specialty_id, date_from, date_to)

    if not adverts:
        print("\nНичего не найдено.")
        conn.close()
        return

    print(f"\nНайдено диссертаций: {len(adverts)}\n")

    # --- Вывод таблицы ---
    print("-" * 100)
    print(f"{'№':<5} {'Дата защиты':<14} {'ФИО соискателя':<35} {'Ссылка'}")
    print("-" * 100)

    for i, a in enumerate(adverts, 1):
        fio = a["fio"][:32] + "..." if len(a["fio"]) > 35 else a["fio"]
        card_url = f"https://vak.gisnauka.ru/adverts-list/advert-card/{a['old_id']}/"
        print(f"{i:<5} {a['date_defend']:<14} {fio:<35} {card_url}")

    print("-" * 100)
    print()

    # --- Сохранение в БД и обработка авторефератов ---
    confirm = input("Сохранить данные в БД (и скачать авторефераты)? [y/N]: ").strip().lower()
    if confirm != "y":
        conn.close()
        return

    print("\nОбработка диссертаций...")

    # Хранить статусы для итоговой таблицы
    autoref_status = {}  # adv_id -> (status_text, autoref_url)

    for idx, advert in enumerate(adverts, 1):
        adv_id = advert["id"]
        fio_raw = advert.get("fio", "")
        old_id = advert.get("old_id")
        print(f"[{idx}/{len(adverts)}] {adv_id[:8]}... - {fio_raw}...")

        # Получить детальную информацию
        detail = get_advert_detail(adv_id)
        save_advert_to_db(conn, detail)

        autoref_url = detail.get("autoref_site")

        if not autoref_url:
            status_text = "Нет ссылки"
            print(f"  {status_text}")
            autoref_status[adv_id] = (status_text, "")
            continue

        # Скачать автореферат
        local_path, status_text = download_autoref(autoref_url, fio_raw)
        print(f"  {status_text}", end="")
        if local_path:
            size_kb = os.path.getsize(local_path) // 1024
            fname = os.path.basename(local_path)
            print(f" -> {fname} ({size_kb} КБ)")
        else:
            print()

        autoref_status[adv_id] = (status_text, autoref_url or "")

        # Если скачан — извлечь публикации
        if local_path:
            pubs = extract_publications_from_pdf(local_path)
            c = conn.cursor()
            for num, pub_text in enumerate(pubs, 1):
                try:
                    c.execute(
                        "INSERT INTO publications (advert_id, publication_number, text) VALUES (?, ?, ?)",
                        (adv_id, num, pub_text),
                    )
                except sqlite3.Error as e:
                    print(f"  Ошибка записи публикации {num}: {e}")

            conn.commit()
            print(f"  Публикаций сохранено: {len(pubs)}")

    print("\nГотово! Данные сохранены в БД:", DB_PATH)
    print("Файлы авторефератов сохранены в папке:", AUTOREFS_DIR)

    # --- Вывод итоговой таблицы со статусом скачивания ---
    c = conn.cursor()
    c.execute("""SELECT a.id, a.old_id, a.date_defend, a.fio, a.dissertation_name,
                        'https://vak.gisnauka.ru/adverts-list/advert-card/' || a.old_id || '/' as card_url
                 FROM adverts a ORDER BY a.date_defend DESC""")

    print(f"\n{'№':<5} {'Дата защиты':<14} {'ФИО соискателя':<30} {'Статус'}")
    print("-" * 95)
    for i, row in enumerate(c.fetchall(), 1):
        adv_id = row[0]
        fio = (row[3] or "")[:27] + "..." if len(row[3] or "") > 30 else row[3] or ""

        status_info = autoref_status.get(adv_id)
        if status_info:
            status_text, status_url = status_info
            if status_text == "OK":
                display_status = "Удалось скачать"
            elif status_text == "Нет ссылки":
                display_status = "Нет ссылки"
            elif status_url and status_url.split("?")[0].lower().endswith(".pdf"):
                print(f"\n  Повторная попытка скачивания для: {fio}")
                local_path, retry_status = download_autoref(status_url, fio)
                if local_path:
                    size_kb = os.path.getsize(local_path) // 1024
                    display_status = f"Удалось скачать ({size_kb} КБ)"
                else:
                    display_status = f"Ошибка скачивания ссылка: {status_url}"
            else:
                display_status = f"Ошибка скачивания ссылка: {status_url}"
        else:
            display_status = "—"

        print(f"{i:<5} {row[2]:<14} {fio:<30} {display_status}")

    conn.close()


if __name__ == "__main__":
    main()
