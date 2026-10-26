from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

USER = ""
MY_PRODUCT_TEXT = "Water, Glycerin (Kosher, Vegetable), 1,2-Hexanediol, Retinol"
MY_PRODUCT_NAME = "Smoke Retinol Serum"


@dataclass
class Smoke:
    client: httpx.Client
    entries: list[dict] = field(default_factory=list)

    def call(
        self,
        name: str,
        method: str,
        path: str,
        expected: int,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> tuple[dict, object]:
        start = time.perf_counter()
        flags: list[str] = []
        try:
            response = self.client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            entry = {
                "name": name,
                "method": method,
                "path": path,
                "status": 0,
                "expected_status": expected,
                "latency_ms": (time.perf_counter() - start) * 1000,
                "body_shape": "",
                "flags": [f"request failed: {type(exc).__name__}: {exc}"],
            }
            self.entries.append(entry)
            return entry, None
        latency = (time.perf_counter() - start) * 1000
        status = response.status_code
        body: object = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                flags.append("body is not JSON")
        if status >= 500:
            flags.append(f"server error {status}")
        elif status != expected:
            if 400 <= status < 500 and 200 <= expected < 300:
                flags.append(f"unexpected {status} where {expected} was expected")
            else:
                flags.append(f"status {status}, expected {expected}")
        if expected != 204 and not response.content:
            flags.append("empty body")
        entry = {
            "name": name,
            "method": method,
            "path": path,
            "status": status,
            "expected_status": expected,
            "latency_ms": latency,
            "body_shape": shape(body),
            "flags": flags,
        }
        self.entries.append(entry)
        return entry, body

    def expect(self, entry: dict, condition: bool, message: str) -> None:
        if not condition:
            entry["flags"].append(f"expectation failed: {message}")


def shape(body: object) -> str:
    if isinstance(body, dict):
        return "{" + ", ".join(sorted(body)) + "}"
    if isinstance(body, list):
        if body and isinstance(body[0], dict):
            return f"list[{len(body)}] of {{" + ", ".join(sorted(body[0])) + "}"
        return f"list[{len(body)}]"
    if body is None:
        return ""
    return type(body).__name__


def iso(moment: datetime) -> str:
    return moment.isoformat()


def find_product(smoke: Smoke, query: str, brand: str) -> str | None:
    _, body = smoke.call(
        f"search {query}", "GET", "/products/search", 200, params={"q": query}
    )
    if not isinstance(body, dict):
        return None
    for result in body.get("results", []):
        if result["brand_name"].lower() == brand.lower():
            return result["id"]
    return None


def log_dose(
    smoke: Smoke, name: str, product_id: str, quantity: float, now: datetime
) -> tuple[dict, object]:
    return smoke.call(
        name,
        "POST",
        f"/users/{USER}/doses",
        201,
        json_body={
            "product_id": product_id,
            "taken_at": iso(now - timedelta(minutes=5)),
            "quantity": quantity,
        },
    )


def sign_up(smoke: Smoke) -> bool:
    global USER
    credentials = {
        "email": f"smoke-{uuid.uuid4().hex[:10]}@example.com",
        "password": secrets.token_urlsafe(18),
    }
    entry, body = smoke.call(
        "sign up", "POST", "/auth/signup", 201, json_body=credentials
    )
    if not isinstance(body, dict) or not body.get("id"):
        return False
    USER = body["id"]
    entry, body = smoke.call("auth me", "GET", "/auth/me", 200)
    if isinstance(body, dict):
        smoke.expect(entry, body.get("id") == USER, "auth/me returned another user")
    with httpx.Client(base_url=smoke.client.base_url, timeout=60) as anonymous:
        logged_out = Smoke(anonymous)
        logged_out.call("logged-out doses refused", "GET", f"/users/{USER}/doses", 401)
        logged_out.call("logged-out search refused", "GET", "/products/search", 401)
        smoke.entries.extend(logged_out.entries)
    return True


def run(base: str) -> list[dict]:
    now = datetime.now(UTC)
    with httpx.Client(base_url=base, timeout=60) as client:
        smoke = Smoke(client)
        if not sign_up(smoke):
            return smoke.entries

        ids = {
            "nodoz": find_product(smoke, "NoDoz", "NoDoz"),
            "tylenol": find_product(
                smoke, "Tylenol Extra Strength", "Tylenol Extra Strength"
            ),
            "dayquil": find_product(smoke, "DayQuil", "DayQuil"),
            "advil": find_product(smoke, "Advil", "Advil"),
            "aleve": find_product(smoke, "Aleve", "Aleve"),
        }
        for key, value in ids.items():
            if value is None:
                smoke.entries.append(
                    {
                        "name": f"resolve {key}",
                        "method": "GET",
                        "path": "/products/search",
                        "status": 0,
                        "expected_status": 200,
                        "latency_ms": 0.0,
                        "body_shape": "",
                        "flags": [f"catalog product for {key} not found"],
                    }
                )
        if any(v is None for v in ids.values()):
            return smoke.entries

        entry, _ = log_dose(smoke, "log NoDoz x1", ids["nodoz"], 1, now)
        entry, body = smoke.call(
            "caffeine curve",
            "GET",
            f"/users/{USER}/curve",
            200,
            params={
                "substance_id": "caffeine",
                "from": iso(now - timedelta(hours=2)),
                "to": iso(now + timedelta(hours=10)),
            },
        )
        points = body.get("points") if isinstance(body, dict) else None
        smoke.expect(entry, bool(points), "curve has no points")

        log_dose(smoke, "log Tylenol Extra Strength x6", ids["tylenol"], 6, now)
        log_dose(smoke, "log DayQuil x2", ids["dayquil"], 2, now)
        entry, body = smoke.call("alerts", "GET", f"/users/{USER}/alerts", 200)
        alerts = body if isinstance(body, list) else []
        duplication = [
            a
            for a in alerts
            if a.get("kind") == "duplication"
            and a.get("substance", {}).get("id") == "acetaminophen"
        ]
        smoke.expect(
            entry,
            len(duplication) == 1,
            f"expected one acetaminophen duplication alert, got {len(duplication)}",
        )
        if len(duplication) == 1:
            alert = duplication[0]
            smoke.expect(
                entry,
                alert.get("severity") == "caution",
                f"severity is {alert.get('severity')!r}, expected 'caution'",
            )
            names = {d["product_name"] for d in alert.get("contributing_doses", [])}
            smoke.expect(
                entry,
                {"Tylenol Extra Strength", "DayQuil"} <= names,
                f"contributing doses name {sorted(names)}",
            )

        for key, label in (("advil", "Advil"), ("aleve", "Aleve")):
            smoke.call(
                f"schedule {label}",
                "POST",
                f"/users/{USER}/schedule",
                201,
                json_body={
                    "product_id": ids[key],
                    "frequency": "daily",
                    "times": ["08:00"],
                },
            )
        entry, body = smoke.call(
            "interaction-check", "GET", f"/users/{USER}/interaction-check", 200
        )
        if isinstance(body, dict):
            smoke.expect(
                entry,
                bool(body.get("interactions")),
                "no interaction reported for Advil + Aleve",
            )

        entry, body = smoke.call(
            "food-guidance", "GET", f"/users/{USER}/food-guidance", 200
        )
        if isinstance(body, dict):
            smoke.expect(entry, bool(body.get("guidance")), "guidance is empty")

        entry, body = smoke.call(
            "skincare-check retinol + salicylic acid",
            "POST",
            "/skincare-check",
            200,
            json_body={
                "products": [
                    {"name": "Retinol", "ingredients": ["retinol"]},
                    {"name": "Salicylic acid", "ingredients": ["salicylic acid"]},
                ]
            },
        )
        if isinstance(body, dict):
            smoke.expect(
                entry,
                bool(body.get("incompatibilities")),
                "retinol + salicylic acid produced no incompatibility",
            )

        log_dose(smoke, "log NoDoz x2", ids["nodoz"], 2, now)
        entry, body = smoke.call(
            "symptom-check headache",
            "POST",
            "/symptom-check",
            200,
            json_body={"symptom": "headache"},
        )
        if isinstance(body, dict):
            smoke.expect(
                entry,
                any(
                    m.get("substance", {}).get("id") == "caffeine"
                    and m.get("source") == "local_reference"
                    for m in body.get("matches", [])
                ),
                "no local-reference caffeine match for headache",
            )

        entry, body = smoke.call(
            "search zzqxv",
            "GET",
            "/products/search",
            200,
            params={"q": "zzqxv"},
        )
        if isinstance(body, dict):
            smoke.expect(
                entry,
                body.get("results") == [] and body.get("total") == 0,
                "search for zzqxv returned results",
            )

        for quantity in (0, -1):
            smoke.call(
                f"quantity {quantity}",
                "POST",
                f"/users/{USER}/doses",
                422,
                json_body={
                    "product_id": ids["nodoz"],
                    "taken_at": iso(now),
                    "quantity": quantity,
                },
            )

        entry, body = smoke.call(
            "create my product",
            "POST",
            f"/users/{USER}/my-products",
            201,
            json_body={
                "brand": "Smoke",
                "name": MY_PRODUCT_NAME,
                "category": "serum",
                "raw_text": MY_PRODUCT_TEXT,
            },
        )
        my_id = None
        if isinstance(body, dict):
            my_id = body.get("id")
            count = len(body.get("ingredients", []))
            smoke.expect(entry, count == 4, f"stored {count} ingredients, expected 4")
        entry, body = smoke.call(
            "search my product",
            "GET",
            "/products/search",
            200,
            params={"q": MY_PRODUCT_NAME},
        )
        if isinstance(body, dict):
            smoke.expect(
                entry,
                any(r["id"] == my_id for r in body.get("results", [])),
                "saved product not found by search",
            )
        if my_id:
            log_dose(smoke, "log my product", my_id, 1, now)
        entry, body = smoke.call(
            "skincare-check logged", "GET", f"/users/{USER}/skincare-check", 200
        )
        if isinstance(body, dict):
            names = [p.get("name") for p in body.get("products", [])]
            smoke.expect(
                entry,
                any(MY_PRODUCT_NAME in str(n) for n in names),
                "logged product missing from skincare-check",
            )

        entry, body = smoke.call(
            "create timeline log entry",
            "POST",
            f"/users/{USER}/log",
            201,
            json_body={"category": "skin", "value": "dry"},
        )
        if isinstance(body, dict) and body.get("id"):
            log_id = body["id"]
            entry, body = smoke.call(
                "timeline contains log entry", "GET", f"/users/{USER}/timeline", 200
            )
            if isinstance(body, list):
                smoke.expect(
                    entry,
                    any(e.get("id") == log_id for e in body),
                    "new log entry not in timeline",
                )
            smoke.call(
                "delete timeline log entry",
                "DELETE",
                f"/users/{USER}/log/{log_id}",
                204,
            )

        return smoke.entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--out", default="reports/api_smoke.json")
    args = parser.parse_args()

    entries = run(args.base)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, indent=2))

    flagged = [e for e in entries if e["flags"]]
    for e in entries:
        mark = "FLAG" if e["flags"] else "ok"
        print(f"{mark:4} {e['status']:>3} {e['latency_ms']:7.0f}ms {e['name']}")
        for flag in e["flags"]:
            print(f"       {flag}")
    print(f"wrote {out}: {len(entries)} checks, {len(flagged)} flagged")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
