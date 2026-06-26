"""
Cloud-Based Bus Pass / Ticket Booking System — v2
===================================================
Run directly in VS Code (Run button / F5). No terminal typing required
beyond the one-time `pip install -r requirements.txt`.

New in this version:
- Seat selection (pick actual seat numbers, not just a count)
- Payment mock step before a booking is confirmed
- Downloadable PDF e-ticket (reportlab, generated on the fly)
- Route search/filter (source, destination)
- Admin booking filters (status, route, date) + extra stats
"""

import io
import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    send_from_directory, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdf_canvas

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "bus_pass_v2.db")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")

app = Flask(__name__)
app.secret_key = "change-this-secret-key-before-deploying"

os.makedirs(QR_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            destination TEXT NOT NULL,
            bus_name TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            total_seats INTEGER NOT NULL,
            fare REAL NOT NULL
        )
    """)

    # status flow: PENDING_PAYMENT -> CONFIRMED -> USED
    #              PENDING_PAYMENT/CONFIRMED -> CANCELLED
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_uid TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            route_id INTEGER NOT NULL,
            passenger_name TEXT NOT NULL,
            seats INTEGER NOT NULL,
            seat_numbers TEXT NOT NULL DEFAULT '',
            fare_charged REAL NOT NULL,
            status TEXT DEFAULT 'PENDING_PAYMENT',
            booked_at TEXT DEFAULT (datetime('now')),
            paid_at TEXT,
            used_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (route_id) REFERENCES routes (id)
        )
    """)

    conn.commit()

    cur.execute("SELECT id FROM users WHERE email = ?", ("admin@buspass.com",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?, ?, ?, 1)",
            ("Admin", "admin@buspass.com", generate_password_hash("admin123")),
        )

    cur.execute("SELECT COUNT(*) FROM routes")
    if cur.fetchone()[0] == 0:
        sample_routes = [
            ("Mumbai", "Pune", "Shivneri Express", "06:00", 40, 350.0),
            ("Mumbai", "Nashik", "City Link", "07:30", 35, 300.0),
            ("Pune", "Bengaluru", "Highway King", "21:00", 45, 950.0),
            ("Delhi", "Jaipur", "Royal Cruiser", "05:30", 38, 600.0),
            ("Bengaluru", "Chennai", "South Star", "08:00", 42, 550.0),
        ]
        cur.executemany(
            "INSERT INTO routes (source, destination, bus_name, departure_time, total_seats, fare) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            sample_routes,
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session or not session.get("is_admin"):
            flash("Admin access required.", "danger")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Seat helpers
# ---------------------------------------------------------------------------
def get_taken_seats(conn, route_id):
    """Seats considered unavailable: anything on a PENDING_PAYMENT, CONFIRMED
    or USED booking. Cancelled bookings free their seats back up."""
    rows = conn.execute(
        "SELECT seat_numbers FROM bookings WHERE route_id = ? AND status != 'CANCELLED'",
        (route_id,),
    ).fetchall()
    taken = set()
    for r in rows:
        if r["seat_numbers"]:
            taken.update(r["seat_numbers"].split(","))
    return taken


# ---------------------------------------------------------------------------
# Public / auth routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    source = request.args.get("source", "").strip()
    destination = request.args.get("destination", "").strip()

    query = "SELECT * FROM routes WHERE 1=1"
    params = []
    if source:
        query += " AND source LIKE ?"
        params.append(f"%{source}%")
    if destination:
        query += " AND destination LIKE ?"
        params.append(f"%{destination}%")
    query += " ORDER BY source"

    conn = get_db()
    routes = conn.execute(query, params).fetchall()

    # all distinct cities, for the search dropdowns
    cities = conn.execute(
        "SELECT DISTINCT source AS city FROM routes UNION SELECT DISTINCT destination FROM routes"
    ).fetchall()
    conn.close()

    return render_template(
        "home.html", routes=routes, cities=[c["city"] for c in cities],
        search_source=source, search_destination=destination,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not name or not email or len(password) < 4:
            flash("Please fill all fields correctly (password min 4 chars).", "danger")
            return redirect(url_for("register"))

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("An account with this email already exists.", "danger")
            conn.close()
            return redirect(url_for("register"))

        conn.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            (name, email, generate_password_hash(password)),
        )
        conn.commit()
        conn.close()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("admin_dashboard") if user["is_admin"] else url_for("home"))

        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Booking routes (user) — now with seat selection + payment step
