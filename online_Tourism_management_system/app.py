import os
import sqlite3 
import tempfile
import json
import csv
import re
import time
import secrets
import urllib.error
import urllib.request
from datetime import datetime
from functools import wraps
from io import StringIO
from pathlib import Path

from flask import (
    Flask,
    flash,
    make_response,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PRIMARY_DATABASE_PATH = BASE_DIR / "tourism.db"
FALLBACK_DATABASE_PATH = Path(tempfile.gettempdir()) / "tourism_management_system.db"
DATABASE_PATH = PRIMARY_DATABASE_PATH
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads" / "package_images"
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
PAYMENT_GATEWAYS = {
    "UPI": "Razorpay Demo",
    "Credit Card": "Stripe Demo",
    "Debit Card": "Stripe Demo",
    "Net Banking": "PayU Demo",
}
ROOM_TYPE_CHOICES = ["Standard", "Deluxe", "Premium", "Executive", "Family Suite", "Cottage"]


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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "tourism-dev-secret-key")
app.config["ADMIN_LOGIN_CODE"] = os.environ.get("ADMIN_LOGIN_CODE", "ADMIN2026")
app.config["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


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


def execute_script(script):
    db = get_db()
    db.executescript(script)
    db.commit()


def ensure_columns(table, columns):
    db = get_db()
    existing_columns = {
        row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in columns.items():
        if name not in existing_columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.commit()


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def parse_room_options(room_options, fallback_room_type=None, fallback_price=None):
    options = []
    for raw_option in (room_options or "").split(";"):
        option_text = raw_option.strip()
        if not option_text:
            continue

        label = option_text
        price = None
        if "-" in option_text:
            label, price_text = option_text.split("-", 1)
            label = label.strip()
        else:
            price_text = option_text

        normalized_price_text = price_text.replace(",", "")
        price_match = re.search(r"\d+(?:\.\d+)?", normalized_price_text)
        if price_match:
            try:
                price = float(price_match.group(0))
            except ValueError:
                price = None

        if label and price:
            options.append({"label": label, "price": price, "text": option_text})

    if not options and fallback_room_type and fallback_price:
        options.append(
            {
                "label": fallback_room_type,
                "price": float(fallback_price),
                "text": f"{fallback_room_type} - Rs. {float(fallback_price):.0f}/night",
            }
        )
    return options


def build_room_config_from_form(form):
    options = []
    room_prices = {}

    for room_type in ROOM_TYPE_CHOICES:
        price_text = form.get(f"room_price_{room_type}", "").strip()
        if not price_text:
            continue
        try:
            price = float(price_text)
        except ValueError:
            raise ValueError(f"Enter a valid price for {room_type}.")
        if price <= 0:
            raise ValueError(f"Enter a price greater than zero for {room_type}.")
        room_prices[room_type] = price
        options.append(f"{room_type} - Rs. {price:.0f}/night")

    if not room_prices:
        raise ValueError("Enter a price for at least one room type.")

    primary_room_type = next(iter(room_prices))
    return primary_room_type, room_prices[primary_room_type], "; ".join(options)


def hotel_has_price_in_range(hotel, min_price=None, max_price=None):
    options = parse_room_options(
        hotel["room_options"],
        hotel["room_type"],
        hotel["price_per_night"],
    )
    for option in options:
        if min_price is not None and option["price"] < min_price:
            continue
        if max_price is not None and option["price"] > max_price:
            continue
        return True
    return False


def prepare_hotels_for_booking(hotel_rows):
    hotels = []
    for hotel in hotel_rows:
        hotel_data = dict(hotel)
        hotel_data["room_options_list"] = parse_room_options(
            hotel["room_options"],
            hotel["room_type"],
            hotel["price_per_night"],
        )
        hotels.append(hotel_data)
    return hotels


def row_value(row, name, default=0):
    if row is None:
        return default
    if isinstance(row, sqlite3.Row):
        if name not in row.keys():
            return default
        value = row[name]
    else:
        value = row.get(name, default)
    return default if value is None else value


def prepare_package_for_display(package):
    package_data = dict(package)
    package_data["price"] = float(row_value(package, "price", 0))
    package_data["transport_price"] = float(row_value(package, "transport_price", 0))
    package_data["food_price"] = float(row_value(package, "food_price", 0))
    package_data["boarding_point"] = row_value(package, "boarding_point", "")
    package_data["package_subtotal"] = (
        package_data["price"]
        + package_data["transport_price"]
        + package_data["food_price"]
    )
    return package_data


def prepare_packages_for_display(package_rows):
    return [prepare_package_for_display(package) for package in package_rows]


def calculate_nights(check_in, check_out):
    start = datetime.strptime(check_in, "%Y-%m-%d").date()
    end = datetime.strptime(check_out, "%Y-%m-%d").date()
    nights = (end - start).days
    if nights < 1:
        raise ValueError("Check-out date must be after check-in date.")
    return nights


def save_package_image(uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return None
    if not allowed_image(uploaded_file.filename):
        raise ValueError("Package image must be a PNG, JPG, JPEG, GIF, or WEBP file.")

    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = secure_filename(uploaded_file.filename)
    filename = f"{int(time.time())}_{safe_name}"
    uploaded_file.save(UPLOAD_FOLDER / filename)
    return url_for("static", filename=f"uploads/package_images/{filename}")


def process_payment_gateway(method, gateway, amount):
    gateway_name = gateway or PAYMENT_GATEWAYS.get(method, "TravelDesk Gateway")
    reference = f"GW{int(time.time())}{secrets.token_hex(3).upper()}"
    transaction_id = f"TXN{int(time.time())}{secrets.token_hex(3).upper()}"
    confirmation_code = f"CNF{secrets.token_hex(4).upper()}"
    status = "Failed" if gateway_name == "Gateway Failure Demo" else "Paid"

    return {
        "gateway": gateway_name,
        "gateway_reference": reference,
        "transaction_id": transaction_id,
        "confirmation_code": confirmation_code if status == "Paid" else "",
        "status": status,
        "gateway_message": (
            f"{gateway_name} confirmed Rs. {amount:.0f}."
            if status == "Paid"
            else f"{gateway_name} declined the transaction."
        ),
    }


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


def initialize_database():
    with app.app_context():
        resolve_database_path()
        UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        execute_script(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                destination TEXT NOT NULL,
                category TEXT NOT NULL,
                duration TEXT NOT NULL,
                price REAL NOT NULL,
                transport_price REAL NOT NULL DEFAULT 0,
                food_price REAL NOT NULL DEFAULT 0,
                boarding_point TEXT NOT NULL DEFAULT '',
                seats_available INTEGER NOT NULL,
                itinerary TEXT NOT NULL,
                offer TEXT NOT NULL DEFAULT 'Standard',
                image_url TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hotels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                destination TEXT NOT NULL,
                room_type TEXT NOT NULL,
                price_per_night REAL NOT NULL,
                rooms_available INTEGER NOT NULL,
                rating REAL NOT NULL,
                facilities TEXT NOT NULL DEFAULT '',
                room_options TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                country TEXT NOT NULL,
                best_season TEXT NOT NULL,
                description TEXT NOT NULL,
                image_url TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                hotel_id INTEGER,
                travelers INTEGER NOT NULL,
                check_in TEXT NOT NULL,
                check_out TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                total_amount REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (package_id) REFERENCES packages(id),
                FOREIGN KEY (hotel_id) REFERENCES hotels(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                gateway TEXT NOT NULL DEFAULT 'TravelDesk Gateway',
                gateway_reference TEXT NOT NULL DEFAULT '',
                transaction_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Paid',
                confirmation_code TEXT NOT NULL DEFAULT '',
                refund_status TEXT NOT NULL DEFAULT 'Not Requested',
                refund_reason TEXT NOT NULL DEFAULT '',
                refunded_at TEXT,
                paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (booking_id) REFERENCES bookings(id)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                comments TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (package_id) REFERENCES packages(id)
            );
            """
        )
        ensure_columns(
            "packages",
            {
                "transport_price": "REAL NOT NULL DEFAULT 0",
                "food_price": "REAL NOT NULL DEFAULT 0",
                "boarding_point": "TEXT NOT NULL DEFAULT ''",
            },
        )
        ensure_columns(
            "payments",
            {
                "gateway": "TEXT NOT NULL DEFAULT 'TravelDesk Gateway'",
                "gateway_reference": "TEXT NOT NULL DEFAULT ''",
                "confirmation_code": "TEXT NOT NULL DEFAULT ''",
                "refund_status": "TEXT NOT NULL DEFAULT 'Not Requested'",
                "refund_reason": "TEXT NOT NULL DEFAULT ''",
                "refunded_at": "TEXT",
            },
        )
        ensure_columns(
            "hotels",
            {
                "facilities": "TEXT NOT NULL DEFAULT ''",
                "room_options": "TEXT NOT NULL DEFAULT ''",
            },
        )
        ensure_columns(
            "bookings",
            {
                "selected_room_type": "TEXT NOT NULL DEFAULT ''",
                "selected_room_price": "REAL NOT NULL DEFAULT 0",
                "hotel_nights": "INTEGER NOT NULL DEFAULT 0",
            },
        )

        db = get_db()
        db.execute(
            """
            UPDATE bookings
            SET status = 'Confirmed'
            WHERE status = 'Pending'
              AND id IN (
                  SELECT booking_id FROM payments WHERE status = 'Paid'
              )
            """
        )

        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            db.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (
                    "System Admin",
                    "admin@tourism.local",
                    generate_password_hash("admin123"),
                    "admin",
                ),
            )

        package_count = db.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
        if package_count == 0:
            db.executemany(
                """
                INSERT INTO packages
                (title, destination, category, duration, price, transport_price, food_price,
                 boarding_point, seats_available, itinerary, offer, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "Golden Triangle Heritage Tour",
                        "Delhi - Agra - Jaipur",
                        "Domestic",
                        "5 Days / 4 Nights",
                        18999,
                        4500,
                        3200,
                        "Delhi Railway Station Gate 2",
                        18,
                        "Delhi monuments, Taj Mahal sunrise visit, Jaipur forts, local markets.",
                        "15% seasonal offer",
                        "https://images.unsplash.com/photo-1524492412937-b28074a5d7da?auto=format&fit=crop&w=1200&q=80",
                    ),
                    (
                        "Kerala Backwater Escape",
                        "Munnar - Alleppey - Kochi",
                        "Domestic",
                        "6 Days / 5 Nights",
                        24999,
                        5200,
                        4100,
                        "Kochi Airport Arrival Gate",
                        14,
                        "Tea gardens, houseboat stay, spice tour, Kochi heritage walk.",
                        "Free airport pickup",
                        "https://images.unsplash.com/photo-1602216056096-3b40cc0c9944?auto=format&fit=crop&w=1200&q=80",
                    ),
                    (
                        "Dubai City Lights",
                        "Dubai",
                        "International",
                        "4 Days / 3 Nights",
                        45999,
                        12500,
                        7800,
                        "Dubai International Airport Terminal 3",
                        10,
                        "Burj Khalifa, desert safari, dhow cruise, city shopping tour.",
                        "Early bird discount",
                        "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=1200&q=80",
                    ),
                ],
            )

        hotel_count = db.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]
        if hotel_count == 0:
            db.executemany(
                """
                INSERT INTO hotels
                (name, destination, room_type, price_per_night, rooms_available, rating, facilities, room_options)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "Amber Palace Stay",
                        "Jaipur",
                        "Deluxe",
                        3200,
                        12,
                        4.4,
                        "Heritage decor, breakfast, Wi-Fi, guided city desk, airport pickup on request.",
                        "Standard - Rs. 2400/night; Deluxe - Rs. 3200/night; Family Suite - Rs. 5200/night",
                    ),
                    (
                        "Lake View Resort",
                        "Munnar",
                        "Premium",
                        4100,
                        8,
                        4.7,
                        "Valley views, restaurant, room heater, tea garden walk, parking, travel desk.",
                        "Premium - Rs. 4100/night; Valley View - Rs. 5600/night; Cottage - Rs. 7200/night",
                    ),
                    (
                        "Marina Grand Hotel",
                        "Dubai",
                        "Executive",
                        7600,
                        6,
                        4.6,
                        "Pool, gym, breakfast buffet, metro access, concierge, airport transfer.",
                        "Executive - Rs. 7600/night; Marina View - Rs. 9800/night; Suite - Rs. 14500/night",
                    ),
                    (
                        "Budget Comfort Inn",
                        "Delhi",
                        "Standard",
                        2100,
                        20,
                        4.1,
                        "Wi-Fi, breakfast, air conditioning, metro nearby, 24-hour front desk.",
                        "Standard - Rs. 2100/night; Deluxe - Rs. 2900/night; Triple Room - Rs. 3600/night",
                    ),
                ],
            )

        destination_count = db.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
        if destination_count == 0:
            db.executemany(
                """
                INSERT INTO destinations
                (name, country, best_season, description, image_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "Jaipur",
                        "India",
                        "October to March",
                        "A heritage city known for forts, palaces, local crafts, and royal cuisine.",
                        "https://images.unsplash.com/photo-1477587458883-47145ed94245?auto=format&fit=crop&w=1200&q=80",
                    ),
                    (
                        "Munnar",
                        "India",
                        "September to May",
                        "A calm hill destination with tea gardens, waterfalls, and scenic drives.",
                        "https://images.unsplash.com/photo-1602216056096-3b40cc0c9944?auto=format&fit=crop&w=1200&q=80",
                    ),
                    (
                        "Dubai",
                        "United Arab Emirates",
                        "November to March",
                        "A modern international destination for shopping, desert safaris, and skyline views.",
                        "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=1200&q=80",
                    ),
                ],
            )

        db.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if "user_id" not in session:
            flash("Please login to continue.", "warning")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if session.get("role") != "admin":
            flash("Admin access is required.", "error")
            return redirect(url_for("home"))
        return view(**kwargs)

    return wrapped_view


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute(
        "SELECT id, name, email, role FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()


def ask_gemini_for_destination(query):
    api_key = app.config.get("GEMINI_API_KEY")
    if not api_key:
        return None, "Destination search API key is missing."

    prompt = (
        "Suggest a concise travel plan for this destination search. "
        "Include best places to visit, ideal trip duration, suitable traveler type, "
        "and one practical tip. Keep it under 120 words. Search: "
        f"{query}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={api_key}"
    )
    request_data = json.dumps(payload).encode("utf-8")
    api_request = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(api_request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return None, f"Destination search is unavailable right now. {error}"

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None, "Destination search returned an empty response."

    return text, None


def hotel_matches_package_destination(hotel_destination, package_destination):
    hotel_destination = hotel_destination.lower().strip()
    package_parts = [
        part.strip().lower()
        for part in package_destination.replace("-", ",").split(",")
        if part.strip()
    ]
    return hotel_destination in package_parts or hotel_destination in package_destination.lower()


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


@app.route("/")
def home():
    db = get_db()
    packages = prepare_packages_for_display(
        db.execute("SELECT * FROM packages ORDER BY id DESC LIMIT 3").fetchall()
    )
    hotels = db.execute("SELECT * FROM hotels ORDER BY rating DESC LIMIT 3").fetchall()
    reviews = db.execute(
        """
        SELECT feedback.*, users.name, packages.title
        FROM feedback
        JOIN users ON users.id = feedback.user_id
        JOIN packages ON packages.id = feedback.package_id
        ORDER BY feedback.id DESC
        LIMIT 4
        """
    ).fetchall()
    return render_template("index.html", packages=packages, hotels=hotels, reviews=reviews)


@app.route("/modules")
def module_selection():
    modules = [
        {
            "name": "Admin Module",
            "description": "Manage packages, users, booking approvals, payments, and reports.",
            "endpoint": "admin_module",
        },
        {
            "name": "User Module",
            "description": "Register, login, search packages, book trips, and view booking history.",
            "endpoint": "user_module",
        },
        {
            "name": "Tour Package Module",
            "description": "View domestic and international packages, prices, duration, itinerary, and offers.",
            "endpoint": "package_module",
        },
        {
            "name": "Hotel Management Module",
            "description": "Browse hotel listings, room availability, room pricing, and ratings.",
            "endpoint": "hotel_module",
        },
        {
            "name": "Booking Module",
            "description": "Create package bookings, track booking status, and view booking records.",
            "endpoint": "booking_module",
        },
        {
            "name": "Payment Module",
            "description": "Review transaction records, payment methods, confirmation, and payment status.",
            "endpoint": "payment_module",
        },
        {
            "name": "Feedback Module",
            "description": "Submit ratings, reviews, complaints, and customer suggestions.",
            "endpoint": "feedback_module",
        },
    ]
    return render_template("modules.html", modules=modules)


@app.route("/modules/admin")
def admin_module():
    if session.get("role") == "admin":
        return redirect("/admin#manage-packages")

    db = get_db()
    stats = {
        "users": db.execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0],
        "packages": db.execute("SELECT COUNT(*) FROM packages").fetchone()[0],
        "bookings": db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0],
        "payments": db.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
    }
    return render_template("module_admin.html", stats=stats)


