from __future__ import annotations
from dataclasses import dataclass
from urllib.request import Request, urlopen


class FetchError(RuntimeError):
    pass


class ContentFetcher:
    def fetch(self, url: str) -> bytes:
        raise NotImplementedError


@dataclass
class UrlFetcher(ContentFetcher):
    timeout_seconds: float = 15.0
    user_agent: str = "tender-watch/0.1"

    def fetch(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read()
        except OSError as exc:
            raise FetchError(f"unable to fetch source: {url}") from exc


@dataclass
class FixtureFetcher(ContentFetcher):
    content: bytes

    def fetch(self, url: str) -> bytes:
        return self.content