# ---------------------------------------------------------------------------
@app.route("/book/<int:route_id>", methods=["GET", "POST"])
@login_required
def book(route_id):
    conn = get_db()
    route = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        conn.close()
        flash("Route not found.", "danger")
        return redirect(url_for("home"))

    taken_seats = get_taken_seats(conn, route_id)
    all_seats = [str(n) for n in range(1, route["total_seats"] + 1)]

    if request.method == "POST":
        passenger_name = request.form["passenger_name"].strip()
        selected_seats = request.form.getlist("seats")  # list of seat numbers as strings

        if not passenger_name:
            flash("Passenger name is required.", "danger")
            conn.close()
            return redirect(url_for("book", route_id=route_id))

        if not selected_seats or len(selected_seats) > 6:
            flash("Select between 1 and 6 seats.", "danger")
            conn.close()
            return redirect(url_for("book", route_id=route_id))

        # Re-check seat availability server-side (prevents race/double-booking
        # and prevents a tampered form from claiming an already-taken seat)
        current_taken = get_taken_seats(conn, route_id)
        if any(s in current_taken for s in selected_seats):
            flash("Sorry, one or more selected seats were just taken. Please choose again.", "danger")
            conn.close()
            return redirect(url_for("book", route_id=route_id))

        if any(s not in all_seats for s in selected_seats):
            flash("Invalid seat selection.", "danger")
            conn.close()
            return redirect(url_for("book", route_id=route_id))

        # ---- Server-side price calculation (prevents incorrect pricing) ----
        fare_charged = route["fare"] * len(selected_seats)
        ticket_uid = str(uuid.uuid4())
        seat_str = ",".join(selected_seats)

        conn.execute(
            "INSERT INTO bookings (ticket_uid, user_id, route_id, passenger_name, seats, "
            "seat_numbers, fare_charged, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING_PAYMENT')",
            (ticket_uid, session["user_id"], route_id, passenger_name,
             len(selected_seats), seat_str, fare_charged),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("checkout", ticket_uid=ticket_uid))

    conn.close()
    return render_template(
        "book.html", route=route, all_seats=all_seats, taken_seats=taken_seats,
    )


# ---------------------------------------------------------------------------
# Payment mock
# ---------------------------------------------------------------------------
@app.route("/checkout/<ticket_uid>", methods=["GET", "POST"])
@login_required
def checkout(ticket_uid):
    conn = get_db()
    booking = conn.execute(
        """SELECT b.*, r.source, r.destination, r.bus_name, r.departure_time
           FROM bookings b JOIN routes r ON b.route_id = r.id
           WHERE b.ticket_uid = ? AND b.user_id = ?""",
        (ticket_uid, session["user_id"]),
    ).fetchone()

    if not booking:
        conn.close()
        flash("Booking not found.", "danger")
        return redirect(url_for("home"))

    if booking["status"] != "PENDING_PAYMENT":
        conn.close()
        return redirect(url_for("ticket", ticket_uid=ticket_uid))

    if request.method == "POST":
        # --- Mock payment: no real card processing, just simulates the step ---
        card_number = request.form.get("card_number", "").replace(" ", "")
        if len(card_number) < 12 or not card_number.isdigit():
            flash("Enter a valid mock card number (12+ digits, demo only — no real charge).", "danger")
            conn.close()
            return redirect(url_for("checkout", ticket_uid=ticket_uid))

        conn.execute(
            "UPDATE bookings SET status = 'CONFIRMED', paid_at = datetime('now') WHERE ticket_uid = ?",
            (ticket_uid,),
        )
        conn.commit()
        conn.close()

        generate_qr_code(ticket_uid)
        flash("Payment successful! Your e-ticket is confirmed.", "success")
        return redirect(url_for("ticket", ticket_uid=ticket_uid))

    conn.close()
    return render_template("checkout.html", b=booking)


def generate_qr_code(ticket_uid):
    img = qrcode.make(ticket_uid)
    img.save(os.path.join(QR_FOLDER, f"{ticket_uid}.png"))


