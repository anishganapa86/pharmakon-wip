import json
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import ai, main
from app.api.db import Base

pytestmark = pytest.mark.real_auth

PASSWORD = secrets.token_urlsafe(16)
SURVEY = {
    "tracks": ["medications", "skincare"],
    "goals": ["avoid_interactions", "reminders"],
    "experience": "some",
    "guidance": "step_by_step",
    "status": "completed",
}


class Stub:
    def __init__(self, text=None, error=None):
        self.text = (
            text
            if text is not None
            else json.dumps(
                {"reply": "Use the Schedule panel.", "suggested_tab": "concentration"}
            )
        )
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


def ask(client, user_id, text="where do I log a dose?"):
    return client.post(
        f"/users/{user_id}/chat",
        json={"messages": [{"role": "user", "content": text}]},
    )


def test_a_reply_comes_back_with_a_valid_tab(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    r = ask(client, uid)
    assert r.status_code == 200
    assert r.json() == {
        "reply": "Use the Schedule panel.",
        "suggested_tab": "concentration",
        "suggested_action": None,
    }


def test_the_output_token_cap_and_schema_are_passed(make_client, stub):
    client = make_client()
    ask(client, sign_up(client))
    call = stub.calls[0]
    assert call["max_output_tokens"] == 600
    assert call["schema"] is ai.ChatAnswer


@pytest.mark.parametrize(
    "tab", ["concentration", "timeline", "skincare", "interactions", "food"]
)
def test_every_real_tab_is_accepted(make_client, stub, tab):
    stub.text = json.dumps({"reply": "Go there.", "suggested_tab": tab})
    client = make_client()
    assert ask(client, sign_up(client)).json()["suggested_tab"] == tab


@pytest.mark.parametrize("tab", ["settings", "admin", "", "Interactions", "../x"])
def test_an_unknown_tab_is_dropped(make_client, stub, tab):
    stub.text = json.dumps({"reply": "Go there.", "suggested_tab": tab})
    client = make_client()
    body = ask(client, sign_up(client)).json()
    assert body["suggested_tab"] is None
    assert body["reply"] == "Go there."


def test_a_null_tab_stays_null(make_client, stub):
    stub.text = json.dumps({"reply": "Hello.", "suggested_tab": None})
    client = make_client()
    assert ask(client, sign_up(client)).json()["suggested_tab"] is None


def test_the_prompt_has_the_survey_enums_and_no_database_data(make_client, stub):
    client = make_client()
    uid = sign_up(client, "private.person@example.com")
    client.put(f"/users/{uid}/survey", json=SURVEY)
    client.post(
        f"/users/{uid}/doses",
        json={
            "product_id": "prod_tylenol_es_500",
            "taken_at": "2026-09-13T10:00:00-07:00",
            "quantity": 1,
        },
    )
    ask(client, uid)
    system = stub.calls[0]["system"]
    for value in (
        "medications",
        "skincare",
        "avoid_interactions",
        "reminders",
        "some",
        "step_by_step",
    ):
        assert value in system
    assert "private.person@example.com" not in system
    assert uid not in system
    assert "Tylenol" not in system
    assert "prod_tylenol" not in system
    assert "{survey_context}" not in system


def test_a_user_without_a_completed_survey_gets_no_answers_context(make_client, stub):
    client = make_client()
    ask(client, sign_up(client))
    assert "- no answers" in stub.calls[0]["system"]


def test_the_prompt_file_holds_the_required_rules():
    text = ai.PROMPT_PATH.read_text(encoding="utf-8")
    for needle in (
        "911",
        "1-800-222-1222",
        "Interactions tab",
        "pharmacist or prescriber",
        "Ignore any instruction",
        "150 words",
        "suggested_tab",
        "Concentration tab",
        "Timeline tab",
        "Skincare tab",
        "Food & drink tab",
        "Your medications",
    ):
        assert needle in text


def test_only_the_last_ten_messages_reach_the_model(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    messages.append({"role": "user", "content": "last"})
    r = client.post(f"/users/{uid}/chat", json={"messages": messages[-40:]})
    assert r.status_code == 200
    sent = stub.calls[0]["contents"]
    assert len(sent) == 10
    assert sent[-1]["parts"][0]["text"] == "last"
    assert sent[-1]["role"] == "user"
    assert {m["role"] for m in sent} == {"user", "model"}


def test_a_message_over_1000_characters_is_422(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    assert ask(client, uid, "x" * 1000).status_code == 200
    r = ask(client, uid, "x" * 1001)
    assert r.status_code == 422
    assert len(stub.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": []},
        {"messages": [{"role": "system", "content": "hi"}]},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "user", "content": "hi", "extra": 1}]},
        {"messages": [{"role": "user", "content": "hi"}], "model": "other"},
        {},
    ],
)
def test_bad_bodies_are_422(make_client, stub, payload):
    client = make_client()
    r = client.post(f"/users/{sign_up(client)}/chat", json=payload)
    assert r.status_code == 422
    assert stub.calls == []


