"""Turning "1 1/2 cups plain flour, sifted" into structured fields (SPEC §4.6).

Recipe sites publish ingredients as prose, and this project stores them as rows.
Something has to bridge that, and §4.6 is explicit about how it should feel:
parse at import time, and **let the cook correct the parse in the UI before
saving**. That second half is what makes this code safe to write at all — it is
allowed to be wrong, as long as it is wrong visibly and in a form somebody can
fix in two clicks.

So the rules here are deliberately conservative:

* **Never invent a quantity.** "Salt to taste" parses to no quantity, not to 1.
* **Never guess a unit that is not written.** "2 onions" is a bare count, not
  2 pieces — the count is the number and the thing is the onion.
* **When a token is not recognised, leave it in the name.** A wrong name the
  cook can see and edit beats a silently dropped word.

Everything here is pure and offline. The parser is the part most likely to be
wrong on a site nobody has tried yet, so it is the part that must be testable
without a network.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from home_ops.modules.kitchen.units import UNITS

#: Written forms that map onto a unit key. Longest match wins, so "fl oz" is
#: never read as "oz", and plurals and stops are handled here rather than by
#: trying to stem English.
UNIT_WORDS: dict[str, str] = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "gr": "g",
    "gs": "g",
    "kg": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "ml": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "cl": "ml",  # centilitres are rare enough to fold, with a factor below
    "l": "l",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "t": "tsp",
    "tbsp": "tbsp",
    "tbs": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "cup": "cup",
    "cups": "cup",
    "fl oz": "floz",
    "floz": "floz",
    "fluid ounce": "floz",
    "fluid ounces": "floz",
    "pt": "pt",
    "pint": "pt",
    "pints": "pt",
    "qt": "qt",
    "quart": "qt",
    "quarts": "qt",
    "gal": "gal",
    "gallon": "gal",
    "gallons": "gal",
    "clove": "clove",
    "cloves": "clove",
    "slice": "slice",
    "slices": "slice",
    "pinch": "pinch",
    "pinches": "pinch",
    "bunch": "bunch",
    "bunches": "bunch",
    "can": "can",
    "cans": "can",
    "tin": "can",
    "tins": "can",
    "packet": "packet",
    "packets": "packet",
    "pack": "packet",
    "packs": "packet",
    "piece": "piece",
    "pieces": "piece",
}

#: `cl` folds onto millilitres, so its number needs multiplying to match.
_UNIT_SCALE: dict[str, Decimal] = {"cl": Decimal(10)}

#: Vulgar fractions, which appear constantly in published recipes and which
#: `Decimal` cannot read.
_VULGAR: dict[str, str] = {
    "¼": "1/4",
    "½": "1/2",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅕": "1/5",
    "⅖": "2/5",
    "⅗": "3/5",
    "⅘": "4/5",
    "⅙": "1/6",
    "⅚": "5/6",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

#: Words that describe the *thing*, not its measurement, and which must not be
#: mistaken for units. "2 large onions" is two onions, not two larges.
_SIZE_WORDS = frozenset({"large", "medium", "small", "extra", "whole", "heaped", "level"})

_KNOWN_UNIT_KEYS = frozenset(unit.key for unit in UNITS)

# Quantity forms, tried in this order. The order is the whole trick: "1 1/2" has
# to be read as one and a half before "1" can claim it, and "1/2" has to be read
# as a half before "1" can claim that too. One combined regex expressing the
# same precedence was write-only, and got both of those wrong.
_MIXED = re.compile(r"^\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s*")
_FRACTION = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*")
# Ranges are written with a hyphen, an en dash or an em dash depending on the
# site's house style, and ruff refuses a literal dash in a pattern as an
# ambiguous character. Naming them as escapes in a normal string and
# interpolating satisfies both: the regex still matches what sites publish.
_DASHES = "-\u2013\u2014"
_RANGE = re.compile(
    rf"^\s*(\d+(?:[.,]\d+)?)\s*(?:[{_DASHES}]|\bto\b|\bor\b)\s*\d+(?:[.,]\d+)?\s*",
    re.IGNORECASE,
)
_PLAIN = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*")


@dataclass(frozen=True)
class ParsedIngredient:
    name: str
    quantity: Decimal | None = None
    unit: str | None = None
    note: str | None = None
    #: False when nothing measurable was found. The UI leans on this to show the
    #: cook which rows are worth a second look, which is what makes a
    #: best-effort parser acceptable.
    confident: bool = True


def _normalise(text: str) -> str:
    """Fold everything into ASCII digits, slashes and single spaces.

    The vulgar fractions are replaced **before** NFKC, not after. NFKC does
    decompose them - but into U+2044 FRACTION SLASH rather than "/", so a
    replacement running afterwards never matches and "1/2 tsp salt" written with
    the glyph parses as 1. The U+2044 mapping below catches any that arrive
    already decomposed.
    """
    folded = text
    for glyph, replacement in _VULGAR.items():
        folded = folded.replace(glyph, f" {replacement} ")
    folded = unicodedata.normalize("NFKC", folded)
    folded = folded.replace("\u2044", "/").replace("\u00a0", " ")
    return " ".join(folded.split())


def _to_decimal(whole: str | None, fraction: str | None) -> Decimal | None:
    total = Decimal(0)
    try:
        if whole:
            total += Decimal(whole.replace(",", "."))
        if fraction:
            numerator, denominator = (part.strip() for part in fraction.split("/"))
            if Decimal(denominator) == 0:
                return None
            total += Decimal(numerator) / Decimal(denominator)
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return None
    return total if total > 0 else None


def _take_unit(words: list[str]) -> tuple[str | None, int]:
    """The unit at the head of `words`, and how many words it consumed.

    Two-word forms are tried first so "fl oz" is never read as "fl" plus a
    stray "oz".
    """
    if not words:
        return None, 0

    def clean(word: str) -> str:
        return word.strip().strip(".,").lower()

    if len(words) >= 2:
        pair = f"{clean(words[0])} {clean(words[1])}"
        if pair in UNIT_WORDS:
            return UNIT_WORDS[pair], 2

    first = clean(words[0])
    if first in _SIZE_WORDS:
        return None, 0
    if first in UNIT_WORDS:
        return UNIT_WORDS[first], 1
    return None, 0


def _take_quantity(text: str) -> tuple[Decimal | None, str]:
    """The leading quantity and whatever follows it.

    A malformed fraction — "1/0", "1/" — yields no quantity and consumes
    nothing, so the text stays in the name for the cook to see rather than being
    half-eaten.
    """
    mixed = _MIXED.match(text)
    if mixed:
        value = _to_decimal(mixed.group(1), f"{mixed.group(2)}/{mixed.group(3)}")
        return (value, text[mixed.end() :]) if value is not None else (None, text)

    fraction = _FRACTION.match(text)
    if fraction:
        value = _to_decimal(None, f"{fraction.group(1)}/{fraction.group(2)}")
        return (value, text[fraction.end() :]) if value is not None else (None, text)

    ranged = _RANGE.match(text)
    if ranged:
        # The low end: scaling from it never over-buys.
        value = _to_decimal(ranged.group(1), None)
        return (value, text[ranged.end() :]) if value is not None else (None, text)

    plain = _PLAIN.match(text)
    if plain:
        value = _to_decimal(plain.group(1), None)
        return (value, text[plain.end() :]) if value is not None else (None, text)

    return None, text


def parse_ingredient(text: str) -> ParsedIngredient:
    """Best effort, and honest about it. Never raises."""
    cleaned = _normalise(text)
    if not cleaned:
        return ParsedIngredient(name="", confident=False)

    # A parenthetical is nearly always a note: "(about 400g)", "(optional)".
    note_parts: list[str] = []

    def steal_parenthetical(match: re.Match[str]) -> str:
        note_parts.append(match.group(1).strip())
        return " "

    cleaned = re.sub(r"\(([^)]*)\)", steal_parenthetical, cleaned)
    cleaned = " ".join(cleaned.split())

    quantity, rest = _take_quantity(cleaned)

    words = rest.split()
    unit_key: str | None = None
    if quantity is not None:
        # A unit only means anything after a number. "Cup of tea" is not a unit.
        raw_first = words[0].strip().strip(".,").lower() if words else ""
        unit_key, consumed = _take_unit(words)
        if unit_key:
            words = words[consumed:]
            scale = _UNIT_SCALE.get(raw_first)
            if scale is not None:
                quantity *= scale

    remainder = " ".join(words).strip()

    # Everything after the first comma is a preparation note, not the thing:
    # "onions, finely chopped".
    if "," in remainder:
        head, _, tail = remainder.partition(",")
        remainder = head.strip()
        if tail.strip():
            note_parts.insert(0, tail.strip())

    # A leading "of" is left over from "a pinch of salt".
    remainder = re.sub(r"^of\s+", "", remainder, flags=re.IGNORECASE).strip()
    remainder = remainder.strip(" .;:")

    note = ", ".join(part for part in note_parts if part) or None
    if unit_key is not None and unit_key not in _KNOWN_UNIT_KEYS:
        unit_key = None

    return ParsedIngredient(
        name=remainder or cleaned,
        quantity=quantity,
        unit=unit_key,
        note=note,
        # Confident means "we found something measurable and something to call
        # it". Anything else is flagged for the cook rather than hidden.
        confident=bool(remainder) and quantity is not None,
    )


def parse_ingredients(lines: list[str]) -> list[ParsedIngredient]:
    return [parse_ingredient(line) for line in lines if line and line.strip()]
