import csv
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from functools import wraps
from io import StringIO
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PRIMARY_DATABASE_PATH = BASE_DIR / "interview_tracking.db"
FALLBACK_DATABASE_PATH = Path(tempfile.gettempdir()) / "interview_tracking.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"
ALLOWED_DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "txt", "png", "jpg", "jpeg"}

APPLICATION_STATUSES = [
    "Applied",
    "Resume Shortlisted",
    "Interview Scheduled",
    "Technical Round",
    "HR Round",
    "Final Round",
    "Selected",
    "Offer Received",
    "Rejected",
    "Withdrawn",
]


def load_environment_file():
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_environment_file()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "interview-tracker-dev-key")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
DATABASE_PATH = PRIMARY_DATABASE_PATH


def resolve_database_path():
    global DATABASE_PATH

    for candidate in (PRIMARY_DATABASE_PATH, FALLBACK_DATABASE_PATH):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(candidate)
            connection.execute("CREATE TABLE IF NOT EXISTS healthcheck (id INTEGER)")
            connection.execute("DROP TABLE healthcheck")
            connection.commit()
            connection.close()
            DATABASE_PATH = candidate
            return
        except sqlite3.Error:
            continue

    DATABASE_PATH = FALLBACK_DATABASE_PATH


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    return cursor


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOCUMENT_EXTENSIONS


