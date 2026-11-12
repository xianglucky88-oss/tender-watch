from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .dedup import content_hash, record_hash, snapshot_hash
from .models import TenderRecord
from .versioning import AttachmentSnapshot, evaluate_collection

SCHEMA_VERSION = 2


class TenderStore:
    """Persistent current notice, immutable evidence versions and observations.

    Invariants maintained by :meth:`upsert` (all writes happen in one SQLite
    transaction):

    * ``tenders`` always points at the newest version that was successfully
      committed;
    * versions and their attachment evidence are append-only;
    * attachment bytes are stored content-addressed and are never overwritten
      by a later download failure;
    * every collection attempt is recorded, including unchanged repeats and
      failed attachment downloads.
    """

    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # ------------------------------------------------------------------
    # Schema / migration
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            tables = self._table_names()
            legacy = (
                "tender_versions" in tables
                and "version_no" not in self._columns("tender_versions")
            )
            if legacy:
                # An original (pre-timeline) database: rebuild the version
                # table without destroying any stored rows.  The rename and
                # the rebuild stay inside this transaction.
                self.connection.execute(
                    "ALTER TABLE tender_versions RENAME TO _tender_versions_legacy"
                )
            self._create_schema()
            self._add_compatibility_columns()
            if legacy:
                self._import_legacy_versions()
            self._repair_partial_migration()
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _create_schema(self) -> None:
        # NOTE: statement-by-statement instead of executescript, because
        # executescript() implicitly commits an open explicit transaction.
        schema_statements = """
            CREATE TABLE IF NOT EXISTS tenders (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                published_at TEXT,
                budget TEXT,
                deadline TEXT,
                source_url TEXT NOT NULL,
                body TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                version_no INTEGER,
                revision_hash TEXT
            );

            CREATE TABLE IF NOT EXISTS attachments (
                project_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                content_hash TEXT,
                content BLOB,
                fetch_state TEXT NOT NULL DEFAULT 'fetched',
                last_fetch_state TEXT,
                last_fetch_error TEXT,
                last_attempt_at TEXT,
                size INTEGER,
                PRIMARY KEY (project_id, url)
            );

            CREATE TABLE IF NOT EXISTS attachment_blobs (
                content_hash TEXT PRIMARY KEY,
                content BLOB NOT NULL,
                size INTEGER NOT NULL,
                first_collected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tender_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                revision_hash TEXT,
                title TEXT,
                published_at TEXT,
                budget TEXT,
                deadline TEXT,
                source_url TEXT,
                body TEXT,
                collected_at TEXT NOT NULL,
                change_reasons TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                complete INTEGER NOT NULL DEFAULT 1,
                UNIQUE (project_id, version_no)
            );

            CREATE TABLE IF NOT EXISTS version_attachments (
                version_id INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                content_hash TEXT,
                available INTEGER NOT NULL,
                fetch_state TEXT NOT NULL,
                PRIMARY KEY (version_id, url)
            );

            CREATE TABLE IF NOT EXISTS collection_events (
                collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                source_url TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                created_version INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                change_reasons TEXT NOT NULL DEFAULT '[]',
                notice_hash TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS collection_attachments (
                collection_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                observed_state TEXT NOT NULL,
                available INTEGER NOT NULL,
                content_hash TEXT,
                error TEXT,
                PRIMARY KEY (collection_id, url)
            );

            CREATE INDEX IF NOT EXISTS idx_versions_project
                ON tender_versions (project_id, version_no);
            CREATE INDEX IF NOT EXISTS idx_events_project
                ON collection_events (project_id, collection_id);
            CREATE INDEX IF NOT EXISTS idx_version_attachments_project
                ON version_attachments (project_id, version_id);
            """
        for statement in schema_statements.split(";"):
            if statement.strip():
                self.connection.execute(statement)

    def _add_compatibility_columns(self) -> None:
        for table, additions in (
            ("tenders", {"version_no": "INTEGER", "revision_hash": "TEXT"}),
            ("tender_versions", {"revision_hash": "TEXT"}),
            (
                "attachments",
                {
                    "last_fetch_state": "TEXT",
                    "last_fetch_error": "TEXT",
                    "last_attempt_at": "TEXT",
                    "size": "INTEGER",
                },
            ),
        ):
            if table not in self._table_names():
                continue
            columns = self._columns(table)
            for name, declaration in additions.items():
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    def _import_legacy_versions(self) -> None:
        """Best-effort, lossless upgrade of the original schema.

        The original version table only stored title/body/collected_at, so
        historical notice fields that were never captured are left NULL and
        the version is marked ``complete = 0``.  The current notice's fields
        are copied into the matching latest version, and the legacy current
        attachment table is copied into that same latest version.
        """

        tables = self._table_names()
        legacy = self.connection.execute(
            "SELECT project_id, record_hash, title, body, collected_at "
            "FROM _tender_versions_legacy ORDER BY project_id, collected_at, rowid"
        ).fetchall()

        current_rows = {}
        if "tenders" in tables:
            for row in self.connection.execute("SELECT * FROM tenders").fetchall():
                current_rows[row["project_id"]] = row

        per_project: dict[str, int] = {}
        inserted: dict[str, list[dict[str, Any]]] = {}
        for row in legacy:
            project_id = row["project_id"]
            version_no = per_project.get(project_id, 0) + 1
            per_project[project_id] = version_no
            current = current_rows.get(project_id)
            matches_current = (
                current is not None and current["record_hash"] == row["record_hash"]
            )
            if matches_current:
                fields = {
                    "title": current["title"],
                    "published_at": current["published_at"],
                    "budget": current["budget"],
                    "deadline": current["deadline"],
                    "source_url": current["source_url"],
                    "body": current["body"],
                }
                complete = int(
                    matches_current
                    and all(
                        fields[key] is not None
                        for key in ("title", "published_at", "source_url", "body")
                    )
                )
            else:
                fields = {
                    "title": row["title"],
                    "published_at": None,
                    "budget": None,
                    "deadline": None,
                    "source_url": None,
                    "body": row["body"],
                }
                complete = 0

            cursor = self.connection.execute(
                """
                INSERT INTO tender_versions
                    (project_id, version_no, record_hash, revision_hash, title,
                     published_at, budget, deadline, source_url, body,
                     collected_at, change_reasons, details_json, complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    version_no,
                    row["record_hash"],
                    row["record_hash"],
                    fields["title"],
                    fields["published_at"],
                    fields["budget"],
                    fields["deadline"],
                    fields["source_url"],
                    fields["body"],
                    row["collected_at"],
                    json.dumps(["legacy_import"]),
                    json.dumps(
                        {
                            "legacy": True,
                            "notice_fields_complete": bool(complete),
                            "attachment_snapshot_complete": False,
                        }
                    ),
                    complete,
                ),
            )
            inserted.setdefault(project_id, []).append(
                {
                    "version_id": cursor.lastrowid,
                    "version_no": version_no,
                    "record_hash": row["record_hash"],
                    "matches_current": matches_current,
                    "complete": complete,
                }
            )

        # Preserve legacy attachment bytes in the immutable blob store.
        if "attachments" in tables:
            legacy_attachments = self.connection.execute(
                "SELECT * FROM attachments"
            ).fetchall()
            blobs: dict[tuple[str, str], dict[str, Any]] = {}
            for item in legacy_attachments:
                digest = item["content_hash"]
                content = item["content"]
                if content is None:
                    # A hash without bytes cannot be verified; treat the
                    # attachment as unavailable rather than leaking an
                    # unverifiable reference into a version snapshot.
                    digest = None
                elif digest is None:
                    digest = content_hash(bytes(content))
                if digest is not None and content is not None:
                    self.connection.execute(
                        """
                        INSERT INTO attachment_blobs
                            (content_hash, content, size, first_collected_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(content_hash) DO NOTHING
                        """,
                        (digest, bytes(content), len(content), ""),
                    )
                blobs[(item["project_id"], item["url"])] = {
                    "url": item["url"],
                    "title": item["title"],
                    "content_hash": digest,
                    "available": int(digest is not None and content is not None),
                }

            for project_id, versions in inserted.items():
                target = None
                for version in versions:
                    if version["matches_current"]:
                        target = version
                if target is None:
                    target = versions[-1]
                for ordinal, ((pid, _url), item) in enumerate(
                    sorted(
                        (
                            (key, value)
                            for key, value in blobs.items()
                            if key[0] == project_id
                        ),
                        key=lambda pair: pair[0][1],
                    )
                ):
                    self.connection.execute(
                        """
                        INSERT INTO version_attachments
                            (version_id, project_id, ordinal, url, title,
                             content_hash, available, fetch_state)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target["version_id"],
                            project_id,
                            ordinal,
                            item["url"],
                            item["title"],
                            item["content_hash"],
                            item["available"],
                            "fetched" if item["available"] else "unavailable",
                        ),
                    )

        # Point the current notice at its version.
        for project_id, versions in inserted.items():
            matching = next(
                (version for version in versions if version["matches_current"]),
                versions[-1],
            )
            current = current_rows.get(project_id)
            if current is None:
                continue
            if matching["matches_current"]:
                target = matching
            else:
                target = versions[-1]
            self.connection.execute(
                "UPDATE tenders SET version_no = ?, revision_hash = ? WHERE project_id = ?",
                (target["version_no"], target["record_hash"], project_id),
            )

        self.connection.execute("DROP TABLE _tender_versions_legacy")

    def _repair_partial_migration(self) -> None:
        """Make sure every stored current notice has a matching version.

        This covers databases created by an interrupted/older migration as
        well as hand-built databases that only contain the original
        ``tenders`` table.
        """

        tables = self._table_names()
        if "tenders" not in tables or "tender_versions" not in tables:
            return

        orphans = self.connection.execute(
            """
            SELECT t.* FROM tenders t
            WHERE NOT EXISTS (
                SELECT 1 FROM tender_versions v WHERE v.project_id = t.project_id
            )
            """
        ).fetchall()
        for current in orphans:
            cursor = self.connection.execute(
                """
                INSERT INTO tender_versions
                    (project_id, version_no, record_hash, revision_hash, title,
                     published_at, budget, deadline, source_url, body,
                     collected_at, change_reasons, details_json, complete)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    current["project_id"],
                    current["record_hash"],
                    current["record_hash"],
                    current["title"],
                    current["published_at"],
                    current["budget"],
                    current["deadline"],
                    current["source_url"],
                    current["body"],
                    current["collected_at"],
                    json.dumps(["legacy_import"]),
                    json.dumps({"legacy": True, "notice_fields_complete": True}),
                ),
            )
            version_id = cursor.lastrowid
            if "attachments" in tables:
                attachment_rows = self.connection.execute(
                    "SELECT * FROM attachments WHERE project_id = ?",
                    (current["project_id"],),
                ).fetchall()
                for ordinal, item in enumerate(attachment_rows):
                    digest = item["content_hash"]
                    content = item["content"]
                    if content is None:
                        digest = None
                    elif digest is None:
                        digest = content_hash(bytes(content))
                    if digest is not None and content is not None:
                        self.connection.execute(
                            """
                            INSERT INTO attachment_blobs
                                (content_hash, content, size, first_collected_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(content_hash) DO NOTHING
                            """,
                            (digest, bytes(content), len(content), current["collected_at"]),
                        )
                    self.connection.execute(
                        """
                        INSERT INTO version_attachments
                            (version_id, project_id, ordinal, url, title,
                             content_hash, available, fetch_state)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version_id,
                            current["project_id"],
                            ordinal,
                            item["url"],
                            item["title"],
                            digest,
                            int(digest is not None and content is not None),
                            "fetched"
                            if digest is not None and content is not None
                            else "unavailable",
                        ),
                    )
            self.connection.execute(
                "UPDATE tenders SET version_no = 1, revision_hash = ? WHERE project_id = ?",
                (current["record_hash"], current["project_id"]),
            )

        self.connection.execute(
            """
            UPDATE tenders
            SET version_no = (
                SELECT v.version_no FROM tender_versions v
                WHERE v.project_id = tenders.project_id
                ORDER BY v.version_no DESC LIMIT 1
            )
            WHERE version_no IS NULL AND EXISTS (
                SELECT 1 FROM tender_versions v WHERE v.project_id = tenders.project_id
            )
            """
        )

    def _table_names(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    def _columns(self, table: str) -> set[str]:
        return {
            row[1]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def upsert(self, record: TenderRecord) -> bool:
        """Record one collection attempt.

        Returns ``True`` when a new immutable version was appended.  An
        unchanged repeat, a repeated attachment outage, or a failure to
        re-fetch an already known attachment returns ``False`` and leaves the
        previous evidence untouched.
        """

        snapshots = self._attachments(record)
        contents: dict[str, bytes] = {}
        seen_urls: set[str] = set()
        for item in record.attachments:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            if item.is_available:
                contents[item.url] = item.content

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._latest_version(record.project_id)
            previous_fields = None
            previous_attachments: list[dict[str, Any]] = []
            if previous is not None:
                previous_fields = {
                    key: previous[key]
                    for key in (
                        "title",
                        "published_at",
                        "budget",
                        "deadline",
                        "source_url",
                        "body",
                    )
                }
                previous_attachments = [
                    {
                        "url": row["url"],
                        "title": row["title"],
                        "available": bool(row["available"]),
                        "content_hash": row["content_hash"],
                    }
                    for row in self._version_attachments(previous["version_id"])
                ]

            last_known = self._last_known_hashes(record.project_id)
            evaluation = evaluate_collection(
                previous_fields=previous_fields,
                previous_attachments=previous_attachments,
                record_fields={
                    "title": record.title,
                    "published_at": record.published_at,
                    "budget": record.budget,
                    "deadline": record.deadline,
                    "source_url": record.source_url,
                    "body": record.body,
                },
                attachments=snapshots,
                last_known_hashes=last_known,
            )

            notice_hash = record_hash(record)
            created_version = False
            version_no = previous["version_no"] if previous is not None else 0

            if evaluation.create_version:
                version_no = version_no + 1
                revision_hash = snapshot_hash(
                    record.project_id,
                    {
                        "title": record.title,
                        "published_at": record.published_at,
                        "budget": record.budget,
                        "deadline": record.deadline,
                        "source_url": record.source_url,
                        "body": record.body,
                    },
                    [item.as_dict() for item in snapshots],
                )
                self._store_blobs(snapshots, contents, record.collected_at)
                cursor = self.connection.execute(
                    """
                    INSERT INTO tender_versions
                        (project_id, version_no, record_hash, revision_hash,
                         title, published_at, budget, deadline, source_url,
                         body, collected_at, change_reasons, details_json,
                         complete)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        record.project_id,
                        version_no,
                        notice_hash,
                        revision_hash,
                        record.title,
                        record.published_at,
                        record.budget,
                        record.deadline,
                        record.source_url,
                        record.body,
                        record.collected_at,
                        json.dumps(evaluation.change_reasons, ensure_ascii=False),
                        json.dumps(evaluation.as_details(), ensure_ascii=False),
                    ),
                )
                version_id = cursor.lastrowid
                self._store_version_attachments(version_id, record.project_id, snapshots)
                self._update_current(record, notice_hash, revision_hash, version_no)
                created_version = True
            elif previous is not None:
                # Keep the current notice's "last observed" timestamp in step
                # with the original single-collection behaviour.  No version,
                # fields or evidence are touched.
                self._touch_current(record, notice_hash)

            self._update_legacy_attachment_cache(
                record, snapshots, contents
            )
            self._record_collection(
                record,
                notice_hash,
                version_no,
                created_version,
                evaluation,
                snapshots,
                contents,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return created_version

    def _attachments(self, record: TenderRecord) -> list[AttachmentSnapshot]:
        result: list[AttachmentSnapshot] = []
        seen: set[str] = set()
        for ordinal, attachment in enumerate(record.attachments):
            if attachment.url in seen:
                continue
            seen.add(attachment.url)
            available = attachment.is_available
            result.append(
                AttachmentSnapshot(
                    url=attachment.url,
                    title=attachment.title,
                    available=available,
                    content_hash=(
                        content_hash(attachment.content) if available else None
                    ),
                    ordinal=ordinal,
                )
            )
        return result

    def _store_blobs(
        self,
        snapshots: Iterable[AttachmentSnapshot],
        contents: dict[str, bytes],
        collected_at: str,
    ) -> None:
        for item in snapshots:
            if not item.available or item.content_hash is None:
                continue
            content = contents.get(item.url, b"")
            self.connection.execute(
                """
                INSERT INTO attachment_blobs
                    (content_hash, content, size, first_collected_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(content_hash) DO NOTHING
                """,
                (item.content_hash, content, len(content), collected_at),
            )

    def _store_version_attachments(
        self, version_id: int, project_id: str, snapshots: Iterable[AttachmentSnapshot]
    ) -> None:
        for item in snapshots:
            self.connection.execute(
                """
                INSERT INTO version_attachments
                    (version_id, project_id, ordinal, url, title, content_hash,
                     available, fetch_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    item.ordinal,
                    item.url,
                    item.title,
                    item.content_hash,
                    int(item.available),
                    "fetched" if item.available else "unavailable",
                ),
            )

    def _update_current(
        self,
        record: TenderRecord,
        notice_hash: str,
        revision_hash: str,
        version_no: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO tenders
                (project_id, title, published_at, budget, deadline, source_url,
                 body, record_hash, collected_at, version_no, revision_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                title = excluded.title,
                published_at = excluded.published_at,
                budget = excluded.budget,
                deadline = excluded.deadline,
                source_url = excluded.source_url,
                body = excluded.body,
                record_hash = excluded.record_hash,
                collected_at = excluded.collected_at,
                version_no = excluded.version_no,
                revision_hash = excluded.revision_hash
            """,
            (
                record.project_id,
                record.title,
                record.published_at,
                record.budget,
                record.deadline,
                record.source_url,
                record.body,
                notice_hash,
                record.collected_at,
                version_no,
                revision_hash,
            ),
        )

    def _touch_current(self, record: TenderRecord, notice_hash: str) -> None:
        self.connection.execute(
            """
            UPDATE tenders
            SET collected_at = ?, record_hash = ?
            WHERE project_id = ?
            """,
            (record.collected_at, notice_hash, record.project_id),
        )

    def _update_legacy_attachment_cache(
        self,
        record: TenderRecord,
        snapshots: list[AttachmentSnapshot],
        contents: dict[str, bytes],
    ) -> None:
        """Maintain the compatibility ``attachments`` table non-destructively.

        ``content``/``content_hash``/``fetch_state`` describe the last known
        evidence and are never cleared by a failed download; the ``last_*``
        columns describe the latest attempt.
        """

        for item in snapshots:
            content = contents.get(item.url)
            if item.available and content is not None:
                self.connection.execute(
                    """
                    INSERT INTO attachments
                        (project_id, url, title, content_hash, content,
                         fetch_state, last_fetch_state, last_fetch_error,
                         last_attempt_at, size)
                    VALUES (?, ?, ?, ?, ?, 'fetched', 'fetched', NULL, ?, ?)
                    ON CONFLICT(project_id, url) DO UPDATE SET
                        title = excluded.title,
                        content_hash = excluded.content_hash,
                        content = excluded.content,
                        fetch_state = 'fetched',
                        last_fetch_state = 'fetched',
                        last_fetch_error = NULL,
                        last_attempt_at = excluded.last_attempt_at,
                        size = excluded.size
                    """,
                    (
                        record.project_id,
                        item.url,
                        item.title,
                        item.content_hash,
                        content,
                        record.collected_at,
                        len(content),
                    ),
                )
            else:
                error = None
                for attachment in record.attachments:
                    if attachment.url == item.url:
                        error = attachment.fetch_error
                        break
                existing = self.connection.execute(
                    "SELECT content FROM attachments WHERE project_id = ? AND url = ?",
                    (record.project_id, item.url),
                ).fetchone()
                if existing is not None and existing["content"] is not None:
                    # Preserve confirmed historical bytes; only record the
                    # failed attempt.
                    self.connection.execute(
                        """
                        UPDATE attachments
                        SET title = ?, last_fetch_state = 'unavailable',
                            last_fetch_error = ?, last_attempt_at = ?
                        WHERE project_id = ? AND url = ?
                        """,
                        (
                            item.title,
                            error,
                            record.collected_at,
                            record.project_id,
                            item.url,
                        ),
                    )
                else:
                    self.connection.execute(
                        """
                        INSERT INTO attachments
                            (project_id, url, title, content_hash, content,
                             fetch_state, last_fetch_state, last_fetch_error,
                             last_attempt_at, size)
                        VALUES (?, ?, ?, NULL, NULL, 'unavailable',
                                'unavailable', ?, ?, 0)
                        ON CONFLICT(project_id, url) DO UPDATE SET
                            title = excluded.title,
                            last_fetch_state = 'unavailable',
                            last_fetch_error = excluded.last_fetch_error,
                            last_attempt_at = excluded.last_attempt_at
                        """,
                        (
                            record.project_id,
                            item.url,
                            item.title,
                            error,
                            record.collected_at,
                        ),
                    )

    def _record_collection(
        self,
        record: TenderRecord,
        notice_hash: str,
        version_no: int,
        created_version: bool,
        evaluation: Any,
        snapshots: list[AttachmentSnapshot],
        contents: dict[str, bytes],
    ) -> None:
        cursor = self.connection.execute(
            """
            INSERT INTO collection_events
                (project_id, collected_at, source_url, version_no,
                 created_version, outcome, change_reasons, notice_hash,
                 details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.project_id,
                record.collected_at,
                record.source_url,
                version_no,
                int(created_version),
                evaluation.outcome,
                json.dumps(evaluation.change_reasons, ensure_ascii=False),
                notice_hash,
                json.dumps(evaluation.as_details(), ensure_ascii=False),
            ),
        )
        collection_id = cursor.lastrowid

        observed: dict[str, str] = {
            obs["url"]: obs["state"] for obs in evaluation.observations
        }
        errors: dict[str, str | None] = {}
        for attachment in record.attachments:
            errors.setdefault(attachment.url, attachment.fetch_error)

        for item in snapshots:
            state = observed.get(item.url)
            if state is None:
                state = "fetched" if item.available else "unavailable"
            self.connection.execute(
                """
                INSERT INTO collection_attachments
                    (collection_id, ordinal, url, title, observed_state,
                     available, content_hash, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collection_id,
                    item.ordinal,
                    item.url,
                    item.title,
                    state,
                    int(item.available),
                    item.content_hash,
                    errors.get(item.url),
                ),
            )

    def _latest_version(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM tender_versions
            WHERE project_id = ?
            ORDER BY version_no DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()

    def _version_attachments(self, version_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM version_attachments
            WHERE version_id = ?
            ORDER BY ordinal
            """,
            (version_id,),
        ).fetchall()

    def _last_known_hashes(self, project_id: str) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for row in self.connection.execute(
            """
            SELECT url, content_hash FROM version_attachments
            WHERE project_id = ? AND available = 1 AND content_hash IS NOT NULL
            ORDER BY version_id ASC, ordinal ASC
            """,
            (project_id,),
        ).fetchall():
            hashes[row["url"]] = row["content_hash"]
        return hashes

    # ------------------------------------------------------------------
    # Read path / observability
    # ------------------------------------------------------------------
    def get_timeline(self, project_id: str) -> dict[str, Any] | None:
        """Return a complete, explainable evidence timeline for one project."""

        current = self.connection.execute(
            "SELECT * FROM tenders WHERE project_id = ?", (project_id,)
        ).fetchone()
        versions = self.connection.execute(
            """
            SELECT * FROM tender_versions
            WHERE project_id = ?
            ORDER BY version_no ASC
            """,
            (project_id,),
        ).fetchall()
        events = self.connection.execute(
            """
            SELECT * FROM collection_events
            WHERE project_id = ?
            ORDER BY collection_id ASC
            """,
            (project_id,),
        ).fetchall()
        if current is None and not versions and not events:
            return None

        timeline_versions = []
        for version in versions:
            attachments = self.connection.execute(
                """
                SELECT va.*, b.size AS content_size
                FROM version_attachments va
                LEFT JOIN attachment_blobs b
                    ON b.content_hash = va.content_hash
                WHERE va.version_id = ?
                ORDER BY va.ordinal
                """,
                (version["version_id"],),
            ).fetchall()
            timeline_versions.append(
                {
                    "version_no": version["version_no"],
                    "record_hash": version["record_hash"],
                    "revision_hash": version["revision_hash"],
                    "collected_at": version["collected_at"],
                    "complete": bool(version["complete"]),
                    "change_reasons": _load_json(version["change_reasons"]),
                    "details": _load_json(version["details_json"]),
                    "notice": {
                        "title": version["title"],
                        "published_at": version["published_at"],
                        "budget": version["budget"],
                        "deadline": version["deadline"],
                        "source_url": version["source_url"],
                        "body": version["body"],
                    },
                    "attachments": [
                        {
                            "url": item["url"],
                            "title": item["title"],
                            "available": bool(item["available"]),
                            "fetch_state": item["fetch_state"],
                            "content_hash": item["content_hash"],
                            "content_size": item["content_size"],
                        }
                        for item in attachments
                    ],
                }
            )

        timeline_events = []
        for event in events:
            observed = self.connection.execute(
                """
                SELECT * FROM collection_attachments
                WHERE collection_id = ?
                ORDER BY ordinal
                """,
                (event["collection_id"],),
            ).fetchall()
            timeline_events.append(
                {
                    "collection_id": event["collection_id"],
                    "collected_at": event["collected_at"],
                    "source_url": event["source_url"],
                    "version_no": event["version_no"],
                    "created_version": bool(event["created_version"]),
                    "outcome": event["outcome"],
                    "change_reasons": _load_json(event["change_reasons"]),
                    "notice_hash": event["notice_hash"],
                    "details": _load_json(event["details_json"]),
                    "attachments": [
                        {
                            "url": item["url"],
                            "title": item["title"],
                            "observed_state": item["observed_state"],
                            "available": bool(item["available"]),
                            "content_hash": item["content_hash"],
                            "error": item["error"],
                        }
                        for item in observed
                    ],
                }
            )

        current_dict = None
        if current is not None:
            current_dict = {
                "version_no": current["version_no"],
                "revision_hash": current["revision_hash"],
                "notice_hash": current["record_hash"],
                "collected_at": current["collected_at"],
                "notice": {
                    "title": current["title"],
                    "published_at": current["published_at"],
                    "budget": current["budget"],
                    "deadline": current["deadline"],
                    "source_url": current["source_url"],
                    "body": current["body"],
                },
            }

        issues = self.verify_consistency(project_id)
        return {
            "project_id": project_id,
            "current": current_dict,
            "versions": timeline_versions,
            "collections": timeline_events,
            "integrity": {"ok": not issues, "issues": issues},
        }

    def get_version_snapshot(
        self, project_id: str, version_no: int
    ) -> dict[str, Any] | None:
        timeline = self.get_timeline(project_id)
        if timeline is None:
            return None
        for version in timeline["versions"]:
            if version["version_no"] == version_no:
                return version
        return None

    def get_attachment_content(self, content_hash_value: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT content FROM attachment_blobs WHERE content_hash = ?",
            (content_hash_value,),
        ).fetchone()
        return bytes(row["content"]) if row is not None else None

    def verify_consistency(self, project_id: str) -> list[str]:
        """Return human-readable invariants broken for one project."""

        issues: list[str] = []
        versions = self.connection.execute(
            "SELECT * FROM tender_versions WHERE project_id = ? ORDER BY version_no",
            (project_id,),
        ).fetchall()
        current = self.connection.execute(
            "SELECT * FROM tenders WHERE project_id = ?", (project_id,)
        ).fetchone()

        expected = 1
        for version in versions:
            if version["version_no"] != expected:
                issues.append(
                    f"version sequence has gap/duplicate: expected {expected}, "
                    f"got {version['version_no']}"
                )
            expected += 1
            for item in self._version_attachments(version["version_id"]):
                if item["available"]:
                    if not item["content_hash"]:
                        issues.append(
                            f"version {version['version_no']} attachment {item['url']} "
                            "is available but has no content hash"
                        )
                        continue
                    blob = self.connection.execute(
                        "SELECT content FROM attachment_blobs WHERE content_hash = ?",
                        (item["content_hash"],),
                    ).fetchone()
                    if blob is None:
                        issues.append(
                            f"version {version['version_no']} attachment {item['url']} "
                            "references a missing blob"
                        )
                    elif content_hash(bytes(blob["content"])) != item["content_hash"]:
                        issues.append(
                            f"blob {item['content_hash']} content does not match its hash"
                        )
                elif item["content_hash"] is not None:
                    issues.append(
                        f"version {version['version_no']} attachment {item['url']} "
                        "is unavailable but carries a content hash"
                    )

        if versions:
            latest = versions[-1]
            if current is None:
                issues.append("versions exist but current notice is missing")
            elif current["version_no"] != latest["version_no"]:
                issues.append(
                    f"current notice points at version {current['version_no']} "
                    f"but latest version is {latest['version_no']}"
                )
            elif latest["complete"]:
                for key in ("title", "published_at", "budget", "deadline", "source_url", "body"):
                    if current[key] != latest[key]:
                        issues.append(f"current notice field {key} disagrees with latest version")
                if (
                    latest["revision_hash"] is not None
                    and current["revision_hash"] != latest["revision_hash"]
                ):
                    issues.append(
                        "current notice revision hash disagrees with latest version"
                    )
        elif current is not None:
            issues.append("current notice exists without any version")

        for event in self.connection.execute(
            "SELECT * FROM collection_events WHERE project_id = ?",
            (project_id,),
        ).fetchall():
            if event["version_no"] is not None:
                exists = self.connection.execute(
                    "SELECT 1 FROM tender_versions WHERE project_id = ? AND version_no = ?",
                    (project_id, event["version_no"]),
                ).fetchone()
                if exists is None:
                    issues.append(
                        f"collection {event['collection_id']} references missing "
                        f"version {event['version_no']}"
                    )
        return issues

    def close(self) -> None:
        self.connection.close()


def _load_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
