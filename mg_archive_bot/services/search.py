from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import ProjectStatus
from ..models import Project
from ..util import normalise_terms

# Ranking per requirement §10: exact tag > project name > event/collection > metadata > description.
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
    matched: dict[str, str] = field(default_factory=dict)  # term -> where it matched best


def _score_term(project: Project, term: str) -> tuple[int, str]:
    tags = [t.lower() for t in project.tag_names]
    best, where = 0, ""
    if term in tags:
        best, where = W_EXACT_TAG, "tag"
    if term in (project.name or "").lower():
        if W_NAME > best:
            best, where = W_NAME, "name"
    if term in (project.event or "").lower() or term in (project.collection or "").lower():
        if W_EVENT_COLLECTION > best:
            best, where = W_EVENT_COLLECTION, "event/collection"
    metadata_fields = (
        project.ministry,
        project.style,
        project.colours,
        str(project.year or ""),
        project.creator,
        project.asset_types,
    )
    if any(term in (v or "").lower() for v in metadata_fields) or any(term in t for t in tags):
        if W_METADATA > best:
            best, where = W_METADATA, "metadata"
    if term in (project.description or "").lower():
        if W_DESCRIPTION > best:
            best, where = W_DESCRIPTION, "description"
    return best, where


def score_project(project: Project, terms: list[str]) -> SearchHit | None:
    """All terms must match (AND). Returns None when any term is unmatched."""
    total = 0
    matched: dict[str, str] = {}
    for term in terms:
        score, where = _score_term(project, term)
        if score == 0:
            return None
        total += score
        matched[term] = where
    return SearchHit(project=project, score=total, matched=matched)


def search_projects(session: Session, query: str, *, include_drafts: bool = False) -> list[SearchHit]:
    terms = normalise_terms(query)[:MAX_TERMS]
    if not terms:
        return []
    whole = " ".join(terms)
    stmt = select(Project)
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
