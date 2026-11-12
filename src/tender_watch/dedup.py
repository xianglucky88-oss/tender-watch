from __future__ import annotations
import hashlib
import json
from typing import Any, Iterable, Mapping
from .models import TenderRecord


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def record_hash(record: TenderRecord) -> str:
    """Original notice-level fingerprint.

    Kept byte-for-byte compatible with the pre-timeline implementation so
    existing databases and callers keep working.  Budget/deadline/source
    changes are still detected semantically by ``versioning.py`` and a
    complete evidence identity is available from :func:`snapshot_hash`.
    """

    canonical = "|".join((record.project_id, record.title, record.published_at, record.body))
    return content_hash(canonical.encode("utf-8"))


def snapshot_hash(
    project_id: str,
    fields: Mapping[str, Any],
    attachments: Iterable[Mapping[str, Any]],
) -> str:
    """Hash a complete, reconstructable evidence snapshot.

    Attachment ordering is deliberately ignored (a cosmetic re-order in the
    HTML must not manufacture a version), while attachment set membership,
    title, availability and content fingerprint are all part of the identity.
    """

    canonical = {
        "project_id": project_id,
        "fields": dict(fields),
        "attachments": sorted(
            (
                {
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "available": bool(item.get("available")),
                    "content_hash": item.get("content_hash"),
                }
                for item in attachments
            ),
            key=lambda item: (str(item["url"]), str(item["title"])),
        ),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return content_hash(payload.encode("utf-8"))
