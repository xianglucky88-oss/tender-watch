from __future__ import annotations
from dataclasses import replace
from .extract import parse_notice
from .fetching import ContentFetcher
from .models import TenderRecord
from .storage import TenderStore


class TenderPipeline:
    def __init__(self, fetcher: ContentFetcher, store: TenderStore):
        self.fetcher = fetcher
        self.store = store

    def collect_one(self, source_url: str) -> tuple[TenderRecord, bool]:
        record = parse_notice(source_url, self.fetcher.fetch(source_url))
        attachments = []
        for attachment in record.attachments:
            try:
                attachments.append(replace(attachment, content=self.fetcher.fetch(attachment.url)))
            except Exception:
                # The notice remains ingestible, while storage records the attachment as unavailable.
                attachments.append(attachment)
        record = replace(record, attachments=tuple(attachments))
        return record, self.store.upsert(record)