@app.route("/ticket/<ticket_uid>")
@login_required
def ticket(ticket_uid):
    conn = get_db()
    row = conn.execute(
        """SELECT b.*, r.source, r.destination, r.bus_name, r.departure_time
           FROM bookings b JOIN routes r ON b.route_id = r.id
           WHERE b.ticket_uid = ? AND b.user_id = ?""",
        (ticket_uid, session["user_id"]),
    ).fetchone()
    conn.close()
    if not row:
        flash("Ticket not found.", "danger")
        return redirect(url_for("my_bookings"))
    if row["status"] == "PENDING_PAYMENT":
        return redirect(url_for("checkout", ticket_uid=ticket_uid))
    return render_template("ticket.html", b=row)


@app.route("/qrcodes/<filename>")
def qr_image(filename):
    return send_from_directory(QR_FOLDER, filename)


@app.route("/ticket/<ticket_uid>/pdf")
@login_required
def ticket_pdf(ticket_uid):
    conn = get_db()
    b = conn.execute(
        """SELECT b.*, r.source, r.destination, r.bus_name, r.departure_time
           FROM bookings b JOIN routes r ON b.route_id = r.id
           WHERE b.ticket_uid = ? AND b.user_id = ?""",
        (ticket_uid, session["user_id"]),
    ).fetchone()
    conn.close()
    if not b:
        flash("Ticket not found.", "danger")
        return redirect(url_for("my_bookings"))

    buf = io.BytesIO()
    width, height = A5
    c = pdf_canvas.Canvas(buf, pagesize=A5)

    # Header band
    c.setFillColor(colors.HexColor("#1e3c72"))
    c.rect(0, height - 28 * mm, width, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(12 * mm, height - 14 * mm, "RouteEase E-Ticket")
    c.setFont("Helvetica", 9)
    c.drawString(12 * mm, height - 22 * mm, f"Ticket ID: {b['ticket_uid']}")

    # Body
    c.setFillColor(colors.black)
    y = height - 38 * mm
    line_gap = 7 * mm

    def line(label, value):
        nonlocal y
        c.setFont("Helvetica-Bold", 10)
        c.drawString(12 * mm, y, f"{label}:")
        c.setFont("Helvetica", 10)
        c.drawString(45 * mm, y, str(value))
        y -= line_gap

    line("Route", f"{b['source']} -> {b['destination']}")
    line("Bus", b["bus_name"])
    line("Departure", b["departure_time"])
    line("Passenger", b["passenger_name"])
    line("Seat(s)", b["seat_numbers"])
    line("Fare Paid", f"Rs. {b['fare_charged']:.0f}")
    line("Status", b["status"])
    line("Booked At", b["booked_at"])

    # QR code image embed
    qr_path = os.path.join(QR_FOLDER, f"{b['ticket_uid']}.png")
    if os.path.exists(qr_path):
        c.drawImage(qr_path, width - 45 * mm, 12 * mm, 30 * mm, 30 * mm)

    c.setFont("Helvetica-Oblique", 7)
    c.setFillColor(colors.grey)
    c.drawString(12 * mm, 10 * mm, "Single-use ticket. Becomes invalid after one scan or cancellation.")

    c.showPage()
    c.save()
    buf.seek(0)

    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"ticket_{b['ticket_uid'][:8]}.pdf",
    )


@app.route("/my-bookings")
@login_required
def my_bookings():
    conn = get_db()
    rows = conn.execute(
        """SELECT b.*, r.source, r.destination, r.bus_name, r.departure_time
           FROM bookings b JOIN routes r ON b.route_id = r.id
           WHERE b.user_id = ? ORDER BY b.booked_at DESC""",
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return render_template("my_bookings.html", bookings=rows)


@app.route("/cancel/<ticket_uid>", methods=["POST"])
@login_required
def cancel(ticket_uid):
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE ticket_uid = ? AND user_id = ?",
        (ticket_uid, session["user_id"]),
    ).fetchone()
    if booking and booking["status"] in ("CONFIRMED", "PENDING_PAYMENT"):
        conn.execute("UPDATE bookings SET status = 'CANCELLED' WHERE ticket_uid = ?", (ticket_uid,))
        conn.commit()
        flash("Ticket cancelled. Seats released back into availability.", "info")
    conn.close()
    return redirect(url_for("my_bookings"))


