"""Тестирование email_search на реальных публикациях."""

import sys
import os
import json
import time
import types

# Настройка путей — корень проекта
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# Мокаем config до импорта email_search
import config as real_config
mock_config = types.ModuleType("config")
mock_config.LLM_API_URL = real_config.LLM_API_URL
mock_config.LLM_MODEL = real_config.LLM_MODEL
mock_config.LLM_TIMEOUT = real_config.LLM_TIMEOUT
mock_config.LLM_MAX_TOKENS = real_config.LLM_MAX_TOKENS
mock_config.LLM_TEMPERATURE = real_config.LLM_TEMPERATURE
sys.modules["config"] = mock_config

# Импортируем email_search
import importlib.util
DEV_DIR = os.path.join(PROJECT_DIR, "email_search_dev")
spec = importlib.util.spec_from_file_location("email_search", os.path.join(DEV_DIR, "email_search.py"))
email_search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(email_search)

# Данные публикаций
PUBLICATIONS = [
    {"title": "Фотограмметрический метод съемки сыпучих материалов на складах с размещением IP камер на грузоподъемных механизмах", "authors": "Токин А. А.", "year": 2024, "journal": "Маркшейдерский вестник", "pages": "31-38"},
    {"title": "Методика автоматизированной съемки и подсчета объемов сыпучих материалов на складах", "authors": "Токин А. А., Шоломицкий А. А.", "year": 2025, "journal": "Вестник СГУГиТ", "pages": "31-40"},
    {"title": "Методика фильтрации облака точек методом скользящего конуса", "authors": "Токин А. А., Шоломицкий А. А., Щербаков В. В.", "year": 2025, "journal": "Вестник СГУГиТ", "pages": "15-23"},
    {"title": "Автоматизированный метод съемки сыпучих материалов на складах", "authors": "Токин А. А.", "year": 2024, "journal": "Международная научно-техническая конференция «Перспективы применения цифровых технологий в рациональном и безопасном недропользовании»", "pages": "149-153"},
    {"title": "Методика автоматизации подсчета объемов на складах сыпучих материалов", "authors": "Токин А. А.", "year": 2025, "journal": "Фундаментальные и прикладные вопросы горных наук", "pages": "86-94"},
    {"title": "Изменение технологических свойств и прочностных характеристик высокоподвижного бетона введением комплексного модификатора", "authors": "Ильина Л.В., Бердов Г.И., Вишняков Н.С., Цекарь Д.А.", "year": 2024, "journal": "Строительные материалы", "pages": "15-20"},
    {"title": "Повышение физико-механических свойств высокоподвижного тяжелого бетона", "authors": "Ильина Л. В., Чулкова И. Л., Вишняков Н. С.", "year": 2024, "journal": "Эксперт: теория и практика", "pages": "18-23"},
    {"title": "Изменение кинетики твердения и упрочнение цементных систем дисперсными минеральными добавками в начальные сроки набора прочности", "authors": "Ильина Л.В., Вишняков Н.С., Бартеньева Е.А., Молодин В.В.", "year": 2025, "journal": "Известия вузов. Строительство", "pages": "143-157"},
    {"title": "Смешанное вяжущее в технологии неавтоклавного пенобетона", "authors": "Ильина Л. В., Бартеньева Е.А., Вишняков Н. С.", "year": 2025, "journal": "Цемент и его применение", "pages": "40-43"},
    {"title": "Самоуплотняющиеся бетоны с комплексной органоминеральной добавкой", "authors": "Ильина Л.В., Вишняков Н.С.", "year": 2026, "journal": "Известия вузов. Строительство", "pages": "59-69"},
    {"title": "Влияние модифицирующих добавок на увеличение прочности высокоподвижного бетона", "authors": "Ильина Л. В., Вишняков Н. С., Цекарь Д. А.", "year": 2023, "journal": "Международная научная и научно-техническая конференция на тему «Инновации в строительстве, сейсмическая безопасность зданий и сооружений»", "pages": "380-384"},
    {"title": "Влияние добавок крентов на процесс схватывания и твердения цементных систем", "authors": "Ильина Л. В., Вишняков Н. С., Цекарь Д. А.", "year": 2023, "journal": "Международная научная и научно-техническая конференция на тему «Инновации в строительстве, сейсмическая безопасность зданий и сооружений»", "pages": "363-367"},
    {"title": "Изменение процессов схватывания и твердения цементных систем введением тонкодисперсного гидратированного вяжущего", "authors": "Ильина Л.В., Вишняков Н.С., Цекарь Д.А.", "year": 2024, "journal": "Материалы VII международной научно-практической конференции «Качество. Технологии. Инновации»", "pages": "78-83"},
    {"title": "Влияние количества суперпластифицирующих добавок на увеличение прочности высокоподвижного бетона", "authors": "Ильина Л.В., Вишняков Н.С.", "year": 2024, "journal": "Материалы VII международной научно-практической конференции «Качество. Технологии. Инновации»", "pages": "117-123"},
    {"title": "Изменение технологических свойств и прочностных характеристик высокоподвижного бетона введением суперпластифицирующей добавки", "authors": "Вишняков Н.С., Ильина Л. В.", "year": 2025, "journal": "Инженерное дело на Дальнем Востоке России : Материалы X Всероссийской научно-практической конференции", "pages": "154-162"},
    {"title": "Математическое моделирование составов и технологических свойств высокоподвижных бетонных смесей", "authors": "Ильина Л.В., Вишняков Н.С.", "year": 2025, "journal": "Материалы VIII международной научно-практической конференции «Качество. Технологии. Инновации»", "pages": "56-62"},
    {"title": "Влияние упрочняющей добавки на прочность цементных бетонов", "authors": "Ильина Л. В., Вишняков Н. С., Шихалев А. Е.", "year": 2025, "journal": "International Conference on Materials Physics, Building Structures and Technologies in Construction (MPCPE-2025)", "pages": "104-111"},
    {"title": "Повышение прочности цементных систем", "authors": "Ильина Л. В., Вишняков Н. С., Шихалев А. Е.", "year": 2025, "journal": "Устойчивое развитие региона: архитектура, строительство, транспорт", "pages": "341-345"},
    {"title": "Роль пластифицирующих добавок в составе современных бетонов", "authors": "Вишняков Н. С.", "year": 2025, "journal": "Труды Новосибирского государственного архитектурно-строительного университета (Сибстрин)", "pages": "139-149"},
    {"title": "Самоуплотняющийся бетон с повышенной прочностью", "authors": "Ильина Л. В., Вишняков Н. С.", "year": 2025, "journal": "Актуальные вопросы современного строительства промышленных регионов России", "pages": "155-158"},
]

def main():
    print(f"Начинаем тестирование: {len(PUBLICATIONS)} публикаций\n")
    print("=" * 80)

    all_results = []
    for i, pub in enumerate(PUBLICATIONS, 1):
        print(f"\n[{i}/{len(PUBLICATIONS)}] {pub['title'][:60]}...")
        print(f"    Авторы: {pub['authors']}, Год: {pub['year']}, Журнал: {pub['journal'][:40]}")

        # Шаг 1: Поиск источника
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

        # Шаг 2: Поиск email
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

        time.sleep(2)  # Пауза между запросами (DDG требует больше времени)

    # Итоговая статистика
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
    print(f"Email найдено в публикациях: {emails_found}/{len(all_results)} ({emails_found/len(all_results)*100:.0f}%)")
    print(f"Уникальных email: {len(unique_emails)}")
    if unique_emails:
        for e in sorted(unique_emails):
            print(f"  - {e}")

    # Таблица по типам источников
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

    # Сохраняем результат
    output_path = os.path.join(DEV_DIR, "test_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\nРезультаты сохранены в: {output_path}")

    return all_results


if __name__ == "__main__":
    main()
