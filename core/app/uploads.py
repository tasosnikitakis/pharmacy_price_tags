"""Uploaded import files, kept between the preview and the apply request only.

A file lives in the system's temporary directory (never under the repository), under
a random token that only the uploader's session knows. It is deleted when the import
is applied or abandoned for a new upload, and any file older than ``MAX_AGE`` is
removed on the next upload. Works the same on Linux and Windows.
"""

from __future__ import annotations

import re
import secrets
import tempfile
import time
from pathlib import Path

#: Largest file accepted; a few thousand rows of CSV is well under 1 MB.
MAX_BYTES = 10 * 1024 * 1024
MAX_AGE_S = 60 * 60
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class UploadStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path(tempfile.gettempdir()) / "esl-import-uploads"

    def _path(self, token: str) -> Path | None:
        return self.directory / f"{token}.csv" if _TOKEN.match(token) else None

    def save(self, data: bytes) -> str:
        self.purge()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path = self._path(token)
        assert path is not None
        path.write_bytes(data)
        return token

    def load(self, token: str) -> bytes | None:
        path = self._path(token)
        if path is None or not path.is_file():
            return None
        return path.read_bytes()

    def delete(self, token: str) -> None:
        path = self._path(token)
        if path is not None:
            path.unlink(missing_ok=True)

    def purge(self, max_age_s: float = MAX_AGE_S) -> None:
        """Remove files older than ``max_age_s``."""
        if not self.directory.is_dir():
            return
        cutoff = time.time() - max_age_s
        for path in self.directory.glob("*.csv"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


def get_upload_store() -> UploadStore:
    """FastAPI dependency; tests override it with a store in a temporary directory."""
    return UploadStore()
