"""Извлечение города и организации из автореферата (regex-based)."""

import re
import pdfplumber

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

# Abbreviations to expand
ABBREVIATIONS = {
    "фгбоу во": "ФГБОУ ВО",
    "фганб": "ФГАНБ",
    "фгбун": "ФГБУН",
    "фгбу": "ФГБУ",
    "фгаоу во": "ФГАОУ ВО",
    "фгбоу": "ФГБОУ",
    "фгбну": "ФГБНУ",
}


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

    # Берём последние 1500 символов
    search_area = text[-1500:] if len(text) > 1500 else text

    # Паттерн 1: "Город, 2026" или "г. Город, 2026"
    pattern1 = r'(?:г\.\s*)?([А-ЯЁA-Z][а-яёa-z\s\-]+?),\s*(20[2-3]\d)\s*$'
    match = re.search(pattern1, search_area, re.MULTILINE)
    if match:
        city = match.group(1).strip()
        if city and len(city) > 1 and len(city) < 50:
            return city

    # Паттерн 2: "Город 2026" (без запятой)
    pattern2 = r'(?:г\.\s*)?([А-ЯЁA-Z][а-яёa-z\s\-]+?)\s+(20[2-3]\d)\s*$'
    match = re.search(pattern2, search_area, re.MULTILINE)
    if match:
        city = match.group(1).strip()
        if city and len(city) > 1 and len(city) < 50:
            return city

    # Паттерн 3: поиск города в списке RUSSIAN_CITIES
    lines = search_area.split('\n')
    for line in lines[-10:]:
        line = line.strip()
        if not line or len(line) > 100:
            continue
        line_lower = line.lower()
        for city in RUSSIAN_CITIES:
            if city in line_lower:
                match = re.search(r'[А-ЯЁA-Z][а-яёa-z\- ]+' + re.escape(city), line, re.IGNORECASE)
                if match:
                    return match.group(0).strip()

    return None


def _extract_org_from_page2_top(text):
    """Извлекает организацию из верхней части второй страницы.

    Ищем полное название учреждения (Федеральное ... учреждение).
    """
    if not text:
        return None

    # Берём первые 2000 символов и убираем переносы строк
    search_area = re.sub(r'\s+', ' ', text[:2000])

    # Паттерн 1: "Федеральном/Федеральное государственном ..."
    match = re.search(
        r'(Федеральном\s+государственном\s+.+?)\s+(Научный|Официальн|Защита|Автореферат)',
        search_area, re.IGNORECASE | re.DOTALL
    )
    if match:
        org = match.group(1).strip()
        # Раскрываем сокращения
        for abbr, full in ABBREVIATIONS.items():
            org = org.replace(abbr, full)
        return org

    # Паттерн 2: "Работа выполнена в ..."
    match = re.search(
        r'Работа\s+выполнена\s+в\s+([^\n,]+)',
        search_area, re.IGNORECASE
    )
    if match:
        org = match.group(1).strip()
        org = re.sub(r'\s+', ' ', org)
        for abbr, full in ABBREVIATIONS.items():
            org = org.replace(abbr, full)
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


def extract_city_and_org(pdf_path):
    """Извлекает город и организацию из автореферата.

    Возвращает: (city, organization_name)
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if len(pdf.pages) < 2:
                return None, None

            page1_text = pdf.pages[0].extract_text() or ""
            page2_text = pdf.pages[1].extract_text() or ""

            # Извлекаем город из первой страницы
            city = _extract_city_from_page1_bottom(page1_text)

            # Извлекаем организацию из второй страницы
            org = _extract_org_from_page2_top(page2_text)

            if city:
                logger.debug(f"City extracted: {city}")
            if org:
                logger.debug(f"Org extracted: {org[:80]}...")

            return city, org

    except Exception as e:
        logger.error(f"Error extracting city/org from {pdf_path}: {e}")
        return None, None