# ---------------------------------------------------------------------------
# Validation endpoint (simulates a conductor scanning the QR)
# ---------------------------------------------------------------------------
@app.route("/validate/<ticket_uid>")
def validate(ticket_uid):
    conn = get_db()
    booking = conn.execute(
        """SELECT b.*, r.source, r.destination, r.bus_name
           FROM bookings b JOIN routes r ON b.route_id = r.id
           WHERE b.ticket_uid = ?""",
        (ticket_uid,),
    ).fetchone()

    if not booking:
        conn.close()
        return render_template("validate.html", result="INVALID", booking=None)

    if booking["status"] == "CANCELLED":
        conn.close()
        return render_template("validate.html", result="CANCELLED", booking=booking)

    if booking["status"] == "PENDING_PAYMENT":
        conn.close()
        return render_template("validate.html", result="UNPAID", booking=booking)

    if booking["status"] == "USED":
        conn.close()
        return render_template("validate.html", result="ALREADY_USED", booking=booking)

    conn.execute(
        "UPDATE bookings SET status = 'USED', used_at = datetime('now') WHERE ticket_uid = ?",
        (ticket_uid,),
    )
    conn.commit()
    conn.close()
    return render_template("validate.html", result="VALID", booking=booking)


# ---------------------------------------------------------------------------
# Admin routes — now with filters + extra stats
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    status_filter = request.args.get("status", "").strip()
    route_filter = request.args.get("route_id", "").strip()
    date_filter = request.args.get("date", "").strip()

    conn = get_db()
    routes = conn.execute("SELECT * FROM routes ORDER BY id DESC").fetchall()

    query = """SELECT b.*, r.source, r.destination, u.name AS user_name
               FROM bookings b
               JOIN routes r ON b.route_id = r.id
               JOIN users u ON b.user_id = u.id
               WHERE 1=1"""
    params = []
    if status_filter:
        query += " AND b.status = ?"
        params.append(status_filter)
    if route_filter:
        query += " AND b.route_id = ?"
        params.append(route_filter)
    if date_filter:
        query += " AND date(b.booked_at) = ?"
        params.append(date_filter)
    query += " ORDER BY b.booked_at DESC LIMIT 100"

    bookings = conn.execute(query, params).fetchall()

    revenue = conn.execute(
        "SELECT COALESCE(SUM(fare_charged), 0) AS total FROM bookings WHERE status IN ('CONFIRMED','USED')"
    ).fetchone()["total"]
    total_bookings = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE status != 'CANCELLED'"
    ).fetchone()["c"]
    pending_payments = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE status = 'PENDING_PAYMENT'"
    ).fetchone()["c"]
    used_count = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE status = 'USED'"
    ).fetchone()["c"]
    cancelled_count = conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE status = 'CANCELLED'"
    ).fetchone()["c"]

    conn.close()
    return render_template(
        "admin_dashboard.html",
        routes=routes,
        bookings=bookings,
        revenue=revenue,
        total_bookings=total_bookings,
        pending_payments=pending_payments,
        used_count=used_count,
        cancelled_count=cancelled_count,
        status_filter=status_filter,
        route_filter=route_filter,
        date_filter=date_filter,
    )


@app.route("/admin/route/add", methods=["POST"])
@admin_required
def add_route():
    source = request.form["source"].strip()
    destination = request.form["destination"].strip()
    bus_name = request.form["bus_name"].strip()
    departure_time = request.form["departure_time"].strip()
    total_seats = int(request.form["total_seats"])
    fare = float(request.form["fare"])

    if total_seats <= 0 or fare <= 0:
        flash("Seats and fare must be positive numbers.", "danger")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    conn.execute(
        "INSERT INTO routes (source, destination, bus_name, departure_time, total_seats, fare) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, destination, bus_name, departure_time, total_seats, fare),
    )
    conn.commit()
    conn.close()
    flash("Route added.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/route/delete/<int:route_id>", methods=["POST"])
@admin_required
def delete_route(route_id):
    conn = get_db()
    conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
    conn.commit()
    conn.close()
    flash("Route deleted.", "info")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# Entry point — just press Run/F5 in VS Code, no terminal needed
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)