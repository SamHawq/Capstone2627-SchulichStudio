"""Criteria planning: design the criteria a dataset can be ranked on, once per dataset.

Built-in criteria list standard field names (`teaching_experience`, `relevant_courses`, ...),
but real data often has other columns, e.g. survey answers like
"verbal_and_written_skills_to_communicate". Planning turns whatever columns the data has into
a criteria catalog (`CriteriaCatalog`):
  - extensions: data fields fitted into the built-in criteria;
  - auto criteria: the remaining relevant fields grouped into new criteria, each covering one
    or more fields, each ranked by its own (generic) ranker using the field notes;
  - skipped fields, with reasons.

When it runs:
  - Compile time (compile_candidates.py): `refresh_catalog` plans the compiled store and saves
    candidate_data/criteria.json. Unchanged data reuses the file, so hand edits are kept.
  - Request time, for data passed in directly (or a store with no criteria.json):
    `plan_catalog` plans it on the fly, cached in candidate_data/criteria_plans.json.

Steps:
  1. `profile_fields` summarizes each field. No candidate answers or IDs, only field names,
     column headers, fill counts, kinds and widely shared category values.
  2. The Criteria Planner agent (llm_agents/criteria_planner.py) returns a `CriteriaDraft`.
  3. `validate_draft` checks it in plain code: unknown or reused fields are dropped, ids are
     made unique, and at most MAX_CATALOG_CRITERIA auto criteria are kept.

At request time `with_catalog` adds the catalog to the registry, and `add_auto_criteria`
picks the auto criteria that join. Requested criteria keep the client's weights; a joining
auto criterion gets
    weight = (average requested weight) * importance / 2
so importance 2 counts like an average requested criterion, 1 half that, 3 one and a half.
"""

import hashlib
import json
import logging
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from .agent_trace import run_agent
from .availability import AVAILABILITY_FIELD
from .datastore import CATALOG_PATH, DATA_DIR
from .intake import normalize
from .llm_agents import build_criteria_planner
from .registry import CriterionRegistry
from .schemas import (
    ID_FIELD, CriteriaCatalog, CriteriaDraft, Criterion, CriterionExtension, FieldNote,
    FieldProfile, HiringRequest, IntakeReport, SkippedField,
)

logger = logging.getLogger(__name__)

MAX_CATALOG_CRITERIA = 12  # auto criteria kept in a catalog
MAX_AUTO_CRITERIA = 5      # auto criteria that join one request (bounds extra ranker calls)
MIN_SHARED = 3             # a category value is shown to the planner only if this many candidates share it
MAX_CATEGORIES = 12        # more distinct values than this -> treated as free text
SENTENCE_WORDS = 6         # fewest words for an answer to count as a sentence (see is_sentence)
PLAN_CACHE = DATA_DIR / "criteria_plans.json"
MAX_ATTEMPTS = 2


def _is_number(x: Any) -> bool:
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return True
    try:
        float(str(x))
        return True
    except ValueError:
        return False


def field_labels(reports: list[IntakeReport]) -> dict[str, str]:
    """Field -> the first original column header that was mapped to it."""
    labels: dict[str, str] = {}
    for report in reports:
        for col, fid in report.column_mapping.items():
            labels.setdefault(fid, col)
    return labels


def profile_fields(
    records: list[dict[str, Any]], exclude: set[str], labels: dict[str, str] | None = None
) -> list[FieldProfile]:
    """Summarize each field (except the ID, availability and `exclude`) for the planner.

    Availability is left out here, not only in the planner's instructions: a separate
    scheduling step handles it, so it must never become a scored criterion.

    Args:
        records: Standardized records (see intake.standardize).
        exclude: Fields that built-in criteria already use.
        labels: Original column header per field (see `field_labels`).

    Returns:
        One profile per field, in first-seen order. A field is a "category" if it has at most
        MAX_CATEGORIES distinct short values; only values shared by MIN_SHARED or more
        candidates are listed, so no individual's answer is passed on.
    """
    skip = exclude | {ID_FIELD, AVAILABILITY_FIELD}
    values: dict[str, list[Any]] = {}
    for rec in records:
        for field, v in rec.items():
            if field not in skip:
                values.setdefault(field, []).append(v)

    profiles = []
    for field, vals in values.items():
        flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
        counts = Counter(str(x).strip() for x in flat)
        if all(_is_number(x) for x in flat):
            kind = "number"
        elif len(counts) <= MAX_CATEGORIES and all(len(k) <= 40 for k in counts):
            kind = "category"
        else:
            kind = "text"
        common = [v for v, n in counts.most_common(8) if n >= MIN_SHARED] if kind == "category" else []
        # Length statistics over every value (not just the first rows, which may be blank),
        # so free-text answers can be told apart from short codes or lists.
        texts = [str(x) for x in flat] if kind != "number" else []
        profiles.append(FieldProfile(
            field=field, label=(labels or {}).get(field, ""), filled=len(vals), kind=kind,
            common_values=common,
            avg_words=round(mean(len(t.split()) for t in texts), 1) if texts else 0.0,
            sentence_share=round(sum(map(is_sentence, texts)) / len(texts), 2) if texts else 0.0))
    return profiles


