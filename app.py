"""Flask-приложение: веб-интерфейс, API, авторизация, QA, /docs."""
import os
import re
import sys
import json
import openpyxl
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, send_file, abort, jsonify, flash,
)
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup logging first
from logging_config import setup_logging
app_logger, sync_logger = setup_logging()

from auth import init_users, login as auth_login, create_user, delete_user, get_all_users, create_user_with_cluster
from search import search_adverts, get_advert_detail, get_all_specialties, get_db, load_cluster_specialties, get_cluster_info, get_unique_cities, get_city_stats
from daily_sync import init_db
from qa import process_question
from extractors.email_search import find_emails_for_publications, _build_ddg_query
from vak_sync.pdf import _read_pdf_full
from config import SESSION_SECRET_KEY, AUTOREFS_DIR, MAX_SPECIALTIES, RESULTS_PER_PAGE

app = Flask(__name__)
app.secret_key = SESSION_SECRET_KEY

logger = app_logger


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session or not session["user"].get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ======================== API DOCUMENTATION ========================

API_DOCS = {
    "auth": {
        "POST /login": {"params": "username, password", "desc": "Авторизация пользователя"},
        "GET /logout": {"desc": "Выход из системы"},
    },
    "search": {
        "GET /search": {"desc": "Страница поиска диссертаций"},
        "GET /api/search": {"params": "specialties[], date_from, date_to, query, page", "desc": "API поиска диссертаций"},
        "GET /export": {"params": "specialties[], date_from, date_to, query", "desc": "Экспорт результатов в Excel"},
    },
    "detail": {
        "GET /detail/<id>": {"desc": "Детальная информация об объявлении"},
        "POST /detail/<id>/save": {"params": "publications[]", "desc": "Сохранение редактирования публикаций"},
        "POST /api/save_search": {"params": "specialties, date_from, date_to, query", "desc": "Сохранение параметров поиска"},
        "POST /api/clear_search": {"desc": "Очистка параметров поиска"},
    },
    "qa": {
        "GET /qa": {"desc": "Страница QA с LLM"},
        "POST /api/qa": {"params": "question", "desc": "Вопрос к LLM (генерация SQL)"},
    },
    "admin": {
        "GET /admin": {"desc": "Панель администратора"},
        "POST /admin/create": {"params": "username, password, is_admin", "desc": "Создание пользователя"},
        "POST /admin/delete/<username>": {"desc": "Удаление пользователя"},
    },
    "map": {
        "GET /map": {"desc": "Карта защит по городам"},
    },
    "email_search": {
        "POST /api/search_email": {"params": "advert_id", "desc": "Поиск email для публикаций объявления (не сохраняется в БД)"},
    },
}

@app.route("/docs")
@login_required
def docs():
    """Документация API."""
    html_parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>API Docs</title>',
        '<style>',
        'body { font-family: monospace; margin: 20px; background: #1a1a2e; color: #e0e0e0; }',
        'h1 { color: #00d4ff; }',
        'h2 { color: #00d4ff; margin-top: 30px; border-bottom: 1px solid #333; padding-bottom: 5px; }',
        'table { border-collapse: collapse; width: 100%; margin: 10px 0; }',
        'th, td { border: 1px solid #333; padding: 8px; text-align: left; }',
        'th { background: #16213e; color: #00d4ff; }',
        'tr:nth-child(even) { background: #0f3460; }',
        '.method { color: #53ff5a; font-weight: bold; }',
        '.params { color: #ffaa00; }',
        '</style></head><body>',
        '<h1>VAK Adverts API Documentation</h1>',
        '<p>Flask application for managing VAK dissertation advertisements.</p>',
        '<p>Base URL: <code>/</code></p>',
    ]

    for category, endpoints in API_DOCS.items():
        html_parts.append(f'<h2>{category.upper()}</h2>')
        html_parts.append('<table><tr><th>Endpoint</th><th>Params</th><th>Description</th></tr>')
        for endpoint, info in endpoints.items():
            method_path = endpoint.split(' ', 1)
            method = method_path[0] if len(method_path) > 1 else 'GET'
            path = method_path[1] if len(method_path) > 1 else method_path[0]
            params = info.get('params', '')
            desc = info.get('desc', '')
            html_parts.append(
                f'<tr>'
                f'<td><span class="method">{method}</span> <code>{path}</code></td>'
                f'<td class="params">{params}</td>'
                f'<td>{desc}</td>'
                f'</tr>'
            )
        html_parts.append('</table>')

    html_parts.append(
        '<h2>Authentication</h2>'
        '<p>All API endpoints except <code>/login</code> require authentication via session.</p>'
        '<p>Admin endpoints (<code>/admin/*</code>) require <code>is_admin=1</code>.</p>'
        '<h2>Search API Response Format</h2>'
        '<pre>{'
        '  "adverts": [...],  // Array of adverts'
        '  "total": 100,       // Total count'
        '  "page": 1,          // Current page'
        '  "per_page": 1000,   // Items per page'
        '  "total_pages": 1    // Total pages'
        '}</pre>'
        '<h2>Configuration</h2>'
        '<pre>PORT=5002'
        'VAK_SECRET_KEY=your-secret-key</pre>'
        '<p>Run: <code>python app.py</code></p>'
        '<hr><p>Generated at ' + datetime.now().strftime('%Y-%m-%d %H:%M') + '</p>'
        '</body></html>'
    )

    return '\n'.join(html_parts)


