#!/usr/bin/env python3
"""Тестирование JSON-LLM подхода извлечения публикаций с включённым/отключённым
механизмом размышлений (thinking / reasoning).

Сравнивает два варианта:
  1. Без размышлений — стандартный системный промпт (как в production)
  2. С размышлениями  — добавлен явный призыв к пошаговому анализу
     в системном промпте + попытка использовать поле reasoning API-запроса

Тестовый файл: Avtoreferat(316).pdf (9 публикаций — эталон)
"""

import re
import json
import os
import time
import requests
import pdfplumber
import urllib3

from config import (
    LLM_API_URL, LLM_MODEL, HEADERS,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# Конфигурация тестирования
# ============================================================

PDF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Avtoreferat(316).pdf")
EXPECTED_PUB_COUNT = 9  # эталонное количество публикаций
LLM_TEXT_CHARS = 12000  # последние ~5 страниц
NUM_RUNS = 10           # количество повторений каждого варианта

# Системный промпт из production (без размышлений)
PUB_SYSTEM_PROMPT_BASE = (
    "Ты — помощник по извлечению библиографических данных из авторефератов диссертаций. "
    "Твоя задача — найти и извлечь ВСЕ публикации автора в разделе "
    "'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ' или аналогичном. "
    "Для каждой публикации найди: порядок (номер), авторы (ФИО), "
    "название статьи, название журнала/издания, год, страницы. "
    "Верни результат в формате JSON массива. Если публикаций не найдено — верни пустой массив []"
)

# Системный промпт с включённым механизмом размышлений
PUB_SYSTEM_PROMPT_THINKING = (
    "Ты — помощник по извлечению библиографических данных из авторефератов диссертаций. "
    "Твоя задача — найти и извлечь ВСЕ публикации автора в разделе "
    "'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ' или аналогичном. "
    "Для каждой публикации найди: порядок (номер), авторы (ФИО), "
    "название статьи, название журнала/издания, год, страницы. "
    "Верни результат в формате JSON массива. Если публикаций не найдено — верни пустой массив []\n\n"
    "ВАЖНО: Сначала внимательно проанализируй весь предоставленный текст. "
    "1. Определи, где находится раздел публикаций. "
    "2. Прочитай каждую строку в этом разделе. "
    "3. Определи, является ли строка публикацией (должны быть авторы, название, источник, год). "
    "4. Извлеки все поля для каждой публикации. "
    "5. Проверь, не дублируются ли публикации. "
    "После анализа верни результат в формате JSON массива."
)

# User-промпт из production
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


# ============================================================
# Чтение PDF
# ============================================================

def read_pdf_text(pdf_path):
    """Читает весь текст из PDF-файла."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return pages


def get_llm_text(pages):
    """Возвращает текст последних ~5 страниц (для LLM)."""
    all_text = "\n".join(pages)
    llm_text = all_text[-LLM_TEXT_CHARS:] if len(all_text) > LLM_TEXT_CHARS else all_text
    return llm_text


def get_section_text(pages):
    """Возвращает текст раздела публикаций (для справки)."""
    all_text = "\n".join(pages)
    pub_headers = [
        'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ АВТОРОМ',
        'СПИСОК РАБОТ, ОПУБЛИКОВАННЫХ ПО ТЕМЕ ДИССЕРТАЦИИ',
        'ОСНОВНЫЕ ПУБЛИКАЦИИ ПО ТЕМЕ ИССЛЕДОВАНИЯ',
        'ОСНОВНЫЕ ПУБЛИКАЦИИ',
        'ПУБЛИКАЦИИ ПО ТЕМЕ ДИССЕРТАЦИИ',
        'Публикации автора по теме диссертации',
        'Основные публикации по теме исследования',
        'Апробация работы и публикации',
        'СПИСОК ПУБЛИКАЦИЙ',
    ]
    all_upper = all_text.upper()
    idx = -1
    for header in pub_headers:
        i = all_upper.find(header.upper())
        if i >= 0:
            idx = i
            break
    if idx < 0:
        return None
    section = all_text[idx:idx + 8000]
    return section


# ============================================================
# LLM API
# ============================================================

def _parse_llm_json_response(content):
    """Парсит JSON-ответ от LLM."""
    if not content:
        return []

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
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        return []


def _extract_fields(pub_dict):
    """Извлекает и нормализует поля публикации из словаря LLM."""
    authors = pub_dict.get("authors", "")
    if isinstance(authors, list):
        authors = ", ".join(authors)
    elif not authors:
        authors = ""

    title = pub_dict.get("title", "")
    if not title:
        title = pub_dict.get("name", "")

    journal = pub_dict.get("journal", "")
    if not journal:
        journal = pub_dict.get("source", "")
        if not journal:
            journal = pub_dict.get("venue", "")

    year = pub_dict.get("year", None)
    if year:
        year = int(str(year).strip())
        if year < 1900 or year > 2099:
            year = None
    else:
        m = re.search(r'\b(20\d{2})\b', str(title + authors))
        if m:
            year = int(m.group(1))

    pages = pub_dict.get("pages", "")
    if not pages:
        pages = pub_dict.get("page_range", "")

    return {
        "authors": authors.strip(),
        "title": title.strip(),
        "journal": journal.strip(),
        "year": year,
        "pages": pages.strip() if isinstance(pages, str) else str(pages).strip(),
    }


def call_llm(system_prompt, llm_text, thinking_enabled=False):
    """Отправляет запрос к LLM с указанным системным промптом.

    Если thinking_enabled=True, добавляет reasoning-поле в запрос.
    Возвращает (публикации, время, raw_response).
    """
    user_prompt = PUB_USER_PROMPT.format(text=llm_text)
    start = time.time()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    }

    # Пытаемся включить thinking/reasoning через API
    if thinking_enabled:
        # Qwen3 поддерживает поле reasoning в API-запросе
        if "reasoning" in payload:
            payload["reasoning"] = True
        # Некоторые API поддерживают thinking как поле
        elif hasattr(payload, 'get'):
            pass  # API может не поддерживать это поле
        # Добавляем hint для reasoning в messages (fallback)
        messages.append({
            "role": "assistant",
            "content": "",
            "reasoning_content": "",
        })

    for attempt in range(3):
        try:
            resp = requests.post(
                LLM_API_URL,
                headers=HEADERS,
                json=payload,
                timeout=LLM_TIMEOUT,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            pubs = _parse_llm_json_response(raw)
            return pubs, elapsed, raw
        except requests.exceptions.Timeout:
            if attempt < 2:
                time.sleep(3)
            else:
                return [], time.time() - start, None
        except Exception as e:
            elapsed = time.time() - start
            if attempt < 2:
                time.sleep(2)
            else:
                return [], elapsed, f"Ошибка: {e}"

    return [], time.time() - start, "Failed after retries"


# ============================================================
# Оценка качества
# ============================================================

def normalize_key(text):
    """Нормализует текст для сравнения/детерупликации."""
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'[.,;:()—–—]', ' ', t)
    t = t.strip()
    return t[:150]


def evaluate_publications(pubs):
    """Оценивает качество извлечённых публикаций."""
    if not pubs:
        return {"count": 0, "valid": 0, "avg_title_len": 0, "year_coverage": 0,
                "journal_coverage": 0, "quality_score": 0, "issues": []}

    valid = 0
    total_title_len = 0
    has_year = 0
    has_journal = 0
    issues = []

    for p in pubs:
        if isinstance(p, str):
            title = p[:100]
            authors = ""
            journal = ""
            year = None
        else:
            p = _extract_fields(p)
            title = p.get("title", "")
            authors = p.get("authors", "")
            journal = p.get("journal", "")
            year = p.get("year")

        total_title_len += len(title)

        # Валидная публикация: есть заголовок + хотя бы одно дополнительное поле
        is_valid = bool(title) and (authors or journal or year)
        if is_valid:
            valid += 1

        if year:
            has_year += 1
        if journal:
            has_journal += 1

        # Проверки на качество
        if len(title) < 10:
            issues.append(f"Слишком короткий заголовок: {title[:30]}...")
        if not year:
            issues.append("Отсутствует год")
        if not authors and not journal:
            issues.append("Нет авторов и журнала")

    count = len(pubs)
    valid_ratio = valid / count
    year_ratio = has_year / count
    journal_ratio = has_journal / count
    avg_title_len = total_title_len / count

    # Quality score (0-100)
    score = 0
    score += valid_ratio * 40      # до 40 за долю валидных
    score += year_ratio * 20       # до 20 за покрытие годами
    score += journal_ratio * 20    # до 20 за покрытие журналами
    score += min(20, count / EXPECTED_PUB_COUNT * 20)  # до 20 за близость к эталону

    return {
        "count": count,
        "valid": valid,
        "valid_ratio": round(valid_ratio * 100, 1),
        "avg_title_len": round(avg_title_len, 1),
        "year_coverage": round(year_ratio * 100, 1),
        "journal_coverage": round(journal_ratio * 100, 1),
        "quality_score": round(min(100, score), 1),
        "issues": issues[:10],  # первые 10 проблем
    }


# ============================================================
# Статистика
# ============================================================

def mean(vals):
    return sum(vals) / len(vals) if vals else 0

def std(vals):
    if len(vals) < 2:
        return 0
    m = mean(vals)
    return (sum((x - m) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5

def min_val(vals):
    return min(vals) if vals else 0

def max_val(vals):
    return max(vals) if vals else 0


# ============================================================
# Тестирование
# ============================================================

def run_test(pdf_path):
    """Запускает полное тестирование с множественными повторениями."""
    print("=" * 70)
    print("ТЕСТИРОВАНИЕ: JSON-LLM извлечение публикаций")
    print("Сравнение: с размышлениями vs без размышлений")
    print(f"Повторений: {NUM_RUNS} (каждый вариант)")
    print("=" * 70)
    print(f"Файл: {os.path.basename(pdf_path)}")
    print(f"LLM: {LLM_MODEL} @ {LLM_API_URL}")
    print(f"Ожидаемое кол-во публикаций: {EXPECTED_PUB_COUNT}")
    print(f"Текст для LLM: последние {LLM_TEXT_CHARS} символов (~5 страниц)")
    print()

    # Читаем PDF
    print("Чтение PDF...")
    try:
        pages = read_pdf_text(pdf_path)
        total_pages = len(pages)
        print(f"  Страниц: {total_pages}")
    except Exception as e:
        print(f"  ОШИБКА чтения PDF: {e}")
        return

    llm_text = get_llm_text(pages)
    print(f"  Текст для LLM: {len(llm_text)} символов")

    # Показываем раздел публикаций (справочно)
    section_text = get_section_text(pages)
    if section_text:
        print(f"\nНайден раздел публикаций ({len(section_text)} символов):")
        print(f"  {section_text[:300]}...")
    else:
        print("\nРаздел публикаций НЕ найден в тексте")

    # ---- Variant A: WITHOUT thinking (multiple runs) ----
    print(f"\n{'='*70}")
    print(f"ВАРИАНТ A: БЕЗ РАЗМЫШЛЕНИЙ ({NUM_RUNS} запусков)")
    print(f"{'='*70}")

    results_a = []
    for run in range(1, NUM_RUNS + 1):
        pubs, elapsed, raw = call_llm(
            PUB_SYSTEM_PROMPT_BASE, llm_text, thinking_enabled=False
        )
        ev = evaluate_publications(pubs)
        results_a.append({
            "run": run,
            "count": ev['count'],
            "valid_ratio": ev['valid_ratio'],
            "year_coverage": ev['year_coverage'],
            "journal_coverage": ev['journal_coverage'],
            "quality_score": ev['quality_score'],
            "time": elapsed,
        })
        status = "OK" if ev['count'] == EXPECTED_PUB_COUNT else f"FAIL ({ev['count']})"
        print(f"  Запуск {run:2d}/{NUM_RUNS}: {ev['count']} pubs | "
              f"valid={ev['valid_ratio']}% | score={ev['quality_score']} | "
              f"time={elapsed:.1f}с | {status}")

    # ---- Variant B: WITH thinking (multiple runs) ----
    print(f"\n{'='*70}")
    print(f"ВАРИАНТ B: С РАЗМЫШЛЕНИЯМИ ({NUM_RUNS} запусков)")
    print(f"{'='*70}")

    results_b = []
    for run in range(1, NUM_RUNS + 1):
        pubs, elapsed, raw = call_llm(
            PUB_SYSTEM_PROMPT_THINKING, llm_text, thinking_enabled=True
        )
        ev = evaluate_publications(pubs)
        results_b.append({
            "run": run,
            "count": ev['count'],
            "valid_ratio": ev['valid_ratio'],
            "year_coverage": ev['year_coverage'],
            "journal_coverage": ev['journal_coverage'],
            "quality_score": ev['quality_score'],
            "time": elapsed,
        })
        status = "OK" if ev['count'] == EXPECTED_PUB_COUNT else f"FAIL ({ev['count']})"
        print(f"  Запуск {run:2d}/{NUM_RUNS}: {ev['count']} pubs | "
              f"valid={ev['valid_ratio']}% | score={ev['quality_score']} | "
              f"time={elapsed:.1f}с | {status}")

    # ---- Statistics ----
    print(f"\n{'='*70}")
    print("СТАТИСТИКА")
    print(f"{'='*70}")

    metrics = ["count", "valid_ratio", "year_coverage", "journal_coverage", "quality_score"]
    metric_labels = {
        "count": "Публикаций",
        "valid_ratio": "Валидных (%)",
        "year_coverage": "Покрытие годами (%)",
        "journal_coverage": "Покрытие журналами (%)",
        "quality_score": "Оценка качества",
    }

    print(f"\n  {'Метрика':<25} {'Без размышлений':<30} {'С размышлениями':<30}")
    print(f"  {'-'*85}")

    stats_a = {}
    stats_b = {}
    for m in metrics:
        vals_a = [r[m] for r in results_a]
        vals_b = [r[m] for r in results_b]
        stats_a[m] = {"mean": mean(vals_a), "std": std(vals_a), "min": min_val(vals_a), "max": max_val(vals_a)}
        stats_b[m] = {"mean": mean(vals_b), "std": std(vals_b), "min": min_val(vals_b), "max": max_val(vals_b)}

        label = metric_labels[m]
        print(f"  {label:<25} mean={stats_a[m]['mean']:6.1f} σ={stats_a[m]['std']:5.1f} "
              f"[{stats_a[m]['min']:5.1f}..{stats_a[m]['max']:5.1f}] | "
              f"mean={stats_b[m]['mean']:6.1f} σ={stats_b[m]['std']:5.1f} "
              f"[{stats_b[m]['min']:5.1f}..{stats_b[m]['max']:5.1f}]")

    # Time stats
    times_a = [r["time"] for r in results_a]
    times_b = [r["time"] for r in results_b]
    total_time_a = sum(times_a)
    total_time_b = sum(times_b)
    print(f"\n  {'Время на запуск (сек)':<25} mean={mean(times_a):6.1f} σ={std(times_a):5.1f} "
          f"[{min_val(times_a):5.1f}..{max_val(times_a):5.1f}] | "
          f"mean={mean(times_b):6.1f} σ={std(times_b):5.1f} "
          f"[{min_val(times_b):5.1f}..{max_val(times_b):5.1f}]")
    print(f"\n  {'Общее время':<25} {total_time_a:.1f} сек ({total_time_a/60:.1f} мин) | "
          f"{total_time_b:.1f} сек ({total_time_b/60:.1f} мин)")

    # Perfect score counts
    perfect_a = sum(1 for r in results_a if r['count'] == EXPECTED_PUB_COUNT and r['quality_score'] == 100)
    perfect_b = sum(1 for r in results_b if r['count'] == EXPECTED_PUB_COUNT and r['quality_score'] == 100)
    print(f"\n  {'Идеальных запусков':<25} {perfect_a}/{NUM_RUNS} | {perfect_b}/{NUM_RUNS}")

    # ---- Per-run detailed comparison ----
    print(f"\n{'='*70}")
    print("ПОЗАПУСКНОЕ СРАВНЕНИЕ")
    print(f"{'='*70}")
    print(f"\n  {'#':<4} {'Без: pubs/score':<18} {'С: pubs/score':<18} {'Δ score':<10} {'Без: time':<12} {'С: time':<12} {'Победитель':<12}")
    print(f"  {'-'*86}")

    winners = {"A": 0, "B": 0, "tie": 0}
    for i in range(NUM_RUNS):
        ra = results_a[i]
        rb = results_b[i]
        delta = rb['quality_score'] - ra['quality_score']
        if abs(delta) < 0.5:
            winner = "~равно~"
            winners["tie"] += 1
        elif delta > 0:
            winner = "С размышл."
            winners["B"] += 1
        else:
            winner = "Без размышл."
            winners["A"] += 1
        print(f"  {i+1:<4} {ra['count']:3d}/{ra['quality_score']:<13} {rb['count']:3d}/{rb['quality_score']:<13} "
              f"{delta:+.1f}        {ra['time']:6.1f}с      {rb['time']:6.1f}с   {winner:<12}")

    # ---- Verdict ----
    print(f"\n{'='*70}")
    print("ИТОГОВЫЙ ВЫВОД")
    print(f"{'='*70}")

    diff_mean = stats_b["quality_score"]["mean"] - stats_a["quality_score"]["mean"]
    print(f"\n  Средняя оценка — Без размышлений:  {stats_a['quality_score']['mean']:.1f} ± {stats_a['quality_score']['std']:.1f}")
    print(f"  Средняя оценка — С размышлениями:  {stats_b['quality_score']['mean']:.1f} ± {stats_b['quality_score']['std']:.1f}")
    print(f"  Разница средних: {diff_mean:+.1f} очков")

    if abs(diff_mean) < 2:
        print(f"\n  >>> Вывод: РАЗНИЦЫ НЕТ (разница < 2 очков, в пределах погрешности)")
    elif diff_mean > 0:
        print(f"\n  >>> Вывод: С РАЗМЫШЛЕНИЯМИ ЛУЧШЕ на {diff_mean:.1f} очков")
    else:
        print(f"\n  >>> Вывод: БЕЗ РАЗМЫШЛЕНИЙ ЛУЧШЕ на {abs(diff_mean):.1f} очков")

    print(f"\n  Победы: Без размышлений={winners['A']} | С размышлениями={winners['B']} | Ничьи={winners['tie']}")

    # ---- Save results ----
    responses_dir = "llm_responses"
    os.makedirs(responses_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    results_summary = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pdf": os.path.basename(pdf_path),
        "total_pages": total_pages,
        "expected_count": EXPECTED_PUB_COUNT,
        "llm_model": LLM_MODEL,
        "num_runs": NUM_RUNS,
        "system_prompts": {
            "without_thinking": PUB_SYSTEM_PROMPT_BASE,
            "with_thinking": PUB_SYSTEM_PROMPT_THINKING,
        },
        "statistics": {
            "without_thinking": stats_a,
            "with_thinking": stats_b,
        },
        "per_run": {
            "without_thinking": results_a,
            "with_thinking": results_b,
        },
        "perfect_a": perfect_a,
        "perfect_b": perfect_b,
        "winners": winners,
        "total_time_a": round(total_time_a, 1),
        "total_time_b": round(total_time_b, 1),
    }

    out_path = os.path.join(responses_dir, f"test_thinking_{timestamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results_summary, f, ensure_ascii=False, indent=2)
    print(f"\nРезультаты сохранены: {out_path}")
    print(f"{'='*70}")


def main():
    if not os.path.exists(PDF_PATH):
        print(f"Файл {PDF_PATH} не найден!")
        print("Поместите тестовый PDF-файл в текущую директорию.")
        return

    run_test(PDF_PATH)


if __name__ == "__main__":
    main()
