"""HTML-to-text stripping and word counting for plot sections (synthetic snippets only)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from laminary_pipeline.ingest.text import html_to_text, sha256_text, word_count


def test_strips_references_hatnotes_tables_figures_headings_and_styles() -> None:
    html = (
        '<div class="mw-parser-output">'
        '<div class="mw-heading mw-heading2"><h2 id="Plot">Plot</h2></div>'
        "<style>.x{color:red}</style>"
        '<div role="note" class="hatnote">Main article: Something else</div>'
        '<figure><img src="a.jpg"><figcaption>A caption</figcaption></figure>'
        '<p>Ada finds a map.<sup class="reference"><a href="#n1">[1]</a></sup> '
        'She sails north<sup class="noprint">[<i>citation needed</i>]</sup>.</p>'
        '<table class="wikitable"><tr><td>Season 1</td><td>10</td></tr></table>'
        "<ul><li>First item</li><li>Second&nbsp;item &amp; more</li></ul>"
        '<div class="navbox">Navigation junk</div>'
        "</div>"
    )
    text = html_to_text(html)
    assert text == "Ada finds a map. She sails north.\n\nFirst item\n\nSecond item & more"
    for junk in ("Plot", "Main article", "caption", "[1]", "citation", "Season", "Navigation", "{"):
        assert junk not in text


def test_nested_skip_and_void_tags_do_not_leak() -> None:
    html = '<p>Keep <span class="reference">drop <b>this</b><br> too</span> this.<br>Next</p>'
    assert html_to_text(html) == "Keep this. Next"


def test_cite_error_messages_are_stripped_by_class() -> None:
    """Real rendering from the pilot (Vikram, 2022): MediaWiki puts the "Cite error" message
    inside the last plot paragraph. It is dropped by its class, whatever its wording."""
    path = (Path(__file__).resolve().parent / "fixtures" / "ingest" / "sections"
            / "cite_error_plot.html")
    html = path.read_text(encoding="utf-8")
    assert "mw-ext-cite-error" in html
    text = html_to_text(html)
    assert "Cite error" not in text and "reflist" not in text and "help page" not in text
    assert text.endswith("walks away after learning about the bounty placed on all of them.")
    other = ('<p>Story ends.<span class="error">Lua error: something</span></p>'
             '<p>Next.<span class="cs1-visible-error citation-comment">Check date</span></p>')
    assert html_to_text(other) == "Story ends.\n\nNext."
    assert html_to_text('<p>An <span class="terror">error</span> here.</p>') == (
        "An error here.")  # only the exact class name counts


def test_word_count_is_unicode_aware_and_ignores_punctuation() -> None:
    assert word_count("Don't stop — it's the 1990s, well-known!") == 6
    assert word_count("Kim Ji-young meets 김지영 in Seoul.") == 6
    assert word_count("") == 0


def test_sha256_is_of_utf8_text() -> None:
    assert sha256_text("é") == hashlib.sha256("é".encode()).hexdigest()
