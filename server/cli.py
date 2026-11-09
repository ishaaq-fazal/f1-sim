"""Admin commands run on the server itself (locally, or in Render's Shell tab).

    python -m server.cli list-users
    python -m server.cli reset-password you@example.com
"""
from __future__ import annotations

import getpass
import sys

from sqlmodel import Session, select

from . import repo
from .auth import hash_password
from .db import User, engine, init_db


def main(argv: list[str]) -> int:
    init_db()
    if argv[:1] == ["list-users"]:
        with Session(engine) as s:
            for u in s.exec(select(User).order_by(User.id)):
                print(f"{u.id}\t{u.email}\t{u.role.value}")
        return 0
    if len(argv) == 2 and argv[0] == "reset-password":
        email = argv[1].strip().lower()
        with Session(engine) as s:
            user = repo.user_by_email(s, email)
            if user is None:
                print(f"No account for {email}. Existing accounts: python -m server.cli list-users")
                return 1
            pw = getpass.getpass("New password (10+ characters): ")
            if len(pw) < 10 or pw != getpass.getpass("Repeat it: "):
                print("Passwords were too short or did not match; nothing changed.")
                return 1
            user.password_hash = hash_password(pw)
            user.token_version += 1          # sign out existing sessions
            s.add(user)
            s.commit()
        print(f"Password updated for {email}.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
