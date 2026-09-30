from datetime import datetime, timezone
import csv
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3
import uuid
import jwt
from flask import Flask, g, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "complaints.db"
MENU_DATA_PATH = BASE_DIR / "sushi_order_items_clean.csv"
UPLOAD_FOLDER = BASE_DIR / "uploads"
JWT_SECRET = os.environ.get("BUNDU_JWT_SECRET", "change-this-development-secret-key")
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE + 1024 * 1024
CORS(app)

VALID_STATUSES = {"Open", "Under Review", "Waiting for Customer", "Resolved"}
VALID_CATEGORIES = {"Wrong order", "Missing item", "Food quality", "Food temperature", "Long waiting time", "Poor service", "Incorrect bill", "Payment problem", "Hygiene concern", "Other"}
VALID_SEVERITIES = {"General feedback", "Problem with my order", "Urgent issue"}
VALID_ORDER_STATUSES = {"Received", "Preparing", "Ready", "Completed", "Cancelled"}
VALID_ORDER_TYPES = {"Dine in", "Takeaway", "Delivery"}
TABLE_NUMBERS = set(chr(i) for i in range(ord('A'), ord('Z') + 1))
VALID_ROLES = {"waiter", "manager"}
MAX_MANAGERS = 3

MENU_PRESENTATION = {
    "roll": ("🍣", "Freshly rolled favourites made with seasoned rice and quality fillings."),
    "nigiri": ("🍣", "Hand-shaped sushi rice topped with fresh, carefully prepared fish."),
    "side": ("🥢", "A comforting side to complete your Bundu dining experience."),
    "drink": ("🥤", "A refreshing drink selected to pair beautifully with your meal."),
}


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def load_menu_items_from_csv():
    menu_items = {}
    with MENU_DATA_PATH.open(newline="", encoding="utf-8") as menu_file:
        for row in csv.DictReader(menu_file):
            name = row["item"].strip()
            category = row["category"].strip().lower()
            icon, description = MENU_PRESENTATION.get(
                category,
                ("🍽", "A Bundu kitchen favourite, prepared fresh to order."),
            )
            menu_items[name] = (
                name,
                category.title(),
                description,
                float(row["unit_price"]),
                icon,
            )
    return list(menu_items.values())


def initialise_database():
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firstname TEXT NOT NULL,
                lastname TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('waiter', 'manager')),
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL CHECK (price >= 0),
                icon TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_item_id INTEGER NOT NULL,
                discount_percent INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_name TEXT NOT NULL,
                role TEXT NOT NULL,
                shift_label TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                items TEXT NOT NULL,
                total REAL NOT NULL CHECK (total >= 0),
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for table, column in [
            ("menu_items", "image TEXT"),
            ("orders", "order_number TEXT"),
            ("orders", "bill_plan TEXT NOT NULL DEFAULT 'pay_individual'"),
            ("orders", "waiter_id INTEGER"),
            ("orders", "order_type TEXT NOT NULL DEFAULT 'Dine in'"),
            ("orders", "table_number INTEGER"),
            ("orders", "rating INTEGER"),
            ("complaints", "order_id INTEGER"),
            ("complaints", "order_number TEXT"),
            ("complaints", "category TEXT"),
            ("complaints", "severity TEXT"),
            ("complaints", "description TEXT"),
            ("complaints", "waiter_id INTEGER"),
            ("complaints", "manager_id INTEGER"),
            ("complaints", "reference TEXT"),
            ("complaints", "identity_mode TEXT NOT NULL DEFAULT 'anonymous'"),
            ("complaints", "manager_response TEXT"),
            ("complaints", "responded_at TEXT"),
            ("complaints", "resolved_at TEXT"),
        ]:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        connection.execute("UPDATE orders SET status = 'Received' WHERE status = 'Pending'")
        connection.execute("UPDATE orders SET status = 'Preparing' WHERE status = 'In progress'")

        menu_items = load_menu_items_from_csv()
        connection.execute("DELETE FROM menu_items")
        connection.executemany(
            """
            INSERT OR IGNORE INTO menu_items
                (name, category, description, price, icon)
            VALUES (?, ?, ?, ?, ?)
            """,
            menu_items,
        )
        connection.execute(
            "UPDATE menu_items SET image = CASE LOWER(category) "
            "WHEN 'roll' THEN 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=900&q=85' "
            "WHEN 'nigiri' THEN 'https://images.unsplash.com/photo-1611143669185-af224c5e3252?auto=format&fit=crop&w=900&q=85' "
            "WHEN 'drink' THEN 'https://images.unsplash.com/photo-1544145945-f90425340c7e?auto=format&fit=crop&w=900&q=85' "
            "ELSE 'https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=900&q=85' END "
            "WHERE image IS NULL OR image = ''"
        )


