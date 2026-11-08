"""One-time operator bootstrap, without hardcoded credentials."""
import argparse
import getpass
import sqlite3

from .config import Settings
from .db import initialize, transaction
from .security import hash_password, audit, timestamp


def bootstrap_admin(username: str, password: str, db_path: str) -> int:
    username = username.strip()
    if not 3 <= len(username) <= 64:
        raise ValueError("Username must be 3-64 characters")
    password_hash = hash_password(password)
    initialize(db_path)
    with transaction(db_path) as db:
        if db.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
            raise ValueError("Administrator already exists; use authenticated user management")
        try:
            cursor = db.execute(
                "INSERT INTO users(username,display_name,password_hash,is_admin,created_at,updated_at) "
                "VALUES(?,?,?,1,?,?)",
                (username, "Administrator", password_hash, timestamp(), timestamp()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username already exists") from exc
        audit(db, cursor.lastrowid, "admin.bootstrap", str(cursor.lastrowid))
        return cursor.lastrowid


def main() -> None:
    parser = argparse.ArgumentParser(description="Odoo BOM Tools administration")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="Create initial Administrator once")
    create.add_argument("--username", default="administrator")
    args = parser.parse_args()
    if args.command == "create-admin":
        password = getpass.getpass("Administrator password (min. 12 chars): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            parser.error("Passwords do not match")
        try:
            user_id = bootstrap_admin(args.username, password, Settings.from_env().db_path)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Administrator created (user ID {user_id}). No password was logged.")


if __name__ == "__main__":
    main()
