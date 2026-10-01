"""Load and validate annotation records against the packaged JSON schema.

Validation has two parts:
- JSON Schema (``laminary_pipeline/schema/annotation.schema.json``), with format checking on;
- semantic checks that JSON Schema can't express (docs/NARRATIVE_SCHEMA.md section 11).
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator, FormatChecker

from laminary_pipeline.arc import FALLBACK_CONFIDENCE_CAP, derive_arc

SCHEMA_RESOURCE = ("laminary_pipeline", "schema/annotation.schema.json")
MAX_SECONDARY_PLOTS = 2
# Minimum confidence for showing a label or using it in a browse row (DECISIONS.md 2026-09-30,
# replacing 0.80 from 2026-09-26). To be re-tuned from gold-set calibration after the
# 500-title pilot.
DISPLAY_CONFIDENCE_THRESHOLD = 0.95
# The only sources allowed as annotation or embedding input until TMDB authorizes LLM use in
# writing (docs/NARRATIVE_SCHEMA.md section 1). Mirrors the schema's rule for LLM/gold records.
ALLOWED_INPUT_LICENSES = frozenset({"CC-BY-SA-4.0", "CC-BY-SA-3.0"})
# An en.wikipedia.org article URL with nothing after the (percent-encoded) title, so no query,
# fragment, whitespace, quotes or markup characters. The schema's ref pattern for LLM and gold
# sources is this same pattern (schema 1.1.0; tests keep the two equal). The pattern alone
# can't see namespaces (they may be percent-encoded), so the input gate also decodes the title
# with ``wikipedia_article_title`` below.
WIKIPEDIA_ARTICLE_REF = re.compile(r'^https://en\.wikipedia\.org/wiki/[^\s?#<>\[\]{}|"]+$')
WIKIPEDIA_REF = WIKIPEDIA_ARTICLE_REF  # the schema mirror
ARTICLE_PREFIX = "https://en.wikipedia.org/wiki/"
FORBIDDEN_TITLE_CHARS = frozenset('<>[]{}|#"')
MAX_TITLE_CHARS = 255
# Prefixes that stop a title from being an English Wikipedia article. The text before the first
# ':' is compared after MediaWiki-style normalization (one leading ':' dropped, runs of spaces
# collapsed, case-insensitive); film titles like "Alien: Covenant" are not affected. No real
# article can start with one of these followed by ':', because MediaWiki would read it as a
# namespace or interwiki link, so refusing them costs no real titles.
# 1. Namespaces and their aliases.
NON_ARTICLE_NAMESPACES = frozenset(
    ns.casefold()
    for base in (
        "User", "Wikipedia", "File", "MediaWiki", "Template", "Help", "Category", "Portal",
        "Draft", "TimedText", "Module", "Gadget", "Gadget definition", "Event",
    )
    for ns in (base, f"{base} talk")
) | frozenset(
    ns.casefold()
    for ns in ("Talk", "Special", "Media", "Image", "Image talk", "Project", "Project talk",
               "WP", "WT")
)
# 2. Pseudo-namespaces: shortcut redirects in the main namespace (MOS:PLOT, CAT:X, H:X, ...).
PSEUDO_NAMESPACES = frozenset(
    p.casefold() for p in ("MOS", "CAT", "H", "P", "T", "MP", "WikiProject", "WPT", "Wikt")
)
# 3. Interwiki prefixes for sister projects and common Wikimedia sites.
INTERWIKI_PREFIXES = frozenset(
    """w wikipedia wikt wiktionary b wikibooks n wikinews q wikiquote s wikisource v wikiversity
    voy wikivoyage species wikispecies d wikidata f wikifunctions c commons m meta metawikimedia
    metawiki mw mediawikiwiki foundation wmf wikimedia outreach incubator phab phabricator
    testwiki test2wiki betawikiversity quality strategy usability toollabs toolforge wikitech
    mediazilla bugzilla gerrit irc""".split()
)
# 4. Wikipedia language editions (interlanguage prefixes), including "en" itself.
LANGUAGE_PREFIXES = frozenset(
    """aa ab ace ady af ak als alt am ami an ang anp ar arc ary arz as ast atj av avk awa ay az
    azb ba ban bar bat-smg bbc bcl bdr be be-tarask be-x-old bew bg bh bi bjn blk bm bn bo bpy
    br bs btm bug bxr ca cbk-zam cdo ce ceb ch cho chr chy ckb co cr crh cs csb cu cv cy da dag
    de dga din diq dsb dtp dty dv dz ee el eml en eo es et eu ext fa fat ff fi fiu-vro fj fo
    fon fr frp frr fur fy ga gag gan gcr gd gl glk gn gom gor got gpe gsw gu guc gur guw gv ha
    hak haw he hi hif ho hr hsb ht hu hy hyw hz ia iba id ie ig igl ii ik ilo inh io is it iu
    ja jam jbo jv ka kaa kab kbd kbp kcg kg kge ki kj kk kl km kn knc ko koi kr krc ks ksh ku
    kus kv kw ky la lad lb lbe lez lfn lg li lij lld lmo ln lo lrc lt ltg lv lzh mad mai
    map-bms mdf mg mh mhr mi min mk ml mn mni mnw mo mos mr mrj ms mt mus mwl my myv mzn na nah
    nan nap nds nds-nl ne new ng nia nl nn no nov nqo nr nrm nso nup nv ny oc olo om or os pa
    pag pam pap pcd pcm pdc pfl pi pih pl pms pnb pnt ps pt pwn qu rm rmy rn ro roa-rup
    roa-tara rsk ru rue rup rw sa sah sat sc scn sco sd se sg sgs sh shi shn si simple sk skr
    sl sm smn sn so sq sr srn ss st stq su sv sw syl szl szy ta tay tcy tdd te tet tg th ti
    tig tk tl tly tn to tpi tr trv ts tt tum tw ty tyv udm ug uk ur uz ve vec vep vi vls vo
    vro wa war wo wuu xal xh xmf yi yo yue za zea zgh zh zh-classical zh-min-nan zh-yue
    zu""".split()
)
NOT_ARTICLE_PREFIXES = (
    NON_ARTICLE_NAMESPACES | PSEUDO_NAMESPACES | INTERWIKI_PREFIXES | LANGUAGE_PREFIXES
)


def _title_prefix(title: str) -> str | None:
    """The normalized text before the first ':' (MediaWiki-style: surrounding spaces and one
    leading ':' dropped, runs of spaces collapsed, casefolded), or None without a ':'."""
    norm = re.sub(r" +", " ", title).strip()
    if norm.startswith(":"):
        norm = norm[1:].lstrip()
    prefix, sep, _ = norm.partition(":")
    return prefix.strip().casefold() if sep else None


@cache
def _schema_text() -> str:
    package, path = SCHEMA_RESOURCE
    return resources.files(package).joinpath(path).read_text(encoding="utf-8")


def load_schema() -> dict[str, Any]:
    """A fresh copy of the annotation schema (callers may mutate it)."""
    return json.loads(_schema_text())


def schema_version() -> str:
    return load_schema()["properties"]["schema_version"]["const"]


def _is_rfc3339_datetime(value: object) -> bool:
    """Strict-enough RFC 3339 date-time: 'T' separator and an explicit offset or 'Z'."""
    if not isinstance(value, str):
        return True  # format only constrains strings
    if len(value) < 20 or value[10] not in "Tt":
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("z", "Z"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def format_checker() -> FormatChecker:
    """jsonschema's format checker, with a stdlib date-time check.

    jsonschema only checks 'date-time' when the optional rfc3339-validator package is
    installed and silently passes otherwise, so we register our own.
    """
    checker = FormatChecker()
    checker.checks("date-time")(_is_rfc3339_datetime)
    return checker


def validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(), format_checker=format_checker())


def semantic_errors(record: dict[str, Any]) -> list[str]:
    """Checks from docs/NARRATIVE_SCHEMA.md section 11. Assumes the record is schema-valid."""
    errors: list[str] = []
    if record.get("record_kind") == "llm_annotation":
        prov = record["provenance"]
        total = sum(src["word_count"] for src in prov["sources"])
        if prov["input_word_count"] != total:
            errors.append(f"input_word_count {prov['input_word_count']} != sum of sources {total}")
    if record.get("outcome") != "annotated":
        return errors
    layers = record["layers"]

    plot = layers["archetypal_plot"]
    primary = plot["primary"]["label"]
    if not plot["plots"][primary]["present"]:
        errors.append(f"primary plot {primary} is not marked present in plots")
    others = [p for p, j in plot["plots"].items() if j["present"] and p != primary]
    if len(others) > MAX_SECONDARY_PLOTS:
        errors.append(f"more than {MAX_SECONDARY_PLOTS} secondary plots present: {others}")

    beats = record["beat_tags"]
    evidence_tags = [e["tag"] for e in beats.get("spoiler_text", {}).get("evidence", [])]
    if len(evidence_tags) != len(set(evidence_tags)):
        errors.append("evidence lists a tag more than once")
    absent = [t for t in evidence_tags if not beats["tags"][t]["present"]]
    if absent:
        errors.append(f"evidence for tags judged absent: {absent}")

    skel = layers["structural_skeleton"]
    arc = skel["emotional_arc"]
    if arc["method"] == "derived":
        if "arc_points" not in skel:
            errors.append("derived emotional_arc without arc_points")
        else:
            d = derive_arc(skel["arc_points"])
            fields = ("label", "threshold_used", "net_change_fallback", "reduced_shape")
            got = tuple(arc[f] for f in fields)
            want = tuple(getattr(d, f) for f in fields)
            if got != want:
                errors.append(f"emotional_arc {got} does not match derivation {want}")
            conf = arc.get("confidence")
            if d.unreliable and conf is not None and conf > FALLBACK_CONFIDENCE_CAP:
                errors.append("fallback or reduced-shape arc must have confidence below 0.5")
    return errors


def wikipedia_article_title(ref: object) -> str:
    """'https://en.wikipedia.org/wiki/The_Matrix' -> 'The Matrix'. Raises ValueError unless
    the ref is a plain article URL and the decoded title is safe to put inside the prompt's
    marker: no ``<>[]{}|#"``, no control or line-separator characters, at most 255 characters
    (the MediaWiki title limit), and no namespace, shortcut, interwiki or language prefix
    (NOT_ARTICLE_PREFIXES, compared after MediaWiki-style normalization)."""
    if not isinstance(ref, str) or not WIKIPEDIA_ARTICLE_REF.match(ref):
        raise ValueError(f"ref {ref!r} is not a plain en.wikipedia.org article URL")
    path = urlparse(ref).path
    if not path.startswith("/wiki/"):
        raise ValueError(f"ref {ref!r} is not an article URL")
    try:
        title = unquote(path[len("/wiki/") :], errors="strict").replace("_", " ")
    except UnicodeDecodeError as e:
        raise ValueError(f"ref {ref!r}: article title is not valid UTF-8") from e
    bad = sorted(
        {c for c in title if c in FORBIDDEN_TITLE_CHARS or unicodedata.category(c)[0] in "CZ"}
        - {" "}
    )
    if bad or not title.strip() or len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"ref {ref!r}: unsafe or invalid article title {title!r}")
    prefix = _title_prefix(title)
    if prefix is not None and prefix in NOT_ARTICLE_PREFIXES:
        raise ValueError(
            f"ref {ref!r}: {prefix!r} pages are not articles (namespace, shortcut, interwiki "
            "or language prefix)"
        )
    return title


def require_wikipedia_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fail-closed gate for anything sent to the model or embedded (prompt builder, embeddings).

    Returns the sources unchanged if every one is a Wikipedia plot section under CC BY-SA whose
    ref is an en.wikipedia.org article (``wikipedia_article_title``); raises ValueError
    otherwise, including for an empty list or any missing field. Never filters silently: one
    bad source rejects the whole title.
    """
    if not sources:
        raise ValueError("no sources: refusing to build model or embedding input")
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            raise ValueError(f"source {i} is not an object")
        kind, license_, ref = src.get("kind"), src.get("license"), src.get("ref")
        if kind != "wikipedia_plot":
            raise ValueError(f"source {i}: kind {kind!r} is not allowed as input")
        if license_ not in ALLOWED_INPUT_LICENSES:
            raise ValueError(f"source {i}: license {license_!r} is not allowed as input")
        try:
            wikipedia_article_title(ref)
        except ValueError as e:
            raise ValueError(f"source {i}: {e}") from e
    return sources


def usable_arc_label(record: dict[str, Any]) -> str | None:
    """The emotional-arc label a consumer may use (rows, why-lines, "more like this", title
    pages), or None. Fallback and reduced-shape labels are never usable, whatever their
    confidence; other labels need DISPLAY_CONFIDENCE_THRESHOLD."""
    if record.get("outcome") != "annotated":
        return None
    arc = record["layers"]["structural_skeleton"]["emotional_arc"]
    if arc.get("net_change_fallback") or arc.get("reduced_shape"):
        return None
    if arc.get("confidence", 0.0) < DISPLAY_CONFIDENCE_THRESHOLD:
        return None
    return arc["label"]


def validate_record(record: dict[str, Any]) -> list[str]:
    """All problems with a record: schema errors first; semantic checks only if schema-valid."""
    schema_errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator().iter_errors(record), key=lambda e: list(e.absolute_path))
    ]
    return schema_errors or semantic_errors(record)
