"""Взаимодействие с VAK API с rate limiting."""
import os
import time
import re
import requests

from config import API_BASE, HEADERS
from rate_limiter import VAK_API_LIMITER
from logging_config import get_logger

logger = get_logger("vak_api")


def _api_request_with_retry(url, params=None, headers=None, max_retries=3, timeout=30):
    """Выполняет HTTP-запрос с повторными попытками и rate limiting."""
    for attempt in range(max_retries):
        # Rate limiting: ждём пока разрешён запрос
        if not VAK_API_LIMITER.allow():
            wait = VAK_API_LIMITER.wait_time()
            logger.debug(f"Rate limited, waiting {wait:.1f}s")
            time.sleep(wait)

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"Connection error, retry {attempt + 1}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "?"
            if status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"HTTP {status_code}, retry {attempt + 1}/{max_retries} in {wait}s")
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
        logger.error(f"File not found: {spec_file}")
        return specs
    with open(spec_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\S+?)\s*[-—–]\s*(.*)', line)
            if m:
                specs.append((m.group(1).strip(), m.group(2).strip()))
            else:
                specs.append((line, ""))
    logger.info(f"Loaded {len(specs)} specialties from {spec_file}")
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
                logger.error(f"API error (page {page}): max retries exceeded")
                break
        except requests.exceptions.HTTPError as e:
            logger.error(f"API error (page {page}): HTTP {e.response.status_code if e.response else '?'}")
            break
        except Exception as e:
            logger.error(f"API error (page {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break
        all_results.extend(results)
        logger.debug(f"Page {page}: {len(all_results)} of {data.get('count', '?')}")

        if data.get("next"):
            page += 1
        else:
            break

    logger.info(f"Total {len(all_results)} adverts for specialty {specialty_id}")
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
        logger.error(f"Detail error {advert_id}: HTTP {e.response.status_code if e.response else '?'}")
        return {}
    except Exception as e:
        logger.error(f"Detail error {advert_id}: {e}")
        return {}
