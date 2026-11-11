from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Attachment:
    url: str
    title: str
    content: bytes = b""


@dataclass(frozen=True)
class TenderRecord:
    project_id: str
    title: str
    published_at: str
    budget: str | None
    deadline: str | None
    source_url: str
    body: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    collected_at: str = field(default_factory=utc_now)
