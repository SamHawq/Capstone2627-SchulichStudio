"""Availability: turns survey time-slot columns into one structured field, per term.

Surveys record availability in several shapes, and this reads all of them:
  - one column per slot, answered yes/no:
        "time_available_to_ta_monday_8_00_am_1_00_pm": "yes,"
  - one column listing the slots a candidate picked:
        "time_available_to_ta": "Monday 8.00 am - 1.00 pm,Tuesday 1.00 pm - 5.00 pm"
  - an in-person-availability question:
        "availability": "Yes, other than one or two days during the in-person portion"

and replaces them with

    "availability": {
        "Winter 2026": {
            "available":   [{"day": "mon", "start": "08:00", "end": "13:00"}, ...],
            "unavailable": [{"day": "fri", "start": "08:00", "end": "13:00"}],
            "conflicting": [...],          # only if sources disagree
            "in_person_portion": "full" | "minor_conflicts" | "away"
        }
    }

The term comes from the source's file or sheet name ("Studio Winter 2026 - ..."), because
availability for one term says nothing about another. A slot nobody answered appears in
neither list, so "said no" and "didn't answer" stay distinct. Times use a 24-hour clock, so
a scheduler can check whether a lab fits inside an available slot with plain comparisons.

Merging sources for the same term (see `merge_availability`): slots are combined; a slot
marked available in one source and unavailable in another moves to "conflicting"; and the
most restrictive in-person answer wins.
"""

import re
from typing import Any

AVAILABILITY_FIELD = "availability"

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Matches "Monday 8.00 am - 1.00 pm" in text and "monday_8_00_am_1_00_pm" in field names.
_SLOT_RE = re.compile(
    r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*[\s_]+"
    r"(\d{1,2})[.:_](\d{2})[\s_]*(am|pm)[\s_\-–to]*"
    r"(\d{1,2})[.:_](\d{2})[\s_]*(am|pm)",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"\b(fall|winter|spring|summer)[\s_]+(\d{4})\b", re.IGNORECASE)

# In-person answers, from least to most restrictive.
IN_PERSON = {
    "full": re.compile(r"fully available", re.IGNORECASE),
    "minor_conflicts": re.compile(r"other than one or two days", re.IGNORECASE),
    "away": re.compile(r"away for three or more days", re.IGNORECASE),
}
_RESTRICTIVENESS = list(IN_PERSON)

Slot = tuple[str, str, str]  # (day, start, end), e.g. ("mon", "08:00", "13:00")

# A field counts as a slot list / in-person question if at least this share of its values look like one.
DETECT_SHARE = 0.5


def _to_24h(hour: str, minute: str, half: str) -> str:
    h = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    return f"{h:02d}:{minute}"


def find_slots(text: str) -> list[Slot]:
    """Every time slot written in `text` (a value or a field name)."""
    return [(m[1][:3].lower(), _to_24h(m[2], m[3], m[4]), _to_24h(m[5], m[6], m[7]))
            for m in _SLOT_RE.finditer(str(text))]


def answer(value: Any) -> bool | None:
    """yes / "yes," -> True, no -> False, anything else -> None."""
    v = str(value).strip().strip(",").strip().lower()
    if v.startswith("yes"):
        return True
    if v.startswith("no"):
        return False
    return None


def in_person(value: Any) -> str | None:
    """Map an in-person-availability answer to "full", "minor_conflicts" or "away"."""
    return next((key for key, pattern in IN_PERSON.items() if pattern.search(str(value))), None)


def term_of(label: str) -> str:
    """Term named in a file/sheet label, e.g. "Winter 2026"; "unknown term" if there is none."""
    m = _TERM_RE.search(label.replace("_", " "))
    return f"{m[1].title()} {m[2]}" if m else "unknown term"


def _flat(value: Any) -> list[Any]:
    return [x for x in (value if isinstance(value, list) else [value]) if x not in (None, "")]


