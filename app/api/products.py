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
    unmodeled_reason: str | None  # None | "unmatched" | "unknown_strength"


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
    """Resolve each labelled ingredient. `unmatched` wins over `unknown_strength`.

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
        else:
            reason = None
        out.append(
            DecomposedIngredient(ing.name, substance_id, ing.strength_mg, reason)
        )
    return out
