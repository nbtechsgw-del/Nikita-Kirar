import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
PRIMARY_DATABASE_PATH = BASE_DIR / "patient_records.db"
FALLBACK_DATABASE_PATH = Path(tempfile.gettempdir()) / "softgoway_patient_records.db"
DATABASE_PATH = PRIMARY_DATABASE_PATH

app = Flask(__name__)

FEATURE_PAGES = {
    "patients": {"title": "Patient Registration"},
    "records": {"title": "Electronic Medical Records"},
    "appointments": {"title": "Appointment Management"},
    "staff": {"title": "Doctor and Staff Access"},
    "billing": {"title": "Billing and Payment"},
    "search": {"title": "Search and Retrieval"},
    "reports": {"title": "Reporting and Analytics"},
    "security": {"title": "Data Security"},
}

PAGE_TEMPLATES = {
    "patients": "patients.html",
    "records": "medical_records.html",
    "appointments": "appointments.html",
    "staff": "staff_access.html",
    "billing": "billing.html",
    "search": "search.html",
    "reports": "reports.html",
    "security": "security.html",
}

ROLE_PERMISSIONS = {
    "admin": {"patients:read", "patients:write", "records:read", "records:write", "appointments:write", "billing:write", "users:read", "reports:read"},
    "doctor": {"patients:read", "records:read", "records:write", "appointments:write", "reports:read"},
    "nurse": {"patients:read", "records:read", "appointments:write"},
    "receptionist": {"patients:read", "patients:write", "appointments:write", "billing:write"},
    "lab": {"patients:read", "records:read", "records:write"},
}


def get_db_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def resolve_database_path():
    global DATABASE_PATH

    for candidate in (PRIMARY_DATABASE_PATH, FALLBACK_DATABASE_PATH):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(candidate)
            connection.execute("CREATE TABLE IF NOT EXISTS db_healthcheck (id INTEGER)")
            connection.execute("DROP TABLE db_healthcheck")
            connection.commit()
            connection.close()
            DATABASE_PATH = candidate
            return
        except sqlite3.Error:
            continue

    DATABASE_PATH = FALLBACK_DATABASE_PATH


def row_to_dict(row):
    return dict(row) if row else None


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_blank(value):
    return value is None or str(value).strip() == ""


def generate_patient_uid(connection):
    next_id = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM patients").fetchone()[0]
    return f"PR-{datetime.now().year}-{next_id:05d}"


def generate_bill_number():
    return f"BILL-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"


