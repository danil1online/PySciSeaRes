import os
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
from auth import init_users, login as auth_login, create_user, delete_user, get_all_users
from search import search_adverts, get_advert_detail, get_all_specialties, get_db
from daily_sync import init_db
from qa import process_question
from config import SESSION_SECRET_KEY, AUTOREFS_DIR

app = Flask(__name__)
app.secret_key = SESSION_SECRET_KEY


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
        '<h1>📚 VAK Adverts API Documentation</h1>',
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
        '<h2>🔐 Authentication</h2>'
        '<p>All API endpoints except <code>/login</code> require authentication via session.</p>'
        '<p>Admin endpoints (<code>/admin/*</code>) require <code>is_admin=1</code>.</p>'
        '<h2>📊 Search API Response Format</h2>'
        '<pre>{'
        '  "adverts": [...],  // Array of adverts'
        '  "total": 100,       // Total count'
        '  "page": 1,          // Current page'
        '  "per_page": 1000,   // Items per page'
        '  "total_pages": 1    // Total pages'
        '}</pre>'
        '<h2>⚙️ Configuration</h2>'
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
            return redirect(url_for("search_page"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ======================== SEARCH ========================

@app.route("/search")
@login_required
def search_page():
    specialties = get_all_specialties()
    from config import MAX_SPECIALTIES
    return render_template("search.html", specialties=specialties, adverts=[], page=1, total=0, total_pages=0, MAX_SPECS=MAX_SPECIALTIES)


@app.route("/api/search")
@login_required
def api_search():
    specialties = request.args.getlist("specialties", type=str)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    query = request.args.get("query", "").strip()
    page = int(request.args.get("page", 1))

    from config import RESULTS_PER_PAGE
    result = search_adverts(
        specialties=specialties or None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        query=query if query else None,
        page=page,
        per_page=RESULTS_PER_PAGE,
    )
    return jsonify(result)


@app.route("/export")
@login_required
def export_excel():
    specialties = request.args.getlist("specialties", type=str)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    query = request.args.get("query", "").strip()

    # Get all results (no pagination for export)
    result = search_adverts(
        specialties=specialties or None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        query=query if query else None,
        page=1,
        per_page=1000,
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
    return render_template("detail.html", advert=advert, back_qs=back_qs)


@app.route("/detail/<advert_id>/save", methods=["POST"])
@login_required
def save_detail(advert_id):
    data = request.get_json(silent=True) or {}

    conn = get_db()
    c = conn.cursor()

    # Update advert fields
    def safe_str(v):
        if v is None:
            return ""
        if not isinstance(v, str):
            return str(v)
        return v.strip()

    c.execute("""
        UPDATE adverts
        SET supervisor_name = COALESCE(?, ''),
            supervisor_work = COALESCE(?, ''),
            date_defend = COALESCE(?, ''),
            dissertation_name = COALESCE(?, ''),
            specialty_cipher = COALESCE(?, ''),
            specialty_text = COALESCE(?, ''),
            council_cipher = COALESCE(?, ''),
            defend_org = COALESCE(?, '')
        WHERE id = ?
    """, (
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

    # Handle publications
    pubs = data.get("publications", [])
    import logging
    logging.warning(f"save_detail: advert_id={advert_id}, pubs={len(pubs)}, pubs_data={pubs}")
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
        year_raw = pub.get("year", "")
        year_val = None
        if year_raw:
            try:
                year_val = int(year_raw)
            except (ValueError, TypeError):
                year_val = None

        logging.warning(f"save_detail: processing pub id={pub_id!r} title={title!r} pub_number={pub_number}")

        if pub_id:
            # Update existing
            c.execute("""
                UPDATE publications
                SET pub_number = ?, authors = COALESCE(?, ''), title = COALESCE(?, ''),
                    journal = COALESCE(?, ''), year = ?, pages = COALESCE(?, '')
                WHERE id = ?
            """, (pub_number, authors, title, journal, year_val, pages, pub_id))
            updated += c.rowcount
        elif title:
            # Insert new
            logging.warning(f"save_detail: INSERTING new pub: advert_id={advert_id}, number={pub_number}, title={title!r}, year={year_val}")
            c.execute(
                "INSERT INTO publications (advert_id, pub_number, authors, title, journal, year, pages) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (advert_id, pub_number, authors, title, journal, year_val, pages)
            )
            inserted += c.rowcount
            # Remember newly inserted id so it won't be deleted
            submitted_ids.add(c.lastrowid)
        else:
            logging.warning(f"save_detail: SKIPPING pub with empty title")

    logging.warning(f"save_detail: submitted_ids={submitted_ids}")

    # Delete publications not in the list (removed) - only existing ones
    c.execute("SELECT id FROM publications WHERE advert_id = ?", (advert_id,))
    current_ids = set(r[0] for r in c.fetchall())
    to_delete = current_ids - submitted_ids
    logging.warning(f"save_detail: current_ids={current_ids}, to_delete={to_delete}")
    if to_delete:
        placeholders = ",".join(["?"] * len(to_delete))
        c.execute(f"DELETE FROM publications WHERE advert_id = ? AND id IN ({placeholders})", (advert_id, *sorted(to_delete)))

    conn.commit()
    conn.close()

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
    return render_template("admin.html", users=users)


@app.route("/admin/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = request.form.get("is_admin") == "on"

    if not username or not password:
        flash("Заполните все поля", "error")
        return redirect(url_for("admin_page"))

    if create_user(username, password, is_admin):
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

@app.before_request
def ensure_db():
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"), exist_ok=True)
    init_db()


if __name__ == "__main__":
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"), exist_ok=True)
    init_db()
    app.run(host="0.0.0.0", port=5002, debug=True)