def _most_restrictive(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), key=_RESTRICTIVENESS.index, default=None)


def _sorted_slots(slots: set[Slot]) -> list[dict[str, str]]:
    return [{"day": d, "start": s, "end": e}
            for d, s, e in sorted(slots, key=lambda x: (DAYS.index(x[0]), x[1], x[2]))]


def _entry(available: set[Slot], unavailable: set[Slot], conflicting: set[Slot],
           portion: str | None) -> dict[str, Any]:
    """One term's availability, with any slot marked both ways moved to "conflicting"."""
    conflicting = conflicting | (available & unavailable)
    entry: dict[str, Any] = {
        "available": _sorted_slots(available - conflicting),
        "unavailable": _sorted_slots(unavailable - conflicting),
    }
    if conflicting:
        entry["conflicting"] = _sorted_slots(conflicting)
    if portion:
        entry["in_person_portion"] = portion
    return entry


def _slots_of(entry: dict[str, Any], key: str) -> set[Slot]:
    return {(s["day"], s["start"], s["end"]) for s in entry.get(key, [])}


def merge_availability(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Combine two `availability` values term by term (see module docstring)."""
    merged = dict(old or {})
    for term, entry in new.items():
        if term not in merged:
            merged[term] = entry
            continue
        prev = merged[term]
        merged[term] = _entry(
            _slots_of(prev, "available") | _slots_of(entry, "available"),
            _slots_of(prev, "unavailable") | _slots_of(entry, "unavailable"),
            _slots_of(prev, "conflicting") | _slots_of(entry, "conflicting"),
            _most_restrictive(prev.get("in_person_portion"), entry.get("in_person_portion")),
        )
    return merged


def extract_availability(
    records: list[dict[str, Any]], term: str
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Replace one source's availability fields with the structured `availability` field.

    Fields are detected from the data:
      - slot fields: the field name contains a time slot; values are yes/no answers (or an
        in-person answer, which some exports put in these columns);
      - slot-list fields: at least DETECT_SHARE of the values contain time slots;
      - in-person fields: at least DETECT_SHARE of the values are in-person answers.

    Args:
        records: One source's standardized records (field names already normalized).
        term: Term to file the availability under (see `term_of`).

    Returns:
        (records, consumed, warnings): the records with availability fields removed and
        `availability` added where there was any; the field names that were consumed; and
        a warning if some values in slot-list fields weren't time slots (e.g. a shifted column).
    """
    fields = {f for r in records for f in r}
    slot_fields = {f: find_slots(f)[0] for f in fields if find_slots(f)}
    list_fields, person_fields = set(), set()
    for f in fields - slot_fields.keys():
        values = [x for r in records for x in _flat(r.get(f))]
        if not values:
            continue
        if sum(bool(find_slots(x)) for x in values) >= DETECT_SHARE * len(values):
            list_fields.add(f)
        elif sum(bool(in_person(x)) for x in values) >= DETECT_SHARE * len(values):
            person_fields.add(f)
    consumed = slot_fields.keys() | list_fields | person_fields
    if not consumed:
        return records, set(), []

    ignored = 0
    for rec in records:
        available: set[Slot] = set()
        unavailable: set[Slot] = set()
        portion = None
        for f in consumed & rec.keys():
            for x in _flat(rec.pop(f)):
                if p := in_person(x):
                    portion = _most_restrictive(portion, p)
                elif f in slot_fields:
                    if (a := answer(x)) is not None:
                        (available if a else unavailable).add(slot_fields[f])
                elif slots := find_slots(x):
                    available.update(slots)
                elif answer(x) is None:
                    ignored += 1   # e.g. "Beginner" in a shifted lab-times column
        if available or unavailable or portion:
            rec[AVAILABILITY_FIELD] = {term: _entry(available, unavailable, set(), portion)}

    warnings = []
    if ignored:
        warnings.append(f"{ignored} value(s) in availability fields weren't time slots or yes/no and were ignored.")
    return records, consumed, warnings
