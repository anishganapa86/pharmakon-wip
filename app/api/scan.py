from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .ai import AIFailure

logger = logging.getLogger("pharmakon.scan")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOKENS = 2048
MAX_INGREDIENTS = 80
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "scan_system.md"
READ_FAILED_MESSAGE = "Couldn't read the label. Try again or enter it manually."
TRUNCATED_NOTE = "The label may be longer than what was read."
USER_INSTRUCTION = "Transcribe this product label."

PRODUCT_TYPES = ("oral_medication", "supplement", "topical", "food_drink", "unknown")
ROLES = ("active", "inactive", "unknown")
QUALITIES = ("clear", "partial", "unreadable")

GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "product_name": {"type": "STRING", "nullable": True},
        "brand": {"type": "STRING", "nullable": True},
        "product_type": {"type": "STRING", "enum": list(PRODUCT_TYPES)},
        "ingredients": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "strength": {"type": "STRING", "nullable": True},
                    "unit": {"type": "STRING", "nullable": True},
                    "role": {"type": "STRING", "enum": list(ROLES)},
                },
                "required": ["name", "role"],
            },
        },
        "label_text_quality": {"type": "STRING", "enum": list(QUALITIES)},
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["product_type", "ingredients", "label_text_quality"],
}


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


def clean_text(value: object, limit: int) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def strip_fences(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL)
    return match.group(1) if match else stripped


STRING_FIELD = r'"{key}"\s*:\s*("(?:[^"\\]|\\.)*"|null)'


def salvage(text: str) -> dict | None:
    found: dict[str, Any] = {}
    for key in ("product_name", "brand", "product_type", "label_text_quality", "notes"):
        match = re.search(STRING_FIELD.format(key=key), text)
        if match:
            try:
                found[key] = json.loads(match.group(1))
            except ValueError:
                continue
    ingredients: list[Any] = []
    start = re.search(r'"ingredients"\s*:\s*\[', text)
    if start:
        decoder = json.JSONDecoder()
        at = start.end()
        while at < len(text):
            while at < len(text) and text[at] in " \r\n\t,":
                at += 1
            if at >= len(text) or text[at] != "{":
                break
            try:
                item, at = decoder.raw_decode(text, at)
            except ValueError:
                break
            ingredients.append(item)
        found["ingredients"] = ingredients
    if not found:
        return None
    found["_salvaged"] = True
    return found


def load_object(text: str) -> dict:
    body = strip_fences(text)
    try:
        data = json.loads(body)
    except ValueError:
        data = salvage(body)
        if data is None:
            logger.warning("scan output was not JSON and could not be salvaged")
            raise AIFailure from None
    if not isinstance(data, dict) or not (
        "ingredients" in data or "label_text_quality" in data
    ):
        logger.warning("scan output was not the expected object")
        raise AIFailure
    return data


def choice(value: object, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    raise AIFailure


def parse_scan(text: str) -> dict:
    data = load_object(text)
    salvaged = bool(data.get("_salvaged"))
    raw_items = data.get("ingredients", [])
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise AIFailure
    truncated = salvaged
    ingredients = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise AIFailure
        name = clean_text(item.get("name"), 200)
        if name is None:
            continue
        if len(ingredients) >= MAX_INGREDIENTS:
            truncated = True
            break
        ingredients.append(
            {
                "name": name,
                "strength": clean_text(item.get("strength"), 60),
                "unit": clean_text(item.get("unit"), 30),
                "role": choice(item.get("role"), ROLES) or "unknown",
            }
        )
    quality = choice(data.get("label_text_quality"), QUALITIES)
    if quality is None:
        quality = "partial" if ingredients or salvaged else "unreadable"
    if truncated and quality == "clear":
        quality = "partial"
    notes = clean_text(data.get("notes"), 300)
    if truncated and notes is None:
        notes = TRUNCATED_NOTE
    if quality == "unreadable":
        ingredients = []
    candidate = {
        "product_name": clean_text(data.get("product_name"), 200),
        "brand": clean_text(data.get("brand"), 200),
        "product_type": choice(data.get("product_type"), PRODUCT_TYPES) or "unknown",
        "ingredients": ingredients,
        "label_text_quality": quality,
        "notes": notes,
    }
    try:
        return ScanResult.model_validate(candidate).model_dump()
    except ValidationError as exc:
        logger.warning(
            "scan output failed validation at %s",
            sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}),
        )
        raise AIFailure from exc
