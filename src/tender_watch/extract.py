from __future__ import annotations
import re
from html.parser import HTMLParser
from .models import Attachment, TenderRecord


class _NoticeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._text: list[str] = []
        self._active_link: str | None = None
        self._active_title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") and values.get("content"):
            self.meta[values["name"]] = values["content"]
        if tag == "a" and values.get("href"):
            self._active_link = values["href"]
            self._active_title = []

    def handle_data(self, data: str) -> None:
        clean = " ".join(data.split())
        if clean:
            self._text.append(clean)
            if self._active_link is not None:
                self._active_title.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_link is not None:
            self.links.append((self._active_link, " ".join(self._active_title)))
            self._active_link = None
            self._active_title = []

    @property
    def body(self) -> str:
        return " ".join(self._text)


def parse_notice(source_url: str, content: bytes) -> TenderRecord:
    parser = _NoticeParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    title = parser.meta.get("title", "").strip()
    project_id = parser.meta.get("project-id", "").strip()
    if not project_id or not title:
        raise ValueError("notice must contain project-id and title metadata")
    links = tuple(Attachment(url=url, title=label or url) for url, label in parser.links if label.lower().endswith(("pdf", "doc", "docx")) or ".pdf" in url.lower())
    text = parser.body
    budget = _find_value(text, r"预算(?:金额)?\s*[:：]\s*([^，,。]+)")
    deadline = _find_value(text, r"截止时间\s*[:：]\s*([^，,。]+)")
    published = parser.meta.get("published-at", "")
    return TenderRecord(project_id, title, published, budget, deadline, source_url, text, links)


def _find_value(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None
