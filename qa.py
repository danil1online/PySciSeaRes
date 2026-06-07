import sqlite3
import os
import json
import re
import requests
import urllib3

from config import LLM_API_URL, LLM_MODEL, HEADERS, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "vak.db")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# System prompt for SQL generation
SQL_SYSTEM_PROMPT = (
    "Вы — эксперт по SQL-запросам к базе данных диссертаций ВАК. "
    "Ваша задача — преобразовать вопрос пользователя на русском языке в SQL-запрос.\n\n"

    "СТРУКТУРА БАЗЫ ДАННЫХ:\n"
    "Таблица adverts — объявления о защите диссертаций:\n"
    "  - id (TEXT, PRIMARY KEY) — уникальный ID\n"
    "  - fio (TEXT) — ФИО соискателя\n"
    "  - date_defend (TEXT) — дата защиты в формате YYYY-MM-DD\n"
    "  - dissertation_name (TEXT) — название диссертации\n"
    "  - specialty_cipher (TEXT) — шифр специальности (например, 2.2.11)\n"
    "  - specialty_text (TEXT) — название специальности\n"
    "  - supervisor_name (TEXT) — ФИО научного руководителя\n"
    "  - supervisor_work (TEXT) — место работы руководителя\n"
    "  - council_cipher (TEXT) — код диссертационного совета\n"
    "  - defend_org (TEXT) — организация защиты\n"
    "  - org_address (TEXT) — адрес совета\n"
    "  - org_phone (TEXT) — телефон совета\n"
    "  - autoref_url (TEXT) — ссылка на автореферат\n"
    "  - autoref_path (TEXT) — локальный путь к PDF\n"
    "  - downloaded (INTEGER) — скачан ли автореферат (0/1)\n"
    "\n"
    "Таблица publications — публикации автора:\n"
    "  - id (INTEGER, PRIMARY KEY)\n"
    "  - advert_id (TEXT, FOREIGN KEY -> adverts.id)\n"
    "  - pub_number (INTEGER) — порядковый номер публикации\n"
    "  - authors (TEXT) — авторы\n"
    "  - title (TEXT) — название публикации\n"
    "  - journal (TEXT) — журнал/источник\n"
    "  - year (INTEGER) — год\n"
    "  - pages (TEXT) — страницы\n"
    "\n"
    "ВАЖНЫЕ ПРАВИЛА:\n"
    "1. Для подсчёта публикаций на диссертацию используйте: COUNT(p.id) FROM publications p LEFT JOIN adverts a ON p.advert_id = a.id\n"
    "2. Для группировки по специальности используйте: GROUP BY specialty_cipher\n"
    "3. Для поиска по тексту используйте: LIKE '%текст%'\n"
    "4. Для сортировки по убыванию используйте: ORDER BY field DESC\n"
    "5. Для фильтрации по году в дате защиты: WHERE date_defend >= 'YYYY-01-01' AND date_defend <= 'YYYY-12-31'\n"
    "6. LIMIT не более 100 результатов\n"
    "7. Для JOIN между adverts и publications используйте: SELECT a.*, COUNT(p.id) FROM adverts a LEFT JOIN publications p ON p.advert_id = a.id\n"
    "8. Для получения уникальных названий публикаций по специальности: SELECT title FROM publications p JOIN adverts a ON p.advert_id = a.id WHERE specialty_cipher = 'X' GROUP BY title\n"
    "9. Для анализа направлений — используйте SELECT title, journal, year FROM publications p JOIN adverts a ON p.advert_id = a.id WHERE specialty_cipher = 'X' ORDER BY year DESC\n"
    "10. Для подсчёта диссертаций без публикаций: SELECT COUNT(DISTINCT a.id) FROM adverts a WHERE NOT EXISTS (SELECT 1 FROM publications p WHERE p.advert_id = a.id)\n"
    "11. Для получения топ-N: ORDER BY ... DESC LIMIT N\n"
    "12. Для вывода списка соискателей по специальности: SELECT a.fio, a.date_defend, a.dissertation_name FROM adverts a WHERE a.specialty_cipher = 'X'\n"
    "13. Для подсчёта диссертаций по специальности: SELECT COUNT(*) as disser_count FROM adverts WHERE specialty_cipher = 'X'\n"
    "14. Для поиска автора с макс. публикациями по специальности: SELECT a.fio, COUNT(p.id) as pub_count FROM adverts a LEFT JOIN publications p ON p.advert_id = a.id WHERE a.specialty_cipher = 'X.' GROUP BY a.id ORDER BY pub_count DESC LIMIT N\n"
    "15. Для анализа направлений публикаций: SELECT title, journal, year FROM publications p JOIN adverts a ON p.advert_id = a.id WHERE a.specialty_cipher = 'X.' GROUP BY title ORDER BY year DESC\n"
    "16. Шифры специальностей в БД заканчиваются на точку (например '2.2.11.', '5.1.1.')\n"
    "17. Запрос — ТОЛЬКО SQL-код"
)


