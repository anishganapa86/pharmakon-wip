"""app.api.interactions, POST /interaction-check, and
GET /users/{id}/interaction-check.

openFDA is never actually reached: every test installs its own
httpx.MockTransport (tests/conftest.py's autouse fixture already stubs
app.api.openfda, but app.api.interactions keeps its own _http client and
cache, same pattern as app/api/side_effects.py).
"""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import interactions, main
from app.api import openfda as openfda_module
from app.api.db import Base
from app.registry.loader import load_registry

NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def session_override():
        with SessionLocal() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = lambda: NOW
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


class FakeOpenFDA:
    """Serves drug_interactions text per generic name; 404 for anything else."""

    def __init__(self, labels: dict[str, str] | None = None, down: bool = False):
        self.labels = {k.upper(): v for k, v in (labels or {}).items()}
        self.down = down
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        self.calls.append(search)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        for name, text in self.labels.items():
            if f'"{name}"' in search:
                label = (
                    text if isinstance(text, dict) else {"drug_interactions": [text]}
                )
                return httpx.Response(200, json={"results": [label]})
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def openfda(monkeypatch):
    fake = FakeOpenFDA()
    monkeypatch.setattr(
        interactions, "_http", httpx.Client(transport=httpx.MockTransport(fake))
    )
    interactions.clear_cache()
    yield fake
    interactions.clear_cache()


def manual_check(client, names):
    r = client.post("/interaction-check", json={"names": names})
    assert r.status_code == 200, r.text
    return r.json()


def log(client, product_id, hours_ago, quantity=1):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "quantity": quantity,
        },
    )
    assert r.status_code == 201, r.text


# --- Curated rules -----------------------------------------------------------


