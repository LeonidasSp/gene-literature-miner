"""
In-context snippets for the gene-mention viewer.

PubTator3 tells us *where* a gene is named (a character offset inside a passage of
a paper) but not the surrounding sentence. This module turns those offsets into
short, readable snippets: the sentence containing the mention, which section it
came from, and where to highlight. It is pure Python (no network) so it can be
unit-tested on its own.

Notes on the PubTator3 data this relies on (checked against real responses):
  * annotation offsets are document-wide, so the position inside a passage is
    `location.offset - passage.offset`;
  * the `sentences` field it returns is empty, so sentences are split here;
  * full-text documents also carry the reference list as ordinary passages
    (section_type "REF") -- those are not the paper's own text and are skipped.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any, Optional

# PubTator section_type -> (label shown to the user, priority; lower is shown first).
SECTIONS: dict[str, tuple[str, int]] = {
    "ABSTRACT": ("Abstract", 0),
    "RESULTS": ("Results", 1),
    "DISCUSS": ("Discussion", 2),
    "CONCL": ("Conclusion", 3),
    "TITLE": ("Title", 4),
    "INTRO": ("Introduction", 5),
    "FIG": ("Figure legend", 6),
    "TABLE": ("Table", 7),
    "METHODS": ("Methods", 8),
    "SUPPL": ("Supplementary", 9),
}
REFERENCES = "REF"

# Abstract-only documents carry no section_type, just a passage type.
_SECTION_FROM_TYPE = {"title": "TITLE", "abstract": "ABSTRACT"}

MAX_SNIPPETS = 5          # snippets kept per gene
POOL_MAX = 60             # candidates buffered per gene while scanning
SECTION_POOL_MAX = 3      # ...of which at most this many per paper section, so a
                          # gene named 200 times in Results can't crowd out Discussion
SENTENCE_LIMIT = 360      # longer sentences are trimmed around the mention
TITLE_LIMIT = 150


def passage_section(passage: dict[str, Any]) -> Optional[str]:
    """PubTator section key for a passage ('RESULTS', 'REF', ...), or None."""
    infons = passage.get("infons") or {}
    st = str(infons.get("section_type") or "").upper()
    if st:
        return st
    return _SECTION_FROM_TYPE.get(str(infons.get("type") or "").lower())


def is_heading(passage: dict[str, Any]) -> bool:
    """Section headings ('title_1', 'title_2'): counted as mentions, but a bare
    heading is not a sentence worth showing as context."""
    return str((passage.get("infons") or {}).get("type") or "").lower().startswith("title_")


def doc_title(doc: dict[str, Any]) -> str:
    """The paper's title, taken from its title passage."""
    for p in doc.get("passages") or []:
        infons = p.get("infons") or {}
        if str(infons.get("section_type") or "").upper() == "TITLE" or str(
            infons.get("type") or ""
        ).lower() in ("title", "front"):
            t = " ".join((p.get("text") or "").split())
            return t if len(t) <= TITLE_LIMIT else t[: TITLE_LIMIT - 1].rstrip() + "…"
    return ""


# ------------------------------------------------------------ sentence splitting
# Words that end in a period without ending a sentence.
_ABBREV = frozenset({
    "e.g", "i.e", "al", "fig", "figs", "vs", "ca", "cf", "spp", "sp", "subsp", "var",
    "no", "nos", "dr", "eq", "eqs", "ref", "refs", "approx", "resp", "viz", "inc",
    "ltd", "co", "st", "pp", "vol", "sec", "mt", "nov",
})
_BOUNDARY = re.compile(r"(?P<end>[.!?][\"')\]]*)(?P<ws>\s+)")


def _is_abbreviation(text: str, dot: int) -> bool:
    """True if the '.' at `dot` closes an abbreviation or an initial ('E. coli')."""
    ws = max(text.rfind(" ", 0, dot), text.rfind("\n", 0, dot), text.rfind("\t", 0, dot))
    token = text[ws + 1 : dot].lstrip("([\"'").lower()
    return (len(token) == 1 and token.isalpha()) or token in _ABBREV


