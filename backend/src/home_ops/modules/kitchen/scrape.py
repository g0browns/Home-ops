"""Reading a recipe out of a web page (SPEC §4.6).

§4.6 is specific about the order, and it is the right order: **schema.org
`Recipe` JSON-LD first, then microdata**, then site-specific handling. That is
how Mealie does it and it is far more reliable than scraping HTML, because it
reads the data the site published *as data* rather than guessing at its layout.

On `recipe-scrapers`, which §4.6 asks to be considered first: it is a good
library, and it is a hundred-plus site-specific scrapers plus BeautifulSoup and
extruct, maintained against sites that change. Almost every recipe site emits
JSON-LD because Google's rich results require it, and reading that is a few
hundred lines with no dependency and fixtures that run offline. So the structured
readers below are ours, and `SITE_HANDLERS` is the seam where `recipe-scrapers`
goes on the first day a site we care about genuinely needs it. That decision is
recorded rather than assumed.

Everything here is pure: HTML in, a draft out. No network — the fetching lives
in `urlfetch.py` where the security is — so every parsing decision is testable
against a saved page.

**Nothing this module returns is trusted.** It comes from a third-party page, so
it is a *draft* the cook corrects before anything is saved (§4.6 again). Strings
are length-capped here rather than at the database, because a hostile page is
allowed to be enormous and the draft has to survive it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any

MAX_FIELD = 10_000
MAX_TITLE = 200
MAX_ROWS = 200

#: Site-specific readers, tried only when the structured ones find nothing.
#: Empty today, and that is the honest state: no site has needed one yet.
SITE_HANDLERS: dict[str, Callable[[str], ScrapedRecipe | None]] = {}


@dataclass
class ScrapedRecipe:
    title: str = ""
    description: str = ""
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    image_url: str | None = None
    ingredients: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: Which reader produced this, so the UI can say where it came from and a
    #: bug report names the path taken.
    source: str = "none"

    @property
    def found_anything(self) -> bool:
        return bool(self.title or self.ingredients or self.steps)


class NoRecipeFound(ValueError):
    """The page carries no recipe we can read."""


# --- small helpers ------------------------------------------------------------


def _text(value: Any, limit: int = MAX_FIELD) -> str:
    """Anything schema.org might put in a text field, flattened to a string.

    Publishers put objects, lists and nulls in places the spec says are text.
    Each of those is a 500 waiting to happen if it reaches `.strip()`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        cleaned = unescape(value)
    elif isinstance(value, (int, float)):
        cleaned = str(value)
    elif isinstance(value, dict):
        cleaned = _text(value.get("text") or value.get("name") or value.get("@value"), limit)
    elif isinstance(value, list):
        cleaned = " ".join(_text(item, limit) for item in value)
    else:
        return ""
    return " ".join(cleaned.split())[:limit]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


_INT = re.compile(r"\d+")


def _first_int(value: Any) -> int | None:
    """A serving count out of "4", "Serves 4", "4-6 people" or {"value": 4}."""
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if isinstance(value, dict):
        return _first_int(value.get("value") or value.get("name"))
    if isinstance(value, list):
        for item in value:
            found = _first_int(item)
            if found is not None:
                return found
        return None
    if not isinstance(value, str):
        return None
    match = _INT.search(value)
    if not match:
        return None
    number = int(match.group())
    # A "serves 400" is a parse gone wrong, not a party.
    return number if 0 < number <= 1000 else None


_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:\d+S)?)?$",
    re.IGNORECASE,
)


