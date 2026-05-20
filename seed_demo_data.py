from dotenv import load_dotenv
import psycopg2

from db_schema import get_database_url, init_database


def main():
    load_dotenv()
    database_url = get_database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL or SUPABASE_DATABASE_URL must be set.")

    init_database(database_url=database_url, verbose=False)

    conn = psycopg2.connect(database_url, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT id FROM clients WHERE email = %s ORDER BY id LIMIT 1;",
            ("demo.cliente@example.com",),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO clients (full_name, phone, email, address, vat_number, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    "Cliente Demo Lda.",
                    "+351 210 000 000",
                    "demo.cliente@example.com",
                    "Rua Demo 10, Lisboa",
                    "PT999999990",
                    "Fake demo client for CRM testing.",
                ),
            )
            row = cur.fetchone()
            client_id = row[0]
        else:
            client_id = row[0]

        cur.execute(
            """
            INSERT INTO cases
                (client_id, case_code, case_type, status, case_reference,
                 general_court, court_department, description)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM cases WHERE case_code = %s
            )
            RETURNING id;
            """,
            (
                client_id,
                "DEMO-2026-001",
                "Civil",
                "open",
                "Proc. Demo 001/26",
                "Tribunal Judicial da Comarca de Lisboa",
                "Juizo Local Civel",
                "Fake demo case for CRM testing.",
                "DEMO-2026-001",
            ),
        )
        row = cur.fetchone()
        if row:
            case_id = row[0]
        else:
            cur.execute(
                "SELECT id FROM cases WHERE case_code = %s ORDER BY id LIMIT 1;",
                ("DEMO-2026-001",),
            )
            case_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO case_clients (case_id, client_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (case_id, client_id),
        )

        cur.execute(
            """
            INSERT INTO transactions (client_id, case_id, amount, type, note)
            SELECT %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM transactions
                WHERE client_id = %s AND case_id = %s AND note = %s
            );
            """,
            (
                client_id,
                case_id,
                250.00,
                "payment",
                "Fake demo payment.",
                client_id,
                case_id,
                "Fake demo payment.",
            ),
        )

        cur.execute(
            """
            INSERT INTO activities
                (client_id, case_id, activity_type, title, note, duration_minutes, price)
            SELECT %s, %s, %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM activities
                WHERE client_id = %s AND case_id = %s AND title = %s
            );
            """,
            (
                client_id,
                case_id,
                "Consultation",
                "Demo initial consultation",
                "Fake demo activity.",
                60,
                120.00,
                client_id,
                case_id,
                "Demo initial consultation",
            ),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print("Demo data inserted or already present.")


if __name__ == "__main__":
    main()