def get_db():
    return sqlite3.connect(DB_PATH)


def get_schema_info():
    """Получить структуру БД и примеры данных для контекста LLM."""
    conn = get_db()
    c = conn.cursor()

    info = {}

    # Table schemas
    c.execute("SELECT sql FROM sqlite_master WHERE type='table'")
    tables_schema = {}
    for row in c.fetchall():
        if row[0]:
            table_name = re.search(r'CREATE TABLE (\w+)', row[0])
            if table_name:
                tables_schema[table_name.group(1)] = row[0]

    # Count rows
    for table_name in tables_schema:
        try:
            c.execute(f"SELECT COUNT(*) FROM {table_name}")
            count = c.fetchone()[0]
            info[table_name] = {"count": count}
        except:
            pass

    # Sample data for adverts
    try:
        c.execute("SELECT id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text, supervisor_name, COUNT(p.id) FROM adverts a LEFT JOIN publications p ON p.advert_id = a.id GROUP BY a.id LIMIT 5")
        info["adverts_sample"] = c.fetchall()
    except:
        info["adverts_sample"] = []

    # Sample data for publications
    try:
        c.execute("SELECT advert_id, pub_number, authors, title, journal, year, pages FROM publications LIMIT 10")
        info["publications_sample"] = c.fetchall()
    except:
        info["publications_sample"] = []

    # Distinct specialties (for dropdown context)
    try:
        c.execute("SELECT DISTINCT specialty_cipher, specialty_text FROM adverts WHERE specialty_cipher IS NOT NULL AND specialty_cipher != '' ORDER BY specialty_cipher LIMIT 20")
        info["specialties"] = c.fetchall()
    except:
        info["specialties"] = []

    # Distinct years in publications
    try:
        c.execute("SELECT DISTINCT year FROM publications WHERE year IS NOT NULL ORDER BY year DESC LIMIT 10")
        info["publication_years"] = [r[0] for r in c.fetchall()]
    except:
        info["publication_years"] = []

    conn.close()
    return info


def build_context_prompt(schema_info):
    """Построить текстовый контекст для LLM."""
    ctx = SCHEMA_INFO_PROMPT.format(
        num_adverts=schema_info.get("adverts", {}).get("count", 0),
        num_pubs=schema_info.get("publications", {}).get("count", 0),
    )

    # Add specialties context
    specialties = schema_info.get("specialties", [])
    if specialties:
        spec_list = "\n".join([f"  {s[0]} — {s[1]}" for s in specialties[:15]])
        ctx += f"\nПримеры специальностей в базе:\n{spec_list}\n"
        if len(specialties) > 15:
            ctx += f"  ... и ещё {len(specialties) - 15} специальностей\n"

    # Add sample adverts
    sample_adverts = schema_info.get("adverts_sample", [])
    if sample_adverts:
        ctx += "\nПримеры записей adverts (id, fio, date_defend, dissertation_name, specialty_cipher, specialty_text, supervisor_name, pub_count):\n"
        for row in sample_adverts:
            ctx += f"  {row}\n"

    # Add sample publications
    sample_pubs = schema_info.get("publications_sample", [])
    if sample_pubs:
        ctx += "\nПримеры записей publications (advert_id, pub_number, authors, title, journal, year, pages):\n"
        for row in sample_pubs[:5]:
            ctx += f"  {row}\n"

    # Add years context
    years = schema_info.get("publication_years", [])
    if years:
        ctx += f"\nГоды публикаций в базе: {', '.join(str(y) for y in years)}\n"

    return ctx


