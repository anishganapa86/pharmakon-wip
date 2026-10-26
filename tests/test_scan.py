import json
import secrets
import struct
import tempfile
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import ai, main, scan
from app.api.db import Base, DoseEvent, UserProduct

pytestmark = pytest.mark.real_auth

PASSWORD = secrets.token_urlsafe(16)


def make_png(width=4, height=4):
    def chunk(kind, payload):
        body = kind + payload
        return (
            struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
        )

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_jpeg(extra=0):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * extra + b"\xff\xd9"


def make_webp():
    return b"RIFF" + struct.pack("<I", 12) + b"WEBPVP8 " + b"\x00" * 8


GOOD = {
    "product_name": "Extra Strength Pain Reliever",
    "brand": "Example Brand",
    "product_type": "oral_medication",
    "ingredients": [
        {"name": "Acetaminophen", "strength": "500", "unit": "mg", "role": "active"},
        {"name": "Stearic acid", "strength": None, "unit": None, "role": "inactive"},
    ],
    "label_text_quality": "clear",
    "notes": None,
}


class Stub:
    def __init__(self, text=None, error=None):
        self.text = json.dumps(GOOD) if text is None else text
        self.error = error
        self.calls = []

    def generate(self, *, system, contents, schema, max_output_tokens):
        self.calls.append(
            {
                "system": system,
                "contents": contents,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture
def maker():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def stub():
    return Stub()


@pytest.fixture
def make_client(maker, stub):
    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_generator] = lambda: stub
    yield lambda: TestClient(main.app)
    main.app.dependency_overrides.clear()


def sign_up(client, email="a@example.com"):
    r = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_image(client, user_id, data, content_type="image/jpeg"):
    return client.post(
        f"/users/{user_id}/scan",
        content=data,
        headers={"Content-Type": content_type},
    )


def test_a_good_label_comes_back_validated(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    r = post_image(client, uid, make_jpeg())
    assert r.status_code == 200
    assert r.json() == GOOD


@pytest.mark.parametrize(
    "data,mime",
    [
        (make_jpeg(), "image/jpeg"),
        (make_png(), "image/png"),
        (make_webp(), "image/webp"),
    ],
)
def test_each_allowed_type_is_sniffed_and_sent_with_its_real_type(
    make_client, stub, data, mime
):
    client = make_client()
    uid = sign_up(client)
    assert post_image(client, uid, data, "application/octet-stream").status_code == 200
    part = stub.calls[0]["contents"][0]["parts"][0]["inline_data"]
    assert part["mime_type"] == mime
    assert part["data"] == data


def test_the_model_gets_the_scan_prompt_schema_and_token_cap(make_client, stub):
    client = make_client()
    post_image(client, sign_up(client), make_jpeg())
    call = stub.calls[0]
    assert call["schema"] is scan.GEMINI_SCHEMA
    assert call["max_output_tokens"] == scan.MAX_OUTPUT_TOKENS
    assert call["system"] == scan.load_prompt()
    assert "Never infer" in call["system"]


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + b"\x00" * 20,
        b"%PDF-1.4 fake",
        b"<html><script>alert(1)</script></html>",
        b"plain text pretending to be an image",
        b"\xff\xd8",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"\x89PNG\r\n",
    ],
)
def test_wrong_magic_bytes_are_415_even_with_an_image_content_type(
    make_client, stub, data
):
    client = make_client()
    r = post_image(client, sign_up(client), data, "image/jpeg")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_image"
    assert stub.calls == []


def test_an_empty_body_is_422(make_client, stub):
    client = make_client()
    assert post_image(client, sign_up(client), b"").status_code == 422
    assert stub.calls == []