@app.route("/modules/user")
def user_module():
    user_count = get_db().execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0]
    return render_template("module_user.html", user_count=user_count)


@app.route("/modules/packages")
def package_module():
    packages = prepare_packages_for_display(
        get_db().execute("SELECT * FROM packages ORDER BY id DESC").fetchall()
    )
    return render_template("module_packages.html", packages=packages)


@app.route("/modules/hotels")
def hotel_module():
    destination = request.args.get("destination", "").strip()
    min_price_text = request.args.get("min_price", "").strip()
    max_price_text = request.args.get("max_price", "").strip()
    try:
        min_price = float(min_price_text) if min_price_text else None
        max_price = float(max_price_text) if max_price_text else None
    except ValueError:
        flash("Enter a valid hotel price range.", "error")
        return redirect(url_for("hotel_module"))

    db = get_db()
    destinations = db.execute("SELECT * FROM destinations ORDER BY name").fetchall()
    hotel_rows = db.execute("SELECT * FROM hotels ORDER BY destination, rating DESC").fetchall()
    hotels = []
    for hotel in hotel_rows:
        if destination and hotel["destination"].lower() != destination.lower():
            continue
        if not hotel_has_price_in_range(hotel, min_price, max_price):
            continue
        hotel_data = dict(hotel)
        hotel_data["room_options_list"] = parse_room_options(
            hotel["room_options"],
            hotel["room_type"],
            hotel["price_per_night"],
        )
        hotels.append(hotel_data)

    return render_template(
        "module_hotels.html",
        hotels=hotels,
        destinations=destinations,
        selected_destination=destination,
        min_price=min_price_text,
        max_price=max_price_text,
    )


