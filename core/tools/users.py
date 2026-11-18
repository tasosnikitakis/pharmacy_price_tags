"""Add or list admin UI users: ``python -m core.tools.users add|list``.

Create the first admin with::

    python -m core.tools.users add --email admin@example.com --role admin

The password is asked for twice without echo; it is never taken as an argument, so
it never lands in the shell history or the process list. Further users are best added
on the Settings page. Uses ``DATABASE_URL`` and the other settings from the
environment, like the core.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.db.models import ROLES
from core.services.users import UserError, create_user, list_users

#: ``event_log.actor`` for changes made with this tool.
ACTOR = "cli:users"

#: Messages for ``UserError`` codes (this tool talks to an operator, in English).
_MESSAGES = {
    "user.email_invalid": "the email address is not valid",
    "user.exists": "a user with this email already exists",
    "user.password_too_short": "the password must have at least 10 characters",
    "user.password_too_long": "the password is too long",
    "user.role_invalid": "unknown role",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.tools.users", description="Add or list admin UI users."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add a user; asks for the password")
    add.add_argument("--email", required=True)
    add.add_argument("--role", required=True, choices=ROLES)
    commands.add_parser("list", help="list the users")
    return parser


def run(
    session: Session,
    argv: Sequence[str],
    ask: Callable[[str], str] = getpass.getpass,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Run the tool on ``session``, committing an addition. Returns the exit code and
    the text to print. ``ask`` reads a password without echo."""
    args = build_parser().parse_args(argv)
    if args.command == "list":
        lines = [
            f"{u.email}\t{u.role}\t{'active' if u.active else 'inactive'}"
            for u in list_users(session)
        ]
        return 0, "\n".join(lines) if lines else "no users"
    password = ask("Password: ")
    if ask("Password again: ") != password:
        return 1, "not added: the passwords do not match"
    try:
        user = create_user(
            session, args.email, password, args.role, actor=ACTOR, now=now or datetime.now(UTC)
        )
    except UserError as exc:
        session.rollback()
        return 1, f"not added: {_MESSAGES.get(exc.code, exc.code)}"
    session.commit()
    return 0, f"added: {user.email} ({user.role})"


def main(argv: Sequence[str] | None = None) -> int:
    from core.db.session import get_sessionmaker

    with get_sessionmaker()() as session:
        code, output = run(session, sys.argv[1:] if argv is None else argv)
    print(output, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
