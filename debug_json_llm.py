#!/usr/bin/env python3
"""Тестовый скрипт для отладки JSON-LLM промпта.

Логика:
1. Выделяем последние ~5 страниц из PDF автореферата
2. Отправляем в LLM с промптом для извлечения публикаций
3. Принимаем JSON
"""
import json
import os
import sys
import time
import requests
import urllib3
import pdfplumber

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import LLM_API_URL, LLM_MODEL, HEADERS

# ============================================================
# Промпт для JSON-LLM
# ============================================================
#
# Структура каждой публикации:
#   - number: порядковый номер (int, опционально)
#   - authors: список авторов (обязательно)
#   - title: название статьи (обязательно)
#   - authors_extended: расширенный список авторов (опционально)
#   - journal: название журнала (если журнал) ИЛИ
#   - conference: название конференции (если материалы конференции)
#   - conference_location: место проведения конференции (если conference)
#   - conference_date: дата конференции (если conference)
#   - year: год (int)
#   - volume: том (опционально)
#   - issue: номер (опционально)
#   - pages: страницы (опционально)
#   - extra: дополнительная информация (опционально)
#
# ВАЖНО: authors и title должны быть всегда. Остальные поля —
#         если информация есть в тексте.
#

PUB_SYSTEM_PROMPT = """Ты — экспертный библиограф. Твоя задача — извлечь ВСЕ публикации автора из текста автореферата диссертации.

Извлекай ТОЛЬКО реальные цитируемые публикации (список работ, опубликованных автором). НЕ включай:
- Выводы и положения диссертации
- Описания структуры работы
- Информацию о руководителе, оппонентах, научной школе
- Апробацию (упоминания конференций, где докладывался автор, но без публикации)
- Списки условных обозначений, определения

Каждая публикация — это точная ссылка на работу автора. Обычно они оформлены как:
"1. Иванов И.И., Петров П.П. Название статьи // Журнал. — 2024. — Т. 10. — № 3. — С. 45-50."

Или для материалов конференции:
"2. Сидоров С.С. Название доклада // Материалы конференции NAME. — Место, 2024. — С. 100-105."

ВАЖНО:
- authors (список авторов) и title (название) — ОБЯЗАТЕЛЬНЫЕ поля. Если статья не имеет автора — не включай её.
- journal ИЛИ conference — должно быть заполнено, если источник указан.
- year — обязательно, если указан в тексте.
- Остальные поля (volume, issue, pages, extra) — заполняй, если есть в тексте."""

PUB_USER_PROMPT = """Извлеки ВСЕ публикации из раздела публикаций автореферата.

Верни ТОЛЬКО JSON массив объектов. НЕ добавляй никакой текст, комментарии, markdown или другое содержимое. Только массив JSON.

Структура одного объекта публикации:
{{
  "number": 1,
  "authors": ["Иванов И.И.", "Петров П.П."],
  "title": "Название статьи",
  "authors_extended": ["Иванов И.И.", "Петров П.П.", "Сидоров С.С."],
  "journal": "Название журнала",
  "conference": "Название конференции",
  "conference_location": "Место",
  "conference_date": "2024-05-01",
  "year": 2024,
  "volume": "10",
  "issue": "3",
  "pages": "45-50",
  "extra": "Вак, scopus"
}}

Правила:
1. authors — массив строк с ФИО авторов. Разделяй запятыми и точками.
2. title — название статьи/работы.
3. journal — название журнала (если журнал), ИЛИ conference — название конференции (если материалы).
4. year — год в виде числа.
5. Если информация отсутствует — пропусти поле или поставь null.
6. НЕ придумывай авторов, названия или журналы.
7. Если раздел публикаций не найден — верни пустой массив [].

Текст автореферата (последние страницы):

{text}
"""


