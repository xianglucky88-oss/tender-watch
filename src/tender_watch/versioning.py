from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Trigger reasons.  They are stable machine-readable identifiers used by the
# CLI/timeline so compliance reviewers can explain why a version exists.
INITIAL = "initial"
NO_CHANGE = "no change"
TITLE_CHANGED = "title_changed"
PUBLISHED_AT_CHANGED = "published_at_changed"
BUDGET_CHANGED = "budget_changed"
DEADLINE_CHANGED = "deadline_changed"
BODY_CHANGED = "body_changed"
SOURCE_URL_CHANGED = "source_url_changed"
ATTACHMENT_ADDED = "attachment_added"
ATTACHMENT_REMOVED = "attachment_removed"
ATTACHMENT_TITLE_CHANGED = "attachment_title_changed"
ATTACHMENT_CONTENT_CHANGED = "attachment_content_changed"
ATTACHMENT_BACKFILLED = "attachment_backfilled"

_FIELD_REASONS: tuple[tuple[str, str], ...] = (
    ("title", TITLE_CHANGED),
    ("published_at", PUBLISHED_AT_CHANGED),
    ("budget", BUDGET_CHANGED),
    ("deadline", DEADLINE_CHANGED),
    ("body", BODY_CHANGED),
    ("source_url", SOURCE_URL_CHANGED),
)


@dataclass(frozen=True)
class AttachmentSnapshot:
    url: str
    title: str
    available: bool
    content_hash: str | None
    ordinal: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "available": self.available,
            "content_hash": self.content_hash,
            "ordinal": self.ordinal,
        }


@dataclass
class Evaluation:
    create_version: bool
    change_reasons: list[str] = field(default_factory=list)
    field_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    attachment_changes: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = NO_CHANGE

    def as_details(self) -> dict[str, Any]:
        return {
            "field_changes": self.field_changes,
            "attachment_changes": self.attachment_changes,
            "observations": self.observations,
        }


def _field(value: Any) -> Any:
    return value if value is not None else None


def evaluate_collection(
    *,
    previous_fields: Mapping[str, Any] | None,
    previous_attachments: Sequence[Mapping[str, Any]] | None,
    record_fields: Mapping[str, Any],
    attachments: Sequence[AttachmentSnapshot],
    last_known_hashes: Mapping[str, str],
) -> Evaluation:
    """Compare an observation with the latest evidence version.

    Semantics (also documented in README):

    * notice fields, source address, attachment set, attachment title,
      attachment content change -> a new immutable version;
    * an attachment becoming unavailable is an observation only and must not
      create a version nor erase earlier content;
    * an attachment that was unavailable becoming available is a backfill and
      does create a version, because the reconstructable evidence improved;
    * an identical repeat (including repeats of the same outage) creates a
      collection event but never a version.
    """

    if previous_fields is None:
        return Evaluation(
            create_version=True,
            change_reasons=[INITIAL],
            outcome="initial",
        )

    field_changes: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for name, reason in _FIELD_REASONS:
        old = _field(previous_fields.get(name))
        new = _field(record_fields.get(name))
        if old != new:
            field_changes[name] = {"old": old, "new": new}
            reasons.append(reason)

    previous = {item["url"]: item for item in (previous_attachments or [])}
    current = {item.url: item for item in attachments}

    attachment_changes: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    for item in attachments:
        old = previous.get(item.url)
        if old is None:
            reasons.append(ATTACHMENT_ADDED)
            attachment_changes.append(
                {
                    "url": item.url,
                    "title": item.title,
                    "change": ATTACHMENT_ADDED,
                    "available": item.available,
                }
            )
            observations.append(_observation(item, "fetched" if item.available else "unavailable"))
            continue

        if _field(old.get("title")) != item.title:
            reasons.append(ATTACHMENT_TITLE_CHANGED)
            attachment_changes.append(
                {
                    "url": item.url,
                    "title": item.title,
                    "change": ATTACHMENT_TITLE_CHANGED,
                    "old_title": old.get("title"),
                }
            )

        old_available = bool(old.get("available"))
        old_hash = old.get("content_hash")
        if old_available and item.available:
            if old_hash != item.content_hash:
                reasons.append(ATTACHMENT_CONTENT_CHANGED)
                attachment_changes.append(
                    {
                        "url": item.url,
                        "title": item.title,
                        "change": ATTACHMENT_CONTENT_CHANGED,
                        "old_content_hash": old_hash,
                        "new_content_hash": item.content_hash,
                    }
                )
            observations.append(_observation(item, "fetched"))
        elif not old_available and item.available:
            reasons.append(ATTACHMENT_BACKFILLED)
            known = last_known_hashes.get(item.url)
            if known is not None and known != item.content_hash:
                reasons.append(ATTACHMENT_CONTENT_CHANGED)
            attachment_changes.append(
                {
                    "url": item.url,
                    "title": item.title,
                    "change": ATTACHMENT_BACKFILLED,
                    "old_content_hash": known,
                    "new_content_hash": item.content_hash,
                }
            )
            observations.append(_observation(item, "backfilled"))
        elif old_available and not item.available:
            # A download outage is an observation, not a revision.
            observations.append(_observation(item, "fetch_failed"))
        else:
            observations.append(_observation(item, "still_unavailable"))

    for url, old in previous.items():
        if url not in current:
            reasons.append(ATTACHMENT_REMOVED)
            attachment_changes.append(
                {
                    "url": url,
                    "title": old.get("title"),
                    "change": ATTACHMENT_REMOVED,
                    "available": bool(old.get("available")),
                }
            )

    create_version = bool(reasons)
    reasons = list(dict.fromkeys(reasons))
    if previous_fields is None:
        outcome = "initial"
    elif create_version:
        outcome = "revised"
    elif any(obs["state"] == "fetch_failed" for obs in observations):
        outcome = "attachment_unavailable"
    elif any(obs["state"] == "still_unavailable" for obs in observations):
        outcome = "attachment_still_unavailable"
    else:
        outcome = "unchanged"

    return Evaluation(
        create_version=create_version,
        change_reasons=reasons if create_version else [NO_CHANGE],
        field_changes=field_changes,
        attachment_changes=attachment_changes,
        observations=observations,
        outcome=outcome,
    )


def _observation(item: AttachmentSnapshot, state: str) -> dict[str, Any]:
    return {
        "url": item.url,
        "title": item.title,
        "state": state,
        "available": item.available,
        "content_hash": item.content_hash,
    }