def parse_duration(value: Any) -> int | None:
    """ISO 8601 duration to whole minutes.

    schema.org says `cookTime` is a Duration, so "PT1H30M". Plenty of sites put
    "1 hr 30 mins" there instead, so both are read.
    """
    if isinstance(value, dict):
        return parse_duration(value.get("@value") or value.get("value"))
    if isinstance(value, list):
        for item in value:
            found = parse_duration(item)
            if found is not None:
                return found
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    match = _DURATION.match(text)
    if match and any(match.groupdict().values()):
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        total = days * 1440 + hours * 60 + minutes
        return total if 0 < total <= 100_000 else None

    # "1 hr 30 mins", "45 minutes", "1 hour"
    hours_match = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", text, re.IGNORECASE)
    minutes_match = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", text, re.IGNORECASE)
    total = (int(hours_match.group(1)) * 60 if hours_match else 0) + (
        int(minutes_match.group(1)) if minutes_match else 0
    )
    return total if 0 < total <= 100_000 else None


def _image_url(value: Any) -> str | None:
    """The image, out of the several shapes publishers use for it."""
    if isinstance(value, str):
        url = value.strip()
    elif isinstance(value, dict):
        return _image_url(value.get("url") or value.get("contentUrl") or value.get("@id"))
    elif isinstance(value, list):
        for item in value:
            found = _image_url(item)
            if found:
                return found
        return None
    else:
        return None
    # Only http(s): a `data:` or `javascript:` URL here is not an image, and
    # urlfetch refuses it anyway. Rejecting early keeps it out of the draft.
    return url[:2000] if url.lower().startswith(("http://", "https://")) else None


def _instructions(value: Any) -> list[str]:
    """Steps out of `recipeInstructions`, which is the messiest field in the wild.

    It legitimately arrives as a string of prose, a list of strings, a list of
    HowToStep objects, or a HowToSection containing a nested itemListElement.
    """
    steps: list[str] = []

    def walk(node: Any) -> None:
        if len(steps) >= MAX_ROWS:
            return
        if isinstance(node, str):
            # Split the RAW string, then clean each part. `_text` collapses all
            # whitespace including newlines, so cleaning first destroys the only
            # boundary a blob of prose has.
            for part in re.split(r"[\r\n]+", node):
                cleaned = _text(part)
                if cleaned:
                    steps.append(cleaned)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = str(node.get("@type", ""))
            if "HowToSection" in kind or "itemListElement" in node:
                walk(node.get("itemListElement"))
            else:
                text = _text(node.get("text") or node.get("name"))
                if text:
                    steps.append(text)

    walk(value)
    return steps[:MAX_ROWS]


# --- JSON-LD ------------------------------------------------------------------


class _ScriptCollector(HTMLParser):
    """Pulls out the bodies of `<script type="application/ld+json">`.

    `html.parser` from the standard library rather than a parsing dependency:
    finding one kind of script tag does not justify BeautifulSoup, and the
    stdlib parser is lenient about the malformed markup real pages contain.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._capture = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        types = {value.lower() for name, value in attrs if name.lower() == "type" and value}
        if any("ld+json" in value for value in types):
            self._capture = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            self.blocks.append("".join(self._buffer))
            self._capture = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)


def _iter_nodes(payload: Any) -> list[dict[str, Any]]:
    """Every object in a JSON-LD payload, including inside @graph and lists."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        # Bounded: a hostile page can nest as deep as it likes.
        if depth > 12:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            found.append(node)
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                if key in node:
                    walk(node[key], depth + 1)

    walk(payload)
    return found


