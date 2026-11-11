from __future__ import annotations
import sqlite3
from pathlib import Path
from .dedup import record_hash, content_hash
from .models import TenderRecord


class TenderStore:
    """保存当前公告、不可变版本、附件证据及可重试的附件状态。"""

    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS tenders (
            project_id TEXT PRIMARY KEY, title TEXT NOT NULL, published_at TEXT,
            budget TEXT, deadline TEXT, source_url TEXT NOT NULL, body TEXT NOT NULL,
            record_hash TEXT NOT NULL, collected_at TEXT NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS tender_versions (
            project_id TEXT NOT NULL, record_hash TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, collected_at TEXT NOT NULL,
            PRIMARY KEY (project_id, record_hash)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS attachments (
            project_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
            content_hash TEXT, content BLOB, fetch_state TEXT NOT NULL DEFAULT 'fetched',
            PRIMARY KEY (project_id, url)
        )""")
        self.connection.commit()

    def upsert(self, record: TenderRecord) -> bool:
        digest = record_hash(record)
        old = self.connection.execute("SELECT record_hash FROM tenders WHERE project_id = ?", (record.project_id,)).fetchone()
        changed = old is None or old[0] != digest
        self.connection.execute("""INSERT INTO tender_versions(project_id, record_hash, title, body, collected_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, record_hash) DO NOTHING""",
            (record.project_id, digest, record.title, record.body, record.collected_at))
        self.connection.execute("""INSERT INTO tenders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET title=excluded.title,
            published_at=excluded.published_at, budget=excluded.budget,
            deadline=excluded.deadline, source_url=excluded.source_url,
            body=excluded.body, record_hash=excluded.record_hash,
            collected_at=excluded.collected_at""",
            (record.project_id, record.title, record.published_at, record.budget, record.deadline,
             record.source_url, record.body, digest, record.collected_at))
        for attachment in record.attachments:
            fetched = bool(attachment.content)
            self.connection.execute("""INSERT INTO attachments
                (project_id, url, title, content_hash, content, fetch_state)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, url) DO UPDATE SET title=excluded.title,
                content_hash=excluded.content_hash, content=excluded.content,
                fetch_state=excluded.fetch_state""",
                (record.project_id, attachment.url, attachment.title,
                 content_hash(attachment.content) if fetched else None,
                 attachment.content if fetched else None,
                 'fetched' if fetched else 'unavailable'))
        self.connection.commit()
        return changed

    def close(self) -> None:
        self.connection.close()