def complaint_to_dict(row):
    return dict(row)


def user_to_dict(row):
    return {
        "id": row["id"],
        "firstname": row["firstname"],
        "lastname": row["lastname"],
        "email": row["email"],
        "role": row["role"],
    }


def order_to_dict(row):
    order = dict(row)
    order["items"] = json.loads(order["items"])
    return order


def public_order(row):
    order = order_to_dict(row)
    order["order_number"] = order.get("order_number") or f"BDU-{order['id']:05d}"
    return order


def create_token(user):
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "email": user["email"],
            "role": user["role"],
            "exp": datetime.now(timezone.utc).timestamp() + 86400,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return jsonify({"error": "A valid Bearer token is required"}), 401

        try:
            g.current_user = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.exceptions.PyJWTError:
            return jsonify({"error": "Invalid or expired token"}), 401

        return view(*args, **kwargs)

    return wrapped


def require_role(role):
    def decorator(view):
        @wraps(view)
        @require_auth
        def wrapped(*args, **kwargs):
            if g.current_user.get("role") != role:
                return jsonify({"error": "You do not have permission to access this resource"}), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.post("/api/auth/register")
def register_user():
    payload = request.get_json(silent=True) or {}
    firstname = str(payload.get("firstname", "")).strip()
    lastname = str(payload.get("lastname", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "")).strip().lower()

    if not firstname or not lastname or not email or len(password) < 6 or role not in VALID_ROLES:
        return jsonify({"error": "firstname, lastname, email, password and a valid role are required"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if role == "manager":
                manager_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'manager'"
                ).fetchone()[0]
                if manager_count >= MAX_MANAGERS:
                    return jsonify({
                        "error": "Manager registration is closed because the maximum of two managers has been reached"
                    }), 409

            connection.execute(
                """
                INSERT INTO users (firstname, lastname, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (firstname, lastname, email, generate_password_hash(password), role, timestamp),
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with that email already exists"}), 409

    return jsonify({"message": "Account created successfully"}), 201


@app.post("/api/auth/login")
def login_user():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))

    with get_connection() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password"}), 401

    return jsonify({"token": create_token(user), "user": user_to_dict(user)})


@app.get("/api/menu-items")
def list_menu_items():
    with get_connection() as connection:
        menu_items = connection.execute(
            "SELECT * FROM menu_items ORDER BY id"
        ).fetchall()

    return jsonify([dict(menu_item) for menu_item in menu_items])


@app.post("/api/menu-items")
@require_role("manager")
def create_menu_item():
    payload = request.form if request.mimetype == "multipart/form-data" else (request.get_json(silent=True) or {})
    name = str(payload.get("name", "")).strip()
    category = str(payload.get("category", "Side")).strip() or "Side"
    description = str(payload.get("description", "Freshly prepared Bundu favourite.")).strip()
    image = str(payload.get("image", "")).strip()
    uploaded_image = request.files.get("image") if request.mimetype == "multipart/form-data" else None
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid price is required"}), 400
    if not name or price < 0:
        return jsonify({"error": "Name and a valid price are required"}), 400
    if uploaded_image and uploaded_image.filename:
        safe_filename = secure_filename(uploaded_image.filename)
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS or not uploaded_image.mimetype.startswith("image/"):
            return jsonify({"error": "Upload a JPG, PNG, WebP or GIF image"}), 400
        contents = uploaded_image.read(MAX_IMAGE_SIZE + 1)
        if len(contents) > MAX_IMAGE_SIZE:
            return jsonify({"error": "Images must be 5 MB or smaller"}), 413
        UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        image_path = UPLOAD_FOLDER / filename
        image_path.write_bytes(contents)
        image = f"/uploads/{filename}"
    try:
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO menu_items (name, category, description, price, icon, image) VALUES (?, ?, ?, ?, ?, ?)",
                (name, category, description, price, "🍽", image),
            )
            item = connection.execute("SELECT * FROM menu_items WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        return jsonify({"error": "A menu item with that name already exists"}), 409
    return jsonify(dict(item)), 201


@app.get("/uploads/<path:filename>")
def uploaded_menu_image(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.delete("/api/menu-items/<int:item_id>")
@require_role("manager")
def delete_menu_item(item_id):
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    if cursor.rowcount == 0:
        return jsonify({"error": "Menu item not found"}), 404
    return jsonify({"message": "Menu item deleted"})


@app.get("/api/promotions")
@require_role("manager")
def list_promotions():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT promotions.*, menu_items.name AS menu_name FROM promotions "
            "JOIN menu_items ON menu_items.id = promotions.menu_item_id ORDER BY promotions.created_at DESC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/promotions/active")
def list_active_promotions():
    current_time = datetime.now().astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT promotions.*, menu_items.name AS menu_name, menu_items.price AS original_price
            FROM promotions
            JOIN menu_items ON menu_items.id = promotions.menu_item_id
            WHERE promotions.start_at <= ? AND promotions.end_at >= ?
            ORDER BY promotions.discount_percent DESC, promotions.created_at DESC
            """,
            (current_time, current_time),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/promotions")
@require_role("manager")
def create_promotion():
    payload = request.get_json(silent=True) or {}
    try:
        menu_item_id = int(payload.get("menu_item_id"))
        discount_percent = int(payload.get("discount_percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "A menu item and valid discount are required"}), 400
    start_at = str(payload.get("start_at", "")).strip()
    end_at = str(payload.get("end_at", "")).strip()
    if not 1 <= discount_percent <= 100 or not start_at or not end_at:
        return jsonify({"error": "Discount and a valid promotion period are required"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        item = connection.execute("SELECT id FROM menu_items WHERE id = ?", (menu_item_id,)).fetchone()
        if not item:
            return jsonify({"error": "Menu item not found"}), 404
        cursor = connection.execute(
            "INSERT INTO promotions (menu_item_id, discount_percent, start_at, end_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (menu_item_id, discount_percent, start_at, end_at, timestamp),
        )
        promotion = connection.execute(
            "SELECT promotions.*, menu_items.name AS menu_name FROM promotions JOIN menu_items ON menu_items.id = promotions.menu_item_id WHERE promotions.id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return jsonify(dict(promotion)), 201


@app.get("/api/shifts")
@require_role("manager")
def list_shifts():
    with get_connection() as connection:
        shifts = connection.execute("SELECT * FROM shifts ORDER BY created_at DESC").fetchall()
    return jsonify([dict(shift) for shift in shifts])


@app.post("/api/shifts")
@require_role("manager")
def create_shift():
    payload = request.get_json(silent=True) or {}
    staff_name = str(payload.get("staff_name", "")).strip()
    role = str(payload.get("role", "Waiter")).strip()
    shift_label = str(payload.get("shift_label", "")).strip()
    status = str(payload.get("status", "Scheduled")).strip()
    if not staff_name or not shift_label:
        return jsonify({"error": "Staff member and shift are required"}), 400
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO shifts (staff_name, role, shift_label, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (staff_name, role, shift_label, status, datetime.now(timezone.utc).isoformat()),
        )
        shift = connection.execute("SELECT * FROM shifts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(dict(shift)), 201


@app.post("/api/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    customer_name = str(payload.get("customer_name", "Guest customer")).strip() or "Guest customer"
    customer_email = str(payload.get("customer_email", "Not provided")).strip().lower() or "Not provided"
    order_type = str(payload.get("order_type", "Dine in")).strip()
    bill_plan = str(payload.get("bill_plan", "pay_individual")).strip().lower() or "pay_individual"
    items = payload.get("items")

    if not isinstance(items, list) or not items:
        return jsonify({"error": "At least one item is required"}), 400
    if order_type not in VALID_ORDER_TYPES:
        return jsonify({"error": "Choose Dine in or Takeaway"}), 400
    if order_type == "Delivery":
        return jsonify({"error": "We currently don't offer delivery"}), 400
    if bill_plan not in {"split_bill", "pay_individual"}:
        bill_plan = "pay_individual"

    clean_items = []
    total = 0
    current_time = datetime.now().astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    for item in items:
        name = str(item.get("name", "")).strip()
        try:
            price = float(item.get("price"))
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            return jsonify({"error": "Each order item must have a valid price and quantity"}), 400
        if not name or price < 0 or quantity < 1:
            return jsonify({"error": "Each order item must have a valid name, price and quantity"}), 400
        with get_connection() as connection:
            promotion = connection.execute(
                """
                SELECT menu_items.price, promotions.discount_percent
                FROM menu_items
                JOIN promotions ON promotions.menu_item_id = menu_items.id
                WHERE menu_items.name = ? AND promotions.start_at <= ? AND promotions.end_at >= ?
                ORDER BY promotions.discount_percent DESC, promotions.created_at DESC
                LIMIT 1
                """,
                (name, current_time, current_time),
            ).fetchone()
        if promotion:
            price = round(promotion["price"] * (100 - promotion["discount_percent"]) / 100, 2)
        clean_items.append({"name": name, "price": price, "quantity": quantity})
        total += price * quantity

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO orders
                (customer_name, customer_email, items, total, status, order_type, bill_plan, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'Received', ?, ?, ?, ?)
            """,
            (customer_name, customer_email, json.dumps(clean_items), total, order_type, bill_plan, timestamp, timestamp),
        )
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()
        connection.execute("UPDATE orders SET order_number = ? WHERE id = ?", (f"BDU-{order['id']:05d}", order['id']))
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()

    return jsonify(public_order(order)), 201


@app.get("/api/orders")
def list_orders():
    with get_connection() as connection:
        active = request.args.get('active', '').lower() == 'true'
        if active:
            orders = connection.execute("SELECT * FROM orders WHERE status NOT IN ('Completed', 'Cancelled') ORDER BY created_at DESC").fetchall()
        else:
            orders = connection.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return jsonify([public_order(order) for order in orders])


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(public_order(order))


@app.post("/api/orders/<int:order_id>/rating")
def rate_order(order_id):
    payload = request.get_json(silent=True) or {}
    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int):
        return jsonify({"error": "Choose a rating from 1 to 5"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"error": "Choose a rating from 1 to 5"}), 400

    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order["status"] != "Completed":
            return jsonify({"error": "You can rate an order after it is completed"}), 409
        if order["rating"] is not None:
            return jsonify({"error": "This order has already been rated"}), 409
        cursor = connection.execute(
            "UPDATE orders SET rating = ? WHERE id = ? AND status = 'Completed' AND rating IS NULL",
            (rating, order_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "This order has already been rated"}), 409
        updated_order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    return jsonify(public_order(updated_order))


@app.patch("/api/orders/<int:order_id>")
@require_auth
def update_order(order_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in VALID_ORDER_STATUSES:
        return jsonify({"error": "status must be Received, Preparing, Ready, Completed or Cancelled"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, order_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Order not found"}), 404
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(public_order(order))


@app.get("/api/tables/available")
def available_tables():
    with get_connection() as connection:
        occupied = connection.execute(
            "SELECT table_number FROM orders WHERE table_number IS NOT NULL AND status NOT IN ('Completed', 'Cancelled')"
        ).fetchall()
    occupied_numbers = {row["table_number"] for row in occupied}
    return jsonify([table for table in TABLE_NUMBERS if table not in occupied_numbers])


@app.patch("/api/orders/<int:order_id>/table")
@require_auth
def assign_order_table(order_id):
    payload = request.get_json(silent=True) or {}
    try:
        table_number = int(payload.get("table_number"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid table number is required"}), 400
    if table_number not in TABLE_NUMBERS:
        return jsonify({"error": "That table does not exist"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order["order_type"] != "Dine in":
            return jsonify({"error": "Only dine-in orders can be assigned a table"}), 400
        occupied = connection.execute(
            "SELECT id FROM orders WHERE table_number = ? AND id != ? AND status NOT IN ('Completed', 'Cancelled')",
            (table_number, order_id),
        ).fetchone()
        if occupied:
            return jsonify({"error": "That table is currently occupied"}), 409
        connection.execute(
            "UPDATE orders SET table_number = ?, updated_at = ? WHERE id = ?",
            (table_number, timestamp, order_id),
        )
        updated = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(public_order(updated))


@app.get("/")
@app.get("/dashboard/")
@app.get("/dashboard/<section>")
def dashboard(section=None):
    """Render the staff dashboard with the given section"""
    valid_sections = ["service", "orders", "tables", "menu", "pantry", "shifts", "complaints"]
    if section is None:
        current_section = "complaints"
    else:
        current_section = section.lower() if section in valid_sections else "complaints"
    return render_template("dashboard.html", current_section=current_section)


@app.get("/api/complaints")
@require_role("manager")
def list_complaints():
    status = request.args.get("status")
    query = """SELECT complaints.*, orders.order_number, orders.created_at AS order_created_at,
               orders.items, orders.total, orders.status AS order_status,
               waiter.firstname AS waiter_firstname, waiter.lastname AS waiter_lastname
               FROM complaints LEFT JOIN orders ON orders.id = complaints.order_id
               LEFT JOIN users AS waiter ON waiter.id = complaints.waiter_id"""
    parameters = []

    if status in VALID_STATUSES:
        query += " WHERE complaints.status = ?"
        parameters.append(status)

    query += " ORDER BY created_at DESC"
    with get_connection() as connection:
        complaints = connection.execute(query, parameters).fetchall()

    return jsonify([complaint_to_dict(complaint) for complaint in complaints])


@app.post("/api/complaints")
def create_complaint():
    payload = request.get_json(silent=True) or {}
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid order is required"}), 400
    category = str(payload.get("category", "")).strip()
    severity = str(payload.get("severity", "Problem with my order")).strip()
    description = str(payload.get("description", "")).strip()
    identity_mode = str(payload.get("identity_mode", "anonymous")).strip().lower()
    customer_email = str(payload.get("customer_email", "")).strip().lower()
    if category not in VALID_CATEGORIES or severity not in VALID_SEVERITIES or not 10 <= len(description) <= 2000:
        return jsonify({"error": "Choose a valid category and severity, and describe the issue in 10 to 2000 characters"}), 400
    if identity_mode not in {"anonymous", "add_email"}:
        identity_mode = "anonymous"
    if identity_mode == "add_email" and not customer_email:
        return jsonify({"error": "Enter an email address or choose Anonymous"}), 400
    if identity_mode == "anonymous":
        customer_email = "Not provided"

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return jsonify({"error": "Order not found"}), 404
        waiter = connection.execute("SELECT id FROM users WHERE role = 'waiter' ORDER BY id LIMIT 1").fetchone()
        manager = connection.execute("SELECT id FROM users WHERE role = 'manager' ORDER BY id LIMIT 1").fetchone()
        reference = f"CMP-{datetime.now(timezone.utc).year}-{order_id:05d}"
        cursor = connection.execute(
            """
            INSERT INTO complaints
                (customer_name, customer_email, message, status, created_at, updated_at,
                  order_id, order_number, category, severity, description, waiter_id, manager_id, reference, identity_mode)
              VALUES (?, ?, ?, 'Open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order["customer_name"], customer_email, description, timestamp, timestamp,
             order_id, order["order_number"] or f"BDU-{order_id:05d}", category, severity, description,
               waiter["id"] if waiter else None, manager["id"] if manager else None, reference, identity_mode),
        )
        complaint = connection.execute(
            """SELECT complaints.*, orders.order_number, orders.created_at AS order_created_at,
               orders.items, waiter.firstname AS waiter_firstname, waiter.lastname AS waiter_lastname
               FROM complaints LEFT JOIN orders ON orders.id = complaints.order_id
               LEFT JOIN users AS waiter ON waiter.id = complaints.waiter_id
               WHERE complaints.id = ?""", (cursor.lastrowid,)
        ).fetchone()

    return jsonify(complaint_to_dict(complaint)), 201


@app.patch("/api/complaints/<int:complaint_id>")
def update_complaint(complaint_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")

    if status not in VALID_STATUSES:
        return jsonify({"error": "Invalid complaint status"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        existing = connection.execute("SELECT id FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if not existing:
            return jsonify({"error": "Complaint not found"}), 404
        connection.execute(
            "UPDATE complaints SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
            (status, timestamp, timestamp if status == "Resolved" else None, complaint_id),
        )
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()

    return jsonify(complaint_to_dict(complaint))


@app.post("/api/complaints/<int:complaint_id>/response")
@require_auth
def respond_to_complaint(complaint_id):
    response_text = str((request.get_json(silent=True) or {}).get("response", "")).strip()
    if not 1 <= len(response_text) <= 2000:
        return jsonify({"error": "A response between 1 and 2000 characters is required"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        complaint = connection.execute("SELECT id FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if not complaint:
            return jsonify({"error": "Complaint not found"}), 404
        connection.execute(
            "UPDATE complaints SET manager_response = ?, responded_at = ?, status = 'Resolved', updated_at = ?, resolved_at = ? WHERE id = ?",
            (response_text, timestamp, timestamp, timestamp, complaint_id),
        )
        updated = connection.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
    return jsonify(complaint_to_dict(updated))


@app.get("/api/health")
def health_check():
    return jsonify({"status": "ok"})

initialise_database()

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)