def _is_recipe(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    values = types if isinstance(types, list) else [types]
    return any(isinstance(value, str) and value.lower() == "recipe" for value in values)


def from_json_ld(html: str) -> ScrapedRecipe | None:
    collector = _ScriptCollector()
    try:
        collector.feed(html)
    except Exception:
        # A parser error on a hostile page is not a reason to 500; it just means
        # this reader found nothing and the next one gets a turn.
        return None

    for block in collector.blocks:
        try:
            payload = json.loads(block)
        except (ValueError, RecursionError):
            continue
        for node in _iter_nodes(payload):
            if _is_recipe(node):
                return _from_node(node, source="json-ld")
    return None


def _from_node(node: dict[str, Any], *, source: str) -> ScrapedRecipe:
    ingredients = [
        _text(item) for item in _as_list(node.get("recipeIngredient") or node.get("ingredients"))
    ]
    keywords = node.get("keywords")
    tags = (
        [part.strip() for part in keywords.split(",")]
        if isinstance(keywords, str)
        else [_text(item) for item in _as_list(keywords)]
    )
    tags += [_text(item) for item in _as_list(node.get("recipeCategory"))]

    return ScrapedRecipe(
        title=_text(node.get("name"), MAX_TITLE),
        description=_text(node.get("description")),
        servings=_first_int(node.get("recipeYield") or node.get("yield")),
        prep_minutes=parse_duration(node.get("prepTime")),
        cook_minutes=parse_duration(node.get("cookTime")),
        image_url=_image_url(node.get("image")),
        ingredients=[line for line in ingredients if line][:MAX_ROWS],
        steps=_instructions(node.get("recipeInstructions")),
        tags=[tag.lower()[:32] for tag in tags if tag][:20],
        source=source,
    )


# --- microdata ----------------------------------------------------------------


class _MicrodataParser(HTMLParser):
    """A deliberately small reader for `itemprop` recipes.

    Microdata is the fallback §4.6 names, and the sites still using it are the
    older ones. This handles the flat shape they actually publish — an itemprop
    per element, text or a `content`/`src`/`href` attribute — and does not try
    to implement nested itemscope properly. Anything more elaborate falls to the
    site-specific seam rather than to a half-right generic parser.
    """

    _ATTR_VALUE = ("content", "datetime", "src", "href")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.props: dict[str, list[str]] = {}
        self.saw_recipe = False
        self._stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if "recipe" in values.get("itemtype", "").lower():
            self.saw_recipe = True

        prop = values.get("itemprop")
        if not prop:
            if tag.lower() not in ("br", "img", "meta", "link", "hr", "input"):
                self._stack.append(("", None))
            return

        attribute = next((values[key] for key in self._ATTR_VALUE if values.get(key)), None)
        if attribute:
            self._record(prop, attribute)
            if tag.lower() in ("meta", "link", "img"):
                return
        self._stack.append((prop, None))

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or not self._stack:
            return
        prop = self._stack[-1][0]
        if prop:
            self._record(prop, text)

    def _record(self, prop: str, value: str) -> None:
        for name in prop.split():
            self.props.setdefault(name, []).append(value[:MAX_FIELD])


def from_microdata(html: str) -> ScrapedRecipe | None:
    parser = _MicrodataParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    if not parser.saw_recipe:
        return None

    props = parser.props

    def first(key: str) -> str:
        return (props.get(key) or [""])[0]

    recipe = ScrapedRecipe(
        title=_text(first("name"), MAX_TITLE),
        description=_text(first("description")),
        servings=_first_int(first("recipeYield")),
        prep_minutes=parse_duration(first("prepTime")),
        cook_minutes=parse_duration(first("cookTime")),
        image_url=_image_url(first("image") or first("photo")),
        ingredients=[
            _text(line)
            for line in (props.get("recipeIngredient") or props.get("ingredients") or [])
            if _text(line)
        ][:MAX_ROWS],
        steps=[_text(line) for line in (props.get("recipeInstructions") or []) if _text(line)][
            :MAX_ROWS
        ],
        source="microdata",
    )
    return recipe if recipe.found_anything else None


# --- the entry point ----------------------------------------------------------


def scrape(html: str, *, url: str = "") -> ScrapedRecipe:
    """Read a recipe out of a page, in §4.6's order.

    Raises `NoRecipeFound` when nothing readable is there, which the route turns
    into a message offering to fill it in by hand — §4.6: "failures must be
    graceful: show me what was extracted, let me fill the gaps".
    """
    for reader in (from_json_ld, from_microdata):
        found = reader(html)
        if found and found.found_anything:
            return found

    handler = SITE_HANDLERS.get(_host_of(url))
    if handler:
        found = handler(html)
        if found and found.found_anything:
            return found

    raise NoRecipeFound("No schema.org recipe data on that page.")


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
