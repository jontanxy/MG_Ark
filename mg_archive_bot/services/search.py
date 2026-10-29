from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import ProjectStatus
from ..models import Project
from ..util import normalise_terms

# Ranking per requirement §10: exact tag > project name > event/collection > metadata > description.
# People's names are never searched: the Creator field is left out on purpose.
# Weights are separated by more than MAX_TERMS so a single higher-tier match always outranks any
# number of lower-tier matches (lexicographic ordering of tiers).
MAX_TERMS = 20
W_EXACT_TAG = 10**8
W_NAME = 10**6
W_EVENT_COLLECTION = 10**4
W_METADATA = 10**2
W_DESCRIPTION = 1


@dataclass
class SearchHit:
    project: Project
    score: int
    matched: dict[str, str] = field(default_factory=dict)  # term -> tier it matched in ("tag", "name", ...)
    reasons: dict[str, str] = field(default_factory=dict)  # term -> where exactly, for people ("tag #gold", "colours")


def _score_term(project: Project, term: str) -> tuple[int, str, str]:
    """(score, tier, reason) of the best place *term* occurs in; score 0 when it occurs nowhere searchable."""
    tags = [t.lower() for t in project.tag_names]
    if term in tags:
        return W_EXACT_TAG, "tag", f"tag #{term}"
    if term in (project.name or "").lower():
        return W_NAME, "name", "project name"
    if term in (project.event or "").lower():
        return W_EVENT_COLLECTION, "event/collection", "event"
    if term in (project.collection or "").lower():
        return W_EVENT_COLLECTION, "event/collection", "collection"
    partial_tag = next((t for t in tags if term in t), None)
    if partial_tag is not None:
        return W_METADATA, "metadata", f"tag #{partial_tag}"
    metadata_fields = (  # no Creator: projects are not found by people's names
        ("ministry", project.ministry),
        ("style", project.style),
        ("colours", project.colours),
        ("year", str(project.year or "")),
        ("asset types", project.asset_types),
    )
    for label, value in metadata_fields:
        if term in (value or "").lower():
            return W_METADATA, "metadata", label
    if term in (project.description or "").lower():
        return W_DESCRIPTION, "description", "description"
    return 0, "", ""


def score_project(project: Project, terms: list[str]) -> SearchHit | None:
    """All terms must match (AND). Returns None when any term is unmatched."""
    total = 0
    matched: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for term in terms:
        score, where, reason = _score_term(project, term)
        if score == 0:
            return None
        total += score
        matched[term] = where
        reasons[term] = reason
    return SearchHit(project=project, score=total, matched=matched, reasons=reasons)


def search_projects(session: Session, query: str, *, include_drafts: bool = False) -> list[SearchHit]:
    terms = normalise_terms(query)[:MAX_TERMS]
    if not terms:
        return []
    whole = " ".join(terms)
    stmt = select(Project).where(Project.status != ProjectStatus.CANCELLED)
    if not include_drafts:
        stmt = stmt.where(Project.status != ProjectStatus.DRAFT)
    hits: list[SearchHit] = []
    for project in session.scalars(stmt).all():
        hit = score_project(project, terms)
        if hit is not None:
            hits.append(hit)
    hits.sort(
        key=lambda h: (
            -h.score,
            0 if h.project.name.lower() == whole else 1,  # whole-name match wins ties
            -(h.project.year or 0),
            h.project.name.lower(),
        )
    )
    return hits
