"""A tiny JSON file that remembers the research results between restarts.

Without it, a restart would forget everything and treat every item as new.
A missing or damaged file is never fatal: we start empty and rewrite it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.core.resources import APP_DIR

logger = logging.getLogger(__name__)


def resolve_store_path(setting: str) -> Path | None:
    """Relative paths are relative to the project folder. Empty means: do not save."""
    text = (setting or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else APP_DIR.parent / path


class JsonFileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s (%s). Starting empty.", self.path, exc)
            return {}

    def save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)  # replace in one step so a crash cannot leave half a file
        except OSError as exc:
            logger.warning("Could not save %s (%s). Results stay in memory only.", self.path, exc)
