from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("pharmakon.ai")

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
        schema: type[BaseModel] | dict,
        max_output_tokens: int,
    ) -> str: ...


class GeminiGenerator:
    def generate(
        self,
        *,
        system: str,
        contents: list[Any],
        schema: type[BaseModel] | dict,
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
            code = getattr(exc, "code", None)
            logger.warning(
                "gemini request failed: %s code=%s status=%s",
                type(exc).__name__,
                code,
                getattr(exc, "status", None),
            )
            if code == 429:
                raise AIQuotaExceeded from exc
            raise AIFailure from exc
        except Exception as exc:
            logger.warning("gemini request failed: %s", type(exc).__name__)
            raise AIFailure from exc
        text = response.text
        candidate = response.candidates[0] if response.candidates else None
        finish = getattr(candidate, "finish_reason", None)
        if not text or str(finish).rsplit(".", 1)[-1] != "STOP":
            usage = response.usage_metadata
            logger.warning(
                "gemini finish=%s text_chars=%s prompt_tokens=%s "
                "output_tokens=%s thought_tokens=%s",
                finish,
                len(text or ""),
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "thoughts_token_count", None),
            )
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


MAX_REPLY_WORDS = 150
MAX_REPLY_CHARS = 1500
ACTIONS = (
    "open_log_intake",
    "open_scan",
    "open_interactions",
    "open_schedule",
    "open_survey",
    "open_settings",
)
SAFETY_ASSERTIONS = re.compile(
    r"\bsafe\b"
    r"|\bsafely\b"
    r"|\bsafer\b"
    r"|\bharmless\b"
    r"|\bno\s+(risk|danger|harm)\b"
    r"|\b(risk|danger|harm)[- ]free\b"
    r"|\b(will|can|may|might|could|would|won't|does|do|did|to)\s+cause\b"
    r"|\bcauses?\b"
    r"|\bcaused\b"
    r"|\bprevents?\b"
    r"|\bprevented\b",
    re.IGNORECASE,
)
EFFICACY_CLAIMS = re.compile(
    r"\b(supports?|improves?|improved|improving|treats?|treated|treating|cures?"
    r"|relieves?|helps?\s+with|helps?\s+(to\s+)?(treat|relieve|reduce|improve|manage)"
    r"|used\s+(to|for)\s+(treat\w*|relie\w*|reduc\w*|improv\w*|support\w*|manag\w*|prevent\w*|help\w*))\b",
    re.IGNORECASE,
)
APP_SUBJECT = (
    r"(pharmakon|the app|this app|this tab|this panel|this screen|this feature"
    r"|scan|the scan button|log intake|the log intake panel|the schedule"
    r"|the (concentration|timeline|skincare|interactions|home|food(\s*&\s*drink)?) tab)"
)
APP_PHRASES = re.compile(
    rf"\b{APP_SUBJECT}\s+(supports?|helps?\s+you)\b"
    r"|\bhelps?\s+you\s+(find|log|track|check|see|understand|add|open|search|read|review|compare|keep)\b"
    r"|\bsupports?\s+(logging|scanning|tracking|checking|searching|reading|adding)\b",
    re.IGNORECASE,
)
NOT_SAFE = re.compile(r"\b(not|n't|isn't|aren't)\s+safe\b", re.IGNORECASE)
SAFE_QUESTION_FRAMES = re.compile(
    r"\b(whether|if)\s+(it|this|that|they|these)(\s+is|\s+are|'s)\s+safe(\s+for\s+you)?",
    re.IGNORECASE,
)
FRAME_CONTEXT = re.compile(
    r"\b(ask|asks|tell|tells|know|say|check|decide|determine|pharmacist|prescriber|doctor)\b",
    re.IGNORECASE,
)
SENTENCES = re.compile(r"[^.!?\n]+[.!?]?")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")


def strip_allowed_frames(text: str) -> str:
    kept = []
    for sentence in SENTENCES.findall(text):
        if FRAME_CONTEXT.search(sentence):
            sentence = SAFE_QUESTION_FRAMES.sub("", sentence)
        kept.append(sentence)
    return " ".join(kept)


def breaks_wording_rules(reply: str) -> bool:
    cleaned = strip_allowed_frames(normalize_text(reply))
    cleaned = NOT_SAFE.sub("", cleaned)
    if EFFICACY_CLAIMS.search(APP_PHRASES.sub(" ", cleaned)):
        return True
    return SAFETY_ASSERTIONS.search(cleaned) is not None


def cut_words(text: str, limit: int) -> str:
    count = 0
    for match in re.finditer(r"\S+", text):
        count += 1
        if count == limit:
            return text[: match.end()]
    return text


class ChatAnswer(BaseModel):
    reply: str
    suggested_tab: str | None = None
    suggested_action: str | None = None


def parse_chat_answer(text: str) -> dict:
    try:
        answer = ChatAnswer.model_validate_json(text)
    except ValidationError as exc:
        raise AIFailure from exc
    reply = answer.reply.strip()
    if not reply or breaks_wording_rules(reply):
        raise AIFailure
    reply = cut_words(reply, MAX_REPLY_WORDS)[:MAX_REPLY_CHARS]
    tab = answer.suggested_tab if answer.suggested_tab in TABS else None
    action = answer.suggested_action if answer.suggested_action in ACTIONS else None
    return {"reply": reply, "suggested_tab": tab, "suggested_action": action}