SCHEMA_INFO_PROMPT = (
    "База данных содержит {num_adverts} объявлений о защите и {num_pubs} публикаций.\n"
    "Таблицы: adverts, publications, users.\n"
)


def generate_sql(question, context):
    """Сгенерировать SQL-запрос из вопроса пользователя через LLM."""
    system_msg = {
        "role": "system",
        "content": SQL_SYSTEM_PROMPT,
    }

    user_msg = {
        "role": "user",
        "content": (
            "Контекст базы данных:\n"
            f"{context}\n\n"
            f"Вопрос пользователя: {question}\n\n"
            "Верни ТОЛЬКО SQL-запрос в блоке ```sql...\n"
            "Никаких объяснений, только SQL.\n\n"
            "Пример формата ответа:\n"
            "```sql\nSELECT specialty_cipher, COUNT(*) as pub_count FROM adverts a LEFT JOIN publications p ON p.advert_id = a.id GROUP BY specialty_cipher ORDER BY pub_count DESC LIMIT 5;\n```"
        ),
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                LLM_API_URL,
                headers=HEADERS,
                json={
                    "model": LLM_MODEL,
                    "messages": [system_msg, user_msg],
                    "max_tokens": LLM_MAX_TOKENS,
                    "temperature": 0,
                },
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            if attempt < 2:
                continue
            raise
        except Exception:
            if attempt < 2:
                continue
            raise

    return None


def parse_sql_response(response):
    """Извлечь SQL-запрос из ответа LLM."""
    sql_match = re.search(r'```sql\s*\n(.*?)\n```', response, re.DOTALL)
    if sql_match:
        return sql_match.group(1).strip().rstrip(';').rstrip()
    # Fallback: try to find any SQL-like SELECT
    sql_match2 = re.search(r'(SELECT\s.+?;?)', response, re.DOTALL | re.IGNORECASE)
    if sql_match2:
        return sql_match2.group(1).strip().rstrip(';').rstrip()
    return response.strip()


def execute_sql(sql):
    """Выполнить SQL-запрос и вернуть результаты."""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(sql)
        columns = [desc[0] for desc in c.description] if c.description else []
        rows = c.fetchall()
        return {
            "columns": columns,
            "rows": [list(row) for row in rows],
            "count": len(rows),
            "error": None,
        }
    except Exception as e:
        return {
            "columns": [],
            "rows": [],
            "count": 0,
            "error": str(e),
        }
    finally:
        conn.close()


def format_result(data):
    """Форматировать результаты SQL-запроса в читаемый вид."""
    if data.get("error"):
        return f"Ошибка выполнения запроса: {data['error']}\n\nЗапрос был: {data.get('_sql', '')}"

    columns = data.get("columns", [])
    rows = data.get("rows", [])
    count = data.get("count", 0)

    if not columns:
        return "Запрос выполнен, но не возвращает данные. Попробуйте другой вопрос."

    # If single column with small result set — show as list
    if len(columns) == 1 and count <= 20:
        header = columns[0]
        result = [f"  {r[0]}" for r in rows[:50]]
        if count > 50:
            result.append(f"  ... и ещё {count - 50} записей")
        return f"{header}:\n" + "\n".join(result)

    # Multi-column: format as table
    col_widths = [len(c) for c in columns]
    for row in rows:
        for i, val in enumerate(row):
            if val is not None:
                col_widths[i] = max(col_widths[i], min(len(str(val)), 60))

    header = " | ".join(c.ljust(min(col_widths[i], 40)) for i, c in enumerate(columns))
    sep = "-+-".join("-" * min(col_widths[i], 40) for i in range(len(columns)))

    lines = [header, sep]
    for row in rows[:50]:
        line = " | ".join(
            str(val if val is not None else "NULL").ljust(min(col_widths[i], 40))
            for i, val in enumerate(row)
        )
        lines.append(line)

    if count > 50:
        lines.append(f"... и ещё {count - 50} записей (всего {count})")
    else:
        lines.append(f"Всего: {count} записей")

    return "\n".join(lines)


def build_answer(question, result):
    """Сформировать текстовый ответ на основе результатов SQL-запроса."""
    if result.get("error"):
        return None

    columns = result.get("columns", [])
    rows = result.get("rows", [])
    count = result.get("count", 0)

    if not columns:
        return "Запрос выполнен, но не возвращает данных. Попробуйте переформулировать вопрос."

    # Single value (COUNT, etc.)
    if len(columns) == 1 and count <= 1:
        val = rows[0][0] if rows else "нет данных"
        col_name = columns[0].lower()
        if "count" in col_name or "total" in col_name or "num" in col_name or "defense" in col_name:
            return f"В базе {val} записей." if val else "В базе нет записей."
        return f"Результат: {val}"

    # Single column, multiple rows (list)
    if len(columns) == 1:
        header = columns[0].capitalize()
        top5 = rows[:5]
        result_lines = [f"  {r[0]}" for r in top5]
        if count > 5:
            result_lines.append(f"  ... и ещё {count - 5} записей")
        return f"{header} (всего {count}):\n" + "\n".join(result_lines)

    # Multi-column results
    answer_parts = []

    # Try to detect common patterns
    col_str = " ".join(columns).lower()

    if "pub_count" in col_str or "count" in col_str:
        # Ranking / counting question
        if len(columns) == 2:
            col2_name = columns[1].replace("_", " ").title()
            filtered_rows = [r for r in rows if r[0] is not None and str(r[0]).strip()]
            filtered_count = len(filtered_rows)
            answer_parts.append(f"Топ-{min(filtered_count, 10)} по {col2_name.lower()} (всего {count}):")
            for row in filtered_rows[:10]:
                answer_parts.append(f"  {row[0]}: {row[1]}")
            if filtered_count > 10:
                answer_parts.append(f"  ... и ещё {filtered_count - 10}")
            return "\n".join(answer_parts)

    # General multi-column
    top5 = rows[:5]
    answer_parts.append(f"Найден {count} результат(ов):\n")
    for row in top5:
        vals = []
        for i, val in enumerate(row):
            if val is not None and str(val).strip():
                vals.append(f"{columns[i]}: {val}")
            elif i == 0 and val is None:
                vals.append(f"{columns[i]}: (не указано)")
        if vals:
            answer_parts.append("  " + "  |  ".join(vals))
    if count > 5:
        answer_parts.append(f"  ... и ещё {count - 5}")

    return "\n".join(answer_parts)


def process_question(question):
    """Основной поток обработки вопроса: контекст -> SQL -> ответ."""
    schema_info = get_schema_info()
    context = build_context_prompt(schema_info)

    # Generate SQL via LLM
    llm_response = generate_sql(question, context)
    if not llm_response:
        return {
            "question": question,
            "error": "Не удалось получить ответ от LLM. Проверьте соединение с LLM-сервером.",
            "sql": None,
            "answer": None,
            "result": None,
        }

    sql = parse_sql_response(llm_response)

    if not sql:
        return {
            "question": question,
            "error": None,
            "sql": None,
            "answer": "Не удалось извлечь SQL-запрос из ответа LLM.",
            "result": None,
        }

    # Execute SQL
    result = execute_sql(sql)

    if result.get("error"):
        return {
            "question": question,
            "error": f"Ошибка SQL: {result['error']}",
            "sql": sql,
            "answer": None,
            "result": result,
        }

    answer = build_answer(question, result)

    return {
        "question": question,
        "error": None,
        "sql": sql,
        "answer": answer,
        "result": {**result, "_sql": sql},
    }