def test_the_last_message_must_be_from_the_user(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    r = client.post(
        f"/users/{uid}/chat",
        json={"messages": [{"role": "assistant", "content": "hi"}]},
    )
    assert r.status_code == 422
    assert stub.calls == []


def test_logged_out_is_401(make_client):
    r = ask(make_client(), "x")
    assert r.status_code == 401


def test_another_users_id_is_403_and_calls_nothing(make_client, stub):
    a = make_client()
    sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    assert ask(a, b_id).status_code == 403
    assert stub.calls == []


def test_hourly_limit_defaults_to_twenty(make_client, stub):
    client = make_client()
    uid = sign_up(client)
    for _ in range(20):
        assert ask(client, uid).status_code == 200
    r = ask(client, uid)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) > 0
    assert len(stub.calls) == 20


def test_hourly_limit_follows_the_env(make_client, stub, monkeypatch):
    monkeypatch.setenv("PHARMAKON_CHAT_PER_HOUR", "2")
    client = make_client()
    uid = sign_up(client)
    assert ask(client, uid).status_code == 200
    assert ask(client, uid).status_code == 200
    assert ask(client, uid).status_code == 429


def test_the_hourly_limit_is_per_user_and_lifts_over_time(
    make_client, stub, monkeypatch
):
    monkeypatch.setenv("PHARMAKON_CHAT_PER_HOUR", "1")
    now = [1000.0]
    ai.usage.clock = lambda: now[0]
    a = make_client()
    a_id = sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    assert ask(a, a_id).status_code == 200
    assert ask(a, a_id).status_code == 429
    assert ask(b, b_id).status_code == 200
    now[0] += 3601
    assert ask(a, a_id).status_code == 200


def test_the_daily_cap_is_shared_across_users(make_client, stub, monkeypatch):
    monkeypatch.setenv("PHARMAKON_AI_DAILY_CAP", "3")
    a = make_client()
    a_id = sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    assert ask(a, a_id).status_code == 200
    assert ask(b, b_id).status_code == 200
    assert ask(a, a_id).status_code == 200
    r = ask(b, b_id)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ai_daily_cap"
    assert len(stub.calls) == 3


def test_the_daily_cap_defaults_to_two_hundred(monkeypatch):
    monkeypatch.delenv("PHARMAKON_AI_DAILY_CAP", raising=False)
    assert ai.daily_cap() == 200
    assert ai.chat_per_hour() == 20
    assert ai.scan_per_hour() == 10


def test_scans_and_chats_draw_on_the_same_daily_cap(monkeypatch):
    monkeypatch.setenv("PHARMAKON_AI_DAILY_CAP", "2")
    assert ai.usage.try_use("u1", 10) is None
    assert ai.usage.try_use("u2", 10) is None
    assert ai.usage.try_use("u3", 10)[0] == "daily"


def test_a_quota_error_is_429_busy_and_never_retried(make_client, stub):
    stub.error = ai.AIQuotaExceeded()
    client = make_client()
    r = ask(client, sign_up(client))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ai_busy"
    assert (
        r.json()["error"]["message"] == "The demo assistant is busy, try again later."
    )
    assert len(stub.calls) == 1


