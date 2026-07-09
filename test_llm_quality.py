import requests
import urllib3
import pdfplumber
import re
import json
import os
import time
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import LLM_API_URL, LLM_LOCAL_URL, LLM_MODEL, LLM_LOCAL_MODEL

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


def send_to_llm(url, model, text, max_tokens=2000, temperature=0.1, retries=3):
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
    # Trim text if too long (LLM 2 35B has slow processing on large inputs)
    if len(text) > 5000:
        text = text[:5000]

    start = time.time()
    for attempt in range(retries):
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=300 if "35B" in model else 120,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return content, elapsed
        except (requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                print(f"    Таймаут, повтор {attempt+1}/{retries}...")
                time.sleep(3)
            else:
                elapsed = time.time() - start
                return None, elapsed
        except Exception as e:
            elapsed = time.time() - start
            return None, elapsed
    return None, time.time() - start


def extract_publications_regex(pub_text):
    raw_items = re.split(r'(?<=\n)\d+\.\s', pub_text)
    publications = []
    for raw in raw_items[1:]:
        if not raw.strip():
            continue
        text = raw.strip()
        text = re.sub(r'\s*\-\s*\n\s*', ' ', text)
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
            r"\b20\d{2}\b", r"\b20\d{2}\.", " т\.\s*\d", r"\bс\.\s*\d",
            r"№\s*\d", r"стр\.?\s*\d", r"doi:\s*10\.",
        ]
        has_strong = any(ind.lower() in low for ind in strong_indicators)
        has_weak = any(re.search(ind, low) for ind in weak_indicators)
        if not has_strong and not has_weak:
            return False
    return True


def _normalize_for_dedup(text):
    t = text.lower()
    t = t.replace("\u2010", "").replace("\u2011", "").replace("\u2012", "")
    t = t.replace("\u2013", "").replace("\u2014", "").replace("\u2015", "")
    t = t.replace("-", "")
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
        return merged
    if llm_pubs and len(llm_pubs) > len(regex_pubs):
        return llm_pubs
    return regex_pubs


def extract_from_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = ""
        for page in pdf.pages:
            t = page.extract_text() or ""
            text += "\n" + t

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

    if idx < 0:
        list_idx = text.find("СПИСОК ОПУБЛИКОВАННЫХ")
        pub_idx = text.find("Публикац")
        if list_idx >= 0:
            idx = list_idx
        elif pub_idx >= 0:
            idx = pub_idx
    if idx < 0:
        return None, None, None, None

    pub_text = text[idx:idx + 4000]
    regex_pubs = extract_publications_regex(pub_text)

    llm_section = text[idx:idx + 3500]
    return regex_pubs, llm_section, idx, len(text)


def evaluate_publications(pubs):
    """Evaluate quality of extracted publications."""
    metrics = {
        "count": len(pubs),
        "avg_length": 0,
        "short_entries": 0,
        "long_entries": 0,
        "has_year": 0,
        "has_journal": 0,
        "has_doi": 0,
        "has_pages": 0,
        "has_confidence_indicators": 0,
        "potential_filler": 0,
    }
    if not pubs:
        return metrics

    total_len = 0
    for p in pubs:
        total_len += len(p)
        low = p.lower()

        if len(p) < 50:
            metrics["short_entries"] += 1
        if len(p) > 300:
            metrics["long_entries"] += 1

        if re.search(r'\b20\d{2}\b', low):
            metrics["has_year"] += 1
        if any(w in low for w in ["журн", "вестн", "сборник", "процед", "транз", "изд"]):
            metrics["has_journal"] += 1
        if "doi" in low:
            metrics["has_doi"] += 1
        if re.search(r'с\.\s*\d|стр\.?\s*\d|с\.\s*\d', low):
            metrics["has_pages"] += 1

        indicators = ["//", "№", "т.", "conf.", "proceedings", "isbn"]
        if any(ind in low for ind in indicators):
            metrics["has_confidence_indicators"] += 1

        filler = ["положения диссертации", "рассмотрен", "исследование показало",
                  "автор установил", "было проведено", "в работе предлож"]
        if any(f in low for f in filler):
            metrics["potential_filler"] += 1

    metrics["avg_length"] = round(total_len / len(pubs), 1)
    return metrics


