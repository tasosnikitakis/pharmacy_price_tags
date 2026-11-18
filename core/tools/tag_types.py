"""Add or list tag types: ``python -m core.tools.tag_types add|list``.

Example, for development with the fake driver::

    python -m core.tools.tag_types add --key fake-2.9-bwr --vendor fake \\
        --model 2.9-bwr --width 296 --height 128 --palette BWR

``add`` is idempotent: adding the same type again changes nothing. It refuses a type
that clashes with an existing key, or vendor and model, and never changes one in place.
It uses ``DATABASE_URL`` and the other settings from the environment, like the core.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.orm import Session

from core.services.tag_types import TagTypeConflict, add_tag_type, list_tag_types
from shared.tag_types import Palette, TagType

#: ``event_log.actor`` for changes made with this tool.
ACTOR = "cli:tag_types"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.tools.tag_types",
        description="Add or list the tag types discovery matches tags against.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add a tag type (idempotent)")
    add.add_argument("--key", required=True, help='stable key, e.g. "gicisky-2.9-bwr"')
    add.add_argument("--vendor", required=True, help="driver vendor, as discovery reports it")
    add.add_argument("--model", required=True, help="tag model, as discovery reports it")
    add.add_argument("--width", required=True, type=int, help="width in pixels")
    add.add_argument("--height", required=True, type=int, help="height in pixels")
    add.add_argument("--palette", required=True, choices=[p.value for p in Palette])
    commands.add_parser("list", help="list the tag types")
    return parser


def run(session: Session, argv: Sequence[str], now: datetime | None = None) -> tuple[int, str]:
    """Run the tool on ``session``, committing an addition. Returns the exit code and
    the text to print. ``argparse`` exits with code 2 on bad arguments."""
    args = build_parser().parse_args(argv)
    if args.command == "list":
        lines = [
            f"{t.key}\t{t.vendor}\t{t.model}\t{t.width}x{t.height}\t{t.palette.value}"
            for t in list_tag_types(session)
        ]
        return 0, "\n".join(lines) if lines else "no tag types"
    try:
        tag_type = TagType(
            key=args.key,
            vendor=args.vendor,
            model=args.model,
            width=args.width,
            height=args.height,
            palette=Palette(args.palette),
        )
    except ValidationError as exc:
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        return 2, f"invalid tag type: {field}: {error['msg']}"
    try:
        added = add_tag_type(session, tag_type, actor=ACTOR, now=now or datetime.now(UTC))
    except TagTypeConflict as exc:
        session.rollback()
        return 1, f"not added: {exc}"
    session.commit()
    return 0, f"{'added' if added.created else 'already present, unchanged'}: {tag_type.key}"


def main(argv: Sequence[str] | None = None) -> int:
    from core.db.session import get_sessionmaker

    with get_sessionmaker()() as session:
        code, output = run(session, sys.argv[1:] if argv is None else argv)
    print(output, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