@app.route("/modules/booking")
def booking_module():
    db = get_db()
    if session.get("role") == "admin":
        bookings = db.execute(
            """
            SELECT bookings.*, users.name, packages.title, hotels.name AS hotel_name,
                   payments.status AS payment_status
            FROM bookings
            JOIN users ON users.id = bookings.user_id
            JOIN packages ON packages.id = bookings.package_id
            LEFT JOIN hotels ON hotels.id = bookings.hotel_id
            JOIN payments ON payments.booking_id = bookings.id
            WHERE users.role = 'user'
              AND payments.status = 'Paid'
            ORDER BY bookings.id DESC
            LIMIT 20
            """
        ).fetchall()
    else:
        bookings = db.execute(
            """
            SELECT bookings.*, users.name, packages.title, hotels.name AS hotel_name,
                   payments.status AS payment_status
            FROM bookings
            JOIN users ON users.id = bookings.user_id
            JOIN packages ON packages.id = bookings.package_id
            LEFT JOIN hotels ON hotels.id = bookings.hotel_id
            LEFT JOIN payments ON payments.booking_id = bookings.id
            ORDER BY bookings.id DESC
            LIMIT 20
            """
        ).fetchall()
    return render_template("module_booking.html", bookings=bookings)


@app.route("/modules/payment")
@login_required
def payment_module():
    db = get_db()
    user = current_user()
    where_clause = ""
    params = []
    if user and user["role"] == "user":
        where_clause = "WHERE bookings.user_id = ?"
        params.append(user["id"])

    payments = db.execute(
        f"""
        SELECT payments.*, bookings.total_amount, bookings.status AS booking_status,
               packages.title, users.name
        FROM payments
        JOIN bookings ON bookings.id = payments.booking_id
        JOIN packages ON packages.id = bookings.package_id
        JOIN users ON users.id = bookings.user_id
        {where_clause}
        ORDER BY payments.id DESC
        LIMIT 20
        """,
        params,
    ).fetchall()
    stats_where = ""
    stats_params = []
    if user and user["role"] == "user":
        stats_where = "AND bookings.user_id = ?"
        stats_params.append(user["id"])

    stats = {
        "transactions": db.execute(
            f"""
            SELECT COUNT(*)
            FROM payments
            JOIN bookings ON bookings.id = payments.booking_id
            WHERE 1 = 1 {stats_where}
            """,
            stats_params,
        ).fetchone()[0],
        "paid": db.execute(
            f"""
            SELECT COUNT(*)
            FROM payments
            JOIN bookings ON bookings.id = payments.booking_id
            WHERE payments.status = 'Paid' {stats_where}
            """,
            stats_params,
        ).fetchone()[0],
        "refunds": db.execute(
            f"""
            SELECT COUNT(*)
            FROM payments
            JOIN bookings ON bookings.id = payments.booking_id
            WHERE payments.refund_status != 'Not Requested' {stats_where}
            """,
            stats_params,
        ).fetchone()[0],
        "amount": db.execute(
            f"""
            SELECT COALESCE(SUM(bookings.total_amount), 0)
            FROM payments
            JOIN bookings ON bookings.id = payments.booking_id
            WHERE payments.status IN ('Paid', 'Refunded') {stats_where}
            """,
            stats_params,
        ).fetchone()[0],
    }
    features = [
        {
            "title": "Payment Gateway Integration",
            "detail": "Checkout connects each payment method to a demo gateway such as Razorpay, Stripe, or PayU.",
            "value": ", ".join(sorted(set(PAYMENT_GATEWAYS.values()))),
        },
        {
            "title": "Transaction Records",
            "detail": "Every successful checkout stores method, gateway reference, transaction ID, amount, and status.",
            "value": f"{stats['transactions']} records",
        },
        {
            "title": "Payment Confirmation",
            "detail": "Paid transactions generate a confirmation code and receipt page for the booking.",
            "value": f"{stats['paid']} confirmed",
        },
        {
            "title": "Refund Processing",
            "detail": "Users can submit refund requests from paid transactions and track refund status here.",
            "value": f"{stats['refunds']} refund cases",
        },
    ]
    return render_template("module_payment.html", payments=payments, stats=stats, features=features)


