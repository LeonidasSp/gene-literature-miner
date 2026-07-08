"""
UniProt REST client for the Gene Literature Miner.

Kept isolated from the NCBI layer. `protein_for_gene` maps an NCBI Gene ID
(falling back to symbol/description + organism) to the best UniProtKB entry and
returns its amino-acid sequence plus functional annotation (EC, keywords, GO,
Pfam, KEGG). Orthologues are handled separately by the OrthoDB client.

UniProt's REST API needs no key. We stay polite with a small rate limiter and
the same retry/back-off shape as the NCBI client.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx

UNIPROTKB = "https://rest.uniprot.org/uniprotkb"

CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

# UniProt tolerates bursts, but be a good citizen from a shared server IP.
_MIN_INTERVAL = 0.15


class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min_interval:
                await asyncio.sleep(self._min_interval - delta)
            self._last = time.monotonic()


_PROTEIN_FIELDS = (
    "accession,id,protein_name,organism_name,length,sequence,reviewed,"
    "ec,keyword,go_id,xref_pfam,xref_kegg,protein_families"
)


class UniProtClient:
    def __init__(self, cache: Any = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
            follow_redirects=True,
        )
        self._limiter = _RateLimiter(_MIN_INTERVAL)
        self._cache = cache

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Optional[httpx.Response]:
        await self._limiter.wait()
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        return None  # soft-fail: protein/homologues are enrichments, not core

    # ---------------------------------------------------------------- protein
    async def protein_for_gene(
        self, *, gene_id: str, candidates: list[str], organism: str
    ) -> Optional[dict[str, Any]]:
        """
        Best UniProtKB protein for a gene. Tries the NCBI GeneID cross-reference
        first (most precise), then each candidate term (a clean gene symbol as a
        `gene:` match, a descriptive phrase as a `protein_name:` match), scoped
        to the organism. Prefers reviewed (Swiss-Prot) entries.
        """
        if self._cache is not None:
            cached = await self._cache.get("protein", str(gene_id))
            if cached is not None:
                return cached or None  # cached "" means "looked up, no hit"

        entry = await self._resolve_protein(gene_id, candidates, organism)

        if self._cache is not None:
            await self._cache.set("protein", str(gene_id), entry or "")
        return entry

    async def _resolve_protein(
        self, gene_id: str, candidates: list[str], organism: str
    ) -> Optional[dict[str, Any]]:
        entry = await self._search_protein(f"xref:geneid-{gene_id}")
        if entry:
            return entry
        org = organism.strip()
        org_clause = f' AND organism_name:"{org}"' if org else ""
        seen: set[str] = set()
        for term in candidates:
            term = (term or "").strip()
            key = term.lower()
            if not term or key in seen:
                continue
            seen.add(key)
            if " " in term or "-" in term:  # descriptive phrase -> protein name
                q = f'protein_name:"{term}"{org_clause}'
            else:  # clean symbol -> gene name
                q = f"gene:{term}{org_clause}"
            entry = await self._search_protein(q)
            if entry:
                return entry
        return None

    async def _search_protein(self, query: str) -> Optional[dict[str, Any]]:
        params = {
            "query": query,
            "format": "json",
            "fields": _PROTEIN_FIELDS,
            "size": "10",
        }
        resp = await self._get(f"{UNIPROTKB}/search", params)
        if resp is None:
            return None
        try:
            results = resp.json().get("results", [])
        except ValueError:
            return None
        if not results:
            return None
        # Prefer a reviewed (Swiss-Prot) entry; otherwise the first hit.
        best = next(
            (r for r in results if "reviewed" in str(r.get("entryType", "")).lower()),
            results[0],
        )
        return _parse_protein(best)


def _parse_protein(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    acc = r.get("primaryAccession")
    if not acc:
        return None
    seq = (r.get("sequence") or {}).get("value", "")
    length = (r.get("sequence") or {}).get("length")
    return {
        "accession": acc,
        "id": r.get("uniProtkbId") or r.get("uniProtKBId") or "",
        "name": _protein_name(r),
        "organism": (r.get("organism") or {}).get("scientificName", ""),
        "length": length if length is not None else (len(seq) or None),
        "reviewed": "reviewed" in str(r.get("entryType", "")).lower(),
        "sequence": seq,
        "url": f"https://www.uniprot.org/uniprotkb/{acc}",
        "annotations": _annotations(r),
    }


def _annotations(r: dict[str, Any]) -> dict[str, Any]:
    """Pull GO / Pfam / KEGG / EC / family / keywords from a UniProt entry."""
    xrefs = r.get("uniProtKBCrossReferences") or []
    go, pfam, kegg = [], [], []
    for x in xrefs:
        db = x.get("database")
        if db == "GO":
            term = _xref_prop(x, "GoTerm")
            go.append({"id": x.get("id"), "term": term})
        elif db == "Pfam":
            pfam.append({"id": x.get("id"), "name": _xref_prop(x, "EntryName")})
        elif db == "KEGG":
            kegg.append(x.get("id"))
    ec = []
    desc = r.get("proteinDescription") or {}
    rec = desc.get("recommendedName") or {}
    for e in rec.get("ecNumbers") or []:
        if e.get("value"):
            ec.append(e["value"])
    keywords = [k.get("name") for k in (r.get("keywords") or []) if k.get("name")]
    return {
        "ec": ec,
        "families": r.get("proteinFamilies") or _families_from_comments(r),
        "keywords": keywords[:12],
        "go": go[:20],
        "pfam": pfam[:10],
        "kegg": kegg[:5],
    }


def _families_from_comments(r: dict[str, Any]) -> str:
    for c in r.get("comments") or []:
        if c.get("commentType") == "SIMILARITY":
            for t in c.get("texts") or []:
                if t.get("value"):
                    return t["value"]
    return ""


def _xref_prop(xref: dict[str, Any], key: str) -> str:
    for p in xref.get("properties") or []:
        if p.get("key") == key:
            return p.get("value", "")
    return ""


def _protein_name(r: dict[str, Any]) -> str:
    desc = r.get("proteinDescription") or {}
    rec = desc.get("recommendedName") or {}
    full = (rec.get("fullName") or {}).get("value")
    if full:
        return full
    subs = desc.get("submissionNames") or []
    if subs:
        return (subs[0].get("fullName") or {}).get("value", "")
    return ""