def is_sentence(text: str) -> bool:
    """Whether an answer reads like prose: SENTENCE_WORDS+ words, at least half of them
    ordinary lowercase words. "Clear explanations and well prepared for every lab." is;
    a course list like "ENGG 233: A, ENSF 338: A-" is not."""
    words = [w.strip(".,;:!?()'\"") for w in text.split()]
    return len(words) >= SENTENCE_WORDS and sum(w.isalpha() and w.islower() for w in words) >= len(words) / 2


def tiebreak_fields(catalog: CriteriaCatalog | None) -> list[str]:
    """Fields no criterion uses that are other information about the candidate (use
    "tiebreak"), for breaking ties in the final review. Excluded uses (availability,
    administrative, protected, identifying, unusable) are never included."""
    return [s.field for s in (catalog.skipped if catalog else []) if s.use == "tiebreak"]


def base_fields(criteria: list[Criterion]) -> set[str]:
    return {f for c in criteria for f in c.relevant_fields}


# --- Planning (one LLM call) ---

async def run_planner(
    purpose: str, base: list[Criterion], profiles: list[FieldProfile]
) -> tuple[CriteriaDraft | None, list[str]]:
    """Ask the Criteria Planner for a draft. A failure is a warning, not an error."""
    payload = json.dumps({
        "purpose": purpose,
        "max_new_criteria": MAX_CATALOG_CRITERIA,
        "built_in_criteria": [
            {"id": c.id, "name": c.name, "description": c.description, "uses_fields": c.relevant_fields}
            for c in base
        ],
        "fields": [p.model_dump() for p in profiles],
    }, indent=2)
    agent = build_criteria_planner()
    logger.info("[planner] Planning %d fields (model: %s)", len(profiles), agent.model or "SDK default")
    logger.debug("[planner] Payload:\n%s", payload)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            draft = await run_agent(agent, payload, CriteriaDraft)
            break
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("[planner] Attempt %d/%d failed after %.1fs: %s",
                           attempt, MAX_ATTEMPTS, time.perf_counter() - start, e)
    else:
        return None, [f"Criteria planner failed, so only the criteria's own fields are used: {last_error}"]

    logger.info("[planner] Done in %.1fs: %d extensions, %d criteria, %d skipped",
                time.perf_counter() - start, len(draft.extensions), len(draft.criteria), len(draft.skipped))
    logger.debug("[planner] Draft:\n%s", draft.model_dump_json(indent=2))
    return draft, []


