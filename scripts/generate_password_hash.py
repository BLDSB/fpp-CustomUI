"""
Generate a bcrypt password hash for use in .env as ADMIN_PASSWORD_HASH.

Usage:
    python scripts/generate_password_hash.py
"""

import getpass
import bcrypt


def main():
    password = getpass.getpass("Enter password: ")
    if not password:
        print("Error: password cannot be empty.")
        raise SystemExit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match.")
        raise SystemExit(1)

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    print("\nAdd this line to your .env file:")
    print(f"ADMIN_PASSWORD_HASH={hashed.decode('utf-8')}")


if __name__ == "__main__":
    main()
