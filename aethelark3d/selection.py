"""Which search result to put in front of someone.

Asked for a phone stand, a person wants something they can print tonight. A
design needing four M3 screws and a set of magnets is a shopping list with a
print attached, and offering it as the answer wastes the evening. The default
tier keeps those out and prefers the designs that come off the plate finished:
print-in-place, snap-fit, single plate.

The same rule aimed at a gearbox would reject every gearbox ever designed,
because a gearbox has bearings. So a mechanical query moves to a tier that
allows fasteners and says plainly what has to be bought — the hardware is not
the problem, discovering it after four hours of printing is.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: Below this, the makes are not consistent enough to recommend unprompted.
MIN_RATING = 4.7

#: How many come back. A voice answer that lists twelve options is a list, not
#: an answer.
MAX_RESULTS = 5

#: Anything the user would have to buy before they could finish the print.
BOM_PATTERNS = (
    r"\bm\d+\b",                     # M3, M5 — screws, bolts, nuts
    r"screw", r"bolt", r"\bnut\b", r"washer",
    r"magnet", r"bearing", r"\brod\b", r"spring",
    r"glue", r"adhes", r"epoxy",
    r"heat[- ]set", r"insert",
    r"threaded",
    r"hardware required", r"requires? ",
)
_BOM = re.compile("|".join(BOM_PATTERNS), re.IGNORECASE)

#: Tags that say the design comes off the plate finished. These are not
#: exempt from the check above — they are checked after it, so "snap-fit,
#: requires magnets" is still a shopping list.
PREFERRED_TAGS = ("print-in-place", "print in place", "printinplace",
                  "snap-fit", "snap fit", "no supports", "support-free",
                  "single plate", "one plate")

#: Queries where fasteners are the norm rather than a failure of design.
MECHANICAL_TERMS = ("gearbox", "gear box", "gear train", "planetary",
                    "vise", "vice", "clamp", "bearing", "linear rail",
                    "worm drive", "lead screw", "actuator", "coupler",
                    "torque", "chuck", "lathe", "spindle")


def _tags(design: dict) -> list[str]:
    return [str(t) for t in (design.get("tags") or [])]


def is_zero_bom(design: dict) -> bool:
    """Whether this prints into a finished object with nothing to buy."""
    return not any(_BOM.search(tag) for tag in _tags(design))


def hardware_required(design: dict) -> list[str]:
    """The tags that name something the user has to source."""
    return [tag for tag in _tags(design) if _BOM.search(tag)]


def tier_for(query: str) -> int:
    """1 for everyday utility, 2 where fasteners are expected.

    Tier 2 is an exception, not a relaxation: it exists because a gearbox
    without bearings is not a gearbox, and a filter that cannot represent that
    returns nothing for a whole class of question.
    """
    text = str(query or "").lower()
    return 2 if any(term in text for term in MECHANICAL_TERMS) else 1


def rank_score(design: dict) -> float:
    """Ordering within a tier: downloads, nudged by how self-contained it is.

    Downloads dominate because a print that thousands of people finished is
    evidence the model works, which no tag is. The bonus only separates
    designs that are otherwise equal.
    """
    downloads = float(design.get("download_count") or 0)
    tags = " ".join(_tags(design)).lower()
    bonus = 1.0 if any(tag in tags for tag in PREFERRED_TAGS) else 0.0
    return downloads + bonus


def select(designs: Iterable[dict], query: str = "",
           limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """The designs worth offering for `query`, best first.

    Every result carries `hardware_required` — empty on a tier 1 pick, and on
    a tier 2 pick the list of what to buy. It is on the result rather than
    alongside it so the caller cannot render one without the other.
    """
    tier = tier_for(query)
    picked: list[dict[str, Any]] = []

    for design in designs or []:
        if float(design.get("rating_score") or 0.0) < MIN_RATING:
            continue
        needed = hardware_required(design)
        if tier == 1 and needed:
            continue
        result = dict(design)
        result["tier"] = tier
        result["hardware_required"] = needed
        picked.append(result)

    picked.sort(key=rank_score, reverse=True)
    return picked[:limit]
