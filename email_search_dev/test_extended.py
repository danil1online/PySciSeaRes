"""Расширенное тестирование email_search."""

import sys
import os
import json
import time
import types

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

PUBLICATIONS = [
    # Каган (строительство/бетон)
    {"title": "Исследование проникающей способности различных сред в бетон в зависимости от технологических факторов, влияющих на его влагопоглощение", "authors": "Каган М.Н., Байбурин А.Х., Коваль С.Б.", "year": 2020, "journal": "Вестник ЮУрГУ. Серия Строительство и архитектура", "pages": "34-45"},
    {"title": "Прочность контакта бетонов при устройстве технологических швов и стыков в железобетонных конструкциях", "authors": "Каган М.Н., Коваль С.Б., Мельник Л.Б., Байбурин А.Х.", "year": 2021, "journal": "Строительное производство", "pages": "9-18"},
    {"title": "Влияние технологических факторов на прочность бетона в зоне контакта свежеуложенного слоя с затвердевшим", "authors": "Каган М.Н., Коваль С.Б.", "year": 2022, "journal": "Вестник ЮУрГУ. Серия Строительство и архитектура", "pages": "68-74"},
    {"title": "Прочность контакта бетонов при устройстве швов и стыков", "authors": "Каган М.Н., Коваль С.Б., Молодцов М.В.", "year": 2023, "journal": "Инженерный вестник Дона", "pages": "503-513"},
    {"title": "Влияние технологических факторов устройства рабочих швов бетонирования на работу железобетонных конструкций", "authors": "Каган М.Н., Дербенцев И.С., Коваль С.Б., Мельник Л.Б.", "year": 2023, "journal": "Вестник ЮУрГУ. Серия Строительство и архитектура", "pages": "59-66"},
    {"title": "Технология устройства незапланированного рабочего шва бетонирования с применением шлакощелочного раствора", "authors": "Каган М.Н.", "year": 2025, "journal": "Инженерный вестник Дона", "pages": "414-421"},
    {"title": "Технологические параметры устройства рабочих швов и стыков в железобетонных конструкциях с использованием шлакощелочных бетонных смесей", "authors": "Каган М.Н.", "year": 2025, "journal": "Вестник ЮУрГУ. Серия Строительство и архитектура", "pages": "43-50"},
    {"title": "Analysis of Various Media Concrete Penetrating Ability Depending on Different Factors Affecting Water Absorption", "authors": "Kagan M.N., Koval S.B.", "year": 2017, "journal": "Procedia Engineering", "pages": "819-825"},
    {"title": "Research the influence of acoustical treatment of concrete on its water absorption", "authors": "Kagan M.N., Baiburin A.K., Sapozhnikov S.B.", "year": 2018, "journal": "IOP Conference Series: Materials Science and Engineering", "pages": ""},
    {"title": "New and Old Concrete Adhesion Strength for Construction Joints in Reinforced Concrete Structures", "authors": "Kagan M.N.", "year": 2024, "journal": "Lecture Notes in Civil Engineering", "pages": "207-217"},
    {"title": "Сцепление бетона в зоне технологического шва при усилении конструкций", "authors": "Вахняк (Каган) М.Н.", "year": 2010, "journal": "Научный поиск", "pages": ""},
    # Соколов (право/геология)
    {"title": "Правовое регулирование государственного геологического контроля (надзора) в контексте реформы контрольной (надзорной) деятельности", "authors": "Соколов А. Д.", "year": 2022, "journal": "Проблемы экономики и юридической практики", "pages": "53-58"},
    {"title": "Специальные режимы государственного контроля (надзора) как способ обеспечения рационального использования и охраны недр", "authors": "Соколов А. Д.", "year": 2023, "journal": "Хозяйство и право", "pages": "59-74"},
    {"title": "Роль реестровой модели в обеспечении безопасного, рационального использования и охраны недр: правовой аспект", "authors": "Соколов А. Д.", "year": 2023, "journal": "Пробелы в российском законодательстве", "pages": "71-75"},
    {"title": "Понятие и принципы государственного геологического контроля (надзора) как способ обеспечения безопасного, рационального использования и охраны недр", "authors": "Соколов А. Д.", "year": 2024, "journal": "Пробелы в российском законодательстве", "pages": "37-44"},
    {"title": "Теоретические и практические вопросы правового обеспечения государственного геологического контроля (надзора)", "authors": "Соколов А. Д.", "year": 2024, "journal": "Проблемы экономики и юридической практики", "pages": "34-40"},
    {"title": "Правовые проблемы учета контрольными (надзорными) органами предпрятых недропользователем мер по своевременному предотвращению и ликвидации вреда окружающей среде", "authors": "Соколов А. Д.", "year": 2022, "journal": "Нефть и газ - 2022", "pages": "460-469"},
    {"title": "Проблемы профилактики правонарушений как средства повышения эффективности контрольной (надзорной) деятельности в сфере ТЭК", "authors": "Соколов А. Д.", "year": 2022, "journal": "Правовое регулирование деятельности ТЭК", "pages": "160-165"},
    {"title": "Правовые проблемы профилактики правонарушений законодательства о недрах", "authors": "Соколов А. Д.", "year": 2023, "journal": "Минеральные ресурсы России. Экономика и управление", "pages": "79-84"},
    {"title": "Проблемы и особенности законодательства о государственном геологическом контроле (надзоре)", "authors": "Соколов А. Д.", "year": 2023, "journal": "Государство и право России в современном мире", "pages": "289-293"},
    {"title": "Государственный геологический контроль (надзор) как часть государственного экологического контроля (надзора): соотношение и особенности", "authors": "Соколов А. Д.", "year": 2024, "journal": "Традиции и новации в системе современного российского права", "pages": "320-322"},
]

