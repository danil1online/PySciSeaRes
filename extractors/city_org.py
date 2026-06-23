"""Извлечение города и организации из автореферата (regex + LLM fallback)."""

import re
import pdfplumber
import requests
import json

from config import LLM_API_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE, HEADERS
from logging_config import get_logger

logger = get_logger("city_org")

RUSSIAN_CITIES = {
    "москва", "санкт-петербург", "петербург", "спб", "новосибирск",
    "екатеринбург", "казань", "нижний новгород", "челябинск", "самара",
    "омск", "красноярск", "пермь", "воронеж", "ростов-на-дону", "уфа",
    "краснодар", "краснодаре", "сочи", "тула", "калуга", "брянск",
    "ярославль", "тверь", "смоленск", "псков", "великий новгород",
    "мурманск", "архангельск", "киров", "томск", "белгород", "курск",
    "липецк", "тамбов", "владимир", "иваново", "рязань", "пенза",
    "саратов", "волгоград", "ижевск", "череповец", "миасс", "северодвинск",
    "салават", "нефтекамск", "краснокамск", "набережные челны",
    "альметьевск", "нурлат", "златоуст", "чебоксары", "новокузнецк",
    "прокопьевск", "горно-алтайск", "пионерский", "светлогорск",
    "апрелевка", "подольск", "клин", "чехов", "железнодорожный",
    "красногорск", "домодедово", "серпухов", "обнинск", "пушкино",
    "люберцы", "мытищи", "химки", "балашиха", "королёв", "электросталь",
    "реутов", "видное", "щелково", "фрязино", "жуковский",
    "новомосковск", "кашира", "суздаль", "кострома", "шуя", "родники",
    "чепецк", "мценск", "рославль", "десногорск", "ржев",
}

ABBREVIATIONS = {
    "фгбоу во": "ФГБОУ ВО",
    "фганб": "ФГАНБ",
    "фгбун": "ФГБУН",
    "фгбу": "ФГБУ",
    "фгаоу во": "ФГАОУ ВО",
    "фгбоу": "ФГБОУ",
    "фгбну": "ФГБНУ",
}

# Case conversion patterns: prepositional → nominative
CASE_PATTERNS = [
    # Федеральном → Федеральное
    (r'^Федеральном\s+', 'Федеральное '),
    (r'^Федеральном\s+', 'Федеральное '),
    # образовательном → образовательное
    (r'образовательном\s+учреждении', 'образовательное учреждение'),
    (r'образовательном\s+учреждении', 'образовательное учреждение'),
    # государственном → государственное
    (r'государственном\s+автономном', 'государственное автономное'),
    (r'государственном\s+бюджетном', 'государственное бюджетное'),
    (r'государственном\s+бюджетном\s+учреждении', 'государственное бюджетное учреждение'),
    (r'государственном\s+бюджетном\s+образовательном', 'государственное бюджетное образовательное'),
    (r'государственном\s+бюджетном\s+учреждении\s+дополнительного', 'государственное бюджетное учреждение дополнительного'),
    (r'государственном\s+бюджетном\s+учреждении\s+дополнительного\s+профессионального', 'государственное бюджетное учреждение дополнительного профессионального'),
    (r'государственном\s+бюджетном\s+учреждении\s+дополнительного\s+профессионального\s+образования', 'государственное бюджетное учреждение дополнительного профессионального образования'),
    # автономном → автономное
    (r'автономном\s+образовательном', 'автономное образовательное'),
    (r'автономном\s+образовательном\s+учреждении', 'автономное образовательное учреждение'),
    # бюджетном → бюджетное
    (r'бюджетном\s+образовательном', 'бюджетное образовательное'),
    (r'бюджетном\s+учреждении', 'бюджетное учреждение'),
    (r'бюджетном\s+учреждении\s+дополнительного', 'бюджетное учреждение дополнительного'),
    (r'бюджетном\s+учреждении\s+дополнительного\s+профессионального', 'бюджетное учреждение дополнительного профессионального'),
    (r'бюджетном\s+учреждении\s+дополнительного\s+профессионального\s+образования', 'бюджетное учреждение дополнительного профессионального образования'),
]