def split_sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) character spans of the sentences in `text`.

    Deliberately conservative: a boundary needs terminal punctuation, whitespace,
    and a capital letter/digit/opening bracket next, and is ignored after common
    abbreviations and single-letter initials so 'E. coli' and 'et al.' don't split.
    """
    spans: list[tuple[int, int]] = []
    start = len(text) - len(text.lstrip())
    for m in _BOUNDARY.finditer(text):
        nxt = text[m.end() : m.end() + 1]
        if not nxt or not (nxt.isupper() or nxt.isdigit() or nxt in "\"'(["):
            continue
        if text[m.start()] == "." and _is_abbreviation(text, m.start()):
            continue
        spans.append((start, m.end("end")))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def sentence_at(spans: list[tuple[int, int]], pos: int) -> Optional[tuple[int, int]]:
    """The span containing character offset `pos`."""
    if not spans:
        return None
    i = bisect_right([s for s, _ in spans], pos) - 1
    if i < 0:
        return None
    s, e = spans[i]
    return (s, e) if s <= pos < e or pos == e else None


def trim_around(
    sentence: str, marks: list[tuple[int, int]], limit: int = SENTENCE_LIMIT
) -> tuple[str, list[list[int]]]:
    """Shorten a very long sentence to a window around its first mention.

    Returns the (possibly '…'-padded) text and the highlight ranges shifted to it.
    """
    if len(sentence) <= limit:
        return sentence, [[s, e] for s, e in marks]
    first_s, first_e = min(marks)
    lo = max(0, (first_s + first_e) // 2 - limit // 2)
    hi = min(len(sentence), lo + limit)
    lo = max(0, hi - limit)
    if lo > 0:  # start on a word boundary, without cutting into the mention
        sp = sentence.find(" ", lo)
        if 0 <= sp < first_s:
            lo = sp + 1
    if hi < len(sentence):
        sp = sentence.rfind(" ", 0, hi)
        if sp > first_e:
            hi = sp
    lead = "…" if lo > 0 else ""
    text = lead + sentence[lo:hi] + ("…" if hi < len(sentence) else "")
    shift = len(lead) - lo
    return text, [[s + shift, e + shift] for s, e in marks if s >= lo and e <= hi]


# ------------------------------------------------------------------ per-gene pool
class SnippetCollector:
    """Collects candidate snippets for ONE gene while a document set is scanned,
    then picks a small, varied selection (`pick`)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple, dict[str, Any]] = {}
        self._per_section: dict[tuple[str, str], int] = {}

    def add(
        self, *, pmid: str, title: str, section: str, passage_no: int,
        sent_start: int, sentence: str, mark: tuple[int, int],
    ) -> None:
        """Record one mention. Several mentions in the same sentence are merged
        into one snippet with several highlights."""
        key = (pmid, passage_no, sent_start)
        entry = self._by_key.get(key)
        if entry is None:
            sec_key = (pmid, section)
            if (len(self._by_key) >= POOL_MAX
                    or self._per_section.get(sec_key, 0) >= SECTION_POOL_MAX):
                return
            self._per_section[sec_key] = self._per_section.get(sec_key, 0) + 1
            entry = self._by_key[key] = {
                "pmid": pmid, "title": title, "section": section,
                "sentence": sentence, "marks": [], "order": len(self._by_key),
            }
        if mark not in entry["marks"]:
            entry["marks"].append(mark)

    def pick(self, limit: int = MAX_SNIPPETS) -> list[dict[str, Any]]:
        """A varied selection: repeatedly take the candidate from the least-used
        paper, then the least-used section of that paper, then the most
        informative section (abstract/results first), then the earliest in the text.
        """
        remaining = list(self._by_key.values())
        chosen: list[dict[str, Any]] = []
        per_paper: dict[str, int] = {}
        per_section: dict[tuple[str, str], int] = {}
        while remaining and len(chosen) < limit:
            best = min(
                remaining,
                key=lambda e: (
                    per_paper.get(e["pmid"], 0),
                    per_section.get((e["pmid"], e["section"]), 0),
                    SECTIONS[e["section"]][1],
                    e["order"],
                ),
            )
            remaining.remove(best)
            chosen.append(best)
            per_paper[best["pmid"]] = per_paper.get(best["pmid"], 0) + 1
            key = (best["pmid"], best["section"])
            per_section[key] = per_section.get(key, 0) + 1
        out = []
        for e in chosen:
            text, marks = trim_around(e["sentence"], sorted(e["marks"]))
            out.append({
                "pmid": e["pmid"], "title": e["title"],
                "section": SECTIONS[e["section"]][0], "text": text, "marks": marks,
            })
        return out
