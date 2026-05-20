import csv
import io
import zipfile
import os
import psycopg2
import psycopg2.extras
from datetime import datetime
from functools import wraps
from werkzeug.security import check_password_hash
from db_schema import get_database_url, init_database

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

from flask import (
    Flask,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)

load_dotenv()

app = Flask(__name__)
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET and (os.getenv("RENDER") or os.getenv("REPL_ID")):
    raise RuntimeError("SESSION_SECRET must be set in hosted environments.")

app.secret_key = SESSION_SECRET or "fallback-dev-secret"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower()
    in ("1", "true", "yes")
    or bool(os.getenv("RENDER")),
)

# Replit should provide DATABASE_URL from Secrets. SUPABASE_DATABASE_URL remains
# supported for older deployments of this project.
DATABASE_URL = get_database_url()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL or SUPABASE_DATABASE_URL must be set.")

    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        db.autocommit = False
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    import urllib.parse

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL / SUPABASE_DATABASE_URL is not set.")
        return
    parsed = urllib.parse.urlparse(DATABASE_URL)
    print(f"Using Supabase/Postgres — host: {parsed.hostname}")
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id              SERIAL PRIMARY KEY,
                full_name       TEXT NOT NULL,
                phone           TEXT NOT NULL,
                email           TEXT,
                address         TEXT,
                whatsapp_number TEXT,
                id_number       TEXT,
                vat_number      TEXT
            );
            """
        )
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS address TEXT;")
        cur.execute(
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS whatsapp_number TEXT;"
        )
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS id_number TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS vat_number TEXT;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id               SERIAL PRIMARY KEY,
                client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                case_code        TEXT    NOT NULL,
                case_type        TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'new',
                opened_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                closed_at        TIMESTAMPTZ,
                case_reference   TEXT,
                general_court    TEXT,
                court_department TEXT
            );
            """
        )
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS case_reference TEXT;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS general_court TEXT;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS court_department TEXT;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id        SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                case_id   INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                amount    NUMERIC(12, 2) NOT NULL,
                type      TEXT NOT NULL CHECK (type IN ('payment', 'expense')),
                date      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                note      TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                id               SERIAL PRIMARY KEY,
                case_id          INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                activity_date    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activity_type    TEXT NOT NULL,
                title            TEXT NOT NULL,
                note             TEXT,
                duration_minutes INTEGER,
                price            NUMERIC(12, 2),
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            "ALTER TABLE activities ADD COLUMN IF NOT EXISTS duration_minutes INTEGER;"
        )
        cur.execute(
            "ALTER TABLE activities ADD COLUMN IF NOT EXISTS price NUMERIC(12, 2);"
        )

        # Step 1: Make phone optional
        cur.execute("ALTER TABLE clients ALTER COLUMN phone DROP NOT NULL;")

        # Step 2: Many-to-many case ↔ clients
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS case_clients (
                id        SERIAL PRIMARY KEY,
                case_id   INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                UNIQUE (case_id, client_id)
            );
            """
        )

        # Step 3: Migrate existing cases — populate case_clients from cases.client_id
        cur.execute(
            """
            INSERT INTO case_clients (case_id, client_id)
            SELECT ca.id, ca.client_id
            FROM cases ca
            ON CONFLICT (case_id, client_id) DO NOTHING;
            """
        )

        conn.commit()
        cur.close()
        conn.close()
        print("Database tables verified OK.")
    except Exception as e:
        print(f"WARNING: Could not initialise database at startup: {e}")
        print("The app will still start — database errors will appear per-request.")


# ---------------------------------------------------------------------------
# Health check (used by deployment system — no auth required)
# ---------------------------------------------------------------------------


if os.getenv("AUTO_INIT_DB", "true").lower() not in ("0", "false", "no"):
    try:
        init_database(DATABASE_URL)
    except Exception as e:
        print(f"WARNING: Could not initialise database at startup: {e}")
        print("The app will still start; database errors will appear per-request.")


@app.route("/api/healthz")
def healthz():
    return "ok", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/health/db")
def health_db():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        conn.close()
        return {"database": "ok"}, 200
    except Exception:
        return {"database": "unavailable"}, 503


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        authenticated = False

        # --- 1. Try database user ---
        try:
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            cur = conn.cursor()
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s", (username,)
            )
            db_user = cur.fetchone()
            cur.close()
            conn.close()

            if db_user and check_password_hash(db_user["password_hash"], password):
                authenticated = True
        except Exception:
            # If the users table doesn't exist yet or DB is unreachable, fall through
            pass

        # --- 2. Fallback: APP_USER / APP_PASS environment variables ---
        if not authenticated:
            app_user = os.getenv("APP_USER", "").strip()
            app_pass = os.getenv("APP_PASS", "").strip()
            if app_user and app_pass and username == app_user and password == app_pass:
                authenticated = True

        if authenticated:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    search = request.args.get("q", "").strip()
    balance_filter = request.args.get("bf", "all").strip()
    cur = db.cursor()

    if search:
        cur.execute(
            "SELECT * FROM clients WHERE full_name ILIKE %s ORDER BY full_name",
            (f"%{search}%",),
        )
    else:
        cur.execute("SELECT * FROM clients ORDER BY full_name")

    clients = cur.fetchall()

    all_items = []
    for c in clients:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM cases WHERE client_id = %s", (c["id"],)
        )
        count = cur.fetchone()["cnt"]
        cur.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN type='payment' THEN amount ELSE 0 END), 0) AS total_payments,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS total_expenses
               FROM transactions WHERE client_id = %s""",
            (c["id"],),
        )
        row = cur.fetchone()
        payments = float(row["total_payments"])
        expenses = float(row["total_expenses"])
        balance = payments - expenses
        uncovered = max(0.0, expenses - payments)
        # Last activity or transaction date for this client
        cur.execute(
            """SELECT MAX(d) AS last_active FROM (
                   SELECT activity_date::date AS d FROM activities WHERE client_id = %s AND activity_date IS NOT NULL
                   UNION ALL
                   SELECT date::date AS d FROM transactions WHERE client_id = %s AND date IS NOT NULL
               ) sub""",
            (c["id"], c["id"]),
        )
        last_activity = cur.fetchone()["last_active"]
        all_items.append(
            {
                "client": c,
                "case_count": count,
                "balance": balance,
                "uncovered": uncovered,
                "last_activity": last_activity,
            }
        )

    # ── Needs-Attention alerts (global, unaffected by search/filter) ──
    cur.execute(
        """SELECT COUNT(*) AS cnt
           FROM (
               SELECT client_id
               FROM transactions
               GROUP BY client_id
               HAVING SUM(CASE WHEN type='expense' THEN amount ELSE 0 END)
                    > SUM(CASE WHEN type='payment'  THEN amount ELSE 0 END)
           ) sub"""
    )
    uncovered_count = cur.fetchone()["cnt"]

    cur.execute(
        """SELECT COUNT(*) AS cnt
           FROM cases ca
           WHERE ca.status != 'closed'
           AND COALESCE(
               (SELECT MAX(activity_date) FROM activities WHERE case_id = ca.id),
               '1970-01-01'::date
           ) < CURRENT_DATE - INTERVAL '7 days'"""
    )
    inactive_cases_count = cur.fetchone()["cnt"]

    cur.close()

    # Summary stats (across all fetched clients, before balance filter)
    total_positive = sum(i["balance"] for i in all_items if i["balance"] > 0)
    total_negative = sum(i["balance"] for i in all_items if i["balance"] < 0)
    open_count = sum(1 for i in all_items if i["balance"] != 0)
    total_uncovered = sum(i["uncovered"] for i in all_items)
    summary = {
        "total_positive": total_positive,
        "total_negative": total_negative,
        "open_count": open_count,
        "total_uncovered": total_uncovered,
    }

    # Apply balance filter
    if balance_filter == "positive":
        client_list = [i for i in all_items if i["balance"] > 0]
    elif balance_filter == "negative":
        client_list = [i for i in all_items if i["balance"] < 0]
    elif balance_filter == "zero":
        client_list = [i for i in all_items if i["balance"] == 0]
    elif balance_filter == "uncovered":
        client_list = [i for i in all_items if i["uncovered"] > 0]
    else:
        client_list = all_items

    return render_template(
        "dashboard.html",
        client_list=client_list,
        search=search,
        balance_filter=balance_filter,
        summary=summary,
        uncovered_count=uncovered_count,
        inactive_cases_count=inactive_cases_count,
    )