def test_ibuprofen_and_naproxen_flagged_as_duplicate_nsaid(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    finding = curated[0]
    ids = {finding["substance_a"]["id"], finding["substance_b"]["id"]}
    assert ids == {"ibuprofen", "naproxen"}
    assert "NSAID" in finding["effect"] or "GI bleeding" in finding["effect"]
    assert finding["source"]
    assert finding["verified"] is False
    assert finding["severity"] is None
    assert body["checked_pairs"] == 1


def test_sertraline_and_dextromethorphan_flagged(client, openfda):
    body = manual_check(client, ["sertraline", "dextromethorphan"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    ids = {curated[0]["substance_a"]["id"], curated[0]["substance_b"]["id"]}
    assert ids == {"sertraline", "dextromethorphan"}
    assert "serotonin" in curated[0]["effect"].lower()


def test_no_source_no_entry_every_rule_is_cited():
    rules = interactions.load_rules()
    assert all(r.source for r in rules)
    assert all(r.verified is False for r in rules)


def test_rule_with_no_source_is_rejected_at_load_time(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"rules": [{"substance_a": "ibuprofen", "substance_b": "warfarin", '
        '"effect": "x", "recommendation": "y", "source": "", "verified": false}]}'
    )
    with pytest.raises(interactions.InvalidRule):
        interactions.load_rules(path=bad)


# --- FDA label text ------------------------------------------------------------


def test_label_text_mentioning_the_other_drug_is_flagged_with_quote(client, openfda):
    # loratadine + warfarin: no curated rule covers this pair, so a literal
    # label mention produces its own fda_label finding.
    openfda.labels["LORATADINE"] = (
        "Ketoconazole: increased plasma concentrations of loratadine were "
        "observed. Warfarin: no clinically significant interaction was "
        "observed in a controlled study."
    )

    body = manual_check(client, ["loratadine", "warfarin"])

    label_findings = [
        f for f in body["interactions"] if f["source_type"] == "fda_label"
    ]
    assert len(label_findings) == 1
    finding = label_findings[0]
    assert finding["substance_a"]["id"] == "loratadine"
    assert finding["substance_b"]["id"] == "warfarin"
    assert "Warfarin" in finding["quoted_text"]
    assert len(finding["quoted_text"]) <= 300
    assert finding["verified"] is False
    assert finding["quote_source"] == "fda_label_verbatim"
    assert finding["message"] == (
        "The FDA label for Loratadine mentions Warfarin in its drug "
        "interactions section."
    )


def test_no_live_label_interaction_finding_is_ever_verified(client, openfda):
    openfda.labels["LORATADINE"] = "Warfarin: no interaction was observed."
    openfda.labels["IBUPROFEN"] = "Naproxen should not be taken with ibuprofen."

    body = manual_check(client, ["loratadine", "warfarin", "ibuprofen", "naproxen"])

    label_findings = [
        f for f in body["interactions"] if f["source_type"] == "fda_label"
    ]
    assert label_findings
    for finding in label_findings:
        assert finding["verified"] is False
        assert finding["quote_source"] == "fda_label_verbatim"
        assert finding["quoted_text"]


def test_curated_interaction_finding_has_no_quote_source(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    (curated,) = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert curated["quote_source"] is None
    assert curated["verified"] is False


def test_label_mention_folded_into_curated_card_names_its_quote_source(client, openfda):
    openfda.labels["IBUPROFEN"] = "Do not use naproxen together with ibuprofen."

    body = manual_check(client, ["ibuprofen", "naproxen"])

    (curated,) = [f for f in body["interactions"] if f["source_type"] == "curated"]
    (mention,) = curated["also_mentioned_in_label"]
    assert mention["quote_source"] == "fda_label_verbatim"


def test_label_text_not_mentioning_the_other_drug_is_not_flagged(client, openfda):
    openfda.labels["LORATADINE"] = "CYP3A4 and CYP2D6 inhibitors may increase levels."
    openfda.labels["FAMOTIDINE"] = "Antacids may decrease absorption of famotidine."

    body = manual_check(client, ["loratadine", "famotidine"])

    assert body["interactions"] == []


def test_two_nonregistry_drugs_label_mention_produces_a_finding(client, openfda):
    # Neither donepezil nor memantine is a registry substance -- each is
    # still checked from its own FDA label, by its normalized, salt-stripped
    # generic name ("Donepezil Hydrochloride" -> "donepezil").
    openfda.labels["DONEPEZIL"] = (
        "Memantine: concomitant administration of donepezil and memantine "
        "did not affect the pharmacokinetics of either drug."
    )

    body = manual_check(client, ["Donepezil Hydrochloride", "Memantine"])

    label_findings = [
        f for f in body["interactions"] if f["source_type"] == "fda_label"
    ]
    assert len(label_findings) == 1
    finding = label_findings[0]
    assert finding["substance_a"]["id"] == "label:donepezil"
    assert finding["substance_b"]["id"] == "label:memantine"
    assert "Memantine" in finding["quoted_text"]
    assert len(finding["quoted_text"]) <= 300
    assert finding["verified"] is False
    assert finding["quote_source"] == "fda_label_verbatim"
    assert finding["severity"] is None
    # A non-registry ingredient has no curated rules and no PK curve.
    assert finding["effect"] is None
    assert finding["recommendation"] is None
    assert finding["overlap"] is None


def test_class_word_match_uses_class_headline_wording(client, openfda):
    # loratadine + lisinopril: no curated rule covers this pair either, so a
    # qualifying class-word match (a combination-use sentence, no literal
    # "lisinopril") produces its own fda_label finding with the class wording.
    openfda.labels["LORATADINE"] = (
        "Concomitant administration with an ACE inhibitor may increase the "
        "antihypertensive effect."
    )

    body = manual_check(client, ["loratadine", "lisinopril"])

    (finding,) = [f for f in body["interactions"] if f["source_type"] == "fda_label"]
    assert finding["message"] == (
        "The FDA label for Loratadine mentions the drug class ACE inhibitors "
        "in its drug interactions section. Lisinopril belongs to this class."
    )


def test_class_word_match_without_combination_context_is_not_flagged(client, openfda):
    # A passing mention of the class name is not evidence of a combination
    # warning -- this sentence is about aspirin-sensitive asthma, not about
    # taking ibuprofen together with another NSAID.
    openfda.labels["IBUPROFEN"] = (
        "Since cross reactivity, including bronchospasm, between aspirin and "
        "NSAIDs has been reported in such aspirin-sensitive patients, "
        "ibuprofen should be used with caution in patients with preexisting "
        "asthma."
    )

    assert interactions.label_mention("ibuprofen", "naproxen", load_registry()) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "This interaction should be given consideration in patients taking "
        "NSAIDs concomitantly with ACE-inhibitors.",
        "Case-control and cohort epidemiological studies showed that "
        "concomitant use of drugs that interfere with serotonin reuptake and "
        "an NSAID may potentiate the risk of bleeding more than an NSAID alone.",
        "These drugs should not be given together with NSAIDs.",
    ],
)
def test_class_only_sentence_about_another_party_is_rejected(sentence, openfda):
    openfda.labels["IBUPROFEN"] = sentence

    assert interactions.label_mention("ibuprofen", "naproxen", load_registry()) is None
    match, rejected = interactions.label_mention_detail(
        "ibuprofen", "naproxen", load_registry()
    )
    assert match is None
    assert rejected == "class"


def test_rejected_class_match_goes_to_unchecked_notice_not_a_card(client, openfda):
    openfda.labels["IBUPROFEN"] = (
        "This interaction should be given consideration in patients taking "
        "NSAIDs concomitantly with ACE-inhibitors."
    )
    openfda.labels["NAPROXEN"] = "Take with water."

    body = manual_check(client, ["ibuprofen", "naproxen"])

    assert [f for f in body["interactions"] if f["source_type"] == "fda_label"] == []
    assert any("not in a sentence about using" in r for r in body["unchecked_reason"])


def test_class_only_sentence_about_using_other_nsaids_together_is_kept(openfda):
    openfda.labels["IBUPROFEN"] = "Do not use with other NSAIDs."

    match = interactions.label_mention("ibuprofen", "naproxen", load_registry())

    assert match is not None
    assert match.sentence == "Do not use with other NSAIDs."
    assert match.is_literal is False
    assert match.other_class == "NSAID"


def test_naproxen_ssri_bleeding_text_does_not_match_ibuprofen(openfda):
    # This sentence is about SSRIs and bleeding risk, not about combining
    # naproxen with another NSAID -- "NSAID" here names naproxen itself, not
    # a combination partner, and there's no literal "ibuprofen" either.
    openfda.labels["NAPROXEN"] = (
        "Drugs that Affect Hemostasis: NSAIDs and SSRIs both increase the "
        "risk of GI bleeding when used with anticoagulants."
    )

    assert interactions.label_mention("naproxen", "ibuprofen", load_registry()) is None


def test_curated_pair_produces_one_card_not_three(client, openfda):
    openfda.labels["IBUPROFEN"] = (
        "Concomitant use with naproxen or other NSAIDs increases the risk of "
        "GI bleeding."
    )
    openfda.labels["NAPROXEN"] = (
        "Concomitant administration with ibuprofen or other NSAIDs is not recommended."
    )

    body = manual_check(client, ["ibuprofen", "naproxen"])

    assert len(body["interactions"]) == 1
    (finding,) = body["interactions"]
    assert finding["source_type"] == "curated"
    assert finding["also_mentioned_in_label"] is not None
    assert len(finding["also_mentioned_in_label"]) == 2
    for mention in finding["also_mentioned_in_label"]:
        assert mention["quoted_text"]


# --- Empty result --------------------------------------------------------------


def test_two_unrelated_drugs_returns_exact_empty_message(client, openfda):
    body = manual_check(client, ["loratadine", "famotidine"])

    assert body["interactions"] == []
    assert body["message"] == (
        "No documented interactions found in our sources. This does not mean "
        "none exist. Ask a pharmacist to review your full medication list."
    )


def test_nonempty_result_has_no_top_level_message(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    assert body["interactions"] != []
    assert body["message"] is None


def test_zero_checked_pairs_replaces_empty_message(client, openfda):
    # Only one substance (nothing to pair against) -- checked_pairs is 0,
    # which is a different situation from "pairs were checked and came up
    # clean", and must not read as though it were.
    body = manual_check(client, ["ibuprofen"])

    assert body["checked_pairs"] == 0
    assert body["message"] == "We couldn't check any pairs for this regimen."


def test_empty_regimen_also_gets_the_no_pairs_message(client, openfda):
    body = client.get("/users/default/interaction-check").json()

    assert body["checked_pairs"] == 0
    assert body["message"] == "We couldn't check any pairs for this regimen."


# --- Medications only: skincare/cosmetic products are excluded -----------------


def test_skincare_product_logged_today_produces_no_unresolved_entries(client, openfda):
    search_body = client.get("/products/search", params={"q": "TIRTIR"}).json()
    product = next(
        p for p in search_body["results"] if p["source"] == "skincare_catalog"
    )
    log(client, product["id"], hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    assert not any("wasn't recognized" in r for r in body["unchecked_reason"])
    assert body["checked_pairs"] == 0


def test_medication_product_with_unresolvable_ingredient_still_unchecked(
    client, openfda
):
    # A name with no letters or digits at all reduces to nothing even after
    # normalization -- genuinely unresolvable, not helped by the new
    # label-by-generic-name fallback (task 2), unlike a real drug name.
    openfda_module._discovered["fda_mystery"] = openfda_module.OpenFDAProduct(
        id="fda_mystery",
        brand_name="Mystery Pill",
        ingredients=(openfda_module.OpenFDAIngredient("###"),),
        route="ORAL",
        matched_via="name",
    )
    log(client, "fda_mystery", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    assert any("'###' wasn't recognized" in r for r in body["unchecked_reason"])


# --- Unresolved ingredients ------------------------------------------------------


def test_unrecognized_ingredient_with_no_label_appears_under_unchecked_reason(
    client, openfda
):
    # "Unobtainium" isn't a registry substance, but it's still checked from
    # its own (normalized) FDA label now -- openfda has no label configured
    # for it, so it surfaces as "no label found", not "wasn't recognized".
    body = manual_check(client, ["ibuprofen", "Unobtainium"])

    assert any(
        "no label found" in reason.lower() and "unobtainium" in reason.lower()
        for reason in body["unchecked_reason"]
    )


def test_name_that_normalizes_to_nothing_is_wasnt_recognized(client, openfda):
    # A name that reduces to nothing after normalization (no letters/digits
    # at all) has no generic name to even look a label up by -- genuinely
    # unresolved, not a "no label found" case.
    body = manual_check(client, ["ibuprofen", "***"])

    assert any(
        "'***' wasn't recognized" in reason for reason in body["unchecked_reason"]
    )


# --- openFDA unreachable ---------------------------------------------------------


def test_openfda_unreachable_curated_rules_still_work(client, openfda):
    openfda.down = True

    body = manual_check(client, ["ibuprofen", "naproxen"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    assert any(
        "openFDA was unreachable" in reason for reason in body["unchecked_reason"]
    )


# --- Concentration overlap -------------------------------------------------------


def test_overlap_computed_for_two_modeled_drugs(client, openfda):
    log(client, "prod_advil_200", hours_ago=2)  # ibuprofen, full modelability
    log(client, "prod_aleve_220", hours_ago=2)  # naproxen, full modelability

    body = client.get("/users/default/interaction-check").json()

    (finding,) = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert finding["overlap"] is not None
    (window,) = finding["overlap"]
    assert window["estimate"] is True
    assert "from" in window and "to" in window


def test_overlap_absent_when_one_substance_is_unmodeled(client, openfda):
    # ethanol is zero-order kinetics (UnsupportedKinetics), so its
    # modelability is never "full" -- compute_overlap must refuse to guess a
    # window for it no matter what the other substance in the pair looks like.
    #
    # Metformin is logged as a real oral openFDA-discovered product here,
    # not via POST /products/user-entered: that endpoint always marks
    # whatever's entered as topical ("the skincare tooling"), which
    # check_regimen now correctly excludes from interaction checks
    # entirely -- it's not a valid way to log an oral medication anymore.
    log(client, "prod_example_beer", hours_ago=1)  # ethanol
    openfda_module._discovered["fda_metformin"] = openfda_module.OpenFDAProduct(
        id="fda_metformin",
        brand_name="Metformin 500mg",
        ingredients=(openfda_module.OpenFDAIngredient("Metformin"),),
        route="ORAL",
        matched_via="name",
    )
    log(client, "fda_metformin", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    curated = [
        f
        for f in body["interactions"]
        if {f["substance_a"]["id"], f["substance_b"]["id"]} == {"metformin", "ethanol"}
    ]
    assert curated
    assert curated[0]["overlap"] is None


# --- GET /users/{id}/interaction-check -------------------------------------------


def test_get_interaction_check_uses_regimen_doses_and_schedule(client, openfda):
    log(client, "prod_advil_200", hours_ago=1)
    r = client.post(
        "/users/default/schedule",
        json={
            "product_id": "prod_aleve_220",
            "frequency": "daily",
            "times": ["09:00"],
        },
    )
    assert r.status_code == 201, r.text

    body = client.get("/users/default/interaction-check").json()

    ids = {
        sid
        for f in body["interactions"]
        for sid in (f["substance_a"]["id"], f["substance_b"]["id"])
    }
    assert {"ibuprofen", "naproxen"} <= ids


def test_get_interaction_check_ignores_doses_older_than_24h(client, openfda):
    log(client, "prod_advil_200", hours_ago=30)
    log(client, "prod_aleve_220", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    assert body["interactions"] == []


def test_manual_check_requires_at_least_one_name(client, openfda):
    r = client.post("/interaction-check", json={"names": []})
    assert r.status_code == 422


def test_unreachable_openfda_is_tried_once_per_substance_not_once_per_pair(
    client, openfda
):
    openfda.down = True

    body = manual_check(client, ["ibuprofen", "warfarin", "loratadine"])

    assert len(openfda.calls) == 3
    assert interactions.LABEL_UNAVAILABLE_NOTE in body["unchecked_reason"]


def test_substance_labels_are_fetched_concurrently(client, openfda, monkeypatch):
    import threading

    barrier = threading.Barrier(3, timeout=5)

    def fake(generic_name):
        barrier.wait()
        return None

    monkeypatch.setattr(interactions, "fetch_drug_interactions_text", fake)

    body = manual_check(client, ["ibuprofen", "warfarin", "loratadine"])

    assert body["checked_pairs"] == 3


# --- "Same ingredient in more than one product" --------------------------------


def _discover_oral(product_id, brand_name, ingredient_names):
    openfda_module._discovered[product_id] = openfda_module.OpenFDAProduct(
        id=product_id,
        brand_name=brand_name,
        ingredients=tuple(
            openfda_module.OpenFDAIngredient(n) for n in ingredient_names
        ),
        route="ORAL",
        matched_via="name",
    )


def test_duplicate_ingredient_across_three_products_yields_a_card_for_donepezil(
    client, openfda
):
    _discover_oral("fda_donepezil", "Donepezil", ["Donepezil"])
    _discover_oral(
        "fda_memantine_donepezil",
        "Memantine and Donepezil",
        ["Memantine", "Donepezil"],
    )
    _discover_oral("fda_namzaric", "Namzaric", ["Memantine", "Donepezil"])
    log(client, "fda_donepezil", hours_ago=1)
    log(client, "fda_memantine_donepezil", hours_ago=1)
    r = client.post(
        "/users/default/schedule",
        json={"product_id": "fda_namzaric", "frequency": "daily", "times": ["09:00"]},
    )
    assert r.status_code == 201, r.text

    body = client.get("/users/default/interaction-check").json()

    donepezil_cards = [
        c for c in body["duplicate_ingredients"] if c["ingredient_name"] == "Donepezil"
    ]
    assert len(donepezil_cards) == 1
    card = donepezil_cards[0]
    assert card["product_count"] == 3
    names_by_kind = {p["name"]: p["kind"] for p in card["products"]}
    assert set(names_by_kind) == {"Donepezil", "Memantine and Donepezil", "Namzaric"}
    assert names_by_kind["Namzaric"] == "schedule"
    assert names_by_kind["Donepezil"] == "dose"
    assert names_by_kind["Memantine and Donepezil"] == "dose"
    assert card["message"] == (
        "Donepezil appears in 3 of your products: Donepezil, Memantine and "
        "Donepezil, Namzaric. Ask a pharmacist to confirm this is intended."
    )
    # openFDA ingredient strengths are always None (app/api/openfda.py never
    # guesses one), so the strength-unknown caveat must be present.
    assert card["strength_note"] == (
        "The strength of each product isn't known here, so we can't add "
        "up the total amount."
    )
    # No severity, no harm claim, and this never creates an alert.
    assert "severity" not in card
    assert client.get("/users/default/alerts").json() == []


def test_two_unrelated_drugs_yield_no_duplicate_card(client, openfda):
    log(client, "prod_advil_200", hours_ago=1)  # ibuprofen
    log(client, "prod_zyrtec_10", hours_ago=1)  # cetirizine

    body = client.get("/users/default/interaction-check").json()

    assert body["duplicate_ingredients"] == []


def test_same_product_scheduled_twice_is_one_product_not_a_duplicate(client, openfda):
    for _ in range(2):
        r = client.post(
            "/users/default/schedule",
            json={
                "product_id": "prod_advil_200",
                "frequency": "daily",
                "times": ["09:00"],
            },
        )
        assert r.status_code == 201, r.text

    body = client.get("/users/default/interaction-check").json()

    assert body["duplicate_ingredients"] == []


# --- Audit rules for the non-registry, same-ingredient and gated paths --------

NONREGISTRY = ["Donepezil", "Memantine", "Rivastigmine"]


def test_nonregistry_labels_are_fetched_concurrently(client, openfda, monkeypatch):
    import threading

    barrier = threading.Barrier(3, timeout=5)

    def fake(generic_name):
        barrier.wait()
        return None

    monkeypatch.setattr(interactions, "fetch_drug_interactions_text", fake)

    body = manual_check(client, NONREGISTRY)

    assert body["checked_pairs"] == 3


def test_nonregistry_label_lookups_go_through_the_disk_cache(client, openfda, tmp_path):
    from app.api import label_cache

    label_cache.set_cache_dir(tmp_path)
    openfda.labels["DONEPEZIL"] = "Memantine may be combined with donepezil."

    manual_check(client, NONREGISTRY)
    first_pass = len(openfda.calls)
    interactions.clear_cache()
    manual_check(client, NONREGISTRY)

    assert first_pass == 3
    assert len(openfda.calls) == first_pass


def test_unreachable_openfda_is_tried_once_per_nonregistry_substance(client, openfda):
    openfda.down = True

    body = manual_check(client, NONREGISTRY)

    assert len(openfda.calls) == 3
    assert interactions.LABEL_UNAVAILABLE_NOTE in body["unchecked_reason"]


def test_nonregistry_label_finding_is_never_verified(client, openfda):
    openfda.labels["DONEPEZIL"] = "Memantine: concomitant use was studied."

    body = manual_check(client, ["Donepezil", "Memantine"])

    findings = body["interactions"]
    assert findings
    assert all(f["verified"] is False for f in findings)
    assert all(f["quote_source"] == "fda_label_verbatim" for f in findings)


def test_same_ingredient_card_is_not_gated_and_carries_no_verified_flag(
    client, openfda, monkeypatch
):
    monkeypatch.delenv("SHOW_UNVERIFIED_RULES", raising=False)
    _discover_oral("fda_donepezil", "Donepezil", ["Donepezil"])
    _discover_oral("fda_namzaric", "Namzaric", ["Memantine", "Donepezil"])
    log(client, "fda_donepezil", hours_ago=1)
    log(client, "fda_namzaric", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    cards = [
        c for c in body["duplicate_ingredients"] if c["ingredient_name"] == "Donepezil"
    ]
    assert len(cards) == 1
    assert cards[0]["product_count"] == 2
    assert "verified" not in cards[0]
    assert "source" not in cards[0]


def test_gate_off_empty_result_still_carries_the_awaiting_notice(
    client, openfda, monkeypatch
):
    monkeypatch.delenv("SHOW_UNVERIFIED_RULES", raising=False)

    body = manual_check(client, ["ibuprofen", "warfarin"])

    assert body["interactions"] == []
    assert body["checked_pairs"] == 1
    assert body["rules_awaiting_verification"]["count"] > 0
    assert (
        "awaiting verification and are not being applied"
        in body["rules_awaiting_verification"]["message"]
    )


def test_gate_off_no_pairs_result_still_carries_the_awaiting_notice(
    client, openfda, monkeypatch
):
    monkeypatch.delenv("SHOW_UNVERIFIED_RULES", raising=False)

    body = manual_check(client, ["ibuprofen"])

    assert body["checked_pairs"] == 0
    assert body["message"] == interactions.NO_PAIRS_MESSAGE
    assert body["rules_awaiting_verification"]["count"] > 0


def test_gate_off_regimen_empty_result_carries_the_awaiting_notice(
    client, openfda, monkeypatch
):
    monkeypatch.delenv("SHOW_UNVERIFIED_RULES", raising=False)
    log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    assert body["interactions"] == []
    assert body["rules_awaiting_verification"]["count"] > 0


def test_only_curated_findings_can_be_unverified_references(client, openfda):
    openfda.labels["IBUPROFEN"] = "Warfarin: concomitant use raises bleeding risk."
    openfda.labels["LORATADINE"] = "Famotidine: concomitant use was studied."

    body = manual_check(client, ["ibuprofen", "warfarin", "loratadine", "famotidine"])

    by_type = {f["source_type"] for f in body["interactions"]}
    assert by_type == {"curated", "fda_label"}
    for f in body["interactions"]:
        if f["source_type"] == "fda_label":
            assert f["verified"] is False
            assert f["quote_source"] == "fda_label_verbatim"
        else:
            assert f["quote_source"] is None


def test_quote_after_a_section_number_heading_is_only_the_sentence(client, openfda):
    openfda.labels["DONEPEZIL"] = (
        "7.2 Use with Other N-methyl-D-aspartate (NMDA) Antagonists The combined "
        "use of memantine with other NMDA antagonists has not been "
        "systematically evaluated."
    )

    body = manual_check(client, ["Donepezil Hydrochloride", "Memantine"])

    (finding,) = [f for f in body["interactions"] if f["source_type"] == "fda_label"]
    assert finding["quoted_text"] == (
        "The combined use of memantine with other NMDA antagonists has not been "
        "systematically evaluated."
    )


def test_unseparable_section_heading_is_not_quoted_and_is_noted(client, openfda):
    openfda.labels["DONEPEZIL"] = (
        "7.2 Drug Interactions Memantine may be used with other medicines."
    )

    body = manual_check(client, ["Donepezil Hydrochloride", "Memantine"])

    assert [f for f in body["interactions"] if f["source_type"] == "fda_label"] == []
    assert any("could not quote cleanly" in r for r in body["unchecked_reason"])


def test_sentence_with_a_cross_reference_is_not_quoted(client, openfda):
    openfda.labels["DONEPEZIL"] = (
        "Memantine has been coadministered with donepezil (see Clinical "
        "Pharmacology 12.3)."
    )

    body = manual_check(client, ["Donepezil Hydrochloride", "Memantine"])

    assert [f for f in body["interactions"] if f["source_type"] == "fda_label"] == []
    assert any("could not quote cleanly" in r for r in body["unchecked_reason"])


def test_label_finding_says_which_label_was_used(client, openfda):
    openfda.labels["DONEPEZIL"] = {
        "drug_interactions": [
            "Memantine: concomitant administration of donepezil and memantine "
            "did not affect the pharmacokinetics of either drug."
        ],
        "set_id": "don-123",
        "openfda": {"brand_name": ["Aricept"]},
    }

    body = manual_check(client, ["Donepezil Hydrochloride", "Memantine"])

    (finding,) = [f for f in body["interactions"] if f["source_type"] == "fda_label"]
    assert finding["label_product_name"] == "Aricept"
    assert finding["label_attribution"] == (
        "From the label for Aricept, a donepezil product. "
        "Not necessarily the exact product you take."
    )
    assert finding["dailymed_url"].endswith("setid=don-123")


def test_curated_card_has_no_label_attribution(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    (finding,) = body["interactions"]
    assert finding["source_type"] == "curated"
    assert finding["label_attribution"] is None
