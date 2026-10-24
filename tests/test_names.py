import pytest

from app.api.names import display_product_name


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("TYLENOL EXTRA STRENGTH", "Tylenol Extra Strength"),
        ("CHILDREN'S MOTRIN", "Children's Motrin"),
        ("MILK OF MAGNESIA PEG 3350", "Milk Of Magnesia PEG 3350"),
        ("CALCIUM DISODIUM EDTA", "Calcium Disodium EDTA"),
        ("NP THYROID 60 MG", "NP Thyroid 60 mg"),
        ("DONEPEZIL HCL", "Donepezil HCl"),
        ("MUCINEX DM", "Mucinex DM"),
    ],
)
def test_all_caps_names_are_title_cased_keeping_known_abbreviations(raw, shown):
    assert display_product_name(raw) == shown


@pytest.mark.parametrize(
    "already_mixed", ["DayQuil Severe", "Advil", "Vicks DayQuil VapoCOOL", "123"]
)
def test_names_with_any_lowercase_are_left_exactly_as_they_are(already_mixed):
    assert display_product_name(already_mixed) == already_mixed
