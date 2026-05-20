"""
One-time utility: create a user in the PostgreSQL 'users' table.

Usage:
    python create_user.py <password>

- Creates the 'users' table if it does not already exist.
- Inserts a user with a bcrypt-hashed password.
- Defaults to username 'rui'. Override with CRM_USERNAME.
- Skips silently if the user already exists (no duplicate).
- Does NOT touch clients, cases, or any other table.
"""

import os
import sys
import psycopg2
from werkzeug.security import generate_password_hash
from db_schema import get_database_url, init_database

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

load_dotenv()

DATABASE_URL = get_database_url()

if not DATABASE_URL:
    print("ERROR: DATABASE_URL or SUPABASE_DATABASE_URL is not set.")
    sys.exit(1)

if len(sys.argv) != 2:
    print("Usage: python create_user.py <password>")
    sys.exit(1)

username = os.getenv("CRM_USERNAME", "rui").strip()
if not username:
    print("ERROR: CRM_USERNAME cannot be empty.")
    sys.exit(1)

password = sys.argv[1]
password_hash = generate_password_hash(password)

init_database(DATABASE_URL, create_users=True, verbose=False)

conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
conn.autocommit = False
cur = conn.cursor()

cur.execute(
    """
    INSERT INTO users (username, password_hash)
    VALUES (%s, %s)
    ON CONFLICT (username) DO NOTHING;
    """,
    (username, password_hash),
)

rows_affected = cur.rowcount
conn.commit()
cur.close()
conn.close()

if rows_affected == 1:
    print(f"User '{username}' created successfully.")
else:
    print(f"User '{username}' already exists — no changes made.")
