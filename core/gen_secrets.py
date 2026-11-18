"""Print freshly generated secrets in .env format: ``python -m core.gen_secrets >> .env``.

Uses only the standard library, so it runs on Windows and on the shop box alike.
"""

from __future__ import annotations

import secrets


def generate() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "GATEWAY_TOKEN": secrets.token_urlsafe(48),
    }


if __name__ == "__main__":
    for key, value in generate().items():
        print(f"{key}={value}")
