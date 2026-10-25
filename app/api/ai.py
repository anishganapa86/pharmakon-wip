from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "gemini-3.5-flash-lite"
TIMEOUT_MS = 20_000
HOUR = 3600.0
DAY = 86400.0
TABS = ("concentration", "timeline", "skincare", "interactions", "food")
TabName = Literal["concentration", "timeline", "skincare", "interactions", "food"]
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "chat_system.md"

BUSY_MESSAGE = "The demo assistant is busy, try again later."
UNAVAILABLE_MESSAGE = "The demo assistant isn't set up on this server."
FAILED_MESSAGE = "The demo assistant couldn't answer. Try again."


class AIUnavailable(Exception):
    pass


class AIQuotaExceeded(Exception):
    pass


class AIFailure(Exception):
    pass


class Generator(Protocol):
    def generate(
        self,
        *,
        system: str,
        contents: list[Any],
        schema: type[BaseModel],
        max_output_tokens: int,
    ) -> str: ...


class GeminiGenerator:
    def generate(
        self,
        *,
        system: str,
        contents: list[Any],
        schema: type[BaseModel],
        max_output_tokens: int,
    ) -> str:
        from google import genai
        from google.genai import errors, types

        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise AIUnavailable
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        try:
            response = client.models.generate_content(
                model=model_name(),
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_output_tokens,
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=schema,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                ),
            )
        except errors.APIError as exc:
            if getattr(exc, "code", None) == 429:
                raise AIQuotaExceeded from exc
            raise AIFailure from exc
        except Exception as exc:
            raise AIFailure from exc
        text = response.text
        if not text:
            raise AIFailure
        return text


def model_name() -> str:
    return os.environ.get("PHARMAKON_AI_MODEL", "").strip() or DEFAULT_MODEL


def has_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def chat_per_hour() -> int:
    return env_int("PHARMAKON_CHAT_PER_HOUR", 20)


def scan_per_hour() -> int:
    return env_int("PHARMAKON_SCAN_PER_HOUR", 10)


def daily_cap() -> int:
    return env_int("PHARMAKON_AI_DAILY_CAP", 200)


class UsageLimits:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.windows: dict[str, deque[float]] = {}

    def reset(self) -> None:
        self.windows.clear()

    def _trim(self, key: str, window: float) -> deque[float]:
        now = self.clock()
        stamps = self.windows.setdefault(key, deque())
        while stamps and now - stamps[0] >= window:
            stamps.popleft()
        return stamps

    def _retry_after(self, stamps: deque[float], window: float) -> int:
        return max(1, int(window - (self.clock() - stamps[0])) + 1)

    def try_use(self, user_key: str, per_hour: int) -> tuple[str, int] | None:
        day = self._trim("global", DAY)
        hour = self._trim(user_key, HOUR)
        if len(day) >= daily_cap():
            return "daily", self._retry_after(day, DAY)
        if len(hour) >= per_hour:
            return "hourly", self._retry_after(hour, HOUR)
        now = self.clock()
        day.append(now)
        hour.append(now)
        return None


usage = UsageLimits()


def load_system_prompt(survey_context: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{survey_context}", survey_context)


def survey_context(row: dict | None) -> str:
    if not row or row.get("status") != "completed":
        return "- no answers"
    return "\n".join(
        [
            f"- tracking: {', '.join(row['tracks']) or 'none'}",
            f"- goals: {', '.join(row['goals']) or 'none'}",
            f"- experience with tracking: {row['experience'] or 'unknown'}",
            f"- guidance wanted: {row['guidance'] or 'unknown'}",
        ]
    )


class ChatAnswer(BaseModel):
    reply: str
    suggested_tab: str | None = None


def parse_chat_answer(text: str) -> dict:
    try:
        answer = ChatAnswer.model_validate_json(text)
    except ValidationError as exc:
        raise AIFailure from exc
    reply = answer.reply.strip()
    if not reply:
        raise AIFailure
    tab = answer.suggested_tab if answer.suggested_tab in TABS else None
    return {"reply": reply[:1200], "suggested_tab": tab}