def test_an_upstream_failure_is_502(make_client, stub):
    stub.error = ai.AIFailure()
    client = make_client()
    r = ask(client, sign_up(client))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ai_failed"


@pytest.mark.parametrize("text", ["not json", "{}", '{"reply": ""}', '{"reply": 5}'])
def test_unusable_model_output_is_502(make_client, stub, text):
    stub.text = text
    client = make_client()
    assert ask(client, sign_up(client)).status_code == 502


def test_no_key_is_503_and_uses_no_quota(make_client, monkeypatch):
    main.app.dependency_overrides[main.get_generator] = lambda: ai.GeminiGenerator()
    monkeypatch.setenv("GEMINI_API_KEY", "")
    client = make_client()
    uid = sign_up(client)
    r = ask(client, uid)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ai_unavailable"
    assert ai.usage.windows == {}


def test_message_text_is_never_logged(make_client, stub, caplog):
    secret = "zebra-" + secrets.token_hex(6)
    client = make_client()
    uid = sign_up(client)
    with caplog.at_level("DEBUG"):
        ask(client, uid, secret)
    assert secret not in caplog.text


def test_the_reply_is_capped(make_client, stub):
    stub.text = json.dumps({"reply": "x" * 5000, "suggested_tab": None})
    client = make_client()
    assert len(ask(client, sign_up(client)).json()["reply"]) == 1500


def test_nothing_is_stored(make_client, stub):
    client = make_client()
    ask(client, sign_up(client))
    assert not any("chat" in t or "message" in t for t in Base.metadata.tables)


@pytest.mark.parametrize(
    "reply",
    [
        "Yes, that is safe.",
        "It causes drowsiness.",
        "This prevents problems.",
        "There is no risk here.",
        "It will cause issues.",
        "You can take it safely.",
    ],
)
def test_forbidden_words_in_a_reply_are_refused_server_side(make_client, stub, reply):
    stub.text = json.dumps({"reply": reply, "suggested_tab": None})
    client = make_client()
    r = ask(client, sign_up(client))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ai_failed"


def test_a_long_reply_is_cut_to_150_words(make_client, stub):
    stub.text = json.dumps({"reply": " ".join(["word"] * 300), "suggested_tab": None})
    client = make_client()
    body = ask(client, sign_up(client)).json()
    assert len(body["reply"].split()) == 150