def create_schema():
    with get_db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_uid TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                gender TEXT NOT NULL,
                date_of_birth TEXT NOT NULL,
                blood_group TEXT,
                phone TEXT NOT NULL,
                email TEXT,
                address TEXT,
                emergency_contact TEXT,
                allergies TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS medical_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                doctor_id INTEGER,
                record_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                document_url TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (patient_id) REFERENCES patients (id),
                FOREIGN KEY (doctor_id) REFERENCES users (id)
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                doctor_id INTEGER NOT NULL,
                appointment_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at TEXT NOT NULL,
                FOREIGN KEY (patient_id) REFERENCES patients (id),
                FOREIGN KEY (doctor_id) REFERENCES users (id)
            );

            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_number TEXT NOT NULL UNIQUE,
                patient_id INTEGER NOT NULL,
                service_description TEXT NOT NULL,
                amount REAL NOT NULL,
                paid_amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unpaid',
                created_at TEXT NOT NULL,
                FOREIGN KEY (patient_id) REFERENCES patients (id)
            );
            """
        )


def seed_database():
    users = [
        ("System Administrator", "admin@hospital.com", "admin123", "admin"),
        ("Dr. Aisha Mehta", "doctor@hospital.com", "doctor123", "doctor"),
        ("Nurse Ravi Kumar", "nurse@hospital.com", "nurse123", "nurse"),
        ("Maya Reception", "reception@hospital.com", "reception123", "receptionist"),
        ("Central Lab", "lab@hospital.com", "lab123", "lab"),
    ]

    with get_db_connection() as connection:
        existing_users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing_users == 0:
            connection.executemany(
                """
                INSERT INTO users (name, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (name, email, generate_password_hash(password), role, now_text())
                    for name, email, password, role in users
                ],
            )

        existing_patients = connection.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        if existing_patients == 0:
            created_at = now_text()
            connection.executemany(
                """
                INSERT INTO patients (
                    patient_uid, first_name, last_name, gender, date_of_birth, blood_group,
                    phone, email, address, emergency_contact, allergies, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("PR-2026-00001", "Anaya", "Sharma", "female", "1994-08-12", "A+", "9876543210", "anaya@example.com", "Pune", "Rohan Sharma", "Penicillin", created_at, created_at),
                    ("PR-2026-00002", "Vikram", "Rao", "male", "1987-01-22", "O-", "9123456780", "vikram@example.com", "Bengaluru", "Neha Rao", "None", created_at, created_at),
                ],
            )
            connection.execute(
                """
                INSERT INTO medical_records (patient_id, doctor_id, record_type, title, description, document_url, recorded_at)
                VALUES (1, 2, 'diagnosis', 'Seasonal asthma review', 'Prescribed inhaler and follow-up after two weeks.', '', ?)
                """,
                (created_at,),
            )
            connection.execute(
                """
                INSERT INTO appointments (patient_id, doctor_id, appointment_at, reason, status, created_at)
                VALUES (1, 2, ?, 'Follow-up consultation', 'scheduled', ?)
                """,
                (datetime.now().strftime("%Y-%m-%dT10:30"), created_at),
            )
            connection.execute(
                """
                INSERT INTO bills (bill_number, patient_id, service_description, amount, paid_amount, status, created_at)
                VALUES (?, 1, 'Consultation and medication', 850.00, 0, 'unpaid', ?)
                """,
                (generate_bill_number(), created_at),
            )


def current_user():
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        return None
    with get_db_connection() as connection:
        return row_to_dict(connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def require_permission(permission):
    user = current_user()
    if not user:
        return None, (jsonify({"error": "Please sign in first."}), 401)
    if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
        return None, (jsonify({"error": "Your role does not have permission for this action."}), 403)
    return user, None


@app.route("/")
def index():
    return redirect(url_for("feature_page", page="patients"))


@app.route("/<page>")
def feature_page(page):
    if page not in FEATURE_PAGES:
        return redirect(url_for("feature_page", page="patients"))
    return render_template(
        PAGE_TEMPLATES[page],
        active_page=page,
        page_title=FEATURE_PAGES[page]["title"],
        feature_pages=FEATURE_PAGES,
    )


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    with get_db_connection() as connection:
        user = connection.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401

    return jsonify({"user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}})


@app.get("/api/users")
def list_users():
    _, error = require_permission("users:read")
    if error:
        return error
    with get_db_connection() as connection:
        users = connection.execute("SELECT id, name, email, role FROM users ORDER BY role, name").fetchall()
    return jsonify({"users": [row_to_dict(user) for user in users]})


@app.get("/api/patients")
def list_patients():
    _, error = require_permission("patients:read")
    if error:
        return error

    search = f"%{(request.args.get('search') or '').strip()}%"
    with get_db_connection() as connection:
        patients = connection.execute(
            """
            SELECT * FROM patients
            WHERE patient_uid LIKE ?
               OR first_name LIKE ?
               OR last_name LIKE ?
               OR phone LIKE ?
            ORDER BY id DESC
            """,
            (search, search, search, search),
        ).fetchall()
    return jsonify({"patients": [row_to_dict(patient) for patient in patients]})


@app.post("/api/patients")
def create_patient():
    _, error = require_permission("patients:write")
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    required_fields = ["first_name", "last_name", "gender", "date_of_birth", "phone"]
    missing = [field for field in required_fields if is_blank(payload.get(field))]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}."}), 400

    timestamp = now_text()
    with get_db_connection() as connection:
        patient_uid = generate_patient_uid(connection)
        connection.execute(
            """
            INSERT INTO patients (
                patient_uid, first_name, last_name, gender, date_of_birth, blood_group,
                phone, email, address, emergency_contact, allergies, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patient_uid,
                payload["first_name"].strip(),
                payload["last_name"].strip(),
                payload["gender"].strip(),
                payload["date_of_birth"].strip(),
                (payload.get("blood_group") or "").strip(),
                payload["phone"].strip(),
                (payload.get("email") or "").strip(),
                (payload.get("address") or "").strip(),
                (payload.get("emergency_contact") or "").strip(),
                (payload.get("allergies") or "").strip(),
                timestamp,
                timestamp,
            ),
        )
    return jsonify({"patient_uid": patient_uid}), 201


@app.get("/api/patients/<int:patient_id>/records")
def list_records(patient_id):
    _, error = require_permission("records:read")
    if error:
        return error

    with get_db_connection() as connection:
        records = connection.execute(
            """
            SELECT medical_records.*, users.name AS doctor_name
            FROM medical_records
            LEFT JOIN users ON users.id = medical_records.doctor_id
            WHERE medical_records.patient_id = ?
            ORDER BY medical_records.recorded_at DESC
            """,
            (patient_id,),
        ).fetchall()
    return jsonify({"records": [row_to_dict(record) for record in records]})


