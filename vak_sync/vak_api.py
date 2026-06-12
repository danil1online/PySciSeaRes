"""Взаимодействие с VAK API."""
import os
import time
import re
import requests

from config import API_BASE, HEADERS


def _api_request_with_retry(url, params=None, headers=None, max_retries=3, timeout=30):
    """Выполняет HTTP-запрос с повторными попытками при ошибках."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt  # экспоненциальная задержка: 1, 2, 4 сек
                print(f"    Соединение потеряно, повтор через {wait}с...")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"    HTTP {status_code}, повтор через {wait}с...")
                time.sleep(wait)
            else:
                raise
    return None


def read_specialties(spec_file=None):
    """Загружает список специальностей из sci_spec.txt."""
    if spec_file is None:
        from config import SCI_SPEC_FILE
        spec_file = SCI_SPEC_FILE
    specs = []
    if not os.path.exists(spec_file):
        print(f"  Файл {spec_file} не найден!")
        return specs
    with open(spec_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Format: "1.2.1 - Искусственный интеллект" or just "1.2.1."
            m = re.match(r'^(\S+?)\s*[-—–]\s*(.*)', line)
            if m:
                specs.append((m.group(1).strip(), m.group(2).strip()))
            else:
                specs.append((line, ""))
    print(f"  Загружено {len(specs)} специальностей из {spec_file}")
    return specs


def search_adverts(specialty_id, date_from, date_to):
    """Ищет объявления о защите по специальности и дате."""
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
        try:
            data = _api_request_with_retry(
                f"{API_BASE}/att/adverts/",
                params=params,
                headers=HEADERS,
                timeout=30,
            )
            if data is None:
                print(f"  Ошибка API (страница {page}): max retries exceeded")
                break
        except requests.exceptions.HTTPError as e:
            print(f"  Ошибка API (страница {page}): HTTP {e.response.status_code if e.response else '?'}")
            break
        except Exception as e:
            print(f"  Ошибка API (страница {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break
        all_results.extend(results)
        print(f"  Страница {page}: {len(all_results)} из {data.get('count', '?')}")

        if data.get("next"):
            page += 1
        else:
            break

    return all_results


def get_advert_detail(advert_id):
    """Получает детальную информацию об объявлении."""
    try:
        data = _api_request_with_retry(
            f"{API_BASE}/att/adverts/{advert_id}/",
            headers=HEADERS,
            timeout=30,
        )
        return data if data else {}
    except requests.exceptions.HTTPError as e:
        print(f"  Ошибка получения детализации {advert_id}: HTTP {e.response.status_code if e.response else '?'}")
        return {}
    except Exception as e:
        print(f"  Ошибка получения детализации {advert_id}: {e}")
        return {}