def test_the_size_cap_allows_exactly_8_mb_and_rejects_one_more_byte(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    head = b"\xff\xd8\xff\xe0"
    at_cap = head + b"\x00" * (scan.MAX_IMAGE_BYTES - len(head))
    assert post_image(client, uid, at_cap).status_code == 200
    over = at_cap + b"\x00"
    r = post_image(client, uid, over)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "image_too_large"
    assert len(stub.calls) == 1


def test_the_cap_is_enforced_while_reading_without_a_content_length(make_client, stub):
    client = make_client()
    uid = sign_up(client)

    def chunks():
        yield b"\xff\xd8\xff\xe0"
        for _ in range(9):
            yield b"\x00" * (1024 * 1024)

    r = client.post(f"/users/{uid}/scan", content=chunks())
    assert r.status_code == 413
    assert stub.calls == []


def test_nothing_is_written_to_disk(make_client, stub, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("the scan wrote a file")

    for name in (
        "TemporaryFile",
        "NamedTemporaryFile",
        "SpooledTemporaryFile",
        "mkstemp",
        "mkdtemp",
    ):
        monkeypatch.setattr(tempfile, name, forbidden)
    monkeypatch.setattr("pathlib.Path.write_bytes", forbidden)
    monkeypatch.setattr("pathlib.Path.write_text", forbidden)
    monkeypatch.chdir(tmp_path)
    client = make_client()
    uid = sign_up(client)
    assert post_image(client, uid, make_png()).status_code == 200
    assert list(tmp_path.iterdir()) == []


def test_nothing_is_saved_to_the_account(make_client, stub, maker):
    client = make_client()
    uid = sign_up(client)
    post_image(client, uid, make_jpeg())
    with maker() as s:
        assert s.scalar(select(func.count()).select_from(UserProduct)) == 0
        assert s.scalar(select(func.count()).select_from(DoseEvent)) == 0
    assert client.get(f"/users/{uid}/my-products").json() == []
    assert client.get(f"/users/{uid}/doses").json() == []


def test_the_image_bytes_are_not_logged(make_client, stub, caplog):
    client = make_client()
    uid = sign_up(client)
    marker = secrets.token_hex(8).encode()
    data = make_jpeg() + marker
    with caplog.at_level("DEBUG"):
        post_image(client, uid, data)
    assert marker.decode() not in caplog.text
    assert repr(marker) not in caplog.text


def test_an_unreadable_label_returns_no_ingredients(make_client, stub):
    stub.text = json.dumps(
        {**GOOD, "label_text_quality": "unreadable", "ingredients": GOOD["ingredients"]}
    )
    client = make_client()
    body = post_image(client, sign_up(client), make_jpeg()).json()
    assert body["label_text_quality"] == "unreadable"
    assert body["ingredients"] == []


def test_a_partial_label_passes_through(make_client, stub):
    stub.text = json.dumps(
        {**GOOD, "label_text_quality": "partial", "notes": "Cut off at the edge."}
    )
    client = make_client()
    body = post_image(client, sign_up(client), make_jpeg()).json()
    assert body["label_text_quality"] == "partial"
    assert body["notes"] == "Cut off at the edge."


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "{}",
        json.dumps({**GOOD, "product_type": "weapon"}),
        json.dumps({**GOOD, "label_text_quality": "great"}),
        json.dumps({**GOOD, "ingredients": "water"}),
        json.dumps({**GOOD, "ingredients": [{"name": "x", "role": "boss"}]}),
    ],
)
def test_malformed_model_output_is_502_with_the_manual_entry_message(
    make_client, stub, text
):
    stub.text = text
    client = make_client()
    r = post_image(client, sign_up(client), make_jpeg())
    assert r.status_code == 502
    assert (
        r.json()["error"]["message"]
        == "Couldn't read the label. Try again or enter it manually."
    )


def test_an_upstream_failure_is_502(make_client, stub):
    stub.error = ai.AIFailure()
    client = make_client()
    assert post_image(client, sign_up(client), make_jpeg()).status_code == 502


def test_a_quota_error_is_429_busy_and_never_retried(make_client, stub):
    stub.error = ai.AIQuotaExceeded()
    client = make_client()
    r = post_image(client, sign_up(client), make_jpeg())
    assert r.status_code == 429
    assert (
        r.json()["error"]["message"] == "The demo assistant is busy, try again later."
    )
    assert len(stub.calls) == 1


