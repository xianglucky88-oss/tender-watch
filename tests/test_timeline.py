from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tender_watch.dedup import content_hash
from tender_watch.fetching import ContentFetcher, FixtureFetcher
from tender_watch.models import Attachment, TenderRecord
from tender_watch.pipeline import TenderPipeline
from tender_watch.storage import TenderStore


NOTICE_URL = "https://example.test/notice"


class ScriptedFetcher(ContentFetcher):
    """Fetch fixture content, or raise scheduled errors for fault injection."""

    def __init__(self, pages: dict[str, bytes | Exception]):
        self.pages = dict(pages)
        self.calls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


def notice_html(
    *,
    project_id: str = "P1",
    title: str = "设备采购公告",
    body: str = "预算金额：100 元。截止时间：2026-12-01 17:00。",
    attachments: tuple[str, ...] = ("a.pdf",),
    published_at: str = "2026-01-01",
) -> bytes:
    links = "".join(f'<a href="{name}">{name}</a>' for name in attachments)
    return (
        f'<meta name="project-id" content="{project_id}">'
        f'<meta name="title" content="{title}">'
        f'<meta name="published-at" content="{published_at}">'
        f"<body>{body}{links}</body>"
    ).encode()


def make_record(
    *,
    project_id: str = "P1",
    title: str = "设备采购公告",
    body: str = "预算金额：100 元。截止时间：2026-12-01 17:00。",
    budget: str | None = "100 元",
    deadline: str | None = "2026-12-01 17:00",
    source_url: str = NOTICE_URL,
    attachments: tuple[Attachment, ...] = (),
    published_at: str = "2026-01-01",
    collected_at: str = "2026-11-12T00:00:00+00:00",
) -> TenderRecord:
    return TenderRecord(
        project_id=project_id,
        title=title,
        published_at=published_at,
        budget=budget,
        deadline=deadline,
        source_url=source_url,
        body=body,
        attachments=attachments,
        collected_at=collected_at,
    )


def _att(name: str, content: bytes | None, *, available: bool | None = None) -> Attachment:
    if content is None:
        return Attachment(name, name, b"", available=False)
    return Attachment(name, name, content, available=True if available is None else available)


def _timeline(store: TenderStore, project_id: str = "P1") -> dict:
    timeline = store.get_timeline(project_id)
    assert timeline is not None
    assert timeline["integrity"]["ok"], timeline["integrity"]["issues"]
    return timeline


