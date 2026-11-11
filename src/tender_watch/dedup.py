from __future__ import annotations
import hashlib
from .models import TenderRecord


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def record_hash(record: TenderRecord) -> str:
    canonical = "|".join((record.project_id, record.title, record.published_at, record.body))
    return content_hash(canonical.encode("utf-8"))