# ---------------------------------------------------------------------------
# Client routes
# ---------------------------------------------------------------------------


@app.route("/clients/add", methods=["GET", "POST"])
@login_required
def add_client():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()

        print(
            f"[ADD CLIENT] Form received — name={full_name!r} phone={phone!r} email={email!r}"
        )

        if not full_name:
            flash("Full name is required.", "error")
        else:
            try:
                import urllib.parse

                parsed = urllib.parse.urlparse(DATABASE_URL)

                address = request.form.get("address", "").strip()
                whatsapp_number = request.form.get("whatsapp_number", "").strip()
                id_number = request.form.get("id_number", "").strip()
                vat_number = request.form.get("vat_number", "").strip()

                db = get_db()
                cur = db.cursor()

                sql = """INSERT INTO clients (full_name, phone, email, address, whatsapp_number, id_number, vat_number)
                         VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"""
                params = (
                    full_name,
                    phone or None,
                    email or None,
                    address or None,
                    whatsapp_number or None,
                    id_number or None,
                    vat_number or None,
                )

                cur.execute(sql, params)
                row = cur.fetchone()
                new_id = row["id"]
                db.commit()
                cur.close()

                flash(f"Client '{full_name}' added.", "success")
                return redirect(url_for("dashboard"))

            except Exception as e:
                print(f"[ADD CLIENT] ERROR: {e}")
                flash(f"Database error: {e}", "error")

    return render_template("add_client.html")


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()

    if not client:
        cur.close()
        flash("Client not found.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address = request.form.get("address", "").strip()
        whatsapp_number = request.form.get("whatsapp_number", "").strip()
        id_number = request.form.get("id_number", "").strip()
        vat_number = request.form.get("vat_number", "").strip()

        if not full_name:
            flash("Full name is required.", "error")
        else:
            cur.execute(
                """UPDATE clients
                   SET full_name = %s, phone = %s, email = %s,
                       address = %s, whatsapp_number = %s, id_number = %s,
                       vat_number = %s
                   WHERE id = %s""",
                (
                    full_name,
                    phone or None,
                    email or None,
                    address or None,
                    whatsapp_number or None,
                    id_number or None,
                    vat_number or None,
                    client_id,
                ),
            )
            db.commit()
            cur.close()
            flash("Client updated successfully.", "success")
            return redirect(url_for("client_detail", client_id=client_id))

    cur.close()
    return render_template("edit_client.html", client=client)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT full_name FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()

    if client:
        cur.execute("DELETE FROM clients WHERE id = %s", (client_id,))
        db.commit()
        flash(f"Client '{client['full_name']}' deleted.", "success")
    else:
        flash("Client not found.", "error")

    cur.close()
    return redirect(url_for("dashboard"))


@app.route("/clients/<int:client_id>")
@login_required
def client_detail(client_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()

    if not client:
        cur.close()
        flash("Client not found.", "error")
        return redirect(url_for("dashboard"))

    cur.execute(
        """SELECT ca.*
           FROM cases ca
           WHERE ca.id IN (
               SELECT case_id FROM case_clients WHERE client_id = %s
               UNION
               SELECT id FROM cases WHERE client_id = %s
           )
           ORDER BY ca.opened_at DESC""",
        (client_id, client_id),
    )
    cases = cur.fetchall()

    cur.execute(
        """SELECT t.*, c.case_code
           FROM transactions t
           LEFT JOIN cases c ON t.case_id = c.id
           WHERE t.client_id = %s
           ORDER BY t.date DESC""",
        (client_id,),
    )
    transactions = cur.fetchall()

    cur.execute(
        """SELECT COALESCE(SUM(CASE WHEN type='payment' THEN amount ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS balance
           FROM transactions WHERE client_id = %s""",
        (client_id,),
    )
    balance = cur.fetchone()["balance"]

    cur.execute(
        """SELECT a.*, ca.case_code FROM activities a
           JOIN cases ca ON a.case_id = ca.id
           WHERE a.client_id = %s ORDER BY a.activity_date DESC""",
        (client_id,),
    )
    activities = cur.fetchall()

    cur.close()
    return render_template(
        "client_detail.html",
        client=client,
        cases=cases,
        transactions=transactions,
        balance=balance,
        activities=activities,
    )


# ---------------------------------------------------------------------------
# Case routes
# ---------------------------------------------------------------------------


@app.route("/clients/<int:client_id>/cases/add", methods=["GET", "POST"])
@login_required
def add_case(client_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()

    if not client:
        cur.close()
        flash("Client not found.", "error")
        return redirect(url_for("dashboard"))

    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    all_clients = cur.fetchall()

    if request.method == "POST":
        case_code = request.form.get("case_code", "").strip()
        case_type = request.form.get("case_type", "").strip()
        status = request.form.get("status", "new").strip()
        closed_at_raw = request.form.get("closed_at", "").strip()
        case_reference = request.form.get("case_reference", "").strip()
        general_court = request.form.get("general_court", "").strip()
        court_department = request.form.get("court_department", "").strip()

        if not case_code or not case_type:
            flash("Case code and case type are required.", "error")
        else:
            closed_at = closed_at_raw if closed_at_raw else None
            cur.execute(
                """INSERT INTO cases
                       (client_id, case_code, case_type, status, opened_at, closed_at,
                        case_reference, general_court, court_department)
                   VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s)
                   RETURNING id""",
                (
                    client_id,
                    case_code,
                    case_type,
                    status,
                    closed_at,
                    case_reference or None,
                    general_court or None,
                    court_department or None,
                ),
            )
            new_case_id = cur.fetchone()["id"]

            # Build set of all linked client IDs (primary + additional selected)
            extra_ids = request.form.getlist("client_ids")
            linked_ids = set([client_id])
            for cid in extra_ids:
                try:
                    linked_ids.add(int(cid))
                except ValueError:
                    pass
            for cid in linked_ids:
                cur.execute(
                    "INSERT INTO case_clients (case_id, client_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (new_case_id, cid),
                )

            db.commit()
            cur.close()
            flash(f"Case '{case_code}' added.", "success")
            return redirect(url_for("client_detail", client_id=client_id))

    cur.close()
    return render_template("add_case.html", client=client, all_clients=all_clients)


@app.route("/cases/<int:case_id>/delete", methods=["POST"])
@login_required
def delete_case(case_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT client_id FROM cases WHERE id = %s", (case_id,))
    case = cur.fetchone()

    if case:
        client_id = case["client_id"]
        cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        db.commit()
        cur.close()
        flash("Case deleted.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    cur.close()
    flash("Case not found.", "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Transaction routes
# ---------------------------------------------------------------------------


@app.route("/clients/<int:client_id>/transactions/add", methods=["GET", "POST"])
@login_required
def add_transaction(client_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()

    if not client:
        cur.close()
        flash("Client not found.", "error")
        return redirect(url_for("dashboard"))

    cur.execute(
        "SELECT id, case_code FROM cases WHERE client_id = %s ORDER BY opened_at DESC",
        (client_id,),
    )
    cases = cur.fetchall()

    # Allow pre-selecting a case via ?case_id=X (e.g. from case detail page)
    preselected_case_id = request.args.get("case_id", "")

    if request.method == "POST":
        amount_raw = request.form.get("amount", "").strip()
        tx_type = request.form.get("type", "").strip()
        case_id_raw = request.form.get("case_id", "").strip()
        note = request.form.get("note", "").strip()

        if not amount_raw or tx_type not in ("payment", "expense"):
            flash("Amount and a valid type are required.", "error")
        else:
            try:
                amount = float(amount_raw)
                if amount <= 0:
                    flash("Amount must be greater than zero.", "error")
                else:
                    case_id = int(case_id_raw) if case_id_raw else None
                    cur.execute(
                        """INSERT INTO transactions (client_id, case_id, amount, type, note)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (client_id, case_id, amount, tx_type, note or None),
                    )
                    db.commit()
                    cur.close()
                    flash("Transaction added.", "success")
                    # Redirect back to the case if one was selected, otherwise client
                    if case_id:
                        return redirect(url_for("case_detail", case_id=case_id))
                    return redirect(url_for("client_detail", client_id=client_id))
            except ValueError:
                flash("Invalid amount.", "error")

    cur.close()
    return render_template(
        "add_transaction.html",
        client=client,
        cases=cases,
        preselected_case_id=preselected_case_id,
    )


@app.route("/transactions/<int:tx_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(tx_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,))
    tx = cur.fetchone()

    if not tx:
        cur.close()
        flash("Transaction not found.", "error")
        return redirect(url_for("dashboard"))

    cur.execute(
        "SELECT id, case_code FROM cases WHERE client_id = %s ORDER BY opened_at DESC",
        (tx["client_id"],),
    )
    cases = cur.fetchall()

    if request.method == "POST":
        amount_raw = request.form.get("amount", "").strip()
        tx_type = request.form.get("type", "").strip()
        date_raw = request.form.get("date", "").strip()
        case_id_raw = request.form.get("case_id", "").strip()
        note = request.form.get("note", "").strip()

        if not amount_raw or tx_type not in ("payment", "expense"):
            flash("Amount and a valid type are required.", "error")
        else:
            try:
                amount = float(amount_raw)
                if amount <= 0:
                    flash("Amount must be greater than zero.", "error")
                else:
                    case_id = int(case_id_raw) if case_id_raw else None
                    date_val = date_raw if date_raw else None
                    cur.execute(
                        """UPDATE transactions
                           SET amount = %s, type = %s,
                               date = COALESCE(%s::TIMESTAMPTZ, date),
                               case_id = %s, note = %s
                           WHERE id = %s""",
                        (amount, tx_type, date_val, case_id, note or None, tx_id),
                    )
                    db.commit()
                    cur.close()
                    flash("Transaction updated.", "success")
                    if case_id:
                        return redirect(url_for("case_detail", case_id=case_id))
                    return redirect(url_for("client_detail", client_id=tx["client_id"]))
            except ValueError:
                flash("Invalid amount.", "error")

    cur.close()
    return render_template("edit_transaction.html", tx=tx, cases=cases)


@app.route("/transactions/<int:tx_id>/delete", methods=["POST"])
@login_required
def delete_transaction(tx_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT client_id, case_id FROM transactions WHERE id = %s", (tx_id,))
    tx = cur.fetchone()

    if tx:
        client_id = tx["client_id"]
        case_id = tx["case_id"]
        cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        db.commit()
        cur.close()
        flash("Transaction deleted.", "success")
        if case_id:
            return redirect(url_for("case_detail", case_id=case_id))
        return redirect(url_for("client_detail", client_id=client_id))

    cur.close()
    flash("Transaction not found.", "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Global Cases routes
# ---------------------------------------------------------------------------


@app.route("/cases")
@login_required
def cases_list():
    db = get_db()
    balance_filter = request.args.get("bf", "all").strip()
    cur = db.cursor()
    cur.execute(
        """SELECT ca.id, ca.client_id, ca.case_code, ca.case_type, ca.status,
                  ca.opened_at, ca.closed_at, ca.case_reference,
                  ca.general_court, ca.court_department,
                  cl.full_name,
                  COALESCE(
                      (SELECT STRING_AGG(cl2.full_name, ', ' ORDER BY cl2.full_name)
                       FROM case_clients cc2
                       JOIN clients cl2 ON cc2.client_id = cl2.id
                       WHERE cc2.case_id = ca.id),
                      cl.full_name
                  ) AS clients_display,
                  COALESCE(SUM(CASE WHEN t.type='payment' THEN t.amount ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END), 0) AS balance,
                  GREATEST(0,
                      COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END), 0)
                    - COALESCE(SUM(CASE WHEN t.type='payment' THEN t.amount ELSE 0 END), 0)
                  ) AS uncovered,
                  (SELECT MAX(activity_date) FROM activities WHERE case_id = ca.id) AS last_activity
           FROM cases ca
           JOIN clients cl ON ca.client_id = cl.id
           LEFT JOIN transactions t ON t.case_id = ca.id
           GROUP BY ca.id, ca.client_id, ca.case_code, ca.case_type, ca.status,
                    ca.opened_at, ca.closed_at, ca.case_reference,
                    ca.general_court, ca.court_department, cl.full_name
           ORDER BY ca.opened_at DESC"""
    )
    all_cases = cur.fetchall()
    cur.close()

    # Summary stats (before balance filter)
    total_positive = sum(c["balance"] for c in all_cases if c["balance"] > 0)
    total_negative = sum(c["balance"] for c in all_cases if c["balance"] < 0)
    open_count = sum(1 for c in all_cases if c["balance"] != 0)
    total_uncovered = sum(float(c["uncovered"]) for c in all_cases)
    summary = {
        "total_positive": total_positive,
        "total_negative": total_negative,
        "open_count": open_count,
        "total_uncovered": total_uncovered,
    }

    # Apply balance filter
    if balance_filter == "positive":
        cases = [c for c in all_cases if c["balance"] > 0]
    elif balance_filter == "negative":
        cases = [c for c in all_cases if c["balance"] < 0]
    elif balance_filter == "zero":
        cases = [c for c in all_cases if c["balance"] == 0]
    elif balance_filter == "uncovered":
        cases = [c for c in all_cases if float(c["uncovered"]) > 0]
    else:
        cases = all_cases

    return render_template(
        "cases.html", cases=cases, balance_filter=balance_filter, summary=summary
    )


@app.route("/cases/<int:case_id>")
@login_required
def case_detail(case_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """SELECT ca.*, cl.full_name
           FROM cases ca
           JOIN clients cl ON ca.client_id = cl.id
           WHERE ca.id = %s""",
        (case_id,),
    )
    case = cur.fetchone()

    if not case:
        cur.close()
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    cur.execute(
        "SELECT * FROM transactions WHERE case_id = %s ORDER BY date DESC",
        (case_id,),
    )
    transactions = cur.fetchall()

    cur.execute(
        """SELECT COALESCE(SUM(CASE WHEN type='payment' THEN amount ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS balance
           FROM transactions WHERE case_id = %s""",
        (case_id,),
    )
    balance = cur.fetchone()["balance"]

    cur.execute(
        "SELECT * FROM activities WHERE case_id = %s ORDER BY activity_date DESC",
        (case_id,),
    )
    activities = cur.fetchall()

    act_total_minutes = sum((a["duration_minutes"] or 0) for a in activities)
    act_total_price = sum(float(a["price"] or 0) for a in activities)

    cur.execute(
        """SELECT cl.id, cl.full_name
           FROM case_clients cc
           JOIN clients cl ON cc.client_id = cl.id
           WHERE cc.case_id = %s
           ORDER BY cl.full_name""",
        (case_id,),
    )
    case_clients_list = list(cur.fetchall())
    if not case_clients_list:
        case_clients_list = [{"id": case["client_id"], "full_name": case["full_name"]}]

    cur.close()
    return render_template(
        "case_detail.html",
        case=case,
        transactions=transactions,
        balance=balance,
        activities=activities,
        act_total_minutes=act_total_minutes,
        act_total_price=act_total_price,
        case_clients_list=case_clients_list,
    )


@app.route("/cases/<int:case_id>/edit", methods=["GET", "POST"])
@login_required
def edit_case(case_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
    case = cur.fetchone()

    if not case:
        cur.close()
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    all_clients = cur.fetchall()

    cur.execute("SELECT client_id FROM case_clients WHERE case_id = %s", (case_id,))
    case_client_ids = set(row["client_id"] for row in cur.fetchall())
    # Fallback: if no case_clients entries yet, default to primary client
    if not case_client_ids:
        case_client_ids = {case["client_id"]}

    if request.method == "POST":
        case_code = request.form.get("case_code", "").strip()
        case_type = request.form.get("case_type", "").strip()
        status = request.form.get("status", "new").strip()
        closed_at_raw = request.form.get("closed_at", "").strip()
        case_reference = request.form.get("case_reference", "").strip()
        general_court = request.form.get("general_court", "").strip()
        court_department = request.form.get("court_department", "").strip()

        selected_ids = set()
        for cid in request.form.getlist("client_ids"):
            try:
                selected_ids.add(int(cid))
            except ValueError:
                pass

        if not case_code or not case_type:
            flash("Case code and case type are required.", "error")
        elif not selected_ids:
            flash("At least one client must be selected.", "error")
        else:
            closed_at = closed_at_raw if closed_at_raw else None
            # Keep current primary if still selected, else pick lowest ID
            new_primary = (
                case["client_id"]
                if case["client_id"] in selected_ids
                else min(selected_ids)
            )
            cur.execute(
                """UPDATE cases
                   SET case_code = %s, case_type = %s, status = %s,
                       closed_at = %s, case_reference = %s,
                       general_court = %s, court_department = %s,
                       client_id = %s
                   WHERE id = %s""",
                (
                    case_code,
                    case_type,
                    status,
                    closed_at,
                    case_reference or None,
                    general_court or None,
                    court_department or None,
                    new_primary,
                    case_id,
                ),
            )
            # Replace case_clients entries
            cur.execute("DELETE FROM case_clients WHERE case_id = %s", (case_id,))
            for cid in selected_ids:
                cur.execute(
                    "INSERT INTO case_clients (case_id, client_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (case_id, cid),
                )
            db.commit()
            cur.close()
            flash("Case updated.", "success")
            return redirect(url_for("case_detail", case_id=case_id))

    cur.close()
    return render_template(
        "edit_case.html",
        case=case,
        all_clients=all_clients,
        case_client_ids=case_client_ids,
    )


# ---------------------------------------------------------------------------
# Global Transactions routes
# ---------------------------------------------------------------------------


@app.route("/transactions")
@login_required
def transactions_list():
    db = get_db()
    cur = db.cursor()

    filter_client = request.args.get("client_id", "").strip()
    filter_case = request.args.get("case_id", "").strip()
    filter_type = request.args.get("type", "").strip()
    filter_date_from = request.args.get("date_from", "").strip()
    filter_date_to = request.args.get("date_to", "").strip()

    conditions = []
    params = []
    if filter_client:
        conditions.append("t.client_id = %s")
        params.append(int(filter_client))
    if filter_case:
        conditions.append("t.case_id = %s")
        params.append(int(filter_case))
    if filter_type == "uncovered":
        conditions.append(
            """t.type = 'expense' AND t.client_id IN (
                SELECT client_id FROM transactions
                GROUP BY client_id
                HAVING SUM(CASE WHEN type='expense' THEN amount ELSE 0 END)
                     > SUM(CASE WHEN type='payment' THEN amount ELSE 0 END)
            )"""
        )
    elif filter_type in ("payment", "expense"):
        conditions.append("t.type = %s")
        params.append(filter_type)
    if filter_date_from:
        conditions.append("t.date >= %s")
        params.append(filter_date_from)
    if filter_date_to:
        conditions.append("t.date <= %s")
        params.append(filter_date_to + " 23:59:59")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    cur.execute(
        """SELECT t.*, cl.full_name, cl.id AS cl_id,
                  ca.case_code, ca.id AS ca_id
           FROM transactions t
           JOIN clients cl ON t.client_id = cl.id
           LEFT JOIN cases ca ON t.case_id = ca.id
           {where}
           ORDER BY t.date DESC""".format(where=where),
        params,
    )
    transactions = cur.fetchall()

    total_payments = sum(
        float(tx["amount"] or 0) for tx in transactions if tx["type"] == "payment"
    )
    total_expenses = sum(
        float(tx["amount"] or 0) for tx in transactions if tx["type"] == "expense"
    )
    net = total_payments - total_expenses

    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    clients = cur.fetchall()
    cur.execute(
        """SELECT ca.id, ca.case_code, ca.client_id
           FROM cases ca ORDER BY ca.case_code"""
    )
    cases = cur.fetchall()

    cur.close()
    return render_template(
        "transactions.html",
        transactions=transactions,
        total_payments=total_payments,
        total_expenses=total_expenses,
        net=net,
        clients=clients,
        cases=cases,
        filter_client=filter_client,
        filter_case=filter_case,
        filter_type=filter_type,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
    )


@app.route("/transactions/add", methods=["GET", "POST"])
@login_required
def add_transaction_global():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    clients = cur.fetchall()
    cur.execute(
        """SELECT ca.id, ca.case_code, ca.client_id, cl.full_name AS client_name
           FROM cases ca JOIN clients cl ON ca.client_id = cl.id
           ORDER BY cl.full_name, ca.case_code"""
    )
    all_cases = cur.fetchall()

    preselected_client = request.args.get("client_id", "")
    preselected_case = request.args.get("case_id", "")

    if request.method == "POST":
        client_id_raw = request.form.get("client_id", "").strip()
        case_id_raw = request.form.get("case_id", "").strip()
        amount_raw = request.form.get("amount", "").strip()
        tx_type = request.form.get("type", "").strip()
        note = request.form.get("note", "").strip()

        if not client_id_raw or not amount_raw or tx_type not in ("payment", "expense"):
            flash("Client, amount and type are required.", "error")
        else:
            try:
                client_id = int(client_id_raw)
                amount = float(amount_raw)
                if amount <= 0:
                    flash("Amount must be greater than zero.", "error")
                else:
                    case_id = int(case_id_raw) if case_id_raw else None
                    cur.execute(
                        """INSERT INTO transactions (client_id, case_id, amount, type, note)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (client_id, case_id, amount, tx_type, note or None),
                    )
                    db.commit()
                    cur.close()
                    flash("Transaction added.", "success")
                    if case_id:
                        return redirect(url_for("case_detail", case_id=case_id))
                    return redirect(url_for("client_detail", client_id=client_id))
            except ValueError:
                flash("Invalid amount.", "error")

    cur.close()
    return render_template(
        "add_transaction_global.html",
        clients=clients,
        all_cases=all_cases,
        preselected_client=preselected_client,
        preselected_case=preselected_case,
    )


@app.route("/cases/add", methods=["GET", "POST"])
@login_required
def add_case_global():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    clients = cur.fetchall()
    cur.close()

    if request.method == "POST":
        client_id_raw = request.form.get("client_id", "").strip()
        if not client_id_raw:
            flash("Please select a client.", "error")
        else:
            return redirect(url_for("add_case", client_id=int(client_id_raw)))

    return render_template("add_case_global.html", clients=clients)


@app.route("/activities/add", methods=["GET", "POST"])
@login_required
def add_activity_global():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    clients = cur.fetchall()
    cur.execute(
        """SELECT ca.id, ca.case_code, ca.client_id, cl.full_name AS client_name
           FROM cases ca JOIN clients cl ON ca.client_id = cl.id
           ORDER BY cl.full_name, ca.case_code"""
    )
    all_cases = cur.fetchall()
    cur.close()

    if request.method == "POST":
        case_id_raw = request.form.get("case_id", "").strip()
        if not case_id_raw:
            flash("Please select a case.", "error")
        else:
            return redirect(url_for("add_activity", case_id=int(case_id_raw)))

    return render_template(
        "add_activity_global.html", clients=clients, all_cases=all_cases
    )


# ---------------------------------------------------------------------------
# Activity routes
# ---------------------------------------------------------------------------


@app.route("/cases/<int:case_id>/activities/add", methods=["GET", "POST"])
@login_required
def add_activity(case_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """SELECT ca.*, cl.full_name FROM cases ca
           JOIN clients cl ON ca.client_id = cl.id
           WHERE ca.id = %s""",
        (case_id,),
    )
    case = cur.fetchone()

    if not case:
        cur.close()
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    if request.method == "POST":
        activity_type = request.form.get("activity_type", "").strip()
        title = request.form.get("title", "").strip()
        activity_date_raw = request.form.get("activity_date", "").strip()
        note = request.form.get("note", "").strip()
        duration_raw = request.form.get("duration_minutes", "").strip()
        price_raw = request.form.get("price", "").strip()

        if not title or not activity_type:
            flash("Activity type and title are required.", "error")
        else:
            date_val = activity_date_raw if activity_date_raw else None
            duration_val = int(duration_raw) if duration_raw else None
            price_val = float(price_raw) if price_raw else None
            cur.execute(
                """INSERT INTO activities (case_id, client_id, activity_date, activity_type, title, note, duration_minutes, price)
                   VALUES (%s, %s, COALESCE(%s::TIMESTAMPTZ, NOW()), %s, %s, %s, %s, %s)""",
                (
                    case_id,
                    case["client_id"],
                    date_val,
                    activity_type,
                    title,
                    note or None,
                    duration_val,
                    price_val,
                ),
            )
            db.commit()
            cur.close()
            flash("Activity added.", "success")
            return redirect(url_for("case_detail", case_id=case_id))

    cur.close()
    return render_template("add_activity.html", case=case)


@app.route("/activities/<int:activity_id>/edit", methods=["GET", "POST"])
@login_required
def edit_activity(activity_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM activities WHERE id = %s", (activity_id,))
    activity = cur.fetchone()

    if not activity:
        cur.close()
        flash("Activity not found.", "error")
        return redirect(url_for("activities_list"))

    if request.method == "POST":
        activity_type = request.form.get("activity_type", "").strip()
        title = request.form.get("title", "").strip()
        activity_date_raw = request.form.get("activity_date", "").strip()
        note = request.form.get("note", "").strip()
        duration_raw = request.form.get("duration_minutes", "").strip()
        price_raw = request.form.get("price", "").strip()

        if not title or not activity_type:
            flash("Activity type and title are required.", "error")
        else:
            date_val = activity_date_raw if activity_date_raw else None
            duration_val = int(duration_raw) if duration_raw else None
            price_val = float(price_raw) if price_raw else None
            cur.execute(
                """UPDATE activities
                   SET activity_type = %s, title = %s, note = %s,
                       activity_date = COALESCE(%s::TIMESTAMPTZ, activity_date),
                       duration_minutes = %s, price = %s
                   WHERE id = %s""",
                (
                    activity_type,
                    title,
                    note or None,
                    date_val,
                    duration_val,
                    price_val,
                    activity_id,
                ),
            )
            db.commit()
            cur.close()
            flash("Activity updated.", "success")
            return redirect(url_for("case_detail", case_id=activity["case_id"]))

    cur.close()
    return render_template("edit_activity.html", activity=activity)


@app.route("/activities/<int:activity_id>/delete", methods=["POST"])
@login_required
def delete_activity(activity_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT case_id FROM activities WHERE id = %s", (activity_id,))
    activity = cur.fetchone()

    if activity:
        case_id = activity["case_id"]
        cur.execute("DELETE FROM activities WHERE id = %s", (activity_id,))
        db.commit()
        cur.close()
        flash("Activity deleted.", "success")
        return redirect(url_for("case_detail", case_id=case_id))

    cur.close()
    flash("Activity not found.", "error")
    return redirect(url_for("dashboard"))


@app.route("/activities")
@login_required
def activities_list():
    db = get_db()
    filter_client = request.args.get("client_id", "").strip()
    filter_case = request.args.get("case_id", "").strip()
    filter_type = request.args.get("atype", "").strip()
    filter_date_from = request.args.get("date_from", "").strip()
    filter_date_to = request.args.get("date_to", "").strip()

    sql = """SELECT a.*, cl.full_name, ca.case_code
             FROM activities a
             JOIN clients cl ON a.client_id = cl.id
             JOIN cases ca ON a.case_id = ca.id
             WHERE 1=1"""
    params = []

    if filter_client:
        sql += " AND a.client_id = %s"
        params.append(int(filter_client))
    if filter_case:
        sql += " AND a.case_id = %s"
        params.append(int(filter_case))
    if filter_type:
        sql += " AND a.activity_type ILIKE %s"
        params.append(f"%{filter_type}%")
    if filter_date_from:
        sql += " AND a.activity_date >= %s"
        params.append(filter_date_from)
    if filter_date_to:
        sql += " AND a.activity_date <= %s::DATE + INTERVAL '1 day'"
        params.append(filter_date_to)

    sql += " ORDER BY a.activity_date DESC"

    cur = db.cursor()
    cur.execute(sql, params)
    activities = cur.fetchall()

    cur.execute("SELECT id, full_name FROM clients ORDER BY full_name")
    clients = cur.fetchall()

    cur.execute("SELECT id, case_code FROM cases ORDER BY case_code")
    cases = cur.fetchall()

    total_minutes = sum((a["duration_minutes"] or 0) for a in activities)
    total_price = sum(float(a["price"] or 0) for a in activities)

    cur.close()
    return render_template(
        "activities.html",
        activities=activities,
        clients=clients,
        cases=cases,
        filter_client=filter_client,
        filter_case=filter_case,
        filter_type=filter_type,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        total_minutes=total_minutes,
        total_price=total_price,
    )


# ---------------------------------------------------------------------------
# Global Search
# ---------------------------------------------------------------------------


@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    f = request.args.get("f", "all")  # all | clients | cases

    clients = []
    cases = []

    if q:
        db = get_db()
        cur = db.cursor()
        pattern = f"%{q}%"

        if f in ("all", "clients"):
            cur.execute(
                """SELECT id, full_name, phone, email
                   FROM clients
                   WHERE full_name ILIKE %s
                      OR phone     ILIKE %s
                      OR email     ILIKE %s
                   ORDER BY full_name
                   LIMIT 50""",
                (pattern, pattern, pattern),
            )
            clients = cur.fetchall()

        if f in ("all", "cases"):
            cur.execute(
                """SELECT ca.id, ca.case_code, ca.case_type, ca.status, ca.client_id,
                          cl.full_name,
                          COALESCE(
                              (SELECT STRING_AGG(cl2.full_name, ', ' ORDER BY cl2.full_name)
                               FROM case_clients cc2
                               JOIN clients cl2 ON cc2.client_id = cl2.id
                               WHERE cc2.case_id = ca.id),
                              cl.full_name
                          ) AS clients_display
                   FROM cases ca
                   JOIN clients cl ON ca.client_id = cl.id
                   WHERE ca.case_code        ILIKE %s
                      OR ca.case_type        ILIKE %s
                      OR ca.case_reference   ILIKE %s
                      OR ca.general_court    ILIKE %s
                      OR ca.court_department ILIKE %s
                      OR cl.full_name        ILIKE %s
                   ORDER BY ca.opened_at DESC
                   LIMIT 50""",
                (pattern, pattern, pattern, pattern, pattern, pattern),
            )
            cases = cur.fetchall()

        cur.close()

    return render_template("search.html", q=q, f=f, clients=clients, cases=cases)


# ---------------------------------------------------------------------------
# Backup / Export
# ---------------------------------------------------------------------------


@app.route("/backup/download")
@login_required
def backup_download():
    """Stream a ZIP file containing CSV exports of all main tables."""
    db = get_db()
    cur = db.cursor()

    tables = [
        ("clients", "SELECT * FROM clients ORDER BY id"),
        ("cases", "SELECT * FROM cases ORDER BY id"),
        ("case_clients", "SELECT * FROM case_clients ORDER BY id"),
        ("transactions", "SELECT * FROM transactions ORDER BY id"),
        ("activities", "SELECT * FROM activities ORDER BY id"),
        ("users", "SELECT id, username FROM users ORDER BY id"),
    ]

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table_name, sql in tables:
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            try:
                cur.execute(sql)
                rows = cur.fetchall()
                writer.writerow([column[0] for column in cur.description])
                for row in rows:
                    writer.writerow(list(row.values()))
            except psycopg2.errors.UndefinedTable:
                db.rollback()

            zf.writestr(f"{table_name}.csv", csv_buf.getvalue())

    cur.close()
    zip_buf.seek(0)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"crm_backup_{timestamp}.zip"

    resp = make_response(zip_buf.read())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