def test_repeat_collections_do_not_create_fake_versions(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        fetcher = ScriptedFetcher(
            {
                NOTICE_URL: notice_html(),
                "a.pdf": b"attachment-one",
            }
        )
        pipeline = TenderPipeline(fetcher, store)
        first, changed_first = pipeline.collect_one(NOTICE_URL)
        _, changed_second = pipeline.collect_one(NOTICE_URL)
        third, changed_third = pipeline.collect_one(NOTICE_URL)

        assert first.project_id == "P1"
        assert (changed_first, changed_second, changed_third) == (True, False, False)
        timeline = _timeline(store)
        assert len(timeline["versions"]) == 1
        assert [item["outcome"] for item in timeline["collections"]] == [
            "initial",
            "unchanged",
            "unchanged",
        ]
        assert len(third.attachments) == 1
        assert timeline["versions"][0]["notice"]["body"].startswith("预算金额：100")
    finally:
        store.close()


def test_only_key_field_change_is_an_explainable_revision(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        store.upsert(make_record(budget="100 元", collected_at="2026-01-01T00:00:00+00:00"))
        assert store.upsert(
            make_record(budget="200 元", collected_at="2026-02-01T00:00:00+00:00")
        ) is True

        timeline = _timeline(store)
        assert len(timeline["versions"]) == 2
        revision = timeline["versions"][1]
        assert revision["change_reasons"] == ["budget_changed"]
        assert revision["notice"]["budget"] == "200 元"
        assert revision["revision_hash"] != timeline["versions"][0]["revision_hash"]
        assert revision["details"]["field_changes"]["budget"] == {
            "old": "100 元",
            "new": "200 元",
        }
        # Same key-field submission again is not a version.
        assert store.upsert(
            make_record(budget="200 元", collected_at="2026-03-01T00:00:00+00:00")
        ) is False
        assert len(_timeline(store)["versions"]) == 2
    finally:
        store.close()


def test_initial_unavailable_attachment_is_backfilled_later(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        failing = ScriptedFetcher(
            {
                NOTICE_URL: notice_html(),
                "a.pdf": RuntimeError("attachment temporarily unavailable"),
            }
        )
        _, first_changed = TenderPipeline(failing, store).collect_one(NOTICE_URL)
        assert first_changed is True

        timeline = _timeline(store)
        version = timeline["versions"][0]
        assert version["attachments"][0]["available"] is False
        assert version["attachments"][0]["content_hash"] is None
        assert timeline["collections"][0]["attachments"][0]["observed_state"] == "unavailable"

        recovery = ScriptedFetcher(
            {NOTICE_URL: notice_html(), "a.pdf": b"recovered-bytes"}
        )
        _, backfill_changed = TenderPipeline(recovery, store).collect_one(NOTICE_URL)
        assert backfill_changed is True

        timeline = _timeline(store)
        assert len(timeline["versions"]) == 2
        assert timeline["versions"][1]["change_reasons"] == ["attachment_backfilled"]
        assert timeline["versions"][1]["attachments"][0]["available"] is True
        assert (
            store.get_attachment_content(
                content_hash(b"recovered-bytes")
            )
            == b"recovered-bytes"
        )

        # A further identical success is just a duplicate observation.
        _, again = TenderPipeline(
            ScriptedFetcher({NOTICE_URL: notice_html(), "a.pdf": b"recovered-bytes"}),
            store,
        ).collect_one(NOTICE_URL)
        assert again is False
        assert len(_timeline(store)["versions"]) == 2
    finally:
        store.close()


def test_temporary_failure_does_not_overwrite_confirmed_evidence(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        TenderPipeline(
            ScriptedFetcher({NOTICE_URL: notice_html(), "a.pdf": b"confirmed-bytes"}),
            store,
        ).collect_one(NOTICE_URL)
        timeline = _timeline(store)
        assert timeline["versions"][0]["attachments"][0]["available"] is True

        failing = ScriptedFetcher(
            {NOTICE_URL: notice_html(), "a.pdf": RuntimeError("outage")}
        )
        _, failed_changed = TenderPipeline(failing, store).collect_one(NOTICE_URL)
        assert failed_changed is False

        timeline = _timeline(store)
        assert len(timeline["versions"]) == 1
        assert timeline["collections"][-1]["outcome"] == "attachment_unavailable"
        assert timeline["collections"][-1]["attachments"][0]["observed_state"] == "fetch_failed"
        # The confirmed historical evidence is untouched.
        assert timeline["versions"][0]["attachments"][0]["available"] is True
        assert (
            store.get_attachment_content(content_hash(b"confirmed-bytes"))
            == b"confirmed-bytes"
        )
        # A later re-fetch of the same bytes returns to the already-confirmed
        # evidence and therefore does not manufacture a version.
        _, recovered = TenderPipeline(
            ScriptedFetcher({NOTICE_URL: notice_html(), "a.pdf": b"confirmed-bytes"}),
            store,
        ).collect_one(NOTICE_URL)
        assert recovered is False
        assert len(_timeline(store)["versions"]) == 1
    finally:
        store.close()


def test_attachment_add_replace_and_remove(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        pages = {
            NOTICE_URL: notice_html(attachments=("a.pdf",)),
            "a.pdf": b"a-v1",
            "b.pdf": b"b-v1",
        }
        fetcher = ScriptedFetcher(pages)
        pipeline = TenderPipeline(fetcher, store)
        _, initial = pipeline.collect_one(NOTICE_URL)
        assert initial is True

        fetcher.pages[NOTICE_URL] = notice_html(attachments=("a.pdf", "b.pdf"))
        _, added = pipeline.collect_one(NOTICE_URL)
        assert added is True

        fetcher.pages["a.pdf"] = b"a-v2"
        _, replaced = pipeline.collect_one(NOTICE_URL)
        assert replaced is True

        fetcher.pages[NOTICE_URL] = notice_html(attachments=("a.pdf",))
        _, removed = pipeline.collect_one(NOTICE_URL)
        assert removed is True

        timeline = _timeline(store)
        assert len(timeline["versions"]) == 4
        assert "attachment_added" in timeline["versions"][1]["change_reasons"]
        assert timeline["versions"][2]["change_reasons"] == [
            "attachment_content_changed"
        ]
        assert "attachment_removed" in timeline["versions"][3]["change_reasons"]
        assert [item["url"] for item in timeline["versions"][1]["attachments"]] == [
            "a.pdf",
            "b.pdf",
        ]
        assert [item["url"] for item in timeline["versions"][3]["attachments"]] == [
            "a.pdf"
        ]
        assert (
            store.get_attachment_content(content_hash(b"a-v1")) == b"a-v1"
        ), "replaced content must remain retrievable as historical evidence"
        assert (
            store.get_attachment_content(content_hash(b"a-v2")) == b"a-v2"
        )
    finally:
        store.close()


def test_source_address_change_is_a_provenance_revision(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        attachment = _att("a.pdf", b"same")
        store.upsert(
            make_record(
                source_url="https://example.test/old",
                attachments=(attachment,),
                collected_at="2026-01-01T00:00:00+00:00",
            )
        )
        assert store.upsert(
            make_record(
                source_url="https://example.test/new",
                attachments=(attachment,),
                collected_at="2026-02-01T00:00:00+00:00",
            )
        ) is True
        timeline = _timeline(store)
        assert timeline["versions"][1]["change_reasons"] == ["source_url_changed"]
        assert timeline["versions"][1]["notice"]["source_url"] == "https://example.test/new"
        assert timeline["versions"][1]["revision_hash"] != timeline["versions"][0]["revision_hash"]
        # Repeating the new address adds no version.
        assert store.upsert(
            make_record(
                source_url="https://example.test/new",
                attachments=(attachment,),
                collected_at="2026-03-01T00:00:00+00:00",
            )
        ) is False
    finally:
        store.close()


def test_partial_attachment_failure_during_notice_revision(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        first_attachment = _att("a.pdf", b"a-confirmed")
        second_attachment = _att("b.pdf", b"b-confirmed")
        store.upsert(
            make_record(
                attachments=(first_attachment, second_attachment),
                collected_at="2026-01-01T00:00:00+00:00",
            )
        )

        # Revision adds new body, a.pdf cannot be fetched, b.pdf changes.
        store.upsert(
            make_record(
                body="预算金额：100 元。截止时间：2026-12-02 17:00。",
                deadline="2026-12-02 17:00",
                attachments=(
                    _att("a.pdf", None),
                    _att("b.pdf", b"b-revised"),
                ),
                collected_at="2026-02-01T00:00:00+00:00",
            )
        )

        timeline = _timeline(store)
        assert len(timeline["versions"]) == 2
        revision = timeline["versions"][1]
        assert "deadline_changed" in revision["change_reasons"]
        assert "body_changed" in revision["change_reasons"]
        assert "attachment_content_changed" in revision["change_reasons"]
        attachments = {item["url"]: item for item in revision["attachments"]}
        assert attachments["a.pdf"]["available"] is False
        assert attachments["b.pdf"]["available"] is True
        # The old a.pdf evidence remains in version 1 and in the blob store.
        assert timeline["versions"][0]["attachments"][0]["available"] is True
        assert (
            store.get_attachment_content(content_hash(b"a-confirmed"))
            == b"a-confirmed"
        )
    finally:
        store.close()


def test_injected_write_interruption_rolls_back_and_retry_converges(tmp_path):
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        store.upsert(
            make_record(
                attachments=(_att("a.pdf", b"a-v1"),),
                collected_at="2026-01-01T00:00:00+00:00",
            )
        )
        baseline = _timeline(store)
        assert len(baseline["versions"]) == 1

        # Fail at the very end of the write path: every earlier insert
        # (version, blob, attachments, current notice) must roll back.
        store.connection.execute(
            """
            CREATE TRIGGER inject_failure BEFORE INSERT ON collection_events
            BEGIN
                SELECT RAISE(ABORT, 'simulated write interruption');
            END
            """
        )
        try:
            try:
                store.upsert(
                    make_record(
                        body="预算金额：300 元。截止时间：2026-12-03 17:00。",
                        budget="300 元",
                        attachments=(_att("a.pdf", b"a-v2"),),
                        collected_at="2026-02-01T00:00:00+00:00",
                    )
                )
            except sqlite3.Error:
                pass
            else:  # pragma: no cover - trigger must always fire
                raise AssertionError("fault injection did not abort the write")

            after_failure = _timeline(store)
            assert len(after_failure["versions"]) == 1
            assert after_failure["current"]["notice"]["budget"] == "100 元"
            assert len(after_failure["collections"]) == 1
        finally:
            store.connection.execute("DROP TRIGGER IF EXISTS inject_failure")

        # Retrying the same collection now commits exactly one new version.
        assert store.upsert(
            make_record(
                body="预算金额：300 元。截止时间：2026-12-03 17:00。",
                budget="300 元",
                attachments=(_att("a.pdf", b"a-v2"),),
                collected_at="2026-02-02T00:00:00+00:00",
            )
        ) is True
        after_retry = _timeline(store)
        assert len(after_retry["versions"]) == 2
        assert after_retry["current"]["notice"]["budget"] == "300 元"
        assert len(after_retry["collections"]) == 2

        # Restart and inspect the same durable state.
        db = tmp_path / "tenders.sqlite3"
        store.close()
        store = TenderStore(db)
        restarted = _timeline(store)
        assert restarted["current"] == after_retry["current"]
        assert len(restarted["versions"]) == 2
        assert len(restarted["collections"]) == 2
    finally:
        store.close()


def test_legacy_database_upgrade_and_recollection(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE tenders (project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        published_at TEXT, budget TEXT, deadline TEXT, source_url TEXT NOT NULL,
        body TEXT NOT NULL, record_hash TEXT NOT NULL, collected_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE tender_versions (project_id TEXT NOT NULL,
        record_hash TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
        collected_at TEXT NOT NULL, PRIMARY KEY (project_id, record_hash))"""
    )
    connection.execute(
        """CREATE TABLE attachments (project_id TEXT NOT NULL, url TEXT NOT NULL,
        title TEXT NOT NULL, content_hash TEXT, content BLOB,
        fetch_state TEXT NOT NULL DEFAULT 'fetched', PRIMARY KEY (project_id, url))"""
    )

    def legacy_hash(title: str, published_at: str, body: str) -> str:
        return hashlib.sha256(
            "|".join(("P1", title, published_at, body)).encode()
        ).hexdigest()

    old_hash = legacy_hash("旧公告", "2025-01-01", "旧正文")
    current_hash = legacy_hash("新公告", "2025-02-01", "新正文")
    blob = b"legacy-attachment"
    connection.execute(
        "INSERT INTO tender_versions VALUES ('P1', ?, '旧公告', '旧正文', ?)",
        (old_hash, "2025-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO tender_versions VALUES ('P1', ?, '新公告', '新正文', ?)",
        (current_hash, "2025-02-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO tenders VALUES ('P1', '新公告', '2025-02-01', NULL, NULL, "
        "'legacy://source', '新正文', ?, '2025-02-01T00:00:00+00:00')",
        (current_hash,),
    )
    connection.execute(
        "INSERT INTO attachments VALUES ('P1', 'a.pdf', 'a.pdf', ?, ?, 'fetched')",
        (content_hash(blob), blob),
    )
    connection.commit()
    connection.close()

    store = TenderStore(db)
    try:
        timeline = _timeline(store)
        assert len(timeline["versions"]) == 2
        assert timeline["versions"][0]["complete"] is False
        assert timeline["versions"][0]["change_reasons"] == ["legacy_import"]
        assert timeline["versions"][1]["complete"] is True
        assert timeline["versions"][1]["notice"]["budget"] is None
        assert timeline["versions"][1]["attachments"][0]["available"] is True
        assert store.get_attachment_content(content_hash(blob)) == blob

        # Re-collecting the unchanged current notice after the upgrade is
        # idempotent even though the new fingerprint algorithm differs.
        assert store.upsert(
            make_record(
                title="新公告",
                body="新正文",
                budget=None,
                deadline=None,
                source_url="legacy://source",
                published_at="2025-02-01",
                attachments=(_att("a.pdf", blob),),
                collected_at="2025-03-01T00:00:00+00:00",
            )
        ) is False
        assert len(_timeline(store)["versions"]) == 2

        assert store.upsert(
            make_record(
                title="新公告",
                body="新正文",
                budget="999 元",
                deadline=None,
                source_url="legacy://source",
                published_at="2025-02-01",
                attachments=(_att("a.pdf", blob),),
                collected_at="2025-04-01T00:00:00+00:00",
            )
        ) is True
        assert _timeline(store)["versions"][2]["change_reasons"] == ["budget_changed"]
    finally:
        store.close()


def test_legacy_database_without_versions_is_repaired(tmp_path):
    """A partially written old database must still upgrade to a coherent state."""

    db = tmp_path / "orphan.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE tenders (project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        published_at TEXT, budget TEXT, deadline TEXT, source_url TEXT NOT NULL,
        body TEXT NOT NULL, record_hash TEXT NOT NULL, collected_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE attachments (project_id TEXT NOT NULL, url TEXT NOT NULL,
        title TEXT NOT NULL, content_hash TEXT, content BLOB,
        fetch_state TEXT NOT NULL DEFAULT 'fetched', PRIMARY KEY (project_id, url))"""
    )
    blob = b"orphan-attachment"
    connection.execute(
        "INSERT INTO tenders VALUES ('P1', '公告', '2026-01-01', '100', NULL, "
        "'https://example.test/n', '正文', 'legacy-hash', '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO attachments VALUES ('P1', 'a.pdf', 'a.pdf', ?, ?, 'fetched')",
        (content_hash(blob), blob),
    )
    connection.commit()
    connection.close()

    store = TenderStore(db)
    try:
        timeline = _timeline(store)
        assert len(timeline["versions"]) == 1
        assert timeline["versions"][0]["change_reasons"] == ["legacy_import"]
        assert timeline["versions"][0]["attachments"][0]["available"] is True
        assert store.get_attachment_content(content_hash(blob)) == blob
    finally:
        store.close()


def test_cli_exposes_project_timeline(tmp_path):
    db = tmp_path / "cli.sqlite3"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "sample_notice.html"

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tender_watch.cli", "--database", str(db), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    first = run("--fixture", str(fixture))
    assert first.returncode == 0, first.stderr
    assert "'changed': True" in first.stdout

    second = run("--fixture", str(fixture))
    assert second.returncode == 0, second.stderr
    assert "'changed': False" in second.stdout

    timeline = run("--timeline", "TENDER-2026-001", "--format", "json")
    assert timeline.returncode == 0, timeline.stderr
    payload = json.loads(timeline.stdout)
    assert payload["project_id"] == "TENDER-2026-001"
    assert len(payload["versions"]) == 1
    assert len(payload["collections"]) == 2
    assert payload["integrity"]["ok"] is True
    assert payload["versions"][0]["attachments"][0]["available"] is True

    text = run("--timeline", "TENDER-2026-001", "--format", "text")
    assert text.returncode == 0, text.stderr
    assert "版本历史" in text.stdout

    missing = run("--timeline", "NOPE")
    assert missing.returncode == 1
    assert "no timeline found" in missing.stderr