def extract_last_pages(pdf_path, num_pages=5):
    """Извлекаем текст последних N страниц PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            if num_pages >= total_pages:
                start_page = 0
            else:
                start_page = total_pages - num_pages

            print(f"  Всего страниц: {total_pages}, берём с {start_page} до конца")

            text = ""
            for i in range(start_page, total_pages):
                page_text = pdf.pages[i].extract_text() or ""
                text += f"\n--- Страница {i+1}/{total_pages} ---\n" + page_text

            return text
    except Exception as e:
        print(f"  Ошибка чтения PDF: {e}")
        return None


def send_to_llm_json(text):
    """Отправляем текст в LLM, ожидаем JSON-ответ."""
    user_prompt = PUB_USER_PROMPT.format(text=text)
    start = time.time()

    print(f"\n  Отправка в LLM ({LLM_MODEL})...")
    print(f"  Текст: {len(text)} символов")

    try:
        resp = requests.post(
            LLM_API_URL,
            headers=HEADERS,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": PUB_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 4000,
                "temperature": 0.1,
            },
            timeout=300,
        )
        elapsed = time.time() - start
        resp.raise_for_status()

        raw = resp.json()["choices"][0]["message"]["content"]
        print(f"  Ответ получен за {elapsed:.1f}с")
        print(f"  Сырой ответ ({len(raw)} символов):")
        print(f"  {'─' * 60}")
        print(f"  {raw[:500]}{'...' if len(raw) > 500 else ''}")
        print(f"  {'─' * 60}\n")

        return raw, elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"  Ошибка LLM ({elapsed:.1f}с): {e}")
        return None, elapsed


def parse_llm_json_response(content):
    """Парсим JSON из ответа LLM."""
    if not content:
        return []

    # Извлекаем JSON из markdown
    if "```json" in content:
        json_str = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        json_str = content.split("```")[1].split("```")[0].strip()
    else:
        # Ищем массив
        start = content.find("[")
        if start < 0:
            print(f"  JSON не найден в ответе")
            return []
        json_str = content[start:].strip()

    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return data
        print(f"  JSON не является массивом: {type(data)}")
        return []
    except json.JSONDecodeError as e:
        print(f"  Ошибка парсинга JSON: {e}")
        print(f"  Попытка исправить...")
        # Попытка найти первый валидный массив
        for i in range(len(json_str)):
            if json_str[i] == '[':
                for j in range(len(json_str), i, -1):
                    try:
                        data = json.loads(json_str[i:j])
                        if isinstance(data, list):
                            print(f"  Найдено на позиции {i}:{j}")
                            return data
                    except json.JSONDecodeError:
                        continue
        return []


def format_publication(pub):
    """Форматируем публикацию для вывода."""
    if not isinstance(pub, dict):
        return str(pub)

    number = pub.get('number', '?')
    authors = ', '.join(pub.get('authors', [])) if isinstance(pub.get('authors'), list) else pub.get('authors', '')
    title = pub.get('title', '')
    journal = pub.get('journal', '') or pub.get('conference', '')
    year = pub.get('year', '')
    pages = pub.get('pages', '')
    volume = pub.get('volume', '')
    issue = pub.get('issue', '')

    parts = [f"{number}. "]
    if authors:
        parts.append(f"{authors}. ")
    if title:
        parts.append(f'"{title}"')
    if journal:
        parts.append(f" // {journal}")
    extras = []
    if volume:
        extras.append(f"Т.{volume}")
    if issue:
        extras.append(f"№{issue}")
    if year:
        extras.append(str(year))
    if pages:
        extras.append(f"С.{pages}")
    if extras:
        parts.append(" — " + ", ".join(extras))

    return ''.join(parts)


def main():
    if len(sys.argv) < 2:
        print("Использование: python debug_json_llm.py <путь_к_автореферату.pdf> [страниц]")
        print("Пример: python debug_json_llm.py /tmp/avtoref.pdf 5")
        sys.exit(1)

    pdf_path = sys.argv[1]
    num_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if not os.path.exists(pdf_path):
        print(f"Файл не найден: {pdf_path}")
        sys.exit(1)

    print(f"\n{'=' * 70}")
    print(f"JSON-LLM отладка")
    print(f"{'=' * 70}")
    print(f"PDF: {pdf_path}")
    print(f"Страниц для чтения: {num_pages}")

    # Шаг 1: Извлекаем последние страницы
    print(f"\n[Шаг 1] Извлечение последних {num_pages} страниц...")
    text = extract_last_pages(pdf_path, num_pages)
    if not text:
        print("Ошибка: не удалось прочитать PDF")
        sys.exit(1)

    print(f"  Получено текста: {len(text)} символов")
    print(f"  Первый фрагмент:\n{text[:300]}...\n")

    # Шаг 2: Отправляем в LLM
    print(f"\n[Шаг 2] Отправка в LLM...")
    raw, elapsed = send_to_llm_json(text)
    if not raw:
        print("Ошибка: нет ответа от LLM")
        sys.exit(1)

    # Шаг 3: Парсим JSON
    print(f"\n[Шаг 3] Парсинг JSON...")
    publications = parse_llm_json_response(raw)
    print(f"  Найдено публикаций: {len(publications)}\n")

    # Вывод результатов
    if publications:
        print(f"\n{'=' * 70}")
        print(f"РЕЗУЛЬТАТ ({len(publications)} публикаций)")
        print(f"{'=' * 70}\n")
        for pub in publications:
            print(format_publication(pub))
            print()

        # Сохраняем сырой ответ
        os.makedirs("llm_responses", exist_ok=True)
        safe_name = os.path.basename(pdf_path).replace('.pdf', '')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        with open(f"llm_responses/{safe_name}_json_raw_{timestamp}.txt", "w", encoding="utf-8") as f:
            f.write(raw)
        
        with open(f"llm_responses/{safe_name}_json_parsed_{timestamp}.json", "w", encoding="utf-8") as f:
            json.dump(publications, f, ensure_ascii=False, indent=2)
        
        print(f"  Сохранено: llm_responses/{safe_name}_json_*.json")
    else:
        print("\nПубликации не найдены.")
        print("Возможные причины:")
        print("  1. Раздел публикаций не на последних страницах")
        print("  2. LLM не смог распознать публикации")
        print("  3. JSON-ответ не был распознан")
        print("\nПопробуйте увеличить num_pages или проверить, что раздел публикаций")
        print("действительно находится на последних страницах PDF.")


if __name__ == "__main__":
    from datetime import datetime
    main()