# FIO patterns to exclude from city matching
FIO_PARTS = [
    "андрей", "владимир", "владимирович", "андреевич", "сергеевич",
    "петрович", "иванович", "никитаевич", "дмитриевич", "алексеевич",
    "ивановна", "петровна", "сергеевна", "андреевна", "владимировна",
    "марковна", "николаевна", "александровна", "евгеньевна", "olegовна",
    "антон", "влад", "дмитр", "серг", "павел", "алекс", "максим",
    "андр", "влад", "дмит", "серг", "пав", "алек", "макс", "миха",
    "иван", "петр", "ник", "александр", "марк", "никита", "евгений",
]


def _is_fio_like(word):
    """Проверяет, похоже ли слово на часть ФИО."""
    lower = word.lower().strip().rstrip('.:,;')
    if len(lower) < 3:
        return False
    return lower in FIO_PARTS or lower.startswith('андр') or lower.startswith('влад')


def _extract_city_from_page1_bottom(text):
    """Извлекает город из нижней части первой страницы.

    Форматы:
    - "Москва, 2026"
    - "г. Москва, 2026"
    - "Москва 2026"
    - "г. Москва 2026"
    """
    if not text:
        return None

    search_area = text[-1500:] if len(text) > 1500 else text

    # Паттерн 1: "Город, 2026" или "г. Город, 2026"
    # Запятая + год — ключевой маркер
    pattern1 = r'(?:г\.\s*)?([А-ЯЁ][А-ЯЁа-яё]+(?:\s+[А-ЯЁ][А-ЯЁа-яё]+)*?),\s*(20[2-3]\d)\s*$'
    match = re.search(pattern1, search_area, re.MULTILINE)
    if match:
        city = match.group(1).strip()
        # Проверяем, что это не часть ФИО
        if _is_valid_city(city):
            return city

    # Паттерн 2: "Город 2026" (без запятой)
    pattern2 = r'(?:г\.\s*)?([А-ЯЁ][А-ЯЁа-яё]+(?:\s+[А-ЯЁ][А-ЯЁа-яё]+)*?)\s+(20[2-3]\d)\s*$'
    match = re.search(pattern2, search_area, re.MULTILINE)
    if match:
        city = match.group(1).strip()
        if _is_valid_city(city):
            return city

    # Паттерн 3: поиск города в списке RUSSIAN_CITIES
    lines = search_area.split('\n')
    for line in lines[-10:]:
        line = line.strip()
        if not line or len(line) > 100:
            continue
        line_lower = line.lower()
        # Ищем город в тексте строки
        for city_name in RUSSIAN_CITIES:
            if city_name in line_lower:
                # Проверяем контекст — год должен быть рядом
                if '202' in line or '203' in line:
                    # Извлекаем город + год
                    match = re.search(r'([А-ЯЁ][А-ЯЁа-яё\s\-]+?,?\s*(?:20[2-3]\d))', line)
                    if match:
                        full = match.group(1).strip().rstrip(',').strip()
                        # Проверяем что это город, а не ФИО
                        if _is_valid_city(full) and len(full) < 40:
                            return full

    return None


def _is_valid_city(city):
    """Проверяет, что строка похожа на город, а не на ФИО."""
    if not city or len(city) < 2 or len(city) > 40:
        return False

    words = city.split()

    # Если есть части ФИО — это не город
    for word in words:
        if _is_fio_like(word):
            return False

    # Город обычно 1-3 слова
    if len(words) > 3:
        return False

    # Город обычно начинается с большой буквы
    if not words[0][0].isupper():
        return False

    # Проверяем, что хотя бы одно слово похоже на город
    city_lower = city.lower()
    for city_name in RUSSIAN_CITIES:
        if city_name in city_lower:
            return True

    # Если нет в списке — проверяем что не похоже на ФИО
    # ФИО обычно: Имя Фамилия или Фамилия Имя О
    has_surname = any(word.endswith('ов') or word.endswith('ев') or word.endswith('ин')
                      for word in words)
    has_patronymic = any(word.endswith('вич') or word.endswith('на')
                         for word in words)

    if has_patronymic:
        return False

    return True


