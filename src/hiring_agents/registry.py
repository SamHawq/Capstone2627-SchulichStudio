"""Criterion Registry: the set of criteria the system knows how to rank on.

Rankers aren't given open-ended tasks. Each one is tied to a registered `Criterion` with a fixed
description, scoring guide, and list of allowed fields. This keeps rankers predictable and makes
the system reusable beyond TA hiring: a new client supplies a different registry, not new code.

To add a built-in criterion, append a `Criterion` to `DEFAULT_CRITERIA`. Make sure its
`relevant_fields` match keys that exist in the candidate data.
"""

from .schemas import Criterion

# Upper limit on client-defined criteria ("10 BONUS agents" in docs/ARCHITECTURE.md).
MAX_BONUS_CRITERIA = 10


class CriterionRegistry:
    """A lookup table of criteria, keyed by `Criterion.id`."""

    def __init__(self, criteria: list[Criterion] | None = None):
        """Create a registry, optionally pre-filled with `criteria`."""
        self._criteria: dict[str, Criterion] = {}
        for c in criteria or []:
            self.register(c)

    def register(self, criterion: Criterion) -> None:
        """Add a criterion.

        Raises:
            ValueError: If the id is already taken, or if adding a bonus criterion would go
                over `MAX_BONUS_CRITERIA`.
        """
        if criterion.id in self._criteria:
            raise ValueError(f"Criterion '{criterion.id}' is already registered")
        if criterion.type == "bonus" and len(self.bonus()) >= MAX_BONUS_CRITERIA:
            raise ValueError(f"At most {MAX_BONUS_CRITERIA} bonus criteria are allowed")
        self._criteria[criterion.id] = criterion

    def get(self, criterion_id: str) -> Criterion | None:
        """Return the criterion with this id, or None if it isn't registered."""
        return self._criteria.get(criterion_id)

    def ids(self) -> list[str]:
        """Return the ids of every registered criterion."""
        return list(self._criteria)

    def bonus(self) -> list[Criterion]:
        """Return all client-defined (bonus) criteria currently registered."""
        return [c for c in self._criteria.values() if c.type == "bonus"]

    def copy(self) -> "CriterionRegistry":
        """Return an independent copy.

        Used so that per-request bonus criteria can be added without changing the shared
        default registry.
        """
        return CriterionRegistry(list(self._criteria.values()))


# Built-in criteria for TA hiring. Other clients can supply their own registry.
DEFAULT_CRITERIA = [
    Criterion(
        id="teaching_experience",
        name="Teaching Experience",
        description="Prior experience teaching, tutoring, or TAing.",
        scoring_guidance=(
            "10 = multiple prior TA terms in closely related courses; "
            "5 = some tutoring or one unrelated TA term; 0 = no teaching experience."
        ),
        relevant_fields=["previous_ta_courses", "teaching_experience"],
    ),
    Criterion(
        id="subject_knowledge",
        name="Subject Knowledge",
        description="Mastery of the course material the TA will support.",
        scoring_guidance=(
            "Weigh grades in courses relevant to the role most heavily, then program and year. "
            "10 = top grades in the course itself or direct prerequisites; 0 = no relevant coursework."
        ),
        relevant_fields=["program", "year_of_study", "relevant_courses"],
    ),
    Criterion(
        id="past_performance",
        name="Past TA Performance",
        description="Quality of previous TA work based on reviews from professors or students.",
        scoring_guidance=(
            "10 = consistently excellent reviews; 5 = mixed or average; "
            "0 = poor reviews. If there are no reviews, score 0 and say so."
        ),
        relevant_fields=["previous_ta_reviews"],
    ),
]


def default_registry() -> CriterionRegistry:
    """Return a new registry containing the built-in TA hiring criteria."""
    return CriterionRegistry(DEFAULT_CRITERIA)