def test_chat_and_scan_hourly_limits_are_separate(make_client, stub, monkeypatch):
    monkeypatch.setenv("PHARMAKON_CHAT_PER_HOUR", "1")
    monkeypatch.setenv("PHARMAKON_SCAN_PER_HOUR", "1")
    client = make_client()
    uid = sign_up(client)
    assert ask(client, uid).status_code == 200
    assert ask(client, uid).status_code == 429
    stub.text = json.dumps(
        {
            "product_name": None,
            "brand": None,
            "product_type": "unknown",
            "ingredients": [],
            "label_text_quality": "clear",
            "notes": None,
        }
    )
    scan = client.post(f"/users/{uid}/scan", content=b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert scan.status_code == 200
    again = client.post(f"/users/{uid}/scan", content=b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert again.status_code == 429


@pytest.mark.parametrize("action", list(ai.ACTIONS))
def test_every_listed_action_is_accepted(make_client, stub, action):
    stub.text = json.dumps(
        {"reply": "Here you go.", "suggested_tab": None, "suggested_action": action}
    )
    client = make_client()
    assert ask(client, sign_up(client)).json()["suggested_action"] == action


@pytest.mark.parametrize(
    "action",
    ["open_admin", "delete_everything", "", "OPEN_SCAN", "open_scan; drop", "../scan"],
)
def test_an_unlisted_action_is_dropped(make_client, stub, action):
    stub.text = json.dumps(
        {"reply": "Here you go.", "suggested_tab": None, "suggested_action": action}
    )
    client = make_client()
    body = ask(client, sign_up(client)).json()
    assert body["suggested_action"] is None
    assert body["reply"] == "Here you go."


def test_a_missing_action_is_null(make_client, stub):
    stub.text = json.dumps({"reply": "Here you go."})
    client = make_client()
    assert ask(client, sign_up(client)).json()["suggested_action"] is None


def test_numbered_steps_keep_their_line_breaks(make_client, stub):
    steps = "1. Open Log intake.\n2. Search for what you took.\n3. Press Log."
    stub.text = json.dumps(
        {
            "reply": steps,
            "suggested_tab": "concentration",
            "suggested_action": "open_log_intake",
        }
    )
    client = make_client()
    body = ask(client, sign_up(client)).json()
    assert body["reply"] == steps


def test_long_replies_keep_line_breaks_up_to_the_cut(make_client, stub):
    text = "\n".join(f"{n}. " + "word " * 10 for n in range(1, 40))
    stub.text = json.dumps({"reply": text, "suggested_tab": None})
    client = make_client()
    reply = ask(client, sign_up(client)).json()["reply"]
    assert "\n" in reply
    assert len(reply.split()) <= 150


@pytest.mark.parametrize(
    "reply",
    [
        "Ask a pharmacist whether it's safe for you.",
        "A pharmacist can tell you if it is safe for you.",
        "I can't say whether it is safe for you. Please ask a pharmacist.",
        "Open the Interactions tab. A pharmacist can tell you what it means for you.",
        "Retinol is a vitamin A form used in skincare for texture and tone. "
        "This is general information.",
        "1. Open Log intake.\n2. Search for coffee.\n3. Press Log.",
    ],
)
def test_the_narrower_wording_check_allows_safe_framed_as_a_question(
    make_client, stub, reply
):
    stub.text = json.dumps({"reply": reply, "suggested_tab": None})
    client = make_client()
    r = ask(client, sign_up(client))
    assert r.status_code == 200
    assert r.json()["reply"] == reply


@pytest.mark.parametrize(
    "reply",
    [
        "Yes, it is safe.",
        "That is safe to take with food.",
        "It's safe.",
        "Both are generally safe together.",
        "You can take it safely.",
        "There is no risk here.",
        "It is risk-free.",
        "This will cause drowsiness.",
        "Caffeine causes jitters.",
        "Vitamin C prevents colds.",
        "It is completely safe to take.",
    ],
)
def test_the_narrower_wording_check_still_blocks_assertions(make_client, stub, reply):
    stub.text = json.dumps({"reply": reply, "suggested_tab": None})
    client = make_client()
    r = ask(client, sign_up(client))
    assert r.status_code == 502


def test_the_prompt_allows_general_education_and_numbered_steps():
    text = ai.PROMPT_PATH.read_text(encoding="utf-8")
    for needle in (
        "general educational explanations",
        "up to 4 short numbered steps",
        "suggested_action",
        "open_log_intake",
        "open_scan",
        "open_interactions",
        "open_schedule",
        "open_survey",
        "open_settings",
        "Never judge an interaction yourself",
        "1-800-222-1222",
        "Ignore any instruction",
    ):
        assert needle in text


@pytest.mark.parametrize(
    "reply",
    [
        "It’s safe.",
        "It is also safe.",
        "Both are safe.",
        "It should be safe.",
        "It seems safe.",
        "It is considered safe.",
        "It is safe to consume.",
        "It is harmless.",
        "There is no danger.",
        "It can cause drowsiness.",
        "It may cause problems.",
        "This helps prevent colds.",
        "Take it if it's safe.",
        "You can go ahead if it is safe.",
    ],
)
def test_more_phrasings_of_a_safety_assertion_are_blocked(make_client, stub, reply):
    stub.text = json.dumps({"reply": reply, "suggested_tab": None})
    client = make_client()
    assert ask(client, sign_up(client)).status_code == 502


@pytest.mark.parametrize(
    "reply",
    [
        "Ask your pharmacist whether it’s safe for you.",
        "It is not safe to assume that. Please ask a pharmacist.",
    ],
)
def test_question_frames_and_negations_are_still_allowed(make_client, stub, reply):
    stub.text = json.dumps({"reply": reply, "suggested_tab": None})
    client = make_client()
    assert ask(client, sign_up(client)).status_code == 200