@app.context_processor
def inject_year():
    return {"now_year": datetime.now().year}


# ======================== AUTH ========================

@app.route("/")
def index():
    return redirect(url_for("search_page"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth_login(username, password)
        if user:
            session["user"] = user
            logger.info(f"User logged in: {username}")
            return redirect(url_for("search_page"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    clusters = load_cluster_specialties()
    cluster_options = []
    for cid in sorted(clusters.keys()):
        info = clusters[cid]
        cluster_options.append((cid, info["dept_name"]))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        cluster_id = request.form.get("cluster_id", "")

        errors = []

        # Validate username
        if not username:
            errors.append("Логин не может быть пустым")
        elif not re.match(r'^[a-zA-Z0-9_]+$', username):
            errors.append("Логин должен содержать только английские буквы, цифры и символ подчёркивания")
        elif len(username) < 3:
            errors.append("Логин должен содержать минимум 3 символа")
        elif len(username) > 50:
            errors.append("Логин должен содержать максимум 50 символов")

        # Validate password
        if not password:
            errors.append("Пароль не может быть пустым")
        elif len(password) < 6:
            errors.append("Пароль должен содержать минимум 6 символов")
        elif not re.search(r'[a-zA-Z]', password):
            errors.append("Пароль должен содержать хотя бы одну английскую букву")
        elif not re.search(r'[0-9]', password):
            errors.append("Пароль должен содержать хотя бы одну цифру")

        # Validate password confirmation
        if password != password_confirm:
            errors.append("Пароли не совпадают")

        # Validate cluster
        if not cluster_id:
            errors.append("Выберите кластер")
        else:
            try:
                cluster_id = int(cluster_id)
                if cluster_id not in clusters:
                    errors.append("Неверный кластер")
            except ValueError:
                errors.append("Неверный кластер")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            if create_user_with_cluster(username, password, is_admin=False, cluster_id=cluster_id):
                flash("Регистрация успешна! Теперь вы можете войти.", "success")
                return redirect(url_for("login"))
            else:
                flash("Пользователь с таким логином уже существует", "error")

    return render_template("register.html", cluster_options=cluster_options)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ======================== SEARCH ========================

@app.route("/search")
@login_required
def search_page():
    specialties = get_all_specialties()
    user = session.get("user", {})
    clusters = load_cluster_specialties()
    cluster_names = {cid: info["dept_name"] for cid, info in clusters.items()}
    cities = get_unique_cities()
    return render_template("search.html", specialties=specialties, adverts=[], page=1, total=0, total_pages=0, MAX_SPECS=MAX_SPECIALTIES, user=user, cluster_names=cluster_names, cities=cities)


@app.route("/api/search")
@login_required
def api_search():
    specialties = request.args.getlist("specialties", type=str)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    query = request.args.get("query", "").strip()
    city = request.args.get("city", "").strip()
    page = int(request.args.get("page", 1))

    user = session.get("user", {})
    cluster_id = None
    if not user.get("is_admin"):
         cluster_id = user.get("cluster_id")

    result = search_adverts(
         specialties=specialties or None,
         date_from=date_from if date_from else None,
         date_to=date_to if date_to else None,
         query=query if query else None,
         city=city if city else None,
         page=page,
         per_page=RESULTS_PER_PAGE,
         cluster_id=cluster_id,
    )
    logger.debug(f"Search API: {len(result['adverts'])} results for user {session.get('user', {}).get('username', '?')}")
    return jsonify(result)


@app.route("/map")
@login_required
def map_page():
    specialties = get_all_specialties()
    city_stats = get_city_stats()
    cities = get_unique_cities()
    logger.info(f"Map page: city_stats={len(city_stats)} cities, cities list={len(cities)} unique")
    if city_stats:
        for cs in city_stats[:5]:
            logger.info(f"  City: {cs.get('city')}, count={cs.get('count')}, specialties={cs.get('specialties')}")
    return render_template("map.html", specialties=specialties, city_stats=city_stats, cities=cities)


@app.route("/api/search_email", methods=["POST"])
@login_required
def api_search_email():
    data = request.get_json(silent=True) or {}
    advert_id = data.get("advert_id", "")

    if not advert_id:
         return jsonify({"error": "advert_id не указан"}), 400

    advert = get_advert_detail(advert_id)
    if not advert:
         return jsonify({"error": "Объявление не найдено"}), 404

    # Get FIO, city, and organization for better search
    fio = advert.get("fio", "")
    city = advert.get("city", "")
    org = advert.get("organization_name", "")

    # Read PDF text
    pdf_path = advert.get("autoref_path", "")
    if not pdf_path or not os.path.exists(pdf_path):
         return jsonify({"error": "PDF автореферата не найден"}), 404

    pdf_full_text = _read_pdf_full(pdf_path)
    if not pdf_full_text:
         return jsonify({"error": "Не удалось прочитать PDF"}), 400

    # Get publications
    pubs = advert.get("publications", [])
    if not pubs:
         return jsonify({"error": "Публикации не найдены"}), 400

    # Build publication data with city and org context
    struct_pubs = []
    for pub in pubs:
         pub_data = {
             "title": pub.get("title", ""),
             "authors": pub.get("authors", ""),
             "journal": pub.get("journal", ""),
             "year": pub.get("year", ""),
             "pages": pub.get("pages", ""),
         }
         # Add city and org to authors for better search
         if city:
             pub_data["authors"] = f"{pub_data['authors']} {city}"
         if org:
             pub_data["authors"] = f"{pub_data['authors']} {org}"
         struct_pubs.append(pub_data)

    # Search emails
    email_results = find_emails_for_publications(struct_pubs, pdf_full_text)

    # Return results without saving to DB
    results = []
    for i, (pub, emails, source) in enumerate(email_results):
         results.append({
             "pub_number": i + 1,
             "title": pub.get("title", "")[:100],
             "emails": emails,
             "source_url": source.get("url", "") if source else "",
             "source_name": source.get("source_name", "") if source else "",
         })

    return jsonify({"results": results})


@app.route("/export")
@login_required
def export_excel():
    specialties = request.args.getlist("specialties", type=str)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    query = request.args.get("query", "").strip()
    city = request.args.get("city", "").strip()

    user = session.get("user", {})
    cluster_id = None
    if not user.get("is_admin"):
         cluster_id = user.get("cluster_id")

    result = search_adverts(
         specialties=specialties or None,
         date_from=date_from if date_from else None,
         date_to=date_to if date_to else None,
         query=query if query else None,
         city=city if city else None,
         page=1,
         per_page=1000,
         cluster_id=cluster_id,
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Диссертации"

    headers = [
        "№", "ФИО", "Дата защиты", "Диссертация",
        "Специальность", "Руководитель", "Место работы руководителя",
        "Кол-во публикаций", "Ссылка на автореферат",
    ]
    ws.append(headers)

    for i, a in enumerate(result["adverts"], 1):
        ws.append([
            i,
            a["fio"],
            a["date_defend"],
            a["dissertation_name"],
            f"{a['specialty_cipher']} - {a['specialty_text']}",
            a["supervisor_name"],
            a["supervisor_work"],
            a.get("pub_count", 0),
            a["autoref_url"] or "",
        ])

    filename = f"vak_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    filepath = os.path.join("/tmp", filename)
    wb.save(filepath)

    logger.info(f"Exported {len(result['adverts'])} adverts to {filename}")
    return send_file(filepath, as_attachment=True, download_name=filename)


# ======================== DETAIL ========================

@app.route("/api/save_search", methods=["POST"])
@login_required
def save_search():
    data = request.get_json(silent=True) or {}
    session["last_search"] = {
        "specialties": data.get("specialties", []),
        "date_from": data.get("date_from", ""),
        "date_to": data.get("date_to", ""),
        "query": data.get("query", ""),
    }
    return jsonify({})


@app.route("/api/clear_search", methods=["POST"])
@login_required
def clear_search():
    session.pop("last_search", None)
    return jsonify({})


@app.route("/detail/<advert_id>")
@login_required
def detail(advert_id):
    advert = get_advert_detail(advert_id)
    if not advert:
        abort(404)
    try:
        db_conn = get_db()
        db_c = db_conn.cursor()
        db_c.execute("SELECT city, organization_name FROM adverts WHERE id = ?", (advert_id,))
        db_row = db_c.fetchone()
        logger.debug(f"Detail DB fetch: id={advert_id}, db_row={db_row}, api_city={advert.get('city')}, api_org={advert.get('organization_name')}")
        if db_row:
            if db_row[0]:
                advert["city"] = db_row[0]
            if db_row[1]:
                advert["organization_name"] = db_row[1]
        db_conn.close()
    except Exception as e:
        logger.debug(f"Detail DB fetch error: {e}")
    if not advert.get("city") and advert.get("city") != "":
        from vak_sync.vak_api import get_advert_detail as _vak_detail
        _detail = _vak_detail(advert_id)
        if _detail and _detail.get("city"):
            advert.setdefault("city", _detail["city"])
        if _detail and _detail.get("organization_name"):
            advert.setdefault("organization_name", _detail["organization_name"])
    search_params = session.get("last_search", {})
    back_qs = ""
    if search_params:
        parts = {}
        if search_params.get("specialties"):
            for spec in search_params["specialties"]:
                parts.setdefault("specialties", []).append(spec)
        if search_params.get("date_from"):
            parts["date_from"] = search_params["date_from"]
        if search_params.get("date_to"):
            parts["date_to"] = search_params["date_to"]
        if search_params.get("query"):
            parts["query"] = search_params["query"]
        if parts:
            qs_parts = []
            for k, v in parts.items():
                if isinstance(v, list):
                    for item in v:
                        qs_parts.append((k, item))
                else:
                    qs_parts.append((k, v))
            back_qs = urlencode(qs_parts)
    return render_template("detail.html", advert=advert, back_qs=back_qs, cities=get_unique_cities())


@app.route("/detail/<advert_id>/save", methods=["POST"])
@login_required
def save_detail(advert_id):
    data = request.get_json(silent=True) or {}

    conn = get_db()
    c = conn.cursor()

    def safe_str(v):
        if v is None:
            return ""
        if not isinstance(v, str):
            return str(v)
        return v.strip()

    c.execute("""
        UPDATE adverts
        SET city = COALESCE(?, ''),
            organization_name = COALESCE(?, ''),
            supervisor_name = COALESCE(?, ''),
            supervisor_work = COALESCE(?, ''),
            date_defend = COALESCE(?, ''),
            dissertation_name = COALESCE(?, ''),
            specialty_cipher = COALESCE(?, ''),
            specialty_text = COALESCE(?, ''),
            council_cipher = COALESCE(?, ''),
            defend_org = COALESCE(?, '')
        WHERE id = ?
    """, (
        safe_str(data.get("city")),
        safe_str(data.get("organization_name")),
        safe_str(data.get("supervisor_name")),
        safe_str(data.get("supervisor_work")),
        safe_str(data.get("date_defend")),
        safe_str(data.get("dissertation_name")),
        safe_str(data.get("specialty_cipher")),
        safe_str(data.get("specialty_text")),
        safe_str(data.get("council_cipher")),
        safe_str(data.get("defend_org")),
        advert_id,
    ))

    pubs = data.get("publications", [])
    inserted = 0
    updated = 0
    submitted_ids = set()
    for pub in pubs:
        pub_id = pub.get("id")
        pub_number = int(pub.get("pub_number", 0))
        authors = str(pub.get("authors", "") or "").strip()
        title = str(pub.get("title", "") or "").strip()
        journal = str(pub.get("journal", "") or "").strip()
        pages = str(pub.get("pages", "") or "").strip()
        email = str(pub.get("email", "") or "").strip()
        source_url = str(pub.get("source_url", "") or "").strip()
        source_name = str(pub.get("source_name", "") or "").strip()
        year_raw = pub.get("year", "")
        year_val = None
        if year_raw:
            try:
                year_val = int(year_raw)
            except (ValueError, TypeError):
                year_val = None

        if pub_id:
            c.execute("""
                UPDATE publications
                SET pub_number = ?, authors = COALESCE(?, ''), title = COALESCE(?, ''),
                    journal = COALESCE(?, ''), year = ?, pages = COALESCE(?, ''), email = COALESCE(?, ''),
                    source_url = COALESCE(?, ''), source_name = COALESCE(?, '')
                WHERE id = ?
            """, (pub_number, authors, title, journal, year_val, pages, email, source_url, source_name, pub_id))
            updated += c.rowcount
        elif title:
            c.execute(
                "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages, email, source_url, source_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (advert_id, pub_number, authors, title, journal, year_val, pages, email, source_url, source_name)
            )
            inserted += c.rowcount
            submitted_ids.add(c.lastrowid)

    c.execute("SELECT id FROM publications WHERE advert_id = ?", (advert_id,))
    current_ids = set(r[0] for r in c.fetchall())
    to_delete = current_ids - submitted_ids
    if to_delete:
        placeholders = ",".join(["?"] * len(to_delete))
        c.execute(f"DELETE FROM publications WHERE advert_id = ? AND id IN ({placeholders})", (advert_id, *sorted(to_delete)))

    conn.commit()
    conn.close()

    logger.info(f"Saved detail: advert_id={advert_id}, inserted={inserted}, updated={updated}, deleted={len(to_delete)}")
    return jsonify({"ok": True})


# ======================== QA / LLM CHAT ========================

@app.route("/qa")
@login_required
def qa_page():
    return render_template("qa.html")


@app.route("/api/qa", methods=["POST"])
@login_required
def api_qa():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Вопрос не задан"}), 400
    result = process_question(question)
    return jsonify(result)


# ======================== ADMIN ========================

@app.route("/admin")
@admin_required
def admin_page():
    users = get_all_users()
    clusters = load_cluster_specialties()
    cluster_options = [(cid, info["dept_name"]) for cid, info in sorted(clusters.items())]
    return render_template("admin.html", users=users, cluster_options=cluster_options)


@app.route("/admin/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = request.form.get("is_admin") == "on"
    cluster_id_str = request.form.get("cluster_id", "").strip()

    cluster_id = None
    if cluster_id_str:
        try:
            cluster_id = int(cluster_id_str)
        except ValueError:
            pass

    if not username or not password:
        flash("Заполните все поля", "error")
        return redirect(url_for("admin_page"))

    if create_user_with_cluster(username, password, is_admin=is_admin, cluster_id=cluster_id):
        flash(f"Пользователь {username} создан", "success")
    else:
        flash(f"Пользователь {username} уже существует", "error")

    return redirect(url_for("admin_page"))


@app.route("/admin/delete/<username>", methods=["POST"])
@admin_required
def admin_delete_user(username):
    if username == "admin":
        flash("Нельзя удалить администратора", "error")
    else:
        delete_user(username)
        flash(f"Пользователь {username} удалён", "success")
    return redirect(url_for("admin_page"))


# ======================== INIT ========================

# Initialize DB once at startup, not on every request
with app.app_context():
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"), exist_ok=True)
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)
