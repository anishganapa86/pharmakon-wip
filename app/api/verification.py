from __future__ import annotations

import os

SHOW_UNVERIFIED_RULES_ENV = "SHOW_UNVERIFIED_RULES"
TRUTHY = frozenset({"1", "true", "yes", "on"})
REQUIRED_WHEN_VERIFIED = ("verified_by", "verified_on", "source_url", "source_quote")


def show_unverified_rules() -> bool:
    value = os.environ.get(SHOW_UNVERIFIED_RULES_ENV, "")
    return value.strip().lower() in TRUTHY


def missing_verification_fields(entry: dict) -> list[str]:
    if entry.get("verified") is not True:
        return []
    return [
        field
        for field in REQUIRED_WHEN_VERIFIED
        if not str(entry.get(field) or "").strip()
    ]


def awaiting_message(count: int) -> str:
    if count == 1:
        return "1 rule is awaiting verification and is not being applied."
    return f"{count} rules are awaiting verification and are not being applied."


def unapplied_notice(unverified_count: int) -> dict | None:
    if show_unverified_rules() or unverified_count <= 0:
        return None
    return {"count": unverified_count, "message": awaiting_message(unverified_count)}


def attach_notice(body: dict, unverified_count: int) -> dict:
    notice = unapplied_notice(unverified_count)
    if notice is not None:
        body["rules_awaiting_verification"] = notice
    return body
