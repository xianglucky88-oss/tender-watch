from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .fetching import FixtureFetcher, UrlFetcher
from .pipeline import TenderPipeline
from .storage import TenderStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect and inspect tender evidence")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--url", default="https://example.invalid/notice")
    parser.add_argument("--database", default="tender-watch.sqlite3")
    parser.add_argument(
        "--timeline",
        "--show-timeline",
        "--history",
        dest="timeline",
        metavar="PROJECT_ID",
        help="print the evidence timeline for a project instead of collecting",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="timeline output format (default: json)",
    )
    args = parser.parse_args()
    store = TenderStore(args.database)
    try:
        if args.timeline:
            timeline = store.get_timeline(args.timeline)
            if timeline is None:
                print(
                    f"no timeline found for project {args.timeline!r}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if args.format == "json":
                print(json.dumps(timeline, ensure_ascii=False, indent=2))
            else:
                print(_format_timeline(timeline))
            return

        fetcher = FixtureFetcher(args.fixture.read_bytes()) if args.fixture else UrlFetcher()
        record, changed = TenderPipeline(fetcher, store).collect_one(args.url)
        print({"project_id": record.project_id, "changed": changed, "attachments": len(record.attachments)})
    finally:
        store.close()


def _format_timeline(timeline: dict) -> str:
    lines = [f"项目 {timeline['project_id']} 证据时间线"]
    current = timeline.get("current")
    if current:
        lines.append(
            f"当前版本: v{current['version_no']}  采集时间: {current['collected_at']}"
        )
        lines.append(f"  标题: {current['notice']['title']}")
        lines.append(f"  来源: {current['notice']['source_url']}")

    lines.append("")
    lines.append("版本历史:")
    for version in timeline["versions"]:
        reasons = ", ".join(version["change_reasons"])
        completeness = "完整" if version["complete"] else "旧库部分快照"
        lines.append(
            f"  v{version['version_no']} {version['collected_at']} "
            f"[{reasons}] ({completeness})"
        )
        lines.append(f"    正文: {version['notice']['body']}")
        for attachment in version["attachments"]:
            state = "可取" if attachment["available"] else "不可取"
            digest = attachment["content_hash"] or "-"
            lines.append(
                f"    附件 {attachment['url']} [{state}] {digest}"
            )

    lines.append("")
    lines.append("采集记录:")
    for event in timeline["collections"]:
        lines.append(
            f"  #{event['collection_id']} {event['collected_at']} "
            f"v{event['version_no']} {event['outcome']} "
            f"reasons={','.join(event['change_reasons'])}"
        )
        for attachment in event["attachments"]:
            error = f" error={attachment['error']}" if attachment["error"] else ""
            lines.append(
                f"    附件 {attachment['url']} 状态={attachment['observed_state']}{error}"
            )

    integrity = timeline["integrity"]
    lines.append("")
    lines.append("一致性: " + ("通过" if integrity["ok"] else "; ".join(integrity["issues"])))
    return "\n".join(lines)


if __name__ == "__main__":
    main()
