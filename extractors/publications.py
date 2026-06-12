import re
import os
import time
import json
import requests
import pdfplumber

from config import (
    LLM_API_URL, LLM_MODEL, HEADERS, MIN_SIZE,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
    MAX_PUBLICATIONS, LLM_RESPONSES_DIR,
)

PUB_SYSTEM_PROMPT = (
    "Ты — помощник по извлечению библиографических данных из авторефератов диссертаций. "
    "Твоя задача — извлечь список всех публикаций автора по теме исследования из текста "
    "раздела 'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ'. "
    "Верни КАЖДУЮ публикацию в формате JSON массива. Каждая публикация — объект с полями: "
    "authors, title, journal, year, pages. "
    "Убери ВСЕ дубликаты. Если раздел не найден — верни {\"error\": \"not_found\"}."
)

PUB_SYSTEM_PROMPT_JSON = (
    "Ты — помощник по извлечению библиографических данных из авторефератов диссертаций. "
    "Твоя задача — найти и извлечь ВСЕ публикации автора в разделе "
    "'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ' или аналогичном. "
    "Для каждой публикации найди: порядок (номер), авторы (ФИО), "
    "название статьи, название журнала/издания, год, страницы. "
    "Верни результат в формате JSON массива. Если публикаций не найдено — верни пустой массив []"
)

PUB_USER_PROMPT = (
    "Извлеки все публикации из предоставленного текста автореферата.\n\n"
    "Каждая публикация должна содержать:\n"
    "1. authors — ФИО авторов\n"
    "2. title — название статьи\n"
    "3. journal — название журнала/издания\n"
    "4. year — год\n"
    "5. pages — номера страниц\n\n"
    "Верни ТОЛЬКО JSON массив, без дополнительного текста.\n\n"
    "Текст:\n\n{text}"
)


def _send_to_llm(text):
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
    start = time.time()
    for attempt in range(3):
        try:
            resp = requests.post(
                LLM_API_URL,
                headers=HEADERS,
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": PUB_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": LLM_MAX_TOKENS,
                    "temperature": LLM_TEMPERATURE,
                },
                timeout=LLM_TIMEOUT,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"], elapsed
        except requests.exceptions.Timeout:
            if attempt < 2:
                time.sleep(2)
    return None, time.time() - start


def _send_to_llm_json(text):
    """Отправляет текст в LLM с промптом для JSON-ответа.

    Возвращает (публикации, время, успех).
    Успех = False только при таймауте/ошибке сети.
    Пустой массив [] — валидный результат (0 публикаций).

    JSON-LLM даёт самые точные результаты, поэтому ждём до 5 минут
    (LLM-сервер может "засыпать", cold start занимает 30-90 сек).
    """
    user_prompt = PUB_USER_PROMPT.format(text=text)
    start = time.time()
    json_timeout = 300  # 5 минут — ждём cold start
    try:
        resp = requests.post(
            LLM_API_URL,
            headers=HEADERS,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": PUB_SYSTEM_PROMPT_JSON},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": LLM_MAX_TOKENS,
                "temperature": LLM_TEMPERATURE,
            },
            timeout=json_timeout,
        )
        elapsed = time.time() - start
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        pubs = _parse_llm_json_response(content)
        return pubs, elapsed, True
    except requests.exceptions.Timeout:
        return [], time.time() - start, False
    except Exception:
        return [], time.time() - start, False


def _parse_llm_json_response(content):
    """Парсит JSON-ответ от LLM."""
    if not content:
        return []

    # Извлекаем JSON из ответа
    if "```json" in content:
        json_str = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        json_str = content.split("```")[1].split("```")[0].strip()
    else:
        start = content.find("[")
        if start >= 0:
            json_str = content[start:].strip()
        else:
            return []

    try:
        data = json.loads(json_str)
        if isinstance(data, list) and len(data) >= 8:
            return data
        return []
    except json.JSONDecodeError:
        return []