def validate_draft(
    draft: CriteriaDraft, profiles: list[FieldProfile], base: list[Criterion], purpose: str
) -> tuple[CriteriaCatalog, list[str]]:
    """Check the planner's draft and turn it into a catalog.

    Args:
        draft: The planner's output.
        profiles: The fields the planner was offered; anything else it names is ignored.
        base: The built-in criteria it could extend; their ids are taken.
        purpose: Stored in the catalog.

    Returns:
        (catalog, warnings): warnings name everything in the draft that was dropped.
    """
    offered = [p.field for p in profiles]
    used: set[str] = set()
    warnings: list[str] = []

    def take(notes: list[FieldNote], owner: str) -> list[FieldNote]:
        kept = []
        for n in notes:
            if n.field not in offered:
                warnings.append(f"Planner named unknown field '{n.field}' for '{owner}'; ignored.")
            elif n.field in used:
                warnings.append(f"Planner used field '{n.field}' twice; kept only for its first criterion.")
            else:
                used.add(n.field)
                kept.append(n)
        return kept

    base_ids = {c.id for c in base}
    extended: dict[str, list[FieldNote]] = {}
    for ext in draft.extensions:
        if ext.criterion_id not in base_ids:
            warnings.append(f"Planner extended unknown criterion '{ext.criterion_id}'; ignored.")
            continue
        extended.setdefault(ext.criterion_id, []).extend(take(ext.fields, ext.criterion_id))
    extensions = [CriterionExtension(criterion_id=cid, fields=notes) for cid, notes in extended.items() if notes]

    criteria: list[Criterion] = []
    taken = set(base_ids)
    for d in draft.criteria:
        if len(criteria) >= MAX_CATALOG_CRITERIA:
            warnings.append(f"Planner proposed more than {MAX_CATALOG_CRITERIA} criteria; '{d.id}' and later ones were dropped.")
            break
        cid = normalize(d.id) or "criterion"
        if cid in taken:
            cid = f"auto_{cid}"
        n = 2
        while cid in taken:
            cid, n = f"{cid}_{n}", n + 1
        notes = take(d.fields, cid)
        if not notes:
            warnings.append(f"Planner's criterion '{d.id}' had no usable fields; dropped.")
            continue
        taken.add(cid)
        criteria.append(Criterion(
            id=cid, name=d.name, description=d.description, scoring_guidance=d.scoring_guidance,
            relevant_fields=[n.field for n in notes], field_notes=notes, type="auto",
            importance=min(3, max(1, d.importance)), courses=[c.strip() for c in d.courses if c.strip()],
        ))

    skipped = [SkippedField(field=s.field, reason=s.reason, use=s.use)
               for s in draft.skipped if s.field in offered and s.field not in used]
    named = used | {s.field for s in skipped}
    # Fields the planner didn't mention are other information (they passed the header screen).
    skipped += [SkippedField(field=f, reason="Not used by any criterion.", use="tiebreak")
                for f in offered if f not in named]
    catalog = CriteriaCatalog(purpose=purpose, fields=offered, extensions=extensions,
                              criteria=criteria, skipped=skipped)
    logger.info("Catalog: extensions %s, criteria %s",
                {e.criterion_id: [n.field for n in e.fields] for e in extensions},
                {c.id: c.relevant_fields for c in criteria})
    return catalog, warnings


async def _plan(purpose, base, profiles) -> tuple[CriteriaCatalog | None, list[str]]:
    draft, warnings = await run_planner(purpose, base, profiles)
    if draft is None:
        return None, warnings
    catalog, issues = validate_draft(draft, profiles, base, purpose)
    return catalog, warnings + issues


def _plan_key(purpose: str, base: list[Criterion], profiles: list[FieldProfile]) -> str:
    """Cache key: same purpose, built-in criteria and field names -> same catalog."""
    basis = {
        "purpose": purpose,
        "criteria": sorted((c.id, sorted(c.relevant_fields)) for c in base),
        "fields": sorted(p.field for p in profiles),
        "max": MAX_CATALOG_CRITERIA,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:16]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def plan_catalog(
    records: list[dict[str, Any]],
    base: list[Criterion],
    purpose: str,
    labels: dict[str, str] | None = None,
    cache_path: Path | None = None,
) -> tuple[CriteriaCatalog | None, list[str]]:
    """Plan a catalog for data passed in at request time: from the cache, or one planner call.

    Delete the cache file (PLAN_CACHE, candidate_data/criteria_plans.json) to force a new plan.

    Returns:
        (catalog, warnings): catalog is None if there's nothing to plan or the planner failed.
    """
    profiles = profile_fields(records, base_fields(base), labels)
    if not profiles:
        return None, []
    cache_path = cache_path or PLAN_CACHE
    key = _plan_key(purpose, base, profiles)
    cache = _read_json(cache_path)
    if key in cache:
        try:
            catalog = CriteriaCatalog.model_validate(cache[key])
            logger.info("Reusing cached criteria catalog %s", key)
            return catalog, []
        except ValueError:
            pass  # written by an older version; plan again
    catalog, warnings = await _plan(purpose, base, profiles)
    if catalog:
        cache[key] = catalog.model_dump()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return catalog, warnings


# --- The store's catalog file (candidate_data/criteria.json) ---

