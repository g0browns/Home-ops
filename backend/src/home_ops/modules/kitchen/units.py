"""Measurement units for recipe ingredients (SPEC §4.6).

Ingredients are structured rows, not free text, which means a unit has to be a
**key from a fixed vocabulary** rather than whatever the cook typed. That is
what makes §4.6's shopping list possible: "2 cups + 500 ml of the same thing
should combine sensibly, or flag that it can't" is only answerable if both sides
resolve to a dimension and a factor.

Three dimensions. Mass and volume convert freely within themselves; COUNT does
not convert to anything, and that is the honest answer — three cloves of garlic
is not a volume, and pretending otherwise is how a shopping list ends up asking
for 0.02 litres of garlic.

**Which pint?** This matters, is easy to get silently wrong, and *was* wrong
here until 2026-08-01: the volume units were Imperial, on the assumption of a
British household. They are **US customary** — this household is American, and
an Imperial pint is 568 ml against the US 473, which is a 20% error carried
straight into recipe scaling and shopping-list totals.

The whole ladder is US customary and internally consistent, which is the part
worth checking rather than trusting:

    1 gallon = 4 quarts = 8 pints = 16 cups = 128 fluid ounces
    1 cup    = 16 tablespoons = 48 teaspoons = 8 fluid ounces

That consistency is why `cup` is the **US customary cup of 236.588 ml (8 fl
oz)** and not the 240 ml US *legal* cup used on nutrition labels. With 240, two
cups would not equal a pint, and a shopping list that adds "1 pint" to "2 cups"
would be quietly out by 7 ml with nothing to point at.

`tsp` and `tbsp` are US customary too — 4.929 ml and 14.787 ml, not the round
metric 5 and 15 — for the same reason: a tablespoon has to be exactly half a
fluid ounce or the ladder stops closing.

**Mass is unchanged and needs no decision.** The ounce and the pound are
avoirdupois, identical in both systems.

Every unit carries its factor in its label, so the UI can show "pint (473 ml)"
and leave nobody guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final


class Dimension(StrEnum):
    MASS = "mass"
    VOLUME = "volume"
    #: Countable or descriptive. Never converts — see the module docstring.
    COUNT = "count"


@dataclass(frozen=True)
class Unit:
    key: str
    singular: str
    plural: str
    dimension: Dimension
    #: Grams for MASS, millilitres for VOLUME, 1 for COUNT.
    factor: Decimal


def _u(key: str, singular: str, plural: str, dimension: Dimension, factor: str) -> Unit:
    return Unit(key, singular, plural, dimension, Decimal(factor))


UNITS: Final[tuple[Unit, ...]] = (
    # --- mass, base gram ---
    _u("g", "gram", "grams", Dimension.MASS, "1"),
    _u("kg", "kilogram", "kilograms", Dimension.MASS, "1000"),
    _u("oz", "ounce", "ounces", Dimension.MASS, "28.349523125"),
    _u("lb", "pound", "pounds", Dimension.MASS, "453.59237"),
    # --- volume, base millilitre ---
    _u("ml", "milliliter", "milliliters", Dimension.VOLUME, "1"),
    _u("l", "liter", "liters", Dimension.VOLUME, "1000"),
    # US customary from here down. Each is an exact fraction of the one below
    # it, so the ladder in the docstring closes: tsp = tbsp/3, tbsp = floz/2,
    # cup = 8 floz, pt = 2 cups, qt = 2 pt, gal = 4 qt.
    _u("tsp", "teaspoon", "teaspoons", Dimension.VOLUME, "4.92892159375"),
    _u("tbsp", "tablespoon", "tablespoons", Dimension.VOLUME, "14.78676478125"),
    _u("floz", "fluid ounce", "fluid ounces", Dimension.VOLUME, "29.5735295625"),
    _u("cup", "cup", "cups", Dimension.VOLUME, "236.5882365"),
    _u("pt", "pint", "pints", Dimension.VOLUME, "473.176473"),
    _u("qt", "quart", "quarts", Dimension.VOLUME, "946.352946"),
    _u("gal", "gallon", "gallons", Dimension.VOLUME, "3785.411784"),
    # --- countable and descriptive ---
    _u("piece", "piece", "pieces", Dimension.COUNT, "1"),
    _u("clove", "clove", "cloves", Dimension.COUNT, "1"),
    _u("slice", "slice", "slices", Dimension.COUNT, "1"),
    _u("pinch", "pinch", "pinches", Dimension.COUNT, "1"),
    _u("bunch", "bunch", "bunches", Dimension.COUNT, "1"),
    _u("can", "can", "cans", Dimension.COUNT, "1"),
    _u("packet", "packet", "packets", Dimension.COUNT, "1"),
)

UNIT_KEYS: Final[tuple[str, ...]] = tuple(unit.key for unit in UNITS)

_BY_KEY: Final[dict[str, Unit]] = {unit.key: unit for unit in UNITS}


def unit(key: str) -> Unit:
    """The unit for a key, or KeyError. Keys are validated at the boundary."""
    return _BY_KEY[key]


def is_unit(key: str | None) -> bool:
    return key in _BY_KEY


def convertible(a: str | None, b: str | None) -> bool:
    """Can a quantity in `a` be expressed in `b`?

    A missing unit means a bare count ("2 eggs"), which converts only to another
    missing unit. COUNT units never convert even to each other: two slices are
    not two cloves.
    """
    if a is None or b is None:
        return a is None and b is None
    if not (is_unit(a) and is_unit(b)):
        return False
    left, right = _BY_KEY[a], _BY_KEY[b]
    if left.dimension is Dimension.COUNT:
        return left.key == right.key
    return left.dimension is right.dimension


def convert(quantity: Decimal, from_key: str, to_key: str) -> Decimal:
    """Exact conversion within a dimension.

    Decimal throughout rather than float: these numbers are added up into a
    shopping list, and 0.1 + 0.2 is a bad look on a list somebody is holding in
    a supermarket.
    """
    if not convertible(from_key, to_key):
        raise ValueError(f"Cannot convert {from_key} to {to_key}")
    return quantity * _BY_KEY[from_key].factor / _BY_KEY[to_key].factor
