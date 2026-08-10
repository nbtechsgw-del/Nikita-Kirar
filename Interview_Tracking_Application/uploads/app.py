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