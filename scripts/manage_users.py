"""
Manage Operator Console user accounts.

Users are stored at  data/users.json  as a dict mapping username -> hashed password.
The file is gitignored so credentials never get committed.

Examples:
    python -m scripts.manage_users add victor "MyPassword!"
    python -m scripts.manage_users add roger  "RogersPassword"
    python -m scripts.manage_users remove roger
    python -m scripts.manage_users list
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = PROJECT_ROOT / "data" / "users.json"


def load() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1].lower()
    users = load()

    if cmd == "list":
        if not users:
            print("(no users defined yet)")
            return 0
        print("Users:")
        for u in sorted(users):
            print(f"  - {u}")
        return 0

    if cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: python -m scripts.manage_users add USERNAME PASSWORD")
            return 1
        username = sys.argv[2].strip().lower()
        password = sys.argv[3]
        if not username or not password:
            print("Username and password are required.")
            return 1
        users[username] = generate_password_hash(password)
        save(users)
        print(f"Added/updated user: {username}")
        return 0

    if cmd in ("remove", "rm", "del", "delete"):
        if len(sys.argv) < 3:
            print("Usage: python -m scripts.manage_users remove USERNAME")
            return 1
        username = sys.argv[2].strip().lower()
        if username in users:
            del users[username]
            save(users)
            print(f"Removed user: {username}")
        else:
            print(f"No such user: {username}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
