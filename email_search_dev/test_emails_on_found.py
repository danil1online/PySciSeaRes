"""Тестирование поиска email на найденных источниках."""

import warnings
warnings.filterwarnings('ignore')

import sys, os, json, time, types

PROJECT_DIR = '/home/user/vak-adverts-list'
sys.path.insert(0, PROJECT_DIR)

mock_config = types.ModuleType("config")
mock_config.LLM_API_URL = "http://195.133.13.56:1234/v1/chat/completions"
mock_config.LLM_MODEL = "qwen3.5-4b"
mock_config.LLM_TIMEOUT = 120
mock_config.LLM_MAX_TOKENS = 2000
mock_config.LLM_TEMPERATURE = 0.1
sys.modules["config"] = mock_config

import importlib.util
DEV_DIR = os.path.join(PROJECT_DIR, "email_search_dev")
spec = importlib.util.spec_from_file_location("email_search", os.path.join(DEV_DIR, "email_search.py"))
email_search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(email_search)

# Загружаем результаты поиска источников
with open(os.path.join(DEV_DIR, "test_sources_full.json")) as f:
    source_results = json.load(f)

# Фильтруем только найденные источники
publications_with_sources = []
for r in source_results:
    if r["source"]:
        publications_with_sources.append({
            "title": r["title"][:80],
            "authors": r["authors"],
            "year": r["year"],
            "journal": r["journal"],
            "source": r["source"],
        })

print(f"Найдено источников: {len(publications_with_sources)}")
print(f"Начинаем поиск email...\n")

all_results = []
for i, pub in enumerate(publications_with_sources, 1):
    print(f"[{i}/{len(publications_with_sources)}] {pub['title'][:60]}...")
    print(f"    Источник: {pub['source'].get('source_name','?')} ({pub['source'].get('source_type','?')})")
    
    try:
        email_result = email_search.find_email_for_publication(
            {"title": pub["title"], "authors": pub["authors"], "year": pub["year"]},
            autoref_text=None
        )
        emails = email_result.get("emails", [])
        if emails:
            print(f"    EMAIL: {', '.join(emails)}")
        else:
            print(f"    EMAIL: не найден")
        
        all_results.append({
            "index": pub.get("index", i),
            "title": pub["title"],
            "authors": pub["authors"],
            "source_name": pub["source"].get("source_name","?"),
            "source_type": pub["source"].get("source_type","?"),
            "emails": emails,
        })
    except Exception as e:
        print(f"    ОШИБКА: {e}")
        all_results.append({
            "index": pub.get("index", i),
            "title": pub["title"],
            "authors": pub["authors"],
            "error": str(e),
            "emails": [],
        })
    
    time.sleep(1)

print("\n" + "=" * 80)
print("ИТОГИ ПОИСКА EMAIL")
print("=" * 80)

emails_found = sum(1 for r in all_results if r["emails"])
unique_emails = set()
for r in all_results:
    unique_emails.update(r["emails"])

print(f"Обработано: {len(all_results)}")
print(f"Email найдено: {emails_found}/{len(all_results)} ({emails_found/len(all_results)*100:.0f}%)")
print(f"Уникальных email: {len(unique_emails)}")
if unique_emails:
    for e in sorted(unique_emails):
        print(f"  - {e}")

# Детализация
print("\nДЕТАЛИ:")
for r in all_results:
    idx = r["index"]
    auth = r["authors"][:25]
    src = r["source_name"][:15]
    if r["emails"]:
        print(f"  #{idx:2d} {auth} [{src}] -> {', '.join(r['emails'])}")
    elif "error" in r:
        print(f"  #{idx:2d} {auth} [{src}] -> ОШИБКА: {r['error'][:40]}")
    else:
        print(f"  #{idx:2d} {auth} [{src}] -> не найден")

output_path = os.path.join(DEV_DIR, "test_emails_full.json")
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(all_results, f, ensure_ascii=False, indent=2)
print(f"\nРезультаты: {output_path}")
