"""Поиск по БД, список специальностей, кластеры."""
import os
import re
import sqlite3

from config import DB_PATH, CLUSTER_CSV_PATH, SCI_SPEC_FILE
from logging_config import get_logger

logger = get_logger("search")


def get_db():
    return sqlite3.connect(DB_PATH)


def search_adverts(specialties=None, date_from=None, date_to=None, query=None,
                   page=1, per_page=10, cluster_id=None, city=None):
    """Поиск диссертаций с фильтрацией по кластерам."""
    cluster_spec_list = None
    if cluster_id is not None:
        cluster_spec_list = load_cluster_specialties(cluster_id)

    def normalize_cipher(c):
        c = c.strip()
        if not c.endswith('.'):
            c = c + '.'
        return c

    effective_specialties = None
    if cluster_spec_list:
        cluster_set = set(normalize_cipher(s) for s in cluster_spec_list)
        if specialties:
            filtered = [normalize_cipher(s) for s in specialties if normalize_cipher(s) in cluster_set]
            if filtered:
                effective_specialties = filtered
            else:
                effective_specialties = list(cluster_set)
        else:
            effective_specialties = list(cluster_set)
    elif specialties:
        effective_specialties = [normalize_cipher(s) for s in specialties]

    sql = """
        SELECT a.id, a.fio, a.date_defend, a.dissertation_name,
               a.specialty_cipher, a.specialty_text,
               a.supervisor_name, a.supervisor_work,
               a.council_cipher, a.defend_org,
               a.autoref_url, a.autoref_path,
               a.city, a.organization_name,
               COUNT(p.id) as pub_count
        FROM adverts a
        LEFT JOIN publications p ON p.advert_id = a.id
        WHERE 1=1
    """
    params = []

    if effective_specialties:
        spec_list = [s.strip() for s in effective_specialties if s.strip()]
        if spec_list:
            placeholders = ",".join(["?"] * len(spec_list))
            sql += f" AND a.specialty_cipher IN ({placeholders})"
            params.extend(spec_list)

    if date_from:
        sql += " AND a.date_defend >= ?"
        params.append(date_from)

    if date_to:
        sql += " AND a.date_defend <= ?"
        params.append(date_to)

    if query:
        sql += " AND (a.fio LIKE ? OR a.dissertation_name LIKE ? OR a.specialty_text LIKE ?)"
        like_query = f"%{query}%"
        params.extend([like_query, like_query, like_query])

    if city:
        sql += " AND (a.city LIKE ? OR a.organization_name LIKE ?)"
        like_city = f"%{city}%"
        params.extend([like_city, like_city])

    sql += " GROUP BY a.id"

    # Count total
    count_sql2 = """
        SELECT COUNT(DISTINCT a.id) FROM adverts a
        LEFT JOIN publications p ON p.advert_id = a.id
        WHERE 1=1
    """
    count_params = []

    if effective_specialties:
        spec_list = [s.strip() for s in effective_specialties if s.strip()]
        if spec_list:
            placeholders = ",".join(["?"] * len(spec_list))
            count_sql2 += f" AND a.specialty_cipher IN ({placeholders})"
            count_params.extend(spec_list)

    if date_from:
        count_sql2 += " AND a.date_defend >= ?"
        count_params.append(date_from)

    if date_to:
        count_sql2 += " AND a.date_defend <= ?"
        count_params.append(date_to)

    if query:
        count_sql2 += " AND (a.fio LIKE ? OR a.dissertation_name LIKE ? OR a.specialty_text LIKE ?)"
        like_query = f"%{query}%"
        count_params.extend([like_query, like_query, like_query])

    if city:
        count_sql2 += " AND (a.city LIKE ? OR a.organization_name LIKE ?)"
        like_city = f"%{city}%"
        count_params.extend([like_city, like_city])

    sql += " ORDER BY a.date_defend DESC"
    offset = (page - 1) * per_page
    sql += f" LIMIT {per_page} OFFSET {offset}"

    logger.debug(f"Search query: {sql[:200]}... params={len(params)}")

    conn = get_db()
    c = conn.cursor()

    total = c.execute(count_sql2, count_params).fetchone()[0]
    rows = c.execute(sql, params).fetchall()

    adverts = []
    for row in rows:
        adverts.append({
            "id": row[0],
            "fio": row[1],
            "date_defend": row[2],
            "dissertation_name": row[3],
            "specialty_cipher": row[4],
            "specialty_text": row[5],
            "supervisor_name": row[6],
            "supervisor_work": row[7],
            "council_cipher": row[8],
            "defend_org": row[9],
            "autoref_url": row[10],
            "autoref_path": row[11],
            "city": row[12],
            "organization_name": row[13],
            "pub_count": row[14],
        })

    total_pages = (total + per_page - 1) // per_page if total > 0 else 0
    conn.close()

    logger.debug(f"Found {total} adverts, returning page {page}")
    return {
        "adverts": adverts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def get_advert_detail(advert_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("""SELECT id, fio, date_defend, dissertation_name,
                        specialty_cipher, specialty_text,
                        supervisor_name, supervisor_work,
                        council_cipher, defend_org,
                        autoref_url, autoref_path,
                        city, organization_name
                 FROM adverts WHERE id = ?""", (advert_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return None

    advert = {
        "id": row[0],
        "fio": row[1],
        "date_defend": row[2],
        "dissertation_name": row[3],
        "specialty_cipher": row[4],
        "specialty_text": row[5],
        "supervisor_name": row[6],
        "supervisor_work": row[7],
        "council_cipher": row[8],
        "defend_org": row[9],
        "autoref_url": row[10],
        "autoref_path": row[11],
        "city": row[12],
        "organization_name": row[13],
    }

    c.execute("SELECT pub_number, authors, title, journal, year, pages, email, source_url, source_name FROM publications WHERE advert_id = ? ORDER BY pub_number", (advert_id,))
    advert["publications"] = [
        {
            "pub_number": r[0],
            "authors": r[1],
            "title": r[2],
            "journal": r[3],
            "year": r[4],
            "pages": r[5],
            "email": r[6] if len(r) > 6 else "",
            "source_url": r[7] if len(r) > 7 else "",
            "source_name": r[8] if len(r) > 8 else "",
        }
        for r in c.fetchall()
    ]

    conn.close()
    return advert


def needs_email_search(advert_id):
    """Проверяет, есть ли публикации без email info."""
    conn = get_db()
    c = conn.cursor()

    c.execute("""SELECT COUNT(*) FROM publications
                 WHERE advert_id = ? AND (email IS NULL OR email = ''
                 OR source_url IS NULL OR source_url = '')""", (advert_id,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_email_search_attempts(advert_id):
    """Получает текущий счётчик попыток поиска email для объявления."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT email_search_attempts FROM adverts WHERE id = ?", (advert_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None:
        return row[0]
    return 0


def increment_email_search_attempts(advert_id):
    """Увеличивает счётчик попыток поиска email."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""UPDATE adverts SET email_search_attempts = COALESCE(email_search_attempts, 0) + 1
                 WHERE id = ?""", (advert_id,))
    conn.commit()
    conn.close()


def get_pub_extract_attempts(advert_id):
    """Получает текущий счётчик попыток извлечения публикаций для объявления."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT pub_extract_attempts FROM adverts WHERE id = ?", (advert_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None:
        return row[0]
    return 0


def increment_pub_extract_attempts(advert_id):
    """Увеличивает счётчик попыток извлечения публикаций."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""UPDATE adverts SET pub_extract_attempts = COALESCE(pub_extract_attempts, 0) + 1
                 WHERE id = ?""", (advert_id,))
    conn.commit()
    conn.close()


def is_new_defense(fio, date_defend):
    """Проверяет, есть ли в БД защита с таким же ФИО и датой защиты."""
    conn = get_db()
    c = conn.cursor()
    if not fio or not date_defend:
        conn.close()
        return True
    c.execute("SELECT COUNT(*) FROM adverts WHERE fio = ? AND date_defend = ?", (fio, date_defend))
    count = c.fetchone()[0]
    conn.close()
    return count == 0


SPECIALTY_GROUPS = {
    "1.1": "Математика и механика",
    "1.2": "Компьютерные науки и информатика",
    "1.3": "Физические науки",
    "1.4": "Химические науки",
    "1.5": "Биологические науки",
    "1.6": "Науки о Земле и окружающей среде",
    "2.1": "Строительство и архитектура",
    "2.2": "Электроника, фотоника, приборостроение и связь",
    "2.3": "Информационные технологии и телекоммуникации",
    "2.4": "Энергетика и электротехника",
    "2.5": "Машиностроение",
    "2.6": "Химические технологии, науки о материалах, металлургия",
    "2.7": "Биотехнологии",
    "2.8": "Недропользование и горные науки",
    "2.9": "Транспортные системы",
    "2.10": "Техносферная безопасность",
    "3.1": "Клиническая медицина",
    "3.2": "Профилактическая медицина",
    "3.3": "Медико-биологические науки",
    "3.4": "Фармацевтические науки",
    "4.1": "Агрономия, лесное и водное хозяйство",
    "4.2": "Зоотехния и ветеринария",
    "4.3": "Агроинженерия и пищевые технологии",
    "5.1": "Право",
    "5.2": "Экономика",
    "5.3": "Психология",
    "5.4": "Социология",
    "5.5": "Политология",
    "5.6": "Исторические науки",
    "5.7": "Философия",
    "5.8": "Педагогика",
    "5.9": "Филология",
    "5.10": "Искусствоведение и культурология",
    "5.11": "Теология",
    "5.12": "Когнитивные науки",
}


def get_group_for_cipher(cipher):
    parts = cipher.strip().split(".")
    if len(parts) >= 2:
        return parts[0] + "." + parts[1]
    return None


def get_all_specialties():
    """Загружает список всех специальностей из sci_spec.txt."""
    specs_by_group = {}
    for group_key in SPECIALTY_GROUPS:
        specs_by_group[group_key] = []
    if os.path.exists(SCI_SPEC_FILE):
        with open(SCI_SPEC_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(\S+?)\s*[-—–]\s*(.*)', line)
                if m:
                    cipher = m.group(1).strip()
                    name = m.group(2).strip()
                else:
                    cipher = line
                    name = ""
                group = get_group_for_cipher(cipher)
                if group and group in specs_by_group:
                    specs_by_group[group].append({"cipher": cipher, "name": name})
    groups = []
    for group_key in sorted(specs_by_group.keys()):
        group_specs = specs_by_group[group_key]
        if group_specs:
            groups.append({
                "key": group_key,
                "name": SPECIALTY_GROUPS[group_key],
                "specialties": group_specs,
            })
    return groups


# ======================== CLUSTER SUPPORT ========================

def load_cluster_specialties(cluster_id=None):
    """Загружает специальности, относящиеся к кластеру."""
    if not os.path.exists(CLUSTER_CSV_PATH):
        return {} if cluster_id is None else []

    all_clusters = {}
    with open(CLUSTER_CSV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(", ", 2)
            if len(parts) < 2:
                continue
            cid = int(parts[0])
            rest = parts[2] if len(parts) > 2 else ""

            specs = re.findall(r'(\d+\.\d+\.\d+)\.\s+-\s+', rest)
            all_clusters[cid] = {
                "dept_name": parts[1],
                "specialty_ciphers": sorted(set(specs)),
            }

    if cluster_id is not None:
        return all_clusters.get(cluster_id, {}).get("specialty_ciphers", [])
    return all_clusters


def get_cluster_info(cluster_id):
    """Возвращает название кластера по его ID."""
    clusters = load_cluster_specialties()
    info = clusters.get(cluster_id, {})
    dept_name = info.get("dept_name", "")
    dept_name = re.sub(r'Кафедра\s+', 'Кафедра ', dept_name) if dept_name else ""
    return dept_name


def get_unique_cities():
    """Возвращает список уникальных городов из БД."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT city FROM adverts WHERE city IS NOT NULL AND city != '' ORDER BY city")
    cities = [row[0] for row in c.fetchall()]
    conn.close()
    return cities


def get_city_stats():
    """Возвращает статистику по городам для карты.

    Возвращает список: [{"city": str, "count": int, "specialties": {cipher: count}}, ...]
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT city, specialty_cipher, COUNT(*) as cnt
        FROM adverts
        WHERE city IS NOT NULL AND city != ''
        GROUP BY city, specialty_cipher
        ORDER BY city, cnt DESC
    """)
    rows = c.fetchall()
    conn.close()

    city_data = {}
    for city, cipher, count in rows:
        if city not in city_data:
            city_data[city] = {"city": city, "count": 0, "specialties": {}}
        city_data[city]["count"] += count
        city_data[city]["specialties"][cipher] = city_data[city]["specialties"].get(cipher, 0) + count

    return list(city_data.values())