def _convert_to_nominative(org):
    """Конвертирует организацию из предложного падежа в именительный."""
    if not org:
        return org

    # Применяем паттерны замены
    for pattern, replacement in CASE_PATTERNS:
        org = re.sub(pattern, replacement, org, flags=re.IGNORECASE)

    # Раскрываем сокращения
    for abbr, full in ABBREVIATIONS.items():
        org = org.replace(abbr, full)

    return org


def _extract_org_from_page2_top(text):
    """Извлекает организацию из верхней части второй страницы."""
    if not text:
        return None

    search_area = re.sub(r'\s+', ' ', text[:2000])

    # Паттерн 1: "Федеральном/Федеральное государственном ..."
    match = re.search(
        r'(Федеральном\s+государственном\s+.+?)\s+(Научный|Официальн|Защита|Автореферат)',
        search_area, re.IGNORECASE | re.DOTALL
    )
    if match:
        org = match.group(1).strip()
        org = _convert_to_nominative(org)
        return org

    # Паттерн 2: "Работа выполнена в ..."
    match = re.search(
        r'Работа\s+выполнена\s+в\s+([^\n,]+)',
        search_area, re.IGNORECASE
    )
    if match:
        org = match.group(1).strip()
        org = re.sub(r'\s+', ' ', org)
        org = _convert_to_nominative(org)
        return org

    # Паттерн 3: "ФГБОУ ВО ..."
    match = re.search(
        r'(ФГБОУ\s+ВО\s+"[^"]+")',
        search_area, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    # Паттерн 4: "ФГБНУ ..."
    match = re.search(
        r'(ФГБНУ\s+"[^"]+")',
        search_area, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    return None


def _extract_with_llm(page1_text, page2_text):
    """Использует LLM для извлечения города и организации."""
    system_prompt = """Ты — помощник по извлечению данных из авторефератов диссертаций.

Извлеки:
1. ГОРОД — город, где защищается диссертация (указан внизу первой страницы, перед годом)
2. ОРГАНИЗАЦИЯ — полное название организации автора в ИМЕНИТЕЛЬНОМ падеже

Ответь ТОЛЬКО в формате JSON:
{"city": "город", "organization": "полное название организации"}

ВАЖНО:
- Город — это город, а не имя автора
- Организация должна быть в именительном падеже (кто? что?), а не в предложном (где? о ком?)
- Пример: "Федеральное государственное автономное образовательное учреждение высшего образования «Сибирский федеральный университет»" (не "федеральном")
"""

    user_prompt = f"""Нижняя часть первой страницы автореферата:
{page1_text[-1000:] if page1_text else ""}

Начало второй страницы автореферата:
{page2_text[:1500] if page2_text else ""}

Верни только JSON, без комментариев."""

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
                "max_tokens": 500,
                "temperature": 0.1,
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Парсим JSON из ответа
        import json
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        data = json.loads(content)
        return data.get("city"), data.get("organization")

    except Exception as e:
        logger.error(f"LLM extraction error: {e}")
        return None, None


def extract_city_and_org(pdf_path):
    """Извлекает город и организацию из автореферата.

    Сначала regex, потом LLM как fallback.
    Возвращает: (city, organization_name)
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if len(pdf.pages) < 2:
                return None, None

            page1_text = pdf.pages[0].extract_text() or ""
            page2_text = pdf.pages[1].extract_text() or ""

            # Regex extraction
            city = _extract_city_from_page1_bottom(page1_text)
            org = _extract_org_from_page2_top(page2_text)

            # LLM fallback if regex failed
            if not city or not org:
                logger.debug("Regex didn't extract all data, trying LLM...")
                llm_city, llm_org = _extract_with_llm(page1_text, page2_text)
                if llm_city and not city:
                    city = llm_city
                if llm_org and not org:
                    org = llm_org

            if city:
                logger.debug(f"City extracted: {city}")
            if org:
                logger.debug(f"Org extracted: {org[:80]}...")

            return city, org

    except Exception as e:
        logger.error(f"Error extracting city/org from {pdf_path}: {e}")
        return None, None
