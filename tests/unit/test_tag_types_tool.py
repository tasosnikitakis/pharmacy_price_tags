"""Argument parsing of ``python -m core.tools.tag_types``; no database needed."""

from __future__ import annotations

import pytest

from core.tools.tag_types import build_parser


def test_add_parses_every_field() -> None:
    args = build_parser().parse_args(
        ["add", "--key", "fake-2.9-bwr", "--vendor", "fake", "--model", "2.9-bwr",
         "--width", "296", "--height", "128", "--palette", "BWR"]
    )  # fmt: skip
    assert (args.command, args.key, args.vendor, args.model) == (
        "add",
        "fake-2.9-bwr",
        "fake",
        "2.9-bwr",
    )
    assert (args.width, args.height, args.palette) == (296, 128, "BWR")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["add", "--key", "k"],
        ["add", "--key", "k", "--vendor", "v", "--model", "m", "--width", "296",
         "--height", "128", "--palette", "RGB"],
        ["add", "--key", "k", "--vendor", "v", "--model", "m", "--width", "wide",
         "--height", "128", "--palette", "BW"],
    ],
)  # fmt: skip
def test_bad_arguments_exit_with_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2
