import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "vak.db")


def get_db():
    return sqlite3.connect(DB_PATH)


def search_adverts(specialties=None, date_from=None, date_to=None, query=None,
                   page=1, per_page=10):
    sql = """
        SELECT a.id, a.fio, a.date_defend, a.dissertation_name,
               a.specialty_cipher, a.specialty_text,
               a.supervisor_name, a.supervisor_work,
               a.council_cipher, a.defend_org,
               a.autoref_url, a.autoref_path,
               COUNT(p.id) as pub_count
        FROM adverts a
        LEFT JOIN publications p ON p.advert_id = a.id
        WHERE 1=1
    """
    params = []

    if specialties:
        spec_list = [s.strip() for s in specialties if s.strip()]
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

    sql += " GROUP BY a.id"

    # Count total
    count_sql = sql.replace("SELECT a.id, ...", "SELECT COUNT(DISTINCT a.id)")
    # Simpler count
    count_sql2 = """
        SELECT COUNT(*) FROM (
            SELECT a.id
            FROM adverts a
            LEFT JOIN publications p ON p.advert_id = a.id
            WHERE 1=1
    """
    count_params = []

    if specialties:
        spec_list = [s.strip() for s in specialties if s.strip()]
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

    count_sql2 += ")"

    sql += " ORDER BY a.date_defend DESC"
    offset = (page - 1) * per_page
    sql += f" LIMIT {per_page} OFFSET {offset}"

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
            "pub_count": row[12],
        })

    total_pages = (total + per_page - 1) // per_page if total > 0 else 0
    conn.close()

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
                        autoref_url, autoref_path
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
    }

    c.execute("SELECT pub_number, authors, title, journal, year, pages FROM publications WHERE advert_id = ? ORDER BY pub_number", (advert_id,))
    advert["publications"] = [
        {
            "pub_number": r[0],
            "authors": r[1],
            "title": r[2],
            "journal": r[3],
            "year": r[4],
            "pages": r[5],
        }
        for r in c.fetchall()
    ]

    conn.close()
    return advert


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
    import os
    import re
    sci_spec_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sci_spec.txt")
    specs_by_group = {}
    for group_key in SPECIALTY_GROUPS:
        specs_by_group[group_key] = []
    if os.path.exists(sci_spec_file):
        with open(sci_spec_file, encoding="utf-8") as f:
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
