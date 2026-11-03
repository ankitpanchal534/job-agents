from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

from jobagent.llm import USER_AGENT

SKIPPED_TAGS = {"script", "style", "noscript", "svg", "template"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer", "main",
    "body", "h1", "h2", "h3", "h4", "h5", "h6",
}
MAX_PAGE_BYTES = 2_000_000


class FetchError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r" +", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def fetch_jd_text(url: str, timeout: float = 30, max_chars: int = 20000) -> str:
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise FetchError(f"only http and https addresses can be fetched, not '{url}'")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read(MAX_PAGE_BYTES).decode(charset, errors="replace")
    except urllib.error.HTTPError as error:
        raise FetchError(f"{url} returned HTTP {error.code}") from error
    except OSError as error:
        raise FetchError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from error

    text = html_to_text(html)
    if not text:
        raise FetchError(
            "the page has no readable text (it may load its content with JavaScript); "
            "save the job description to a file and use --text-file"
        )
    return text[:max_chars]
