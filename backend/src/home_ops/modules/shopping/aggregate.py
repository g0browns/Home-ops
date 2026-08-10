"""Adding a week of recipes up into shopping-list lines (SPEC §4.6, §4.12).

§4.6 sets the bar precisely: "aggregating duplicate ingredients with unit
conversion (2 cups + 500ml of the same thing should combine sensibly, **or flag
that it can't**)". That last clause is the whole design. Two quantities of the
same ingredient combine only when they are genuinely the same *kind* of
quantity, and when they are not the honest answer is two lines and a flag —
never a number invented to make one line look tidy.

What can and cannot combine:

* **Mass with mass, volume with volume.** 2 cups + 500 ml of stock is 973 ml —
  a US cup is 236.588 ml, and units.py says why.
* **Mass with volume: never.** 200 g of flour and 2 cups of flour cannot be
  added without a density, which a recipe does not carry. Two lines.
* **Counts only with the identical count.** Three cloves and two slices are not
  five of anything, and neither is a volume.
* **An unmeasured ingredient stays unmeasured.** "Salt to taste" twice is still
  salt to taste, not 2 salt.

Choosing the unit to show is its own small decision. If every contributing row
used the same unit, that unit is kept — a list saying "2 cups" is more use in a
kitchen than one saying "480 ml". Once units are mixed there is no such answer,
so it falls to the dimension's base and is promoted to kg or litres when the
number gets unwieldy.

Pure: rows in, lines out. No database, no session, so every rule above is
testable directly.

Unchanged by the Phase 6 move out of the Kitchen except for where it lives. The
rules here are about quantities, not about which list a line lands on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from home_ops.modules.kitchen.units import Dimension, convert, is_unit
from home_ops.modules.kitchen.units import unit as unit_for

#: Above these, a total reads better in the larger unit. 1500 g is "1.5 kg" to a
#: person and "1500 g" to nobody.
PROMOTE_MASS = Decimal(1000)
PROMOTE_VOLUME = Decimal(1000)


@dataclass(frozen=True)
class Wanted:
    """One ingredient line off one recipe, as it was written."""

    ingredient_id: UUID
    name: str
    aisle: str | None
    quantity: Decimal | None
    unit: str | None


@dataclass
class Line:
    """One line of the shopping list."""

    ingredient_id: UUID
    name: str
    aisle: str | None
    quantity: Decimal | None
    unit: str | None
    #: How many recipe lines were folded into this one.
    from_count: int = 1
    #: True when this ingredient produced more than one line because the
    #: quantities could not be added. The UI says so rather than hiding it.
    uncombined: bool = False


def _class_of(row: Wanted) -> str:
    """Which rows may be added to which.

    The key is what makes "cannot combine" the default rather than an
    afterthought: rows only meet if they land in the same class.
    """
    if row.quantity is None:
        return "unmeasured"
    if row.unit is None:
        return "count"
    if not is_unit(row.unit):
        # A unit we do not recognise cannot be converted, so it can only ever
        # combine with itself.
        return f"unknown:{row.unit}"

    dimension = unit_for(row.unit).dimension
    if dimension is Dimension.COUNT:
        return f"count:{row.unit}"
    return dimension.value


def _combine(rows: list[Wanted]) -> tuple[Decimal | None, str | None]:
    """The total for one class of rows, and the unit to show it in."""
    first = rows[0]

    if first.quantity is None:
        return None, None

    units = {row.unit for row in rows}
    same_unit = len(units) == 1

    if same_unit and (first.unit is None or not is_unit(first.unit)):
        # A bare count, or a unit outside the vocabulary. Nothing to convert to,
        # and the class key guarantees these only ever met their own kind.
        return _tidy(sum((row.quantity or Decimal(0) for row in rows), Decimal(0))), first.unit

    if first.unit is None or not is_unit(first.unit):
        # Mixed and unconvertible. Unreachable, because the class key separates
        # them; returning the first unit is the honest fallback rather than
        # inventing one.
        return _tidy(sum((row.quantity or Decimal(0) for row in rows), Decimal(0))), first.unit

    dimension = unit_for(first.unit).dimension
    base = "g" if dimension is Dimension.MASS else "ml"
    # Everything written in one unit keeps it: "2 cups" is more use in a kitchen
    # than "480 ml". Once units are mixed there is no such answer, so it falls
    # to the dimension's base.
    target = first.unit if same_unit else base

    total = Decimal(0)
    for row in rows:
        quantity = row.quantity or Decimal(0)
        # `row.unit is None` cannot happen here — a row with no unit is its own
        # class — but it is what makes the conversion below sound, so it is
        # checked rather than asserted away.
        total += (
            convert(quantity, row.unit, target) if row.unit and row.unit != target else quantity
        )

    # Promote only out of the base unit. 1500 g is "1.5 kg" to a person and
    # "1500 g" to nobody — but 1500 cups is not 1.5 of anything, and a total
    # somebody wrote in tablespoons should stay in tablespoons.
    if target == "g" and total >= PROMOTE_MASS:
        return _tidy(convert(total, "g", "kg")), "kg"
    if target == "ml" and total >= PROMOTE_VOLUME:
        return _tidy(convert(total, "ml", "l")), "l"
    return _tidy(total), target


def _tidy(value: Decimal) -> Decimal:
    """Trim the trailing zeros a Decimal division leaves behind.

    Without this a total reads as 0.5000000 and the list looks like a
    spreadsheet rather than something to take to a shop.
    """
    normalised = value.normalize()
    exponent = normalised.as_tuple().exponent
    # `normalize()` writes 1000 as 1E+3. Correct, and not what anybody wants to
    # read on a shopping list, so it goes back to plain digits.
    if isinstance(exponent, int) and exponent > 0:
        return normalised.quantize(Decimal(1))
    return normalised


def aggregate(rows: list[Wanted]) -> list[Line]:
    """Fold a week of ingredient lines into a shopping list.

    Ordered by aisle then name, because that is the order a shop is walked and
    §4.6 asks for the list grouped by aisle.
    """
    grouped: dict[UUID, dict[str, list[Wanted]]] = {}
    for row in rows:
        grouped.setdefault(row.ingredient_id, {}).setdefault(_class_of(row), []).append(row)

    lines: list[Line] = []
    for classes in grouped.values():
        split = len(classes) > 1
        for members in classes.values():
            quantity, unit = _combine(members)
            first = members[0]
            lines.append(
                Line(
                    ingredient_id=first.ingredient_id,
                    name=first.name,
                    aisle=first.aisle,
                    quantity=quantity,
                    unit=unit,
                    from_count=len(members),
                    uncombined=split,
                )
            )

    # An ingredient with no aisle sorts last rather than first: the named
    # sections are the useful part of the walk, and the leftovers go at the end.
    lines.sort(key=lambda line: (line.aisle is None, (line.aisle or "").lower(), line.name.lower()))
    return lines


@dataclass
class Basket:
    """What a generation produced, and what it could not see."""

    lines: list[Line] = field(default_factory=list)
    #: Planned meals whose recipe this caller may not open. Reported as a count
    #: so the list is honest about being incomplete without disclosing what is
    #: missing — see plan_service for the same reasoning.
    hidden_meals: int = 0
    #: Planned entries that are free text rather than a recipe. They have no
    #: ingredients to add, and a shopper should know they were not forgotten.
    text_meals: list[str] = field(default_factory=list)
    #: Ingredients that were wanted and are already sitting on another list,
    #: because somebody moved them there. Reported so "23 lines" and a list of
    #: 21 do not look like a bug (SPEC §4.12).
    kept_on_other_lists: int = 0