def _extract_publications_regex(pub_text):
    raw_items = re.split(r'(?<=\n)(?:\d+[\.\)])\s', pub_text)
    publications = []
    for raw in raw_items[1:]:
        if not raw.strip():
            continue
        text = raw.strip()
        text = re.sub(r'\s*-\s*\n\s*', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 20:
            publications.append(text)
    return publications


def _is_likely_publication(text):
    low = text.lower().strip()
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

    conclusion_starts = [
        "анализ", "разработ", "провед", "синтезир", "исслед", "получен",
        "представл", "доказан", "определен", "установлен", "выполнен",
        "решен", "сформулирован", "обоснован", "выявлен", "создан",
        "разработана", "разработано", "разработаны",
        "представлен", "полученн", "доложен", "внедрен",
        "обобщен", "систематизир", "классифицир", "проанализир",
        "сравнен", "проверен", "оценен",
        "методы", "использование", "подход", "подходы",
        "подход требует", "методы инспекции", "методы анализа",
    ]
    words = text[:80].lower().split()
    first_word = words[0] if words else ""
    if any(first_word.startswith(c) for c in conclusion_starts):
        strong_indicators = ["//", "журн", "конференц", "пат. ", "пат.",
                             "свидетельств", "мбд", "web of science", "вак", "scopus"]
        weak_indicators = [
            r"\b20\d{2}\b", r"\b20\d{2}\.", r" т\.\s*\d", r"\bс\.\s*\d",
            r"№\s*\d", r"стр\.?\s*\d", r"doi:\s*10\.",
        ]
        has_strong = any(ind.lower() in low for ind in strong_indicators)
        has_weak = any(re.search(ind, low) for ind in weak_indicators)
        if not has_strong and not has_weak:
            return False
    return True


def _normalize_for_dedup(text):
    t = text.lower()
    t = t.replace("\u2010", " ").replace("\u2011", " ").replace("\u2012", " ")
    t = t.replace("\u2013", " ").replace("\u2014", " ").replace("\u2015", " ")
    t = t.replace("-", " ")
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'[.,;:()—–—]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:120]


def _parse_llm_publications(response_text):
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
        if re.match(r'^\d+[\.\)]\s', stripped) or re.match(r'^[A-Z]\d+[\.\)]\s', stripped):
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
        cleaned = re.sub(r'^(?:[A-Z]?\d+[\.\)])\s*', '', entry).strip()
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

    return publications[:MAX_PUBLICATIONS]


def _merge_publications(regex_pubs, llm_pubs):
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
        return merged[:MAX_PUBLICATIONS]
    if llm_pubs and len(llm_pubs) > len(regex_pubs):
        return llm_pubs[:MAX_PUBLICATIONS]
    return regex_pubs[:MAX_PUBLICATIONS]


def _find_publications_section(text):
    pub_headers = [
        'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ АВТОРОМ',
        'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ ПО ТЕМЕ ДИССЕРТАЦИИ',
        'СПИСОК ОПУБЛИКОВАННЫХ АВТОРОМ ДИССЕРТАЦИИ',
        'СПИСОК ПУБЛИКАЦИЙ',
        'СПИСОК ОПУБЛИКОВАННЫХ РАБОТ',
        'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ',
        'ОСНОВНЫЕ ПУБЛИКАЦИИ',
        'ПУБЛИКАЦИИ ПО ТЕМЕ ДИССЕРТАЦИИ',
        'Публикации автора по теме диссертации',
        'Основные публикации по теме исследования',
        'Апробация работы и публикации',
        'Апробация и публикации',
        'СПИСОК ОСНОВНЫХ ПУБЛИКАЦИЙ',
        'Основные публикации',
        'Публикации в изданиях',
        'Публикации по теме диссертации',
    ]

    text_upper = text.upper()
    idx = -1
    found = None

    for header in pub_headers:
        i = text_upper.find(header.upper())
        if i >= 0:
            idx = i
            found = header
            break

    if idx < 0:
        # Fallback: try broad keywords
        list_upper = text_upper.rfind('СПИСОК')
        published_upper = text_upper.rfind('ОПУБЛИКОВАН')
        pub_upper = text_upper.rfind('ПУБЛИКАЦ')

        # Prefer most specific match
        best = None
        best_found = None

        if published_upper >= 0:
            best = published_upper
            best_found = 'Опубликов (fallback)'
        if list_upper >= 0 and (best is None or list_upper > best):
            best = list_upper
            best_found = 'Список (fallback)'
        if pub_upper >= 0 and (best is None or pub_upper > best):
            best = pub_upper
            best_found = 'Публикац (fallback)'

        if best is not None:
            idx = best
            found = best_found

    return idx, found


def extract_publications_from_pdf_text(text):
    """Извлекает публикации из текста (PDF уже прочитан снаружи).
    
    Порядок:
    1. JSON-LLM (последние ~5 страниц, ~12000 символов)
       - Если JSON валиден (даже пустой []) — используем результат
    2. Regex + LLM fallback (если JSON не валиден)
    
    Возвращает: (publications, found_section, time_taken)
    """
    idx, found = _find_publications_section(text)

    # Берём последние ~5 страниц текста для LLM
    llm_text = text[-12000:] if len(text) > 12000 else text

    # Шаг 1: JSON-LLM — всегда пробуем первым
    json_pubs, json_time, json_success = _send_to_llm_json(llm_text)

    if json_success:
        # JSON получен валидный (даже []) — используем
        formatted_pubs = []
        for p in json_pubs:
            if isinstance(p, dict):
                formatted_pubs.append(p)
        print(f"  JSON-LLM: {len(formatted_pubs)} публикаций ({json_time:.1f}с)")
        return formatted_pubs, found, json_time

    # Шаг 2: JSON не получен (таймаут/ошибка) — fallback на Regex + LLM
    if idx < 0:
        print(f"  JSON-LLM не удался, раздел не найден")
        return [], found, json_time

    # Regex от начала раздела публикаций
    pub_text = text[idx:idx + 4000]
    regex_pubs = _extract_publications_regex(pub_text)
    print(f"  Regex: {len(regex_pubs)} публикаций (раздел: {found})")

    llm_pubs = []
    llm_time = None
    try:
        response, llm_time = _send_to_llm(llm_text)
        llm_pubs = _parse_llm_publications(response)
        if len(llm_pubs) > 30:
            llm_pubs = llm_pubs[:20]
        if llm_pubs:
            print(f"  LLM: {len(llm_pubs)} публикаций ({llm_time:.1f}с)")
    except Exception as e:
        print(f"  Ошибка LLM: {e}")

    publications = _merge_publications(regex_pubs, llm_pubs)
    return publications, found, llm_time


def extract_publications_from_pdf(pdf_path):
    """Извлекает публикации из PDF-файла (legacy, для обратной совместимости)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages:
                t = page.extract_text() or ""
                text += "\n" + t
        return extract_publications_from_pdf_text(text)
    except Exception as e:
        print(f"  Ошибка чтения PDF {pdf_path}: {e}")
        return [], None, None


def parse_publication_to_structured(text):
    low = text.lower().strip()
    year = None
    journal = ""
    pages = ""
    authors = ""
    title = ""

    m = re.search(r'(20\d{2})\b', low)
    if m:
        year = int(m.group(1))

    m = re.search(r'(?:стр\.?\s*|с\.?\s*)(\d[\d\s-]*)', low)
    if m:
        pages = m.group(1).strip()

    if "//" in text:
        parts = text.split("//", 1)
        before_slash = parts[0].strip()
        after_slash = parts[1].strip() if len(parts) > 1 else ""
        words = before_slash.split()
        if len(words) > 3:
            title = before_slash
            if after_slash:
                journal_m = re.match(r'([^\.,\d]+)', after_slash)
                if journal_m:
                    journal = journal_m.group(1).strip()
        elif len(words) > 0:
            title = before_slash
    else:
        sentences = re.split(r'[.\n]', text)
        if sentences:
            title = sentences[0].strip()

    if not title and not journal:
        return {
            "authors": text[:100],
            "title": "",
            "journal": "",
            "year": year,
            "pages": pages,
        }

    return {
        "authors": authors,
        "title": title,
        "journal": journal,
        "year": year,
        "pages": pages,
    }


def parse_pub_to_json(texts):
    pub_jsons = []
    seen = set()
    for t in texts:
        # Если уже словарь (JSON-LLM) — используем как есть
        if isinstance(t, dict):
            p = {
                "authors": t.get("authors", ""),
                "title": t.get("title", ""),
                "journal": t.get("journal", ""),
                "year": t.get("year"),
                "pages": t.get("pages", ""),
            }
        else:
            # Старый формат — парсим строку
            p = parse_publication_to_structured(t)

        norm = _normalize_for_dedup(p["title"] if p["title"] else "")
        if norm in seen:
            continue
        seen.add(norm)
        if p["title"] or p["journal"]:
            pub_jsons.append(p)
    return pub_jsons[:MAX_PUBLICATIONS]