def quality_score(metrics):
    """Calculate a 0-100 quality score from metrics."""
    score = 50  # baseline
    count = metrics["count"]

    # Optimal count is 5-20
    if 5 <= count <= 20:
        score += 15
    elif 3 <= count <= 30:
        score += 5
    elif count > 30:
        score -= 20  # likely hallucination
    elif count == 0:
        score -= 10

    # Year coverage
    if count > 0:
        year_ratio = metrics["has_year"] / count
        score += year_ratio * 10

    # Journal coverage
    if count > 0:
        journal_ratio = metrics["has_journal"] / count
        score += journal_ratio * 10

    # Confidence indicators
    if count > 0:
        indicator_ratio = metrics["has_confidence_indicators"] / count
        score += indicator_ratio * 10

    # Penalty for fillers
    if count > 0:
        filler_ratio = metrics["potential_filler"] / count
        score -= filler_ratio * 15

    # Penalty for short/long entries
    if count > 0:
        short_ratio = metrics["short_entries"] / count
        score -= short_ratio * 10

    return max(0, min(100, round(score)))


def run_test(pdf_files):
    print("=" * 80)
    print("ТЕСТИРОВАНИЕ КАЧЕСТВА LLM НА АВТОРЕФЕРАТАХ ДИССЕРТАЦИЙ")
    print("=" * 80)
    print(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PDF файлов: {len(pdf_files)}")
    print(f"LLM 1: {LLM_MODEL} ({LLM_API_URL})")
    print(f"LLM 2: {LLM_LOCAL_MODEL} ({LLM_LOCAL_URL})")
    print("=" * 80)
    print()

    results = []

    for idx, pdf_path in enumerate(pdf_files, 1):
        fname = os.path.basename(pdf_path)
        print(f"\n{'='*60}")
        print(f"ПРИБЛИЖЕНИЕ {idx}/{len(pdf_files)}: {fname}")
        print(f"{'='*60}")

        # Extract text and find publications section
        try:
            regex_pubs, llm_section, section_idx, total_pages = extract_from_pdf(pdf_path)
        except Exception as e:
            print(f"  Ошибка чтения PDF: {e}")
            continue

        if regex_pubs is None:
            print("  Раздел публикаций не найден в PDF")
            continue

        total_pages_count = 0
        with pdfplumber.open(pdf_path) as pdf:
            total_pages_count = len(pdf.pages)
        print(f"  Страниц PDF: {total_pages_count}, раздел найден на позиции: {section_idx}")
        print(f"  Regex-публикаций: {len(regex_pubs)}")

        if not regex_pubs:
            print("  Regex не нашёл публикаций — переходим к LLM")
        else:
            print("  Примеры regex-публикаций:")
            for i, p in enumerate(regex_pubs[:3]):
                print(f"    [{i+1}] {p[:100]}...")

        # Test LLM 1
        print(f"\n  Запрос к LLM 1 ({LLM_MODEL})...")
        llm1_response, llm1_time = send_to_llm(LLM_API_URL, LLM_MODEL, llm_section)
        llm1_pubs = _parse_llm_publications(llm1_response) if llm1_response else []
        if len(llm1_pubs) > 30:
            llm1_pubs = llm1_pubs[:20]
        print(f"  LLM 1: {len(llm1_pubs)} публикаций ({llm1_time:.1f} сек)")

        # Test LLM 2
        print(f"\n  Запрос к LLM 2 ({LLM_LOCAL_MODEL})...")
        llm2_response, llm2_time = send_to_llm(LLM_LOCAL_URL, LLM_LOCAL_MODEL, llm_section)
        llm2_pubs = _parse_llm_publications(llm2_response) if llm2_response else []
        if len(llm2_pubs) > 30:
            llm2_pubs = llm2_pubs[:20]
        print(f"  LLM 2: {len(llm2_pubs)} публикаций ({llm2_time:.1f} сек)")

        # Merge with regex
        merged_regex_llm1 = _merge_publications(regex_pubs, llm1_pubs)
        merged_regex_llm2 = _merge_publications(regex_pubs, llm2_pubs)

        print(f"\n  Мерж regex+LLM1: {len(merged_regex_llm1)} публикаций")
        print(f"  Мерж regex+LLM2: {len(merged_regex_llm2)} публикаций")

        # Evaluate each approach
        regex_metrics = evaluate_publications(regex_pubs)
        llm1_metrics = evaluate_publications(llm1_pubs)
        llm2_metrics = evaluate_publications(llm2_pubs)
        merged1_metrics = evaluate_publications(merged_regex_llm1)
        merged2_metrics = evaluate_publications(merged_regex_llm2)

        regex_score = quality_score(regex_metrics)
        llm1_score = quality_score(llm1_metrics)
        llm2_score = quality_score(llm2_metrics)
        merged1_score = quality_score(merged1_metrics)
        merged2_score = quality_score(merged2_metrics)

        print(f"\n  --- ОЦЕНКИ КАЧЕСТВА ---")
        print(f"  Regex:        {regex_score}/100  (pubs={regex_metrics['count']})")
        print(f"  LLM 1 (2B):   {llm1_score}/100  (pubs={llm1_metrics['count']}, time={llm1_time:.1f}с)")
        print(f"  LLM 2 (35B):  {llm2_score}/100  (pubs={llm2_metrics['count']}, time={llm2_time:.1f}с)")
        print(f"  Мерж+LLM1:    {merged1_score}/100  (pubs={merged1_metrics['count']})")
        print(f"  Мерж+LLM2:    {merged2_score}/100  (pubs={merged2_metrics['count']})")

        # LLM comparison
        print(f"\n  --- СРАВНЕНИЕ LLM ---")
        winner = "LLM 2 (35B)" if llm2_score > llm1_score else "LLM 1 (2B)" if llm1_score > llm2_score else "Ничья"
        print(f"  Победитель: {winner}")
        print(f"  Разница: {abs(llm1_score - llm2_score)} очков")
        if llm1_score != llm2_score:
            better = llm1_pubs if llm1_score > llm2_score else llm2_pubs
            better_m = llm1_metrics if llm1_score > llm2_score else llm2_metrics
            print(f"  Лучший LLM: {better_m['count']} pubs, avg_len={better_m['avg_length']}, year_ratio={better_m['has_year']/max(1,better_m['count']):.2%}, journal_ratio={better_m['has_journal']/max(1,better_m['count']):.2%}")

        # Detailed metrics
        print(f"\n  --- ПОДРОБНЫЕ МЕТРИКИ ---")
        print(f"  {'Метрика':<25} {'Regex':<8} {'LLM1':<8} {'LLM2':<8}")
        print(f"  {'-'*55}")
        print(f"  {'count':<25} {regex_metrics['count']:<8} {llm1_metrics['count']:<8} {llm2_metrics['count']:<8}")
        print(f"  {'avg_length':<25} {regex_metrics['avg_length']:<8} {llm1_metrics['avg_length']:<8} {llm2_metrics['avg_length']:<8}")
        print(f"  {'short_entries':<25} {regex_metrics['short_entries']:<8} {llm1_metrics['short_entries']:<8} {llm2_metrics['short_entries']:<8}")
        print(f"  {'has_year':<25} {regex_metrics['has_year']:<8} {llm1_metrics['has_year']:<8} {llm2_metrics['has_year']:<8}")
        print(f"  {'has_journal':<25} {regex_metrics['has_journal']:<8} {llm1_metrics['has_journal']:<8} {llm2_metrics['has_journal']:<8}")
        print(f"  {'has_doi':<25} {regex_metrics['has_doi']:<8} {llm1_metrics['has_doi']:<8} {llm2_metrics['has_doi']:<8}")
        print(f"  {'has_pages':<25} {regex_metrics['has_pages']:<8} {llm1_metrics['has_pages']:<8} {llm2_metrics['has_pages']:<8}")
        print(f"  {'conf_indicators':<25} {regex_metrics['has_confidence_indicators']:<8} {llm1_metrics['has_confidence_indicators']:<8} {llm2_metrics['has_confidence_indicators']:<8}")
        print(f"  {'potential_filler':<25} {regex_metrics['potential_filler']:<8} {llm1_metrics['potential_filler']:<8} {llm2_metrics['potential_filler']:<8}")

        # Save LLM responses for manual inspection
        responses_dir = "llm_responses"
        os.makedirs(responses_dir, exist_ok=True)
        safe_name = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ ]', '_', fname)
        with open(os.path.join(responses_dir, f"{safe_name}_llm1.json"), "w", encoding="utf-8") as f:
            json.dump({
                "pdf": fname,
                "llm": LLM_MODEL,
                "time_sec": round(llm1_time, 2),
                "pub_count": len(llm1_pubs),
                "response": llm1_response,
                "parsed_publications": llm1_pubs,
            }, f, ensure_ascii=False, indent=2)
        with open(os.path.join(responses_dir, f"{safe_name}_llm2.json"), "w", encoding="utf-8") as f:
            json.dump({
                "pdf": fname,
                "llm": LLM_LOCAL_MODEL,
                "time_sec": round(llm2_time, 2),
                "pub_count": len(llm2_pubs),
                "response": llm2_response,
                "parsed_publications": llm2_pubs,
            }, f, ensure_ascii=False, indent=2)

        results.append({
            "pdf": fname,
            "regex": {"count": regex_metrics["count"], "score": regex_score},
            "llm1": {"count": llm1_metrics["count"], "score": llm1_score, "time": llm1_time},
            "llm2": {"count": llm2_metrics["count"], "score": llm2_score, "time": llm2_time},
            "merged1": {"count": merged1_metrics["count"], "score": merged1_score},
            "merged2": {"count": merged2_metrics["count"], "score": merged2_score},
            "regex_metrics": regex_metrics,
            "llm1_metrics": llm1_metrics,
            "llm2_metrics": llm2_metrics,
        })

    # Summary
    print(f"\n\n{'='*80}")
    print(f"ИТОГОВАЯ ТАБЛИЦА")
    print(f"{'='*80}")

    print(f"\n{'PDF':<45} {'Regex':<8} {'LLM1':<8} {'LLM2':<8} {'M1':<8} {'M2':<8} {'Winner':<8}")
    print(f"{'-'*95}")

    total_regex_score = 0
    total_llm1_score = 0
    total_llm2_score = 0
    total_merged1_score = 0
    total_merged2_score = 0
    winner_count = {"LLM1": 0, "LLM2": 0, "Tie": 0, "NoData": 0}

    for r in results:
        name = r["pdf"][:42] + "..." if len(r["pdf"]) > 45 else r["pdf"]
        print(f"{name:<45} {r['regex']['score']:<8} {r['llm1']['score']:<8} {r['llm2']['score']:<8} {r['merged1']['score']:<8} {r['merged2']['score']:<8}", end="")

        if r["llm2"]["score"] > r["llm1"]["score"]:
            print(f" {'LLM2':<8}", end="")
            winner_count["LLM2"] += 1
        elif r["llm1"]["score"] > r["llm2"]["score"]:
            print(f" {'LLM1':<8}", end="")
            winner_count["LLM1"] += 1
        else:
            print(f" {'Tie':<8}", end="")
            winner_count["Tie"] += 1
        print()

        total_regex_score += r["regex"]["score"]
        total_llm1_score += r["llm1"]["score"]
        total_llm2_score += r["llm2"]["score"]
        total_merged1_score += r["merged1"]["score"]
        total_merged2_score += r["merged2"]["score"]

    n = len(results) if results else 1
    print(f"\n--- СРЕДНИЕ ОЦЕНКИ ---")
    print(f"  Regex:        {total_regex_score/n:.1f}/100")
    print(f"  LLM 1 (2B):   {total_llm1_score/n:.1f}/100")
    print(f"  LLM 2 (35B):  {total_llm2_score/n:.1f}/100")
    print(f"  Мерж+LLM1:    {total_merged1_score/n:.1f}/100")
    print(f"  Мерж+LLM2:    {total_merged2_score/n:.1f}/100")
    print(f"\n--- ПОБЕДЫ LLM ---")
    print(f"  LLM 1 (2B):  {winner_count['LLM1']}/{n}")
    print(f"  LLM 2 (35B): {winner_count['LLM2']}/{n}")
    print(f"  Ничья:       {winner_count['Tie']}/{n}")

    # Overall summary
    print(f"\n--- ОБЩИЙ ВЫВОД ---")
    avg_llm1 = total_llm1_score / n
    avg_llm2 = total_llm2_score / n
    if avg_llm2 > avg_llm1 + 5:
        print(f"  LLM 2 (35B)显著优于 LLM 1 (2B) — средняя разница {avg_llm2 - avg_llm1:.1f} очков")
    elif avg_llm1 > avg_llm2 + 5:
        print(f"  LLM 1 (2B)优于 LLM 2 (35B) — средняя разница {avg_llm1 - avg_llm2:.1f} очков")
    else:
        print(f"  LLM 1 (2B) и LLM 2 (35B) показывают сопоставимые результаты (разница {abs(avg_llm1 - avg_llm2):.1f} очков)")

    if total_merged2_score / n > max(total_llm1_score, total_llm2_score) / n + 5:
        print(f"  Мерж с LLM 2 даёт лучший результат — regex+LLM комбинация эффективнее одного метода")

    # Save summary
    os.makedirs("llm_responses", exist_ok=True)
    with open("llm_responses/summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "date": datetime.now().isoformat(),
            "num_pdfs": len(pdf_files),
            "llm1": {"model": LLM_MODEL, "url": LLM_API_URL, "avg_score": round(total_llm1_score/n, 1)},
            "llm2": {"model": LLM_LOCAL_MODEL, "url": LLM_LOCAL_URL, "avg_score": round(total_llm2_score/n, 1)},
            "regex_avg": round(total_regex_score/n, 1),
            "merged1_avg": round(total_merged1_score/n, 1),
            "merged2_avg": round(total_merged2_score/n, 1),
            "winners": winner_count,
            "per_pdf": results,
        }, f, ensure_ascii=False, indent=2)

    print(f"\nРезультаты сохранены в llm_responses/")
    print("="*80)


