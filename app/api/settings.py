from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def env_list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(default)


def cookie_secure() -> bool:
    return env_flag("PHARMAKON_COOKIE_SECURE")
