import json
import re
import requests
import pdfplumber

from config import (
    LLM_API_URL, LLM_MODEL, HEADERS,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
)

from .cache import get_cached, cache_result

SUPERVISOR_SYSTEM_PROMPT = (
    "Ты — помощник по извлечению данных о научном руководителе из автореферата диссертации. "
    "Извлеки ФИО и место работы научного руководителя. "
    "Верни ТОЛЬКО JSON в формате: "
    '{"supervisor_name": "ФИО", "supervisor_work": "место работы"} '
    'Если не найдено: {"supervisor_name": null, "supervisor_work": null}.'
)


def extract_supervisor_from_pdf_text(text):
    """Извлекает руководителя из текста (PDF уже прочитан снаружи)."""
    return _send_supervisor_request(text)


def extract_supervisor_from_pdf(pdf_path):
    """Извлекает руководителя из PDF-файла (legacy, для обратной совместимости)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            pages_to_read = min(3, len(pdf.pages))
            for i in range(pages_to_read):
                t = pdf.pages[i].extract_text() or ""
                text += "\n" + t
        return extract_supervisor_from_pdf_text(text)
    except Exception as e:
        print(f"  Ошибка чтения PDF для руководителя {pdf_path}: {e}")
        return {"supervisor_name": None, "supervisor_work": None}


def _send_supervisor_request(text):
    prompt = (
        "Извлеки из текста автореферата диссертации информацию о научном руководителе.\n\n"
        "Формат данных в автореферате обычно такой:\n"
        "Научный руководитель: Киселев Сергей Константинович,\n"
        "доктор технических наук, доцент, заведующий\n"
        "кафедрой «Измерительно-вычислительные комплексы» ФГБОУ ВО «Ульяновский государственный технический университет»\n\n"
        "Нужно извлечь:\n"
        "- supervisor_name: полное ФИО (три слова через пробел)\n"
        "- supervisor_work: место работы (должность, кафедра, организация)\n\n"
        "Верни ТОЛЬКО JSON: {\"supervisor_name\": \"...\", \"supervisor_work\": \"...\"}\n"
        "Если не найдено: {\"supervisor_name\": null, \"supervisor_work\": null}\n\n"
        "Текст (первые 3000 символов):\n\n" + text[:3000]
    )

    try:
        # Проверяем кэш (ключ — текст, так как промпт фиксированный)
        cached_content, cached_time, from_cache = get_cached("", text[:3000])
        if from_cache:
            content = cached_content
        else:
            resp = requests.post(
                LLM_API_URL,
                headers=HEADERS,
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 500,
                    "temperature": LLM_TEMPERATURE,
                },
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            # Сохраняем в кэш
            cache_result("", text[:3000], content, 0)

        # Try to extract JSON from response
        json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "supervisor_name": data.get("supervisor_name"),
                "supervisor_work": data.get("supervisor_work"),
            }
        return {"supervisor_name": None, "supervisor_work": None}

    except Exception as e:
        print(f"  Ошибка LLM запроса для руководителя: {e}")
        return {"supervisor_name": None, "supervisor_work": None}
