"""The "pairs" title set: titles the similarity pairs need that the pilot candidates lack.

``data/config/similarity_pairs.csv`` names each pair's titles by title, year and type
(``gold.pairs``). ``evaluate/pairs.py`` needs QIDs or title keys, and a fingerprint for both
titles, so every pair title must end up with a QID and an annotatable plot file. Hari's
decision (DECISIONS 2026-10-02, similarity pairs option b): fetch the missing ones as a separate
set that does not change the 500-title pilot, its effective selection or the gold set.

Per pair title (in pairs-file order, each distinct title once):

1. **In the candidate list** (same normalized title, type, year within 1; or a resolved QID that
   is a candidate): nothing to resolve. If the candidate is in the effective pilot
   (``pilot_effective.jsonl``), the pilot run annotates it and the title is covered. If not (a
   reserve outside it, or a pilot title whose plot was skipped), it joins the pairs set as a
   copy of its candidate row with ``candidate_role`` kept, so ``plots --pairs`` fetches it when
   it has no plot file yet and a later pairs annotation run includes it. The pairs set is
   therefore exactly the pair titles the pilot run won't annotate.
2. **Otherwise** it is looked up on Wikidata by exact English label or alias (the gold-seed
   lookup query) and resolved with the candidate detail query, matching normalized title, type
   and year within 1. The closest year wins; two or more items at that distance are
   **ambiguous** and none is picked, unless the pairs file pins that side's QID (``qid_a`` /
   ``qid_b``, see ``gold.pairs``): a pin picks its item from the title/type/year matches, and a
   pin that is not among them leaves the title **unresolved**. No hit is **unresolved**. A hit
   the candidate rules exclude (no TMDB id, several TMDB ids, a non-narrative genre, no enwiki
   article) is **ineligible**.
   Nothing is guessed: those titles are reported and left out.
3. A resolved QID in ``EXCLUDED`` is **excluded**: its enwiki article was checked by hand and is
   not about this title, so its plot file must not be annotated for the pairs. It gets no row
   and no QID, so its pairs are reported not scorable.

Resolved rows have the candidate row shape (``candidates._row``: bucket, language, series
status, TMDB and IMDb ids, source) with ``role: "pairs"`` plus ``pair_title``, ``pair_year``,
``pair_ids``, ``year_match`` and ``pairs_version``.

Outputs (all under the data dir, gitignored): ``pairs_candidates.jsonl``,
``similarity_pairs_resolved.csv`` (the ``evaluate/pairs.py`` format: QIDs, ``should_match`` /
``should_not_match``; pairs with an unresolved side are left out) and
``reports/pairs_summary.json``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from laminary_pipeline.gold.pairs import Pair, pins
from laminary_pipeline.ingest import candidates as cand
from laminary_pipeline.ingest.wikidata import Wikidata

PAIRS_VERSION = "1.2.0"  # 1.1.0: QID pins from the pairs file; 1.2.0: EXCLUDED QIDs
ROLE = "pairs"
EXPECTATION = {"match": "should_match", "no_match": "should_not_match"}
RESOLVED_COLUMNS = ["title_a", "title_b", "expectation", "note"]

# QIDs whose enwiki article is known not to be about the item (QA, checked by hand). The pair
# resolution never uses them; the reason is reported.
EXCLUDED: dict[str, str] = {
    "Q4384067": "Wikidata's enwiki sitelink for this anime series (TMDB tv 31724) is the "
                "compilation-film article ('Three-part film by Gorō Taniguchi'), whose plot "
                "section describes the films; the series article 'Code Geass' belongs to the "
                "franchise item Q207981 (no TMDB id, no year). QA S1, 2026-10-03",
}

TitleKey = tuple[str, int, str]  # (title, year, media_type) as written in the pairs file


@dataclass
class TitleResolution:
    key: TitleKey
    pair_ids: list[str]
    status: str  # in_pilot | candidate_outside_pilot | resolved | ambiguous | unresolved |
    #              ineligible | excluded
    qid: str | None = None
    detail: str | None = None
    hits: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        title, year, media_type = self.key
        return {"title": title, "year": year, "media_type": media_type,
                "pair_ids": self.pair_ids, "status": self.status, "qid": self.qid,
                "detail": self.detail, "hits": self.hits}


def pair_titles(pairs: Sequence[Pair]) -> dict[TitleKey, list[str]]:
    """Each distinct pair title, in file order, with the pair ids it appears in."""
    out: dict[TitleKey, list[str]] = {}
    for p in pairs:
        for side in (p.a, p.b):
            out.setdefault(side, []).append(p.pair_id)
    return out


def _names(item: dict[str, Any]) -> set[str]:
    base = re.sub(r"\s*\(.*\)$", "", item.get("enwiki_title") or "")
    found = [item.get("title") or "", base, item.get("gold_seed_title") or "",
             item.get("pair_title") or "", *item.get("seed_labels", [])]
    return {cand.normalize_title(n) for n in found if n}


def matches(key: TitleKey, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items with the key's normalized title (label, enwiki title without its "(...)" suffix,
    or a looked-up alias), type, and a year within 1, closest year first."""
    title, year, media_type = key
    want = cand.normalize_title(title)
    hits = [it for it in items
            if it.get("media_type") == media_type and it.get("year")
            and abs(it["year"] - year) <= 1 and want in _names(it)]
    return sorted(hits, key=lambda it: (abs(it["year"] - year), -it.get("sitelinks", 0),
                                        it["qid"]))