def main():
    print(f"Расширенное тестирование: {len(PUBLICATIONS)} публикаций\n")
    print("=" * 80)

    all_results = []
    for i, pub in enumerate(PUBLICATIONS, 1):
        print(f"\n[{i}/{len(PUBLICATIONS)}] {pub['title'][:60]}...")
        print(f"    Авторы: {pub['authors']}, Год: {pub['year']}, Журнал: {pub['journal'][:40]}")

        print(f"    -> Поиск источника...", end=" ", flush=True)
        source = email_search._find_source_for_publication(pub, autoref_text=None)
        if source:
            src_name = source.get("source_name", "?")
            src_type = source.get("source_type", "?")
            src_url = source.get("url", "")[:80]
            print(f"НАЙДЕН ({src_name}, {src_type})")
            print(f"       URL: {src_url}...")
        else:
            print("НЕ НАЙДЕН")

        print(f"    -> Поиск email...", end=" ", flush=True)
        email_result = email_search.find_email_for_publication(pub, autoref_text=None)
        emails = email_result.get("emails", [])
        if emails:
            print(f"НАЙДЕНЫ: {', '.join(emails)}")
        else:
            print("НЕ НАЙДЕНЫ")

        all_results.append({
            "index": i,
            "title": pub["title"],
            "authors": pub["authors"],
            "year": pub["year"],
            "journal": pub["journal"],
            "source": source,
            "emails": emails,
        })

        time.sleep(2)

    print("\n" + "=" * 80)
    print("ИТОГОВАЯ СТАТИСТИКА")
    print("=" * 80)

    sources_found = sum(1 for r in all_results if r["source"] is not None)
    emails_found = sum(1 for r in all_results if r["emails"])
    unique_emails = set()
    for r in all_results:
        unique_emails.update(r["emails"])

    print(f"Публикаций обработано: {len(all_results)}")
    print(f"Источников найдено: {sources_found}/{len(all_results)} ({sources_found/len(all_results)*100:.0f}%)")
    print(f"Email найдено: {emails_found}/{len(all_results)} ({emails_found/len(all_results)*100:.0f}%)")
    print(f"Уникальных email: {len(unique_emails)}")
    if unique_emails:
        for e in sorted(unique_emails):
            print(f"  - {e}")

    print("\nИСТОЧНИКИ:")
    source_types = {}
    for r in all_results:
        if r["source"]:
            sn = r["source"].get("source_name", "unknown")
            st = r["source"].get("source_type", "unknown")
            key = f"{sn} ({st})"
            source_types[key] = source_types.get(key, 0) + 1
    for k, v in sorted(source_types.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    # Детализация по авторам
    print("\nПО АВТОРАМ:")
    author_stats = {}
    for r in all_results:
        auth = r["authors"].split(",")[0].strip()
        if auth not in author_stats:
            author_stats[auth] = {"sources": 0, "emails": 0}
        if r["source"]:
            author_stats[auth]["sources"] += 1
        if r["emails"]:
            author_stats[auth]["emails"] += 1
    for auth, stats in sorted(author_stats.items()):
        print(f"  {auth}: источники {stats['sources']}, email {stats['emails']}")

    output_path = os.path.join(DEV_DIR, "test_results_extended.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\nРезультаты: {output_path}")

    return all_results

if __name__ == "__main__":
    main()