@app.route("/modules/feedback")
def feedback_module():
    reviews = get_db().execute(
        """
        SELECT feedback.*, users.name, packages.title
        FROM feedback
        JOIN users ON users.id = feedback.user_id
        JOIN packages ON packages.id = feedback.package_id
        ORDER BY feedback.id DESC
        LIMIT 20
        """
    ).fetchall()
    return render_template("module_feedback.html", reviews=reviews)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        role = request.form.get("role", "user")
        admin_code = request.form.get("admin_code", "").strip()

        if role not in {"user", "admin"}:
            flash("Choose a valid account type.", "error")
            return redirect(url_for("register"))

        if role == "admin" and admin_code != app.config["ADMIN_LOGIN_CODE"]:
            flash("Invalid admin authentication code.", "error")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must contain at least 6 characters.", "error")
            return redirect(url_for("register"))

        try:
            get_db().execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role),
            )
            get_db().commit()
            flash("Registration successful. Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("This email is already registered.", "error")

    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        admin_code = request.form.get("admin_code", "").strip()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            if user["role"] == "admin" and admin_code != app.config["ADMIN_LOGIN_CODE"]:
                flash("Admin authentication code is required.", "error")
                return redirect(url_for("login"))

            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            flash("Welcome back.", "success")
            if user["role"] == "admin":
                return redirect("/admin")
            return redirect(url_for("home"))

        flash("Invalid email or password.", "error")

    return render_template("auth.html", mode="login")


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        admin_code = request.form.get("admin_code", "").strip()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if (
            user
            and user["role"] == "admin"
            and check_password_hash(user["password_hash"], password)
            and admin_code == app.config["ADMIN_LOGIN_CODE"]
        ):
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            flash("Admin login successful.", "success")
            return redirect("/admin")

        flash("Invalid admin credentials.", "error")

    return render_template("auth.html", mode="admin_login")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/packages")
def packages():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    query = "SELECT * FROM packages WHERE 1 = 1"
    params = []

    if search:
        query += " AND (title LIKE ? OR destination LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY id DESC"
    package_list = prepare_packages_for_display(get_db().execute(query, params).fetchall())
    return render_template("packages.html", packages=package_list, search=search, category=category)


@app.route("/destination-search", methods=["GET", "POST"])
@login_required
def destination_search():
    query = ""
    ai_result = None
    error = None
    matching_packages = []

    if request.method == "POST":
        query = request.form["destination"].strip()
        if query:
            ai_result, error = ask_gemini_for_destination(query)
            matching_packages = prepare_packages_for_display(get_db().execute(
                """
                SELECT * FROM packages
                WHERE title LIKE ? OR destination LIKE ? OR itinerary LIKE ?
                ORDER BY id DESC
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            ).fetchall())
        else:
            error = "Enter a destination to search."

    return render_template(
        "destination_search.html",
        query=query,
        ai_result=ai_result,
        error=error,
        packages=matching_packages,
    )


@app.route("/book/<int:package_id>", methods=["GET", "POST"])
@login_required
def book_package(package_id):
    if session.get("role") == "admin":
        flash("Admins can manage packages, but cannot book packages.", "warning")
        return redirect(url_for("packages"))

    db = get_db()
    package = db.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()

    if package is None:
        flash("Package not found.", "error")
        return redirect(url_for("packages"))
    package = prepare_package_for_display(package)

    min_price_text = request.args.get("min_price", "").strip()
    max_price_text = request.args.get("max_price", "").strip()
    try:
        min_price = float(min_price_text) if min_price_text else None
        max_price = float(max_price_text) if max_price_text else None
    except ValueError:
        flash("Enter a valid hotel price range.", "error")
        return redirect(url_for("book_package", package_id=package_id))

    all_hotels = db.execute("SELECT * FROM hotels ORDER BY rating DESC").fetchall()
    destination_hotels = [
        hotel
        for hotel in all_hotels
        if hotel["rooms_available"] > 0
        and hotel_matches_package_destination(hotel["destination"], package["destination"])
    ]
    matching_hotels = [
        hotel
        for hotel in destination_hotels
        if hotel_has_price_in_range(hotel, min_price, max_price)
    ]
    hotels = prepare_hotels_for_booking(matching_hotels)

    if request.method == "POST":
        travelers = int(request.form["travelers"])
        room_choice = request.form.get("room_choice", "none")
        hotel_id = None
        selected_room_type = ""
        selected_room_price = 0
        hotel_nights = 0
        check_in = request.form["check_in"]
        check_out = request.form["check_out"]
        payment_method = request.form["payment_method"]

        if travelers > package["seats_available"]:
            flash("Requested travelers exceed available seats.", "error")
            return redirect(url_for("book_package", package_id=package_id))

        try:
            hotel_nights = calculate_nights(check_in, check_out)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("book_package", package_id=package_id))

        hotel_amount = 0
        if room_choice != "none":
            try:
                hotel_id_text, room_index_text = room_choice.split(":", 1)
                hotel_id = int(hotel_id_text)
                room_index = int(room_index_text)
            except ValueError:
                flash("Choose a valid hotel room option.", "error")
                return redirect(url_for("book_package", package_id=package_id))

            hotel = db.execute("SELECT * FROM hotels WHERE id = ?", (hotel_id,)).fetchone()
            if hotel is None or not hotel_matches_package_destination(hotel["destination"], package["destination"]):
                flash("Choose a hotel from the package destination.", "error")
                return redirect(url_for("book_package", package_id=package_id))
            if hotel["rooms_available"] < 1:
                flash("This hotel has no rooms available.", "error")
                return redirect(url_for("book_package", package_id=package_id))
            room_options = parse_room_options(
                hotel["room_options"],
                hotel["room_type"],
                hotel["price_per_night"],
            )
            if room_index < 0 or room_index >= len(room_options):
                flash("Choose a valid room type for the selected hotel.", "error")
                return redirect(url_for("book_package", package_id=package_id))
            selected_room = room_options[room_index]
            selected_room_type = selected_room["label"]
            selected_room_price = selected_room["price"]
            hotel_amount = selected_room_price * hotel_nights

        package_amount = package["package_subtotal"] * travelers
        total_amount = package_amount + hotel_amount
        gateway_result = process_payment_gateway(payment_method, None, total_amount)
        booking_status = "Confirmed" if gateway_result["status"] == "Paid" else "Cancelled"
        cursor = db.execute(
            """
            INSERT INTO bookings
            (user_id, package_id, hotel_id, travelers, check_in, check_out, status, total_amount,
             selected_room_type, selected_room_price, hotel_nights)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                package_id,
                hotel_id,
                travelers,
                check_in,
                check_out,
                booking_status,
                total_amount,
                selected_room_type,
                selected_room_price,
                hotel_nights if hotel_id else 0,
            ),
        )
        booking_id = cursor.lastrowid
        payment_cursor = db.execute(
            """
            INSERT INTO payments
            (booking_id, method, gateway, gateway_reference, transaction_id, status,
             confirmation_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                booking_id,
                payment_method,
                gateway_result["gateway"],
                gateway_result["gateway_reference"],
                gateway_result["transaction_id"],
                gateway_result["status"],
                gateway_result["confirmation_code"],
            ),
        )
        if gateway_result["status"] == "Paid":
            db.execute(
                "UPDATE packages SET seats_available = seats_available - ? WHERE id = ?",
                (travelers, package_id),
            )
        db.commit()
        flash(gateway_result["gateway_message"], "success" if gateway_result["status"] == "Paid" else "error")
        return redirect(url_for("payment_confirmation", payment_id=payment_cursor.lastrowid))

    return render_template(
        "book.html",
        package=package,
        hotels=hotels,
        has_destination_hotels=bool(destination_hotels),
        min_price=min_price_text,
        max_price=max_price_text,
    )


@app.route("/bookings")
@login_required
def booking_history():
    db = get_db()
    is_admin = session.get("role") == "admin"
    if is_admin:
        bookings = db.execute(
            """
            SELECT bookings.*, users.name AS user_name, packages.title, packages.destination,
                   packages.price AS package_price,
                   packages.transport_price, packages.food_price,
                   packages.boarding_point,
                   hotels.name AS hotel_name, payments.id AS payment_id, payments.method,
                   payments.gateway, payments.gateway_reference, payments.transaction_id,
                   payments.confirmation_code, payments.refund_status,
                   payments.status AS payment_status
            FROM bookings
            JOIN users ON users.id = bookings.user_id
            JOIN packages ON packages.id = bookings.package_id
            LEFT JOIN hotels ON hotels.id = bookings.hotel_id
            JOIN payments ON payments.booking_id = bookings.id
            WHERE users.role = 'user'
              AND payments.status = 'Paid'
            ORDER BY bookings.id DESC
            """
        ).fetchall()
    else:
        bookings = db.execute(
            """
            SELECT bookings.*, users.name AS user_name, packages.title, packages.destination,
                   packages.price AS package_price,
                   packages.transport_price, packages.food_price,
                   packages.boarding_point,
                   hotels.name AS hotel_name, payments.id AS payment_id, payments.method,
                   payments.gateway, payments.gateway_reference, payments.transaction_id,
                   payments.confirmation_code, payments.refund_status,
                   payments.status AS payment_status
            FROM bookings
            JOIN users ON users.id = bookings.user_id
            JOIN packages ON packages.id = bookings.package_id
            LEFT JOIN hotels ON hotels.id = bookings.hotel_id
            LEFT JOIN payments ON payments.booking_id = bookings.id
            WHERE bookings.user_id = ?
            ORDER BY bookings.id DESC
            """,
            (session["user_id"],),
        ).fetchall()
    return render_template("bookings.html", bookings=bookings, is_admin=is_admin)


@app.route("/payments/confirmation/<int:payment_id>")
@login_required
def payment_confirmation(payment_id):
    payment = get_db().execute(
        """
        SELECT payments.*, bookings.total_amount, bookings.status AS booking_status,
               bookings.check_in, bookings.check_out, bookings.selected_room_type,
               bookings.selected_room_price, bookings.hotel_nights,
               packages.title, packages.destination, packages.price AS package_price,
               packages.transport_price, packages.food_price, packages.boarding_point,
               users.name
        FROM payments
        JOIN bookings ON bookings.id = payments.booking_id
        JOIN packages ON packages.id = bookings.package_id
        JOIN users ON users.id = bookings.user_id
        WHERE payments.id = ?
          AND (bookings.user_id = ? OR ? = 'admin')
        """,
        (payment_id, session["user_id"], session.get("role")),
    ).fetchone()

    if payment is None:
        flash("Payment record not found.", "error")
        return redirect(url_for("booking_history"))

    return render_template("payment_confirmation.html", payment=payment)


@app.route("/payments/<int:payment_id>/refund", methods=["POST"])
@login_required
def request_refund(payment_id):
    db = get_db()
    payment = db.execute(
        """
        SELECT payments.*, bookings.user_id, bookings.status AS booking_status
        FROM payments
        JOIN bookings ON bookings.id = payments.booking_id
        WHERE payments.id = ? AND bookings.user_id = ?
        """,
        (payment_id, session["user_id"]),
    ).fetchone()

    if payment is None:
        flash("Payment record not found.", "error")
        return redirect("/modules/payment")
    if payment["status"] not in {"Paid", "Refund Requested"}:
        flash("Only paid transactions can be submitted for refund.", "warning")
        return redirect("/modules/payment")
    if payment["refund_status"] in {"Requested", "Processed"}:
        flash("Refund request is already recorded.", "warning")
        return redirect("/modules/payment")

    reason = request.form.get("refund_reason", "").strip() or "Customer requested refund."
    db.execute(
        """
        UPDATE payments
        SET status = 'Refund Requested', refund_status = 'Requested', refund_reason = ?
        WHERE id = ?
        """,
        (reason, payment_id),
    )
    db.commit()
    flash("Refund request submitted.", "success")
    return redirect("/modules/payment")


@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    db = get_db()
    user_packages = db.execute(
        """
        SELECT DISTINCT packages.id, packages.title
        FROM bookings
        JOIN packages ON packages.id = bookings.package_id
        WHERE bookings.user_id = ?
        ORDER BY packages.title
        """,
        (session["user_id"],),
    ).fetchall()

    if request.method == "POST":
        db.execute(
            "INSERT INTO feedback (user_id, package_id, rating, comments) VALUES (?, ?, ?, ?)",
            (
                session["user_id"],
                request.form["package_id"],
                request.form["rating"],
                request.form["comments"].strip(),
            ),
        )
        db.commit()
        flash("Thank you for your feedback.", "success")
        return redirect(url_for("home"))

    return render_template("feedback.html", packages=user_packages)


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        "users": db.execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0],
        "packages": db.execute("SELECT COUNT(*) FROM packages").fetchone()[0],
        "hotels": db.execute("SELECT COUNT(*) FROM hotels").fetchone()[0],
        "destinations": db.execute("SELECT COUNT(*) FROM destinations").fetchone()[0],
        "bookings": db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0],
        "revenue": db.execute("SELECT COALESCE(SUM(total_amount), 0) FROM bookings").fetchone()[0],
    }
    reports = {
        "pending_bookings": db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Pending'").fetchone()[0],
        "confirmed_bookings": db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Confirmed'").fetchone()[0],
        "cancelled_bookings": db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Cancelled'").fetchone()[0],
        "paid_payments": db.execute("SELECT COUNT(*) FROM payments WHERE status = 'Paid'").fetchone()[0],
        "refund_requests": db.execute("SELECT COUNT(*) FROM payments WHERE refund_status = 'Requested'").fetchone()[0],
        "failed_payments": db.execute("SELECT COUNT(*) FROM payments WHERE status = 'Failed'").fetchone()[0],
        "refunded_payments": db.execute("SELECT COUNT(*) FROM payments WHERE status = 'Refunded'").fetchone()[0],
    }
    bookings = db.execute(
        """
        SELECT bookings.*, users.name, packages.title, payments.id AS payment_id,
               payments.transaction_id, payments.method, payments.status AS payment_status,
               payments.refund_status
        FROM bookings
        JOIN users ON users.id = bookings.user_id
        JOIN packages ON packages.id = bookings.package_id
        LEFT JOIN payments ON payments.booking_id = bookings.id
        ORDER BY bookings.id DESC
        """
    ).fetchall()
    packages = prepare_packages_for_display(
        db.execute("SELECT * FROM packages ORDER BY id DESC").fetchall()
    )
    hotels = db.execute("SELECT * FROM hotels ORDER BY destination, rating DESC").fetchall()
    destinations = db.execute("SELECT * FROM destinations ORDER BY name").fetchall()
    feedback_list = db.execute(
        """
        SELECT feedback.*, users.name, users.email, packages.title
        FROM feedback
        JOIN users ON users.id = feedback.user_id
        JOIN packages ON packages.id = feedback.package_id
        ORDER BY feedback.id DESC
        """
    ).fetchall()
    payments = db.execute(
        """
        SELECT payments.*, bookings.total_amount, bookings.status AS booking_status,
               packages.title, users.name
        FROM payments
        JOIN bookings ON bookings.id = payments.booking_id
        JOIN packages ON packages.id = bookings.package_id
        JOIN users ON users.id = bookings.user_id
        ORDER BY payments.id DESC
        """
    ).fetchall()
    users = db.execute("SELECT id, name, email, created_at FROM users WHERE role = 'user'").fetchall()
    return render_template(
        "admin.html",
        stats=stats,
        reports=reports,
        bookings=bookings,
        packages=packages,
        hotels=hotels,
        destinations=destinations,
        payments=payments,
        users=users,
        feedback_list=feedback_list,
        room_type_choices=ROOM_TYPE_CHOICES,
    )


@app.route("/admin/packages", methods=["POST"])
@login_required
@admin_required
def add_package():
    try:
        image_url = save_package_image(request.files.get("package_image"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect("/admin#manage-packages")

    image_url = image_url or request.form.get("image_url", "").strip()
    if not image_url:
        flash("Add an image URL or upload a package image.", "error")
        return redirect("/admin#manage-packages")

    get_db().execute(
        """
        INSERT INTO packages
        (title, destination, category, duration, price, transport_price, food_price,
         boarding_point, seats_available, itinerary, offer, image_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request.form["title"].strip(),
            request.form["destination"].strip(),
            request.form["category"],
            request.form["duration"].strip(),
            request.form["price"],
            request.form.get("transport_price", 0),
            request.form.get("food_price", 0),
            request.form.get("boarding_point", "").strip(),
            request.form["seats_available"],
            request.form["itinerary"].strip(),
            request.form["offer"].strip(),
            image_url,
        ),
    )
    get_db().commit()
    flash("Package added successfully.", "success")
    return redirect("/admin#manage-packages")


@app.route("/admin/packages/<int:package_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_package(package_id):
    try:
        uploaded_image_url = save_package_image(request.files.get("package_image"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect("/admin#manage-packages")

    image_url = uploaded_image_url or request.form.get("image_url", "").strip()
    if not image_url:
        existing = get_db().execute("SELECT image_url FROM packages WHERE id = ?", (package_id,)).fetchone()
        image_url = existing["image_url"] if existing else ""

    get_db().execute(
        """
        UPDATE packages
        SET title = ?, destination = ?, category = ?, duration = ?, price = ?,
            transport_price = ?, food_price = ?, boarding_point = ?, seats_available = ?,
            itinerary = ?, offer = ?, image_url = ?
        WHERE id = ?
        """,
        (
            request.form["title"].strip(),
            request.form["destination"].strip(),
            request.form["category"],
            request.form["duration"].strip(),
            request.form["price"],
            request.form.get("transport_price", 0),
            request.form.get("food_price", 0),
            request.form.get("boarding_point", "").strip(),
            request.form["seats_available"],
            request.form["itinerary"].strip(),
            request.form["offer"].strip(),
            image_url,
            package_id,
        ),
    )
    get_db().commit()
    flash("Package updated successfully.", "success")
    return redirect("/admin#manage-packages")


@app.route("/admin/packages/<int:package_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_package(package_id):
    booking_count = get_db().execute(
        "SELECT COUNT(*) FROM bookings WHERE package_id = ?",
        (package_id,),
    ).fetchone()[0]
    if booking_count:
        flash("This package has bookings, so it cannot be deleted.", "warning")
        return redirect("/admin#manage-packages")

    get_db().execute("DELETE FROM packages WHERE id = ?", (package_id,))
    get_db().commit()
    flash("Package deleted.", "success")
    return redirect("/admin#manage-packages")


@app.route("/admin/hotels", methods=["POST"])
@app.route("/admin/add_hotel", methods=["POST"])
@app.route("/add_hotel", methods=["POST"])
@login_required
@admin_required
def add_hotel():
    try:
        name = request.form.get("name", "").strip()
        destination = request.form.get("destination", "").strip()
        rooms_available = request.form.get("rooms_available", "").strip()
        rating = request.form.get("rating", "").strip()
        facilities = request.form.get("facilities", "").strip()
        room_type, price_per_night, room_options = build_room_config_from_form(request.form)

        if not all([name, destination, rooms_available, rating]):
            flash("Fill all hotel fields before adding.", "error")
            return redirect("/admin#manage-hotels")

        destination_exists = get_db().execute(
            "SELECT COUNT(*) FROM destinations WHERE LOWER(name) = LOWER(?)",
            (destination,),
        ).fetchone()[0]
        if not destination_exists:
            flash("Add the destination first, then add hotels related to that destination.", "error")
            return redirect("/admin#manage-hotels")

        get_db().execute(
            """
            INSERT INTO hotels
            (name, destination, room_type, price_per_night, rooms_available, rating, facilities, room_options)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, destination, room_type, price_per_night, rooms_available, rating, facilities, room_options),
        )
        get_db().commit()
        flash(f"Hotel '{name}' added to the hotel management table.", "success")
    except (sqlite3.Error, ValueError) as error:
        flash(f"Hotel was not added: {error}", "error")
    return redirect("/admin#manage-hotels")


@app.route("/admin/hotels/<int:hotel_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_hotel(hotel_id):
    get_db().execute(
        """
        UPDATE hotels
        SET name = ?, destination = ?, room_type = ?, price_per_night = ?,
            rooms_available = ?, rating = ?, facilities = ?, room_options = ?
        WHERE id = ?
        """,
        (
            request.form["name"].strip(),
            request.form["destination"].strip(),
            request.form["room_type"].strip(),
            request.form["price_per_night"],
            request.form["rooms_available"],
            request.form["rating"],
            request.form.get("facilities", "").strip(),
            request.form.get("room_options", "").strip(),
            hotel_id,
        ),
    )
    get_db().commit()
    flash("Hotel updated successfully.", "success")
    return redirect("/admin#manage-hotels")


@app.route("/admin/hotels/<int:hotel_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_hotel(hotel_id):
    get_db().execute("UPDATE bookings SET hotel_id = NULL WHERE hotel_id = ?", (hotel_id,))
    get_db().execute("DELETE FROM hotels WHERE id = ?", (hotel_id,))
    get_db().commit()
    flash("Hotel deleted. Existing bookings were kept.", "success")
    return redirect("/admin#manage-hotels")


@app.route("/admin/destinations", methods=["POST"])
@app.route("/admin/add_destination", methods=["POST"])
@app.route("/add_destination", methods=["POST"])
@login_required
@admin_required
def add_destination():
    try:
        name = request.form.get("name", "").strip()
        country = request.form.get("country", "").strip()
        best_season = request.form.get("best_season", "").strip()
        description = request.form.get("description", "").strip()
        image_url = request.form.get("image_url", "").strip()
        if not image_url:
            image_url = "https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=1200&q=80"

        if not all([name, country, best_season, description]):
            flash("Fill all destination fields before adding.", "error")
            return redirect("/admin#manage-destinations")

        get_db().execute(
            """
            INSERT INTO destinations
            (name, country, best_season, description, image_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, country, best_season, description, image_url),
        )
        get_db().commit()
        flash(f"Destination '{name}' added to the destination management table.", "success")
    except sqlite3.Error as error:
        flash(f"Destination was not added: {error}", "error")
    return redirect("/admin#manage-destinations")


@app.route("/admin/destinations/<int:destination_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_destination(destination_id):
    get_db().execute(
        """
        UPDATE destinations
        SET name = ?, country = ?, best_season = ?, description = ?, image_url = ?
        WHERE id = ?
        """,
        (
            request.form["name"].strip(),
            request.form["country"].strip(),
            request.form["best_season"].strip(),
            request.form["description"].strip(),
            request.form["image_url"].strip(),
            destination_id,
        ),
    )
    get_db().commit()
    flash("Destination updated successfully.", "success")
    return redirect("/admin#manage-destinations")


@app.route("/admin/destinations/<int:destination_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_destination(destination_id):
    get_db().execute("DELETE FROM destinations WHERE id = ?", (destination_id,))
    get_db().commit()
    flash("Destination deleted.", "success")
    return redirect("/admin#manage-destinations")


@app.route("/admin/payments/<int:payment_id>/status", methods=["POST"])
@login_required
@admin_required
def update_payment_status(payment_id):
    status = request.form["status"]
    if status not in {"Paid", "Pending", "Failed", "Refund Requested", "Refunded"}:
        flash("Invalid payment status.", "error")
        return redirect("/admin")

    if status == "Refunded":
        get_db().execute(
            """
            UPDATE payments
            SET status = ?, refund_status = 'Processed', refunded_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, payment_id),
        )
    elif status == "Refund Requested":
        get_db().execute(
            """
            UPDATE payments
            SET status = ?, refund_status = 'Requested'
            WHERE id = ?
            """,
            (status, payment_id),
        )
    else:
        get_db().execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
    get_db().commit()
    flash("Payment status updated.", "success")
    return redirect("/admin")


@app.route("/admin/payments/<int:payment_id>/refund/<action>", methods=["POST"])
@login_required
@admin_required
def process_refund(payment_id, action):
    if action not in {"approve", "reject"}:
        flash("Invalid refund action.", "error")
        return redirect("/admin")

    db = get_db()
    payment = db.execute(
        """
        SELECT payments.*, bookings.package_id, bookings.travelers, bookings.status AS booking_status
        FROM payments
        JOIN bookings ON bookings.id = payments.booking_id
        WHERE payments.id = ?
        """,
        (payment_id,),
    ).fetchone()
    if payment is None:
        flash("Payment record not found.", "error")
        return redirect("/admin")

    if action == "approve":
        db.execute(
            """
            UPDATE payments
            SET status = 'Refunded', refund_status = 'Processed', refunded_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payment_id,),
        )
        if payment["booking_status"] != "Cancelled":
            db.execute(
                "UPDATE bookings SET status = 'Cancelled' WHERE id = ?",
                (payment["booking_id"],),
            )
            db.execute(
                "UPDATE packages SET seats_available = seats_available + ? WHERE id = ?",
                (payment["travelers"], payment["package_id"]),
            )
        flash("Refund processed successfully.", "success")
    else:
        db.execute(
            """
            UPDATE payments
            SET status = 'Paid', refund_status = 'Rejected'
            WHERE id = ?
            """,
            (payment_id,),
        )
        flash("Refund request rejected.", "success")

    db.commit()
    return redirect("/admin")


@app.route("/admin/reports/download")
@login_required
@admin_required
def download_admin_report():
    db = get_db()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report", "Value"])
    writer.writerow(["Registered Users", db.execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0]])
    writer.writerow(["Tour Packages", db.execute("SELECT COUNT(*) FROM packages").fetchone()[0]])
    writer.writerow(["Hotels", db.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]])
    writer.writerow(["Destinations", db.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]])
    writer.writerow(["Total Bookings", db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]])
    writer.writerow(["Pending Bookings", db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Pending'").fetchone()[0]])
    writer.writerow(["Confirmed Bookings", db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Confirmed'").fetchone()[0]])
    writer.writerow(["Cancelled Bookings", db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'Cancelled'").fetchone()[0]])
    writer.writerow(["Refund Requests", db.execute("SELECT COUNT(*) FROM payments WHERE refund_status = 'Requested'").fetchone()[0]])
    writer.writerow(["Refunded Payments", db.execute("SELECT COUNT(*) FROM payments WHERE status = 'Refunded'").fetchone()[0]])
    writer.writerow(["Total Revenue", db.execute("SELECT COALESCE(SUM(total_amount), 0) FROM bookings").fetchone()[0]])
    writer.writerow([])
    writer.writerow(["Booking ID", "User", "Package", "Travelers", "Amount", "Booking Status", "Payment Status", "Refund Status", "Created"])

    bookings = db.execute(
        """
        SELECT bookings.*, users.name, packages.title, payments.status AS payment_status,
               payments.refund_status
        FROM bookings
        JOIN users ON users.id = bookings.user_id
        JOIN packages ON packages.id = bookings.package_id
        LEFT JOIN payments ON payments.booking_id = bookings.id
        ORDER BY bookings.id DESC
        """
    ).fetchall()
    for booking in bookings:
        writer.writerow(
            [
                booking["id"],
                booking["name"],
                booking["title"],
                booking["travelers"],
                booking["total_amount"],
                booking["status"],
                booking["payment_status"] or "Not recorded",
                booking["refund_status"] or "Not Requested",
                booking["created_at"],
            ]
        )

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=tourism_admin_report.csv"
    response.headers["Content-Type"] = "text/csv"
    return response


@app.route("/admin/bookings/<int:booking_id>/approve", methods=["POST"])
@login_required
@admin_required
def approve_booking(booking_id):
    db = get_db()
    booking = db.execute(
        """
        SELECT bookings.*, packages.seats_available
        FROM bookings
        JOIN packages ON packages.id = bookings.package_id
        WHERE bookings.id = ?
        """,
        (booking_id,),
    ).fetchone()
    if booking and booking["status"] == "Cancelled":
        if booking["travelers"] > booking["seats_available"]:
            flash("Not enough seats are available to approve this cancelled booking.", "error")
            return redirect("/admin")
        db.execute(
            "UPDATE packages SET seats_available = seats_available - ? WHERE id = ?",
            (booking["travelers"], booking["package_id"]),
        )
    db.execute("UPDATE bookings SET status = 'Confirmed' WHERE id = ?", (booking_id,))
    db.commit()
    flash("Booking approved.", "success")
    return redirect("/admin")


@app.route("/admin/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
@admin_required
def cancel_booking(booking_id):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        flash("Booking not found.", "error")
        return redirect("/admin")

    if booking["status"] != "Cancelled":
        db.execute("UPDATE bookings SET status = 'Cancelled' WHERE id = ?", (booking_id,))
        db.execute(
            "UPDATE packages SET seats_available = seats_available + ? WHERE id = ?",
            (booking["travelers"], booking["package_id"]),
        )

    refund_action = request.form.get("refund_action", "none")
    if request.form.get("refund_paid") == "1":
        refund_action = "request"

    if refund_action not in {"none", "request", "process"}:
        flash("Invalid refund option.", "error")
        db.commit()
        return redirect("/admin")

    if refund_action in {"request", "process"}:
        payment = db.execute(
            """
            SELECT * FROM payments
            WHERE booking_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (booking_id,),
        ).fetchone()
        if payment is None:
            flash("Booking cancelled, but no payment record was found for refund.", "warning")
        elif payment["status"] != "Paid":
            flash("Booking cancelled, but only paid transactions can be refunded.", "warning")
        elif payment["refund_status"] in {"Requested", "Processed"}:
            flash("Booking cancelled. Refund is already in progress.", "warning")
        elif refund_action == "process":
            db.execute(
                """
                UPDATE payments
                SET status = 'Refunded',
                    refund_status = 'Processed',
                    refund_reason = 'Admin cancelled booking and processed refund.',
                    refunded_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payment["id"],),
            )
            flash("Booking cancelled and refund processed.", "success")
            db.commit()
            return redirect("/admin")
        else:
            db.execute(
                """
                UPDATE payments
                SET status = 'Refund Requested',
                    refund_status = 'Requested',
                    refund_reason = 'Admin cancelled booking and requested refund.'
                WHERE id = ?
                """,
                (payment["id"],),
            )
            flash("Booking cancelled and refund request created.", "success")
            db.commit()
            return redirect("/admin")

    db.commit()
    flash("Booking cancelled.", "success")
    return redirect("/admin")


initialize_database()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=True, use_reloader=False, port=port)