def test_no_key_is_503_and_uses_no_quota(make_client, monkeypatch):
    main.app.dependency_overrides[main.get_generator] = lambda: ai.GeminiGenerator()
    monkeypatch.setenv("GEMINI_API_KEY", "")
    client = make_client()
    r = post_image(client, sign_up(client), make_jpeg())
    assert r.status_code == 503
    assert ai.usage.windows == {}


def test_logged_out_is_401(make_client):
    assert post_image(make_client(), "x", make_jpeg()).status_code == 401


def test_another_users_id_is_403_and_calls_nothing(make_client, stub):
    a = make_client()
    sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    assert post_image(a, b_id, make_jpeg()).status_code == 403
    assert stub.calls == []


def test_the_hourly_scan_limit_defaults_to_ten(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    for _ in range(10):
        assert post_image(client, uid, make_jpeg()).status_code == 200
    r = post_image(client, uid, make_jpeg())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert len(stub.calls) == 10


def test_rejected_uploads_do_not_use_the_quota(make_client, stub, monkeypatch):
    monkeypatch.setenv("PHARMAKON_SCAN_PER_HOUR", "1")
    client = make_client()
    uid = sign_up(client)
    for _ in range(3):
        assert post_image(client, uid, b"GIF89a....").status_code == 415
    assert post_image(client, uid, make_jpeg()).status_code == 200


def test_chats_and_scans_share_the_daily_cap(make_client, stub, monkeypatch):
    monkeypatch.setenv("PHARMAKON_AI_DAILY_CAP", "2")
    client = make_client()
    uid = sign_up(client)
    stub.text = json.dumps({"reply": "Hi.", "suggested_tab": None})
    chat = client.post(
        f"/users/{uid}/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert chat.status_code == 200
    stub.text = json.dumps(GOOD)
    assert post_image(client, uid, make_jpeg()).status_code == 200
    r = post_image(client, uid, make_jpeg())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ai_daily_cap"


def test_ingredient_matching_uses_the_registry(make_client):
    client = make_client()
    sign_up(client)
    r = client.post(
        "/ingredients/match",
        json={"names": ["Acetaminophen", "paracetamol", "Stearic acid", "Retinol"]},
    )
    assert r.status_code == 200
    by_name = {x["name"]: x for x in r.json()["results"]}
    assert by_name["Acetaminophen"]["matched"] is True
    assert by_name["Acetaminophen"]["substance_id"] == "acetaminophen"
    assert by_name["paracetamol"]["substance_id"] == "acetaminophen"
    assert by_name["Stearic acid"] == {
        "name": "Stearic acid",
        "matched": False,
        "substance_id": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"names": []},
        {"names": ["x"] * 81},
        {"names": ["x" * 201]},
        {},
        {"names": ["a"], "x": 1},
    ],
)
def test_ingredient_matching_rejects_bad_bodies(make_client, payload):
    client = make_client()
    sign_up(client)
    assert client.post("/ingredients/match", json=payload).status_code == 422


def test_ingredient_matching_needs_login(make_client):
    assert (
        make_client().post("/ingredients/match", json={"names": ["a"]}).status_code
        == 401
    )


def test_a_truncated_model_answer_is_200_partial_not_502(make_client, stub):
    full = json.dumps(GOOD)
    stub.text = full[: full.index("Stearic") + 6]
    client = make_client()
    r = post_image(client, sign_up(client), make_jpeg())
    assert r.status_code == 200
    body = r.json()
    assert body["label_text_quality"] == "partial"
    assert [i["name"] for i in body["ingredients"]] == ["Acetaminophen"]


def test_a_partial_model_answer_is_200_with_what_was_printed(make_client, stub):
    stub.text = json.dumps(
        {
            "product_name": None,
            "brand": None,
            "product_type": "unknown",
            "ingredients": [
                {
                    "name": "Acetaminophen",
                    "strength": 500,
                    "unit": "mg",
                    "role": "active",
                }
            ],
            "label_text_quality": "partial",
            "notes": None,
        }
    )
    client = make_client()
    body = post_image(client, sign_up(client), make_jpeg()).json()
    assert body["label_text_quality"] == "partial"
    assert body["ingredients"][0]["strength"] == "500"
    assert body["product_name"] is None