def init_db():
    resolve_database_path()
    UPLOAD_FOLDER.mkdir(exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            mobile_number TEXT,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'job_seeker',
            profile_photo TEXT,
            resume_path TEXT,
            skills TEXT,
            experience TEXT,
            education TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS companies (
            company_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            industry TEXT,
            website TEXT,
            location TEXT,
            hr_name TEXT,
            hr_email TEXT,
            hr_phone TEXT,
            status TEXT NOT NULL DEFAULT 'Active'
        );

        CREATE TABLE IF NOT EXISTS job_applications (
            application_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            job_title TEXT NOT NULL,
            job_description TEXT,
            job_location TEXT,
            employment_type TEXT,
            salary TEXT,
            application_date TEXT NOT NULL,
            resume_submitted TEXT,
            application_status TEXT NOT NULL DEFAULT 'Applied',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (company_id) REFERENCES companies(company_id)
        );

        CREATE TABLE IF NOT EXISTS status_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            remarks TEXT,
            FOREIGN KEY (application_id) REFERENCES job_applications(application_id)
        );

        CREATE TABLE IF NOT EXISTS interviews (
            interview_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            interview_round TEXT NOT NULL,
            interview_date TEXT NOT NULL,
            interview_time TEXT NOT NULL,
            interview_mode TEXT NOT NULL,
            interviewer_name TEXT,
            meeting_link TEXT,
            location TEXT,
            remarks TEXT,
            FOREIGN KEY (application_id) REFERENCES job_applications(application_id)
        );

        CREATE TABLE IF NOT EXISTS reminders (
            reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reminder_title TEXT NOT NULL,
            reminder_date TEXT NOT NULL,
            reminder_time TEXT,
            reminder_type TEXT NOT NULL,
            notification_status TEXT NOT NULL DEFAULT 'Pending',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS documents (
            document_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            document_name TEXT NOT NULL,
            document_type TEXT NOT NULL,
            file_path TEXT,
            upload_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS notes (
            note_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            note_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (application_id) REFERENCES job_applications(application_id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            notification_title TEXT NOT NULL,
            notification_message TEXT NOT NULL,
            notification_status TEXT NOT NULL DEFAULT 'Unread',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        """
    )
    admin = db.execute("SELECT user_id FROM users WHERE email = ?", ("admin@example.com",)).fetchone()
    if not admin:
        db.execute(
            """
            INSERT INTO users (full_name, email, mobile_number, password, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Admin User",
                "admin@example.com",
                "9999999999",
                generate_password_hash("admin123"),
                "admin",
                now_text(),
            ),
        )
    db.commit()
    db.close()


BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} | Interview Tracking</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #667085;
      --line: #d9dee8;
      --accent: #16745f;
      --accent-dark: #0f5f4d;
      --danger: #b42318;
      --warn: #b54708;
      --info: #175cd3;
      --radius: 8px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.5 Arial, Helvetica, sans-serif;
    }
    a { color: var(--accent-dark); text-decoration: none; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 245px 1fr; }
    .sidebar {
      background: #17202a;
      color: #f8fafc;
      padding: 22px 16px;
      position: sticky;
      top: 0;
      height: 100vh;
    }
    .brand { font-size: 20px; font-weight: 800; margin-bottom: 20px; }
    .nav a {
      display: block;
      color: #d7dde7;
      padding: 9px 10px;
      border-radius: 6px;
      margin: 2px 0;
    }
    .nav a:hover, .nav .active { background: #263442; color: white; }
    .main { padding: 24px; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      margin-bottom: 18px;
    }
    h1 { margin: 0; font-size: 28px; line-height: 1.2; }
    h2 { font-size: 20px; margin: 0 0 12px; }
    .muted { color: var(--muted); }
    .grid { display: grid; gap: 14px; }
    .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .two { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .panel, .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px;
    }
    .stat strong { display: block; font-size: 28px; margin-top: 2px; }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { background: #eef2f6; font-size: 13px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .button, button {
      border: 0;
      background: var(--accent);
      color: white;
      padding: 9px 12px;
      border-radius: 6px;
      cursor: pointer;
      display: inline-block;
      font: inherit;
    }
    .button:hover, button:hover { background: var(--accent-dark); color: white; }
    .secondary { background: #475467; }
    .danger { background: var(--danger); }
    .ghost { background: transparent; color: var(--accent-dark); border: 1px solid var(--line); }
    form.grid-form {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    label { display: grid; gap: 5px; font-weight: 700; color: #344054; }
    input, select, textarea {
      width: 100%;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
      background: white;
    }
    textarea { min-height: 90px; resize: vertical; }
    .span-2 { grid-column: 1 / -1; }
    .flash { padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; background: #ecfdf3; border: 1px solid #abefc6; }
    .flash.error { background: #fef3f2; border-color: #fecdca; }
    .badge { display: inline-block; padding: 3px 8px; border-radius: 999px; background: #eef2f6; font-size: 12px; color: #344054; }
    .auth {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: #eef2f6;
    }
    .auth .panel { width: min(440px, 100%); }
    @media (max-width: 900px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar { position: static; height: auto; }
      .stats, .two, form.grid-form { grid-template-columns: 1fr; }
      .main { padding: 16px; }
    }
  </style>
</head>
<body>
{% if user %}
<div class="shell">
  <aside class="sidebar">
    <div class="brand">Interview Tracker</div>
    <nav class="nav">
      <a class="{{ 'active' if active == 'dashboard' else '' }}" href="{{ url_for('dashboard') }}">Dashboard</a>
      <a class="{{ 'active' if active == 'profile' else '' }}" href="{{ url_for('profile') }}">Profile</a>
      <a class="{{ 'active' if active == 'applications' else '' }}" href="{{ url_for('applications') }}">Applications</a>
      <a class="{{ 'active' if active == 'interviews' else '' }}" href="{{ url_for('interviews') }}">Interviews</a>
      <a class="{{ 'active' if active == 'reminders' else '' }}" href="{{ url_for('reminders') }}">Reminders</a>
      <a class="{{ 'active' if active == 'documents' else '' }}" href="{{ url_for('documents') }}">Documents</a>
      <a class="{{ 'active' if active == 'reports' else '' }}" href="{{ url_for('reports') }}">Reports</a>
      {% if user.role == 'admin' %}
      <a class="{{ 'active' if active == 'admin' else '' }}" href="{{ url_for('admin') }}">Admin</a>
      {% endif %}
      <a href="{{ url_for('logout') }}">Logout</a>
    </nav>
  </aside>
  <main class="main">
    <div class="topbar">
      <div>
        <h1>{{ title }}</h1>
        <div class="muted">{{ subtitle }}</div>
      </div>
      <div class="badge">{{ user.full_name }} · {{ user.role.replace('_', ' ').title() }}</div>
    </div>
    {% for category, message in get_flashed_messages(with_categories=true) %}
      <div class="flash {{ category }}">{{ message }}</div>
    {% endfor %}
    {{ body|safe }}
  </main>
</div>
{% else %}
<div class="auth">
  <div class="panel">
    <h1>{{ title }}</h1>
    <p class="muted">{{ subtitle }}</p>
    {% for category, message in get_flashed_messages(with_categories=true) %}
      <div class="flash {{ category }}">{{ message }}</div>
    {% endfor %}
    {{ body|safe }}
  </div>
</div>
{% endif %}
</body>
</html>
"""