from __future__ import annotations
import argparse
from pathlib import Path
from .fetching import FixtureFetcher, UrlFetcher
from .pipeline import TenderPipeline
from .storage import TenderStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--url", default="https://example.invalid/notice")
    parser.add_argument("--database", default="tender-watch.sqlite3")
    args = parser.parse_args()
    fetcher = FixtureFetcher(args.fixture.read_bytes()) if args.fixture else UrlFetcher()
    store = TenderStore(args.database)
    try:
        record, changed = TenderPipeline(fetcher, store).collect_one(args.url)
        print({"project_id": record.project_id, "changed": changed, "attachments": len(record.attachments)})
    finally:
        store.close()


if __name__ == "__main__":
    main()
