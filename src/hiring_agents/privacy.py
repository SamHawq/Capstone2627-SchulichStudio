"""Strips identifying and protected data before anything is sent to an LLM.

Runs after intake.standardize, which has already dropped every column that isn't the ID or a
field some active criterion needs. The checks here are a second layer on top of that:
  1. `pseudonymize` runs once per request. It removes every field in `SENSITIVE_FIELDS` and
     replaces the real ID with a random-looking ref. The ref -> real-ID map stays in our
     process and is never sent to an LLM.
  2. `view_for` runs once per ranker. It sends only the fields that criterion needs, so the
     subject-knowledge ranker never sees reviews, and so on.
"""

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any

from .schemas import ID_FIELD, Candidate, Criterion

logger = logging.getLogger(__name__)

# Field names (case-insensitive) that are never sent to an LLM. Protected characteristics are
# included; see Security & Privacy in docs/ARCHITECTURE.md before changing this.
# intake.DEFAULT_FIELDS is the main list; this one catches anything that skips intake.
# Note: matching is by field NAME only. Personal details written inside free-text fields
# (e.g. a name in a reference letter) are not detected yet.
SENSITIVE_FIELDS = {
    "ucid", "student_id", "id",
    "name", "first_name", "last_name", "email", "phone", "address", "date_of_birth",
    "ethnicity", "race", "sex", "gender", "religion", "disability", "citizenship",
}


def pseudonymize(
    raw_candidates: list[dict[str, Any]], id_field: str = ID_FIELD
) -> tuple[list[Candidate], dict[str, str]]:
    """Turn raw candidate records into sanitized `Candidate`s.

    Each candidate gets a ref of the form "C-xxxxxxxx", made from a keyed hash (HMAC-SHA256) of
    their real ID. The key ("salt") is random per run unless PSEUDONYM_SALT is set, so refs
    can't be reversed or linked across runs without the returned map. Set PSEUDONYM_SALT only
    if you need refs to stay the same across runs (e.g. for debugging).

    Args:
        raw_candidates: Records straight from the data source, one dict per candidate.
        id_field: Key holding each candidate's real unique ID. Always removed from the fields.

    Returns:
        (candidates, ref_map): sanitized candidates, and a dict mapping ref to real ID used to
        re-identify the results afterwards. Keep `ref_map` on our side only.

    Raises:
        KeyError: If a record has no `id_field`.
        ValueError: If two records share an ID (or, very unlikely, two IDs hash to the same ref).
    """
    salt = os.getenv("PSEUDONYM_SALT", secrets.token_hex(16)).encode()
    candidates, ref_map, removed = [], {}, set()
    for raw in raw_candidates:
        real_id = str(raw[id_field])
        # First 8 hex chars (32 bits) is plenty for hundreds of applicants; collisions are caught below.
        ref = "C-" + hmac.new(salt, real_id.encode(), hashlib.sha256).hexdigest()[:8]
        if ref in ref_map:
            raise ValueError(f"Duplicate candidate id or ref collision for '{real_id}'")
        ref_map[ref] = real_id
        fields = {k: v for k, v in raw.items()
                  if k != id_field and k.lower() not in SENSITIVE_FIELDS}
        removed |= raw.keys() - fields.keys()
        candidates.append(Candidate(ref=ref, fields=fields))
    # Field NAMES only; values and the ref -> real-ID map are never logged.
    logger.info("Pseudonymized %d candidates; removed sensitive fields: %s",
                len(candidates), sorted(removed) or "none")
    return candidates, ref_map


def view_for(candidate: Candidate, criterion: Criterion) -> dict[str, Any]:
    """Build the record a single ranker sees for one candidate.

    Includes the candidate's ref plus only the fields listed in `criterion.relevant_fields`.
    Missing fields are left out rather than sent as null, and the ranker is instructed to
    score missing data as 0.

    Example:
        {"candidate_ref": "C-3f9a12bc", "previous_ta_courses": [...], "teaching_experience": "..."}
    """
    return {
        "candidate_ref": candidate.ref,
        **{f: candidate.fields[f] for f in criterion.relevant_fields if f in candidate.fields},
    }