def main():
    # Collect all PDF files
    autorefs_dir = "autorefs"
    if not os.path.exists(autorefs_dir):
        print(f"Папка {autorefs_dir} не найдена!")
        return

    pdf_files = sorted([
        os.path.join(autorefs_dir, f)
        for f in os.listdir(autorefs_dir)
        if f.endswith('.pdf')
    ])

    # If we have fewer than 10, try to fetch more from API
    if len(pdf_files) < 10:
        needed = 10 - len(pdf_files)
        print(f"\nНайдено {len(pdf_files)} PDF, пытаемся загрузить ещё {needed} из API...")
        try:
            api_pdf_files = fetch_and_download_more(needed)
            pdf_files.extend(api_pdf_files)
        except Exception as e:
            print(f"  Не удалось загрузить: {e}")

    if len(pdf_files) == 0:
        print("Нет PDF файлов для тестирования!")
        return

    pdf_files = pdf_files[:10]
    print(f"Всего PDF для тестирования: {len(pdf_files)}")
    for i, p in enumerate(pdf_files, 1):
        size = os.path.getsize(p) // 1024
        print(f"  {i}. {os.path.basename(p)} ({size} КБ)")

    run_test(pdf_files)


def fetch_and_download_more(count):
    """Fetch more dissertations from API and download their autoref PDFs."""
    import sqlite3
    API_BASE = "https://vak.gisnauka.ru/api"
    HEADERS = {"Accept": "application/json"}
    DOWNLOAD_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    specs = ['3.1.10.', '1.3.6.', '2.1.7.', '1.4.14.', '2.9.7.']
    new_pdfs = []

    for spec in specs:
        if len(new_pdfs) >= count:
            break
        try:
            resp = requests.get(f'{API_BASE}/att/adverts/', params={
                'specialty': spec, 'date_defend_from': '2024-01-01',
                'date_defend_to': '2024-12-31', 'page_size': 20
            }, headers=HEADERS, timeout=15)
            adverts = resp.json().get('results', [])
            for a in adverts:
                if len(new_pdfs) >= count:
                    break
                detail = requests.get(f'{API_BASE}/att/adverts/{a["id"]}/', headers=HEADERS, timeout=15)
                d = detail.json()
                autoref_url = d.get('autoref_site', '')
                if not autoref_url:
                    continue
                fname = d.get('fio', 'unknown')
                fname = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ ]', '_', fname)
                fname = re.sub(r'\s+', '_', fname).strip('_') + ".pdf"
                save_path = os.path.join(autorefs_dir, fname)
                if os.path.exists(save_path):
                    continue

                try:
                    resp2 = requests.get(autoref_url, stream=True, timeout=120, headers=DOWNLOAD_HEADERS, verify=False)
                    resp2.raise_for_status()
                    content_len = int(resp2.headers.get('Content-Length', 0))
                    if content_len < 500 * 1024:
                        # Try to get content without length check
                        pass
                    with open(save_path, "wb") as f:
                        for chunk in resp2.iter_content(chunk_size=8192):
                            f.write(chunk)
                    if os.path.getsize(save_path) > 0:
                        new_pdfs.append(save_path)
                        print(f"  Скачан: {fname}")
                except Exception as e:
                    if os.path.exists(save_path):
                        os.remove(save_path)
        except Exception as e:
            print(f"  Ошибка при загрузке из {spec}: {e}")

    return new_pdfs


if __name__ == "__main__":
    main()
