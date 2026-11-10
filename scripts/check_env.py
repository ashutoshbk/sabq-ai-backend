"""Step 1: check your conda environment. Works offline, never calls OpenAI.

Run from the project folder:
    python scripts/check_env.py
"""

import importlib
import os
import sys
from importlib import metadata

REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("openai", "openai"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("httpx", "httpx"),
    ("pytest", "pytest"),
]

failures = 0


def line(ok: bool, text: str) -> None:
    global failures
    if not ok:
        failures += 1
    print(("[ OK ] " if ok else "[FAIL] ") + text)


print(f"Python {sys.version.split()[0]} ({sys.executable})")
line(sys.version_info >= (3, 10), "Python 3.10 or newer")

for module, dist in REQUIRED:
    try:
        importlib.import_module(module)
        line(True, f"{dist} {metadata.version(dist)}")
    except Exception as exc:  # noqa: BLE001
        line(False, f"{dist} missing or broken ({type(exc).__name__}). Run: pip install -r requirements.txt")

try:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key="check")
    line(hasattr(client, "responses"), "openai SDK has the Responses API (needed for web search)")
except Exception as exc:  # noqa: BLE001
    line(False, f"openai SDK check failed: {exc}")

key = os.getenv("OPENAI_API_KEY", "")
line(bool(key), "OPENAI_API_KEY is set in this environment" + (f" (ends with ...{key[-4:]})" if len(key) > 8 else ""))

print()
print("All good. Next: pytest" if failures == 0 else f"{failures} problem(s). Fix them, then run this again.")
sys.exit(1 if failures else 0)
