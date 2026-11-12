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
                content = self.fetcher.fetch(attachment.url)
                attachments.append(
                    replace(attachment, content=content, available=True, fetch_error=None)
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not drop the notice
                # The notice remains ingestible.  Storage records this attempt
                # as unavailable while preserving any previously confirmed
                # evidence.
                attachments.append(
                    replace(attachment, available=False, fetch_error=str(exc))
                )
        record = replace(record, attachments=tuple(attachments))
        return record, self.store.upsert(record)
