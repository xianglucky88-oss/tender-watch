from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Attachment:
    """One attachment link observed in a notice.

    ``available`` is tri-state on purpose.  A caller that knows whether the
    download succeeded should set it explicitly (an empty, successfully
    fetched file is still available).  Callers that only build a record with
    content fall back to the historical ``bool(content)`` interpretation.
    """

    url: str
    title: str
    content: bytes = b""
    available: bool | None = None
    fetch_error: str | None = None

    @property
    def is_available(self) -> bool:
        if self.available is not None:
            return self.available
        return bool(self.content)

    @property
    def content_hash(self) -> str | None:
        if not self.is_available:
            return None
        import hashlib

        return hashlib.sha256(self.content).hexdigest()


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
