"""Seed product catalog: product_id -> labelled ingredients.

SYNTHETIC DEMO DATA. This is a stand-in for the openFDA label decomposition
(Punya's), which will replace it. The public demo runs on seeded synthetic data
only (docs/api-contract.md, "Authentication and data"). Strengths are the
commonly marketed label values and have not been checked against current
labels — treat them like registry entries with verified=false.

Ingredient strings are kept as a label would print them and resolved through
Registry.resolve(), the same normalization path real labels will take. An
unresolved string is `unmatched`; it is never hand-mapped here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..registry.loader import Registry


@dataclass(frozen=True)
class LabelIngredient:
    name: str  # as labelled
    strength_mg: float | None  # per product unit; None = label gives no mass


@dataclass(frozen=True)
class Product:
    id: str
    brand_name: str
    ingredients: tuple[LabelIngredient, ...]


@dataclass(frozen=True)
class DecomposedIngredient:
    name: str
    substance_id: str | None
    strength_mg: float | None
    # None | "unmatched" | "unknown_strength" | "no_pk_parameters"
    unmodeled_reason: str | None


PRODUCTS: dict[str, Product] = {
    p.id: p
    for p in [
        Product(
            "prod_tylenol_es_500",
            "Tylenol Extra Strength",
            (LabelIngredient("ACETAMINOPHEN", 500),),
        ),
        Product(
            "prod_dayquil_liquicap",
            "DayQuil",
            (
                LabelIngredient("ACETAMINOPHEN", 325),
                LabelIngredient("DEXTROMETHORPHAN HBR", 10),
                LabelIngredient("PHENYLEPHRINE HCL", 5),
            ),
        ),
        Product("prod_advil_200", "Advil", (LabelIngredient("IBUPROFEN", 200),)),
        Product("prod_nodoz_200", "NoDoz", (LabelIngredient("CAFFEINE", 200),)),
        Product(
            "prod_nyquil_liquicap",
            "NyQuil",
            (
                LabelIngredient("ACETAMINOPHEN", 325),
                LabelIngredient("DEXTROMETHORPHAN HBR", 15),
                LabelIngredient("DOXYLAMINE SUCCINATE", 6.25),
            ),
        ),
        Product(
            "prod_example_cold_multi",
            "Example Multi-Symptom Cold",
            (
                LabelIngredient("ACETAMINOPHEN", None),
                LabelIngredient("PHENYLEPHRINE HCL", 5),
            ),
        ),
        Product(
            "prod_supp_vitamin_c",
            "Vitamin C",
            (LabelIngredient("ASCORBIC ACID", None),),
        ),
        Product(
            "prod_supp_vitamin_d",
            "Vitamin D",
            (LabelIngredient("CHOLECALCIFEROL", None),),
        ),
        Product(
            "prod_supp_fish_oil",
            "Fish oil",
            (LabelIngredient("FISH OIL", None),),
        ),
        # ~14 g ethanol, one US standard drink.
        Product(
            "prod_example_beer",
            "Example Beer (12 oz)",
            (LabelIngredient("ALCOHOL", 14000),),
        ),
        # Strengths below are per-unit (per tablet/caplet/LiquiCap) values as
        # printed on each product's FDA Drug Facts panel, not derived by regex
        # from label free text. Still unverified in the sense the module
        # docstring describes: nobody has cross-checked these against a current
        # label image, so treat them like registry entries with verified=false.
        Product(
            "prod_tylenol_rs_325",
            "Tylenol Regular Strength",
            # Drug Facts: acetaminophen 325 mg per caplet.
            (LabelIngredient("ACETAMINOPHEN", 325),),
        ),
        Product(
            "prod_aleve_220",
            "Aleve",
            # Drug Facts: naproxen sodium 220 mg per caplet.
            (LabelIngredient("NAPROXEN SODIUM", 220),),
        ),
        Product(
            "prod_benadryl_25",
            "Benadryl",
            # Drug Facts: diphenhydramine HCl 25 mg per capsule.
            (LabelIngredient("DIPHENHYDRAMINE HCL", 25),),
        ),
        Product(
            "prod_nyquil_severe",
            "NyQuil Severe",
            # Drug Facts, per LiquiCap: acetaminophen 325 mg,
            # dextromethorphan HBr 10 mg, doxylamine succinate 6.25 mg.
            (
                LabelIngredient("ACETAMINOPHEN", 325),
                LabelIngredient("DEXTROMETHORPHAN HBR", 10),
                LabelIngredient("DOXYLAMINE SUCCINATE", 6.25),
            ),
        ),
        Product(
            "prod_excedrin_es",
            "Excedrin Extra Strength",
            # Drug Facts, per caplet: acetaminophen 250 mg, aspirin 250 mg,
            # caffeine 65 mg.
            (
                LabelIngredient("ACETAMINOPHEN", 250),
                LabelIngredient("ASPIRIN", 250),
                LabelIngredient("CAFFEINE", 65),
            ),
        ),
        Product(
            "prod_mucinex_dm",
            "Mucinex DM",
            # Drug Facts, per tablet: dextromethorphan HBr 20 mg,
            # guaifenesin 400 mg.
            (
                LabelIngredient("DEXTROMETHORPHAN HBR", 20),
                LabelIngredient("GUAIFENESIN", 400),
            ),
        ),
        Product(
            "prod_prilosec_otc_20",
            "Prilosec OTC",
            # Drug Facts: omeprazole magnesium, 20.6 mg delivering 20 mg
            # omeprazole per tablet; labelled strength is 20 mg.
            (LabelIngredient("OMEPRAZOLE MAGNESIUM", 20),),
        ),
        Product(
            "prod_claritin_10",
            "Claritin",
            # Drug Facts: loratadine 10 mg per tablet.
            (LabelIngredient("LORATADINE", 10),),
        ),
        Product(
            "prod_zyrtec_10",
            "Zyrtec",
            # Drug Facts: cetirizine HCl 10 mg per tablet.
            (LabelIngredient("CETIRIZINE HCL", 10),),
        ),
        Product(
            "prod_pepcid_20",
            "Pepcid",
            # Drug Facts: famotidine 20 mg per tablet.
            (LabelIngredient("FAMOTIDINE", 20),),
        ),
        Product(
            "prod_unisom_sleeptabs_25",
            "Unisom SleepTabs",
            # Drug Facts: doxylamine succinate 25 mg per tablet.
            (LabelIngredient("DOXYLAMINE SUCCINATE", 25),),
        ),
    ]
}


FOOD_NOTE_CAFFEINE = "Typical caffeine; varies by product."
FOOD_LIST_SOURCE = "Source: Pharmakon food list"
USDA_SOURCE = "Source: USDA FoodData Central"

COMPONENT_INGREDIENT = {
    "caffeine": "CAFFEINE",
    "alcohol": "ALCOHOL",
    "grapefruit": "GRAPEFRUIT",
    "vitamin_k": "VITAMIN K",
    "tyramine": "TYRAMINE",
}
INGREDIENT_COMPONENT = {name: key for key, name in COMPONENT_INGREDIENT.items()}

FOOD_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("coffee", "Coffee", ("caffeine",)),
    ("decaf_coffee", "Decaf coffee", ()),
    ("tea", "Tea", ("caffeine",)),
    ("green_tea", "Green tea", ("caffeine",)),
    ("herbal_tea", "Herbal tea", ()),
    ("energy_drink", "Energy drink", ("caffeine",)),
    ("cola", "Cola", ("caffeine",)),
    ("wine", "Wine", ("alcohol",)),
    ("spirits", "Spirits", ("alcohol",)),
    ("grapefruit", "Grapefruit", ("grapefruit",)),
    ("grapefruit_juice", "Grapefruit juice", ("grapefruit",)),
    ("kale", "Kale", ("vitamin_k",)),
    ("spinach", "Spinach", ("vitamin_k",)),
    ("collard_greens", "Collard greens", ("vitamin_k",)),
    ("broccoli", "Broccoli", ("vitamin_k",)),
    ("aged_cheese", "Aged cheese", ("tyramine",)),
    ("cured_meats", "Cured meats", ("tyramine",)),
    ("soy_sauce", "Soy sauce", ("tyramine",)),
    ("chicken", "Chicken", ()),
    ("rice", "Rice", ()),
    ("bread", "Bread", ()),
    ("eggs", "Eggs", ()),
    ("milk", "Milk", ()),
    ("apple", "Apple", ()),
    ("banana", "Banana", ()),
    ("oatmeal", "Oatmeal", ()),
    ("pasta", "Pasta", ()),
    ("potato", "Potato", ()),
    ("salmon", "Salmon", ()),
    ("beef", "Beef", ()),
    ("pork", "Pork", ()),
    ("yogurt", "Yogurt", ()),
    ("orange", "Orange", ()),
    ("orange_juice", "Orange juice", ()),
    ("carrot", "Carrot", ()),
    ("tomato", "Tomato", ()),
    ("peanut_butter", "Peanut butter", ()),
    ("water", "Water", ()),
)


def _food_product(key: str, name: str, components: tuple[str, ...]) -> Product:
    if components:
        ingredients = tuple(
            LabelIngredient(COMPONENT_INGREDIENT[c], None) for c in components
        )
    else:
        ingredients = (LabelIngredient(name.upper(), None),)
    return Product(f"prod_food_{key}", name, ingredients)


FOOD_PRODUCTS = {
    f"prod_food_{key}": _food_product(key, name, components)
    for key, name, components in FOOD_ITEMS
}
PRODUCTS.update(FOOD_PRODUCTS)

FOOD_DRINK_IDS = frozenset(FOOD_PRODUCTS) | {"prod_example_beer"}
SUPPLEMENT_IDS = frozenset(
    {"prod_supp_vitamin_c", "prod_supp_vitamin_d", "prod_supp_fish_oil"}
)


def is_food(product_id: str) -> bool:
    return product_id in FOOD_DRINK_IDS or product_id.startswith("usda_")


def food_label(product_id: str) -> str | None:
    if product_id.startswith("usda_"):
        return USDA_SOURCE
    if product_id not in FOOD_DRINK_IDS:
        return None
    components = {
        INGREDIENT_COMPONENT.get(i.name) for i in PRODUCTS[product_id].ingredients
    }
    if product_id in ("prod_food_coffee", "prod_food_tea", "prod_food_green_tea"):
        return f"{FOOD_LIST_SOURCE}. {FOOD_NOTE_CAFFEINE}"
    if "caffeine" in components:
        return f"{FOOD_LIST_SOURCE}. {FOOD_NOTE_CAFFEINE}"
    return FOOD_LIST_SOURCE


def intake_kind(product_id: str, source: str) -> str | None:
    if is_food(product_id):
        return "food_drink"
    if product_id in SUPPLEMENT_IDS:
        return "supplement"
    if source == "skincare_catalog":
        return "skincare"
    if source in ("my_product", "user_entered"):
        return None
    return "medication"


def match_score(query: str, name: str, ingredient_names: list[str]) -> int:
    needle = query.strip().lower()
    lowered = name.lower()
    if lowered == needle:
        return 100
    if lowered.startswith(needle):
        return 80
    if any(word.startswith(needle) for word in re.split(r"\W+", lowered)):
        return 60
    if needle in lowered:
        return 40
    if any(needle in ingredient.lower() for ingredient in ingredient_names):
        return 30
    return 10


def search(q: str, registry: Registry) -> list[Product]:
    """Case-insensitive substring match on brand name or labelled ingredient.

    A resolved ingredient also matches on its registry display name, so a search
    for "paracetamol" or "APAP" finds acetaminophen products.
    """
    needle = q.strip().lower()
    queried_substance = registry.resolve(q)
    hits = []
    for product in PRODUCTS.values():
        haystack = [product.brand_name]
        substances = set()
        for ing in product.ingredients:
            haystack.append(ing.name)
            substance_id = registry.resolve(ing.name)
            if substance_id is not None:
                substances.add(substance_id)
                haystack.append(registry.get(substance_id)["name"])
        if any(needle in s.lower() for s in haystack) or (
            queried_substance is not None and queried_substance in substances
        ):
            hits.append(product)
    return hits


def decompose(product, registry: Registry) -> list[DecomposedIngredient]:
    """Resolve each labelled ingredient. `unmatched` wins over `unknown_strength`,
    which wins over `no_pk_parameters` (recognized, but the registry has too few
    parameters to chart it: logged only, like the other two).

    Accepts anything shaped like `Product` -- brand_name plus an `ingredients`
    sequence of name/strength_mg pairs. `openfda.OpenFDAProduct` has that same
    shape, so this also decomposes openFDA-discovered products.
    """
    out = []
    for ing in product.ingredients:
        substance_id = registry.resolve(ing.name)
        if substance_id is None:
            reason = "unmatched"
        elif ing.strength_mg is None:
            reason = "unknown_strength"
        elif registry.modelability(substance_id) == "none":
            reason = "no_pk_parameters"
        else:
            reason = None
        out.append(
            DecomposedIngredient(ing.name, substance_id, ing.strength_mg, reason)
        )
    return out