def _hit_summary(it: dict[str, Any]) -> dict[str, Any]:
    return {"qid": it["qid"], "title": it.get("title"), "year": it.get("year"),
            "enwiki_title": it.get("enwiki_title"), "tmdb_id": it.get("tmdb_id"),
            "sitelinks": it.get("sitelinks")}


def pick(key: TitleKey, items: Sequence[dict[str, Any]], pin: str | None = None,
         ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """(status, the chosen item, the closest hits): resolved / ambiguous / unresolved. With a
    ``pin``, only the pinned item can be chosen, and only if it matches the key."""
    hits = matches(key, items)
    if pin:
        chosen = [it for it in hits if it["qid"] == pin]
        return ("resolved", chosen[0], chosen) if chosen else ("unresolved", None, hits)
    if not hits:
        return "unresolved", None, []
    best = abs(hits[0]["year"] - key[1])
    top = [it for it in hits if abs(it["year"] - key[1]) == best]
    if len(top) > 1:
        return "ambiguous", None, top
    return "resolved", top[0], top


def to_look_up(pairs: Sequence[Pair], candidates: Sequence[dict[str, Any]],
               ) -> tuple[list[TitleKey], list[str]]:
    """The pair titles the candidate list doesn't resolve (to look up on Wikidata), and the
    pinned QIDs among them."""
    pinned = pins(pairs)
    keys = [k for k in pair_titles(pairs)
            if pick(k, candidates, pinned.get(k))[0] != "resolved"]
    return keys, sorted({pinned[k] for k in keys if k in pinned})


def lookup_labels(keys: Sequence[TitleKey]) -> list[str]:
    return sorted({k[0] for k in keys})


def gather(wd: Wikidata, keys: Sequence[TitleKey],
           pinned: Sequence[str] = ()) -> dict[str, dict[str, Any]]:
    """Wikidata items for these titles: the label/alias lookup, then the detail query (which
    also covers any ``pinned`` QIDs, so a pin is checked even if the label lookup misses it)."""
    if not keys:
        return {}
    labels: dict[str, set[str]] = {q: set() for q in pinned}
    for hit in wd.seeds(lookup_labels(keys)):
        labels.setdefault(hit["qid"], set())
        if hit["seed_label"]:
            labels[hit["qid"]].add(hit["seed_label"])
    items = wd.details(sorted(labels))
    for qid, found in labels.items():
        if qid in items:
            items[qid]["seed_labels"] = sorted(found)
    return items


def pairs_row(
    item: dict[str, Any], key: TitleKey, pair_ids: list[str], rank: int,
    retrieved_at: str | None, pinned: bool = False,
) -> dict[str, Any]:
    """A candidate-shaped row for a resolved pair title (same bucket, language and series
    status logic as ``candidates.select``)."""
    it = dict(item)
    it["genre"] = cand.coarse_genre(it["genres"])
    it["bucket"], it["bucket_basis"] = cand.classify_with_basis(it)
    it["language"] = cand.primary_language(it)
    row = cand._row(it, cand.BUCKET_BY_NAME[it["bucket"]], ROLE, rank, None, retrieved_at)
    return {**row, **_pair_fields(it, key, pair_ids, pinned), "candidate_role": None}


def _pair_fields(item: dict[str, Any], key: TitleKey, pair_ids: list[str],
                 pinned: bool = False) -> dict[str, Any]:
    return {"pair_title": key[0], "pair_year": key[1], "pair_ids": pair_ids,
            "year_match": "exact" if item["year"] == key[1] else "within_1",
            "qid_pinned": pinned,
            "pairs_version": PAIRS_VERSION}


@dataclass
class PairsSet:
    rows: list[dict[str, Any]]
    titles: list[TitleResolution]
    qids: dict[TitleKey, str]  # every pair title with a QID (candidates and the pairs set)
    excluded: dict[TitleKey, str] = field(default_factory=dict)  # title -> "QID: reason"


def build(
    pairs: Sequence[Pair],
    candidates: Sequence[dict[str, Any]],
    items: dict[str, dict[str, Any]],
    *,
    in_pilot: Callable[[str], bool],
    retrieved_at: str | None = None,
) -> PairsSet:
    """Resolve every pair title (see the module docstring). ``items`` are the Wikidata details
    from ``gather`` for the titles not in ``candidates``."""
    by_qid = {c["qid"]: c for c in candidates}
    rows: list[dict[str, Any]] = []
    titles: list[TitleResolution] = []
    qids: dict[TitleKey, str] = {}
    pinned = pins(pairs)
    for key, pair_ids in pair_titles(pairs).items():
        pin = pinned.get(key)
        status, hit, top = pick(key, candidates, pin)
        if status == "ambiguous":  # two candidates fit: never pick one
            titles.append(TitleResolution(key, pair_ids, "ambiguous",
                                          detail="several candidates match",
                                          hits=[_hit_summary(h) for h in top]))
            continue
        if hit is None:
            status, hit, top = pick(key, list(items.values()), pin)
            if hit is not None and hit["qid"] in by_qid:  # found by alias: still a candidate
                hit = by_qid[hit["qid"]]
            elif status != "resolved":
                titles.append(TitleResolution(
                    key, pair_ids, status, hits=[_hit_summary(h) for h in top],
                    detail="several Wikidata items match" if status == "ambiguous"
                    else f"pinned QID {pin} is not a Wikidata film/series with this English "
                         "label or alias, type and year (within 1)" if pin
                    else "no Wikidata film/series with this English label or alias, type and "
                         "year (within 1) and an enwiki article"))
                continue
            elif hit["qid"] in EXCLUDED:
                titles.append(TitleResolution(key, pair_ids, "excluded", hit["qid"],
                                              EXCLUDED[hit["qid"]], [_hit_summary(hit)]))
                continue
            else:
                reason = cand.excluded_reason(hit)
                if reason:
                    titles.append(TitleResolution(key, pair_ids, "ineligible", hit["qid"],
                                                  reason, [_hit_summary(hit)]))
                    continue
                rows.append(pairs_row(hit, key, pair_ids, len(rows) + 1, retrieved_at,
                                      pin is not None))
                titles.append(TitleResolution(
                    key, pair_ids, "resolved", hit["qid"],
                    rows[-1]["year_match"] + ("; pinned in the pairs file" if pin else ""),
                    [_hit_summary(hit)]))
                qids[key] = hit["qid"]
                continue
        # a candidate
        if hit["qid"] in EXCLUDED:
            titles.append(TitleResolution(key, pair_ids, "excluded", hit["qid"],
                                          EXCLUDED[hit["qid"]], [_hit_summary(hit)]))
            continue
        qids[key] = hit["qid"]
        if in_pilot(hit["qid"]):
            titles.append(TitleResolution(key, pair_ids, "in_pilot", hit["qid"],
                                          f"candidate role {hit.get('role')}"))
            continue
        rows.append({**hit, "role": ROLE, "bucket_rank": len(rows) + 1,
                     **_pair_fields(hit, key, pair_ids, pin is not None),
                     "candidate_role": hit.get("role")})
        titles.append(TitleResolution(key, pair_ids, "candidate_outside_pilot", hit["qid"],
                                      f"candidate role {hit.get('role')}, not in the "
                                      "effective pilot"))
    _flag_shared_qids(titles)
    excluded = {t.key: f"{t.qid} excluded" for t in titles if t.status == "excluded"}
    return PairsSet(rows, titles, qids, excluded)


def _flag_shared_qids(titles: list[TitleResolution]) -> None:
    """Two different pair titles resolving to one QID is a data error worth a look."""
    seen: dict[str, TitleResolution] = {}
    for t in titles:
        if t.qid is None:
            continue
        if t.qid in seen:
            note = f"same QID as {seen[t.qid].key[0]} ({seen[t.qid].key[1]})"
            t.detail = f"{t.detail}; {note}" if t.detail else note
        else:
            seen[t.qid] = t


def resolved_pairs(pairs: Sequence[Pair], qids: dict[TitleKey, str]) -> list[dict[str, str]]:
    """``evaluate/pairs.py`` rows for the pairs with both titles resolved."""
    out = []
    for p in pairs:
        a, b = qids.get(p.a), qids.get(p.b)
        if a and b:
            out.append({"title_a": a, "title_b": b, "expectation": EXPECTATION[p.expect],
                        "note": f"{p.pair_id}: {p.a[0]} ({p.a[1]}) / {p.b[0]} ({p.b[1]})"})
    return out


def resolved_csv(rows: Sequence[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RESOLVED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def scorability(
    pairs: Sequence[Pair], qids: dict[TitleKey, str], not_annotatable: Callable[[str], str | None],
    excluded: dict[TitleKey, str] | None = None,
) -> list[dict[str, Any]]:
    """Per pair: scorable once embeddings exist when both titles have a QID and an annotatable
    plot file (``annotate.ready``); else why not, per side."""
    out = []
    for p in pairs:
        problems = []
        for side in (p.a, p.b):
            qid = qids.get(side)
            if qid is None:
                why = (excluded or {}).get(side, "no QID")
                problems.append(f"{side[0]} ({side[1]}): {why}")
                continue
            reason = not_annotatable(qid)
            if reason is not None:
                problems.append(f"{side[0]} ({side[1]}, {qid}): {reason[:160]}")
        out.append({"pair_id": p.pair_id, "expect": p.expect,
                    "title_a": p.a[0], "qid_a": qids.get(p.a),
                    "title_b": p.b[0], "qid_b": qids.get(p.b),
                    "scorable": not problems, "problems": problems})
    return out


def summary(pairs_set: PairsSet, scored: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for t in pairs_set.titles:
        counts[t.status] = counts.get(t.status, 0) + 1
    return {
        "pairs_version": PAIRS_VERSION,
        "pair_titles": len(pairs_set.titles),
        "title_status": counts,
        "pairs_set_rows": len(pairs_set.rows),
        "pairs_resolved": sum(1 for s in scored if s["qid_a"] and s["qid_b"]),
        "pairs_scorable": sum(1 for s in scored if s["scorable"]),
        "pairs": len(scored),
        "titles": [t.as_dict() for t in pairs_set.titles],
        "pair_status": scored,
    }


def format_summary(s: dict[str, Any]) -> str:
    lines = [
        f"Pair titles: {s['pair_titles']} distinct; {s['title_status']}",
        f"Pairs set: {s['pairs_set_rows']} titles (role {ROLE!r})",
        f"Pairs with both titles resolved: {s['pairs_resolved']}/{s['pairs']}; "
        f"scorable now (both annotatable): {s['pairs_scorable']}/{s['pairs']}",
    ]
    for t in s["titles"]:
        if t["status"] in ("ambiguous", "unresolved", "ineligible", "excluded"):
            hits = ", ".join(f"{h['qid']} {h['title']!r} ({h['year']})" for h in t["hits"])
            lines.append(f"  {t['status'].upper()}: {t['title']} ({t['year']}, "
                         f"{t['media_type']}) in {','.join(t['pair_ids'])}: {t['detail']}"
                         + (f" [{hits}]" if hits else ""))
        elif t["detail"] and "same QID" in t["detail"]:
            lines.append(f"  CHECK: {t['title']} ({t['year']}) {t['qid']}: {t['detail']}")
    for p in s["pair_status"]:
        if not p["scorable"]:
            lines.append(f"  not scorable {p['pair_id']}: " + "; ".join(p["problems"]))
    return "\n".join(lines)
