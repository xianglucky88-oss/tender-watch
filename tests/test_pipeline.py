from tender_watch.fetching import FixtureFetcher
from tender_watch.pipeline import TenderPipeline
from tender_watch.storage import TenderStore


def test_collect_is_idempotent(tmp_path):
    html = '<meta name="project-id" content="P1"><meta name="title" content="公告"><meta name="published-at" content="2026-01-01"><a href="x.pdf">x.pdf</a>'
    store = TenderStore(tmp_path / "tenders.sqlite3")
    try:
        pipeline = TenderPipeline(FixtureFetcher(html.encode()), store)
        first, changed_first = pipeline.collect_one("fixture://notice")
        second, changed_second = pipeline.collect_one("fixture://notice")
        assert first.project_id == second.project_id == "P1"
        assert changed_first is True
        assert changed_second is False
        assert len(first.attachments) == 1
    finally:
        store.close()
