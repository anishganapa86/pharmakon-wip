from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .ai import AIFailure

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOKENS = 1500
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "scan_system.md"
READ_FAILED_MESSAGE = "Couldn't read the label. Try again or enter it manually."
USER_INSTRUCTION = "Transcribe this product label."


class ScanIngredient(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    strength: str | None = Field(default=None, max_length=60)
    unit: str | None = Field(default=None, max_length=30)
    role: Literal["active", "inactive", "unknown"] = "unknown"


class ScanResult(BaseModel):
    product_name: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)
    product_type: Literal[
        "oral_medication", "supplement", "topical", "food_drink", "unknown"
    ] = "unknown"
    ingredients: list[ScanIngredient] = Field(default_factory=list, max_length=80)
    label_text_quality: Literal["clear", "partial", "unreadable"]
    notes: str | None = Field(default=None, max_length=300)


def sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def image_contents(data: bytes, mime: str) -> list[dict]:
    return [
        {
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": mime, "data": data}},
                {"text": USER_INSTRUCTION},
            ],
        }
    ]


def parse_scan(text: str) -> dict:
    try:
        result = ScanResult.model_validate_json(text)
    except ValidationError as exc:
        raise AIFailure from exc
    if result.label_text_quality == "unreadable":
        result = result.model_copy(update={"ingredients": []})
    return result.model_dump()