@app.post("/api/patients/<int:patient_id>/records")
def create_record(patient_id):
    user, error = require_permission("records:write")
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    required_fields = ["record_type", "title", "description"]
    missing = [field for field in required_fields if is_blank(payload.get(field))]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}."}), 400

    with get_db_connection() as connection:
        patient = connection.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if not patient:
            return jsonify({"error": "Patient not found."}), 404
        connection.execute(
            """
            INSERT INTO medical_records (patient_id, doctor_id, record_type, title, description, document_url, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patient_id,
                user["id"],
                payload["record_type"].strip(),
                payload["title"].strip(),
                payload["description"].strip(),
                (payload.get("document_url") or "").strip(),
                now_text(),
            ),
        )
    return jsonify({"message": "Record saved."}), 201


@app.get("/api/appointments")
def list_appointments():
    _, error = require_permission("patients:read")
    if error:
        return error

    with get_db_connection() as connection:
        appointments = connection.execute(
            """
            SELECT appointments.*, patients.first_name || ' ' || patients.last_name AS patient_name, users.name AS doctor_name
            FROM appointments
            JOIN patients ON patients.id = appointments.patient_id
            JOIN users ON users.id = appointments.doctor_id
            ORDER BY appointments.appointment_at DESC
            """
        ).fetchall()
    return jsonify({"appointments": [row_to_dict(item) for item in appointments]})


@app.post("/api/appointments")
def create_appointment():
    _, error = require_permission("appointments:write")
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    required_fields = ["patient_id", "doctor_id", "appointment_at", "reason"]
    missing = [field for field in required_fields if is_blank(payload.get(field))]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}."}), 400

    with get_db_connection() as connection:
        patient = connection.execute("SELECT id FROM patients WHERE id = ?", (payload["patient_id"],)).fetchone()
        doctor = connection.execute("SELECT id FROM users WHERE id = ? AND role = 'doctor'", (payload["doctor_id"],)).fetchone()
        if not patient:
            return jsonify({"error": "Patient not found."}), 404
        if not doctor:
            return jsonify({"error": "Doctor not found."}), 404
        connection.execute(
            """
            INSERT INTO appointments (patient_id, doctor_id, appointment_at, reason, status, created_at)
            VALUES (?, ?, ?, ?, 'scheduled', ?)
            """,
            (
                payload["patient_id"],
                payload["doctor_id"],
                payload["appointment_at"],
                payload["reason"].strip(),
                now_text(),
            ),
        )
    return jsonify({"message": "Appointment scheduled."}), 201


@app.get("/api/bills")
def list_bills():
    _, error = require_permission("patients:read")
    if error:
        return error

    with get_db_connection() as connection:
        bills = connection.execute(
            """
            SELECT bills.*, patients.first_name || ' ' || patients.last_name AS patient_name
            FROM bills
            JOIN patients ON patients.id = bills.patient_id
            ORDER BY bills.created_at DESC
            """
        ).fetchall()
    return jsonify({"bills": [row_to_dict(bill) for bill in bills]})


@app.post("/api/bills")
def create_bill():
    _, error = require_permission("billing:write")
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    required_fields = ["patient_id", "service_description", "amount"]
    missing = [field for field in required_fields if is_blank(payload.get(field))]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}."}), 400

    try:
        amount = float(payload["amount"])
    except ValueError:
        return jsonify({"error": "Amount must be a number."}), 400

    bill_number = generate_bill_number()
    with get_db_connection() as connection:
        patient = connection.execute("SELECT id FROM patients WHERE id = ?", (payload["patient_id"],)).fetchone()
        if not patient:
            return jsonify({"error": "Patient not found."}), 404
        connection.execute(
            """
            INSERT INTO bills (bill_number, patient_id, service_description, amount, paid_amount, status, created_at)
            VALUES (?, ?, ?, ?, 0, 'unpaid', ?)
            """,
            (bill_number, payload["patient_id"], payload["service_description"].strip(), amount, now_text()),
        )
    return jsonify({"bill_number": bill_number}), 201


@app.get("/api/reports/summary")
def reports_summary():
    _, error = require_permission("reports:read")
    if error:
        return error

    today = datetime.now().strftime("%Y-%m-%d")
    with get_db_connection() as connection:
        total_patients = connection.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        today_appointments = connection.execute(
            "SELECT COUNT(*) FROM appointments WHERE substr(appointment_at, 1, 10) = ?",
            (today,),
        ).fetchone()[0]
        unpaid_bills = connection.execute("SELECT COUNT(*) FROM bills WHERE status != 'paid'").fetchone()[0]
        records_by_type = connection.execute(
            """
            SELECT record_type, COUNT(*) AS total
            FROM medical_records
            GROUP BY record_type
            ORDER BY total DESC, record_type
            """
        ).fetchall()

    return jsonify(
        {
            "summary": {
                "total_patients": total_patients,
                "today_appointments": today_appointments,
                "unpaid_bills": unpaid_bills,
                "records_by_type": [row_to_dict(row) for row in records_by_type],
            }
        }
    )


def initialize_app():
    resolve_database_path()
    create_schema()
    seed_database()


initialize_app()


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
