import os
import urllib.parse

import psycopg2


def get_database_url():
    return os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")


def init_database(database_url=None, create_users=True, verbose=True):
    database_url = database_url or get_database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL or SUPABASE_DATABASE_URL must be set.")

    parsed = urllib.parse.urlparse(database_url)
    if verbose:
        print(f"Using PostgreSQL host: {parsed.hostname}")

    conn = psycopg2.connect(database_url, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id              SERIAL PRIMARY KEY,
                full_name       TEXT NOT NULL,
                phone           TEXT,
                email           TEXT,
                address         TEXT,
                whatsapp_number TEXT,
                id_number       TEXT,
                vat_number      TEXT,
                notes           TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute("ALTER TABLE clients ALTER COLUMN phone DROP NOT NULL;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS address TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS whatsapp_number TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS id_number TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS vat_number TEXT;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS notes TEXT;")
        cur.execute(
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id               SERIAL PRIMARY KEY,
                client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                case_code        TEXT NOT NULL,
                case_type        TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'new',
                opened_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                closed_at        TIMESTAMPTZ,
                case_reference   TEXT,
                general_court    TEXT,
                court_department TEXT,
                description      TEXT,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS case_reference TEXT;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS general_court TEXT;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS court_department TEXT;")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS description TEXT;")
        cur.execute(
            "ALTER TABLE cases ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id         SERIAL PRIMARY KEY,
                client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                case_id    INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                amount     NUMERIC(12, 2) NOT NULL,
                type       TEXT NOT NULL CHECK (type IN ('payment', 'expense', 'invoice', 'adjustment')),
                date       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                note       TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'transactions_type_check'
                      AND conrelid = 'transactions'::regclass
                ) THEN
                    ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
                END IF;
                ALTER TABLE transactions
                    ADD CONSTRAINT transactions_type_check
                    CHECK (type IN ('payment', 'expense', 'invoice', 'adjustment'));
            END $$;
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
        cur.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS price NUMERIC(12, 2);")
        cur.execute(
            "ALTER TABLE activities ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )

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
        cur.execute(
            """
            INSERT INTO case_clients (case_id, client_id)
            SELECT ca.id, ca.client_id
            FROM cases ca
            ON CONFLICT (case_id, client_id) DO NOTHING;
            """
        )

        if create_users:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            SERIAL PRIMARY KEY,
                    username      TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    if verbose:
        print("Database tables verified OK.")