def load_catalog(path: Path = CATALOG_PATH) -> CriteriaCatalog | None:
    """Read a saved catalog; None if there isn't one."""
    if not path.exists():
        return None
    return CriteriaCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def write_catalog(catalog: CriteriaCatalog, path: Path = CATALOG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
    return path


async def refresh_catalog(
    records: list[dict[str, Any]],
    base: list[Criterion],
    purpose: str,
    labels: dict[str, str] | None = None,
    path: Path = CATALOG_PATH,
    rebuild: bool = False,
) -> tuple[CriteriaCatalog | None, bool, list[str]]:
    """Plan the compiled store's catalog and save it, unless the saved one still fits.

    The saved catalog is reused (hand edits kept) if it was planned for the same purpose and
    the same fields. Otherwise, or with `rebuild`, it is planned again and the old file is
    kept next to it as criteria.prev.json.

    Returns:
        (catalog, planned, warnings): planned is True if the planner ran.
    """
    profiles = profile_fields(records, base_fields(base), labels)
    saved = load_catalog(path)
    if (saved and not rebuild and saved.purpose == purpose
            and set(saved.fields) == {p.field for p in profiles}):
        return saved, False, []
    if not profiles:
        return None, False, ["No fields to plan criteria for."]
    catalog, warnings = await _plan(purpose, base, profiles)
    if catalog:
        if saved:
            shutil.copyfile(path, path.with_suffix(".prev.json"))
        write_catalog(catalog, path)
    return catalog, True, warnings


def new_fields(catalog: CriteriaCatalog, records: list[dict[str, Any]], base: list[Criterion]) -> list[str]:
    """Fields in the data that the catalog wasn't planned with (the store changed since)."""
    known = set(catalog.fields) | base_fields(base) | {ID_FIELD, AVAILABILITY_FIELD}
    return sorted({f for r in records for f in r} - known)


# --- Request time ---

def with_catalog(
    registry: CriterionRegistry, catalog: CriteriaCatalog
) -> tuple[CriterionRegistry, list[str]]:
    """A copy of `registry` with the catalog's extensions applied and its criteria added."""
    warnings = []
    extra = {e.criterion_id: e.fields for e in catalog.extensions}
    criteria = []
    for cid in registry.ids():
        c = registry.get(cid)
        notes = [n for n in extra.pop(cid, []) if n.field not in c.relevant_fields]
        criteria.append(c.model_copy(update={
            "relevant_fields": c.relevant_fields + [n.field for n in notes],
            "field_notes": c.field_notes + notes,
        }))
    for cid in extra:
        warnings.append(f"Catalog extends criterion '{cid}', which isn't registered; ignored.")
    for c in catalog.criteria:
        if registry.get(c.id) is None:
            criteria.append(c)
        else:
            warnings.append(f"Catalog criterion '{c.id}' clashes with a registered criterion; ignored.")
    return CriterionRegistry(criteria), warnings


def _code(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def applies_to(criterion: Criterion, role: str) -> bool:
    """True if the criterion isn't course-specific, or the role names one of its courses."""
    return not criterion.courses or any(_code(c) in _code(role) for c in criterion.courses)


def add_auto_criteria(
    catalog: CriteriaCatalog, active: list[Criterion], request: HiringRequest
) -> tuple[list[Criterion], dict[str, float], list[SkippedField]]:
    """Add the catalog's auto criteria that apply to this request, and give them weights.

    An auto criterion joins if the request didn't name it (naming it with weight 0 keeps it
    out), it applies to the role (see `applies_to`), and fewer than MAX_AUTO_CRITERIA have
    joined; the most important go first.

    Args:
        catalog: The data's catalog.
        active: The requested criteria that are running (after `with_catalog`), not empty.
        request: For the requested weights and the role.

    Returns:
        (criteria, weights, skipped): the requested criteria followed by the joining auto
        criteria; a weight for each; and every catalog field no running criterion uses, with
        the reason.
    """
    weights = {c.id: request.criteria[c.id] for c in active}
    base = mean(weights.values())
    criteria, joined = list(active), 0
    left_out: dict[str, str] = {}   # criterion id -> reason
    for c in sorted((c for c in catalog.criteria if c.id not in weights), key=lambda c: -c.importance):
        if c.id in request.criteria:
            left_out[c.id] = "given weight 0 in the request"
        elif not applies_to(c, request.role):
            left_out[c.id] = f"specific to {', '.join(c.courses)}, which the role doesn't name"
        elif joined >= MAX_AUTO_CRITERIA:
            left_out[c.id] = f"more than {MAX_AUTO_CRITERIA} auto criteria apply; less important ones are left out"
        else:
            criteria.append(c)
            weights[c.id] = round(base * c.importance / 2, 4)
            joined += 1
    for e in catalog.extensions:
        if e.criterion_id not in weights:
            left_out[e.criterion_id] = "not requested"

    owner = {n.field: cid for cid, notes in
             [(c.id, c.field_notes) for c in catalog.criteria] + [(e.criterion_id, e.fields) for e in catalog.extensions]
             for n in notes}
    skipped = list(catalog.skipped) + [
        SkippedField(field=f, reason=f"Used by '{cid}', which didn't run: {left_out[cid]}.", use="unused_criterion")
        for f, cid in owner.items() if cid in left_out
    ]
    logger.info("Auto criteria joined: %s", {c.id: weights[c.id] for c in criteria if c.id not in request.criteria})
    return criteria, weights, skipped
