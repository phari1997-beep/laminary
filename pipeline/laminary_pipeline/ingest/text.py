"""Turn MediaWiki section HTML into plain annotation text, and count its words.

The text produced here is exactly what gets hashed (``content_sha256``), counted
(``word_count``) and later sent to the model and shown to gold labelers. The prompt builder must
send ``text`` byte-for-byte and use ``word_count()`` from this module for its pre-call check
(docs/NARRATIVE_SCHEMA.md section 1).

Removed: references and all superscripts (``[1]``, ``[citation needed]``), tables (episode
lists, series overviews), hatnotes ("Main article: ..."), images and captions, navboxes,
headings, edit links, style and script, and MediaWiki error messages such as "Cite error: ..."
(by their ``error`` / ``mw-ext-cite-error`` class). Kept: paragraph and list text, with
paragraphs separated by a blank line.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from html.parser import HTMLParser

SKIP_TAGS = frozenset(
    {"style", "script", "table", "sup", "figure", "figcaption", "math", "h1", "h2", "h3", "h4",
     "h5", "h6", "noscript", "audio", "video", "img", "map"}
)
SKIP_CLASSES = frozenset(
    {"reference", "references", "reflist", "mw-editsection", "hatnote", "navbox", "noprint",
     "thumb", "gallery", "infobox", "sidebar", "shortdescription", "metadata", "ambox",
     "mw-cite-backlink", "toc", "mw-heading", "mw-empty-elt", "rellink", "dablink",
     "portalbox", "sistersitebox", "side-box", "mbox-small", "quotebox",
     # MediaWiki error messages ("Cite error: There are <ref group=lower-alpha> tags on this
     # page...", citation-template and Lua errors): the wiki's rendering, never plot text.
     # Matched on the class MediaWiki puts on the message, not on its wording.
     "error", "mw-ext-cite-error", "cs1-visible-error", "cs1-hidden-error", "scribunto-error"}
)
BLOCK_TAGS = frozenset({"p", "li", "dd", "dt", "blockquote", "div", "ul", "ol", "dl", "br"})
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)
# Bracketed leftovers that survive as plain text in some templates.
_BRACKET_NOTES = re.compile(r"\[(?:\d{1,3}|[a-z]|note \d+|citation needed|clarification needed)\]")
_SPACES = re.compile(r"[ \t  ​]+")
# A word: a run of letters/digits, optionally joined by apostrophes or hyphens (don't, 1990s,
# well-known). Unicode-aware, so non-Latin names count too.
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.skip_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            if tag == "br" and self.skip_depth is None:
                self.parts.append("\n")
            return
        self.stack.append(tag)
        if self.skip_depth is not None:
            return
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag in SKIP_TAGS or classes & SKIP_CLASSES or attr.get("role") == "note":
            self.skip_depth = len(self.stack)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self.skip_depth is None:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS or tag not in self.stack:
            return
        # Pop to the matching open tag (tolerates unclosed children).
        while self.stack:
            open_tag = self.stack.pop()
            if self.skip_depth is not None and len(self.stack) < self.skip_depth:
                self.skip_depth = None
            if open_tag == tag:
                break
        if self.skip_depth is None and tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth is None:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    raw = unicodedata.normalize("NFC", "".join(parser.parts))
    raw = _BRACKET_NOTES.sub("", raw)
    paragraphs = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [_SPACES.sub(" ", ln).strip() for ln in block.split("\n")]
        para = " ".join(ln for ln in lines if ln)
        para = re.sub(r"\s+([,.;:!?])", r"\1", para)
        if para:
            paragraphs.append(para)
    return "\n\n".join(paragraphs)
