"""
UniProt REST client for the Gene Literature Miner.

Two capabilities, kept isolated from the NCBI layer so either can evolve
independently:

  * `protein_for_gene`  — map an NCBI Gene ID (falling back to symbol +
    organism) to the best UniProtKB entry and return its amino-acid sequence.
  * `homologs_for_protein` — from a UniProt accession, find its UniRef50
    cluster and return the cluster members as cross-species homologues.

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
UNIREF = "https://rest.uniprot.org/uniref"

CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

# UniProt tolerates bursts, but be a good citizen from a shared server IP.
_MIN_INTERVAL = 0.15
# UniRef identity used to define "homologue" (0.5 = UniRef50, broad; 0.9 tighter).
HOMOLOG_IDENTITY = os.environ.get("UNIREF_IDENTITY", "0.5")


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

# UniRef identity presets the UI exposes.
_IDENTITY_MAP = {"0.5": "50", "0.9": "90", "1.0": "100"}


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

    # -------------------------------------------------------------- homologues
    async def homologs_for_protein(
        self, accession: str, *, limit: int = 25, identity: str = HOMOLOG_IDENTITY
    ) -> dict[str, Any]:
        """
        Cross-species homologues via the protein's UniRef cluster.

        `identity` selects the cluster tightness: "0.5" (UniRef50, broad),
        "0.9" (UniRef90), or "1.0" (UniRef100). Returns
        {"cluster": ..., "identity": ..., "count": N, "homologs": [...]}.
        """
        identity = identity if identity in _IDENTITY_MAP else HOMOLOG_IDENTITY
        if self._cache is not None:
            cached = await self._cache.get("uniref", f"{accession}:{identity}")
            if cached is not None:
                return cached

        cluster = await self._uniref_cluster(accession, identity)
        if not cluster:
            result = {"cluster": None, "homologs": [], "identity": identity}
        else:
            members = await self._uniref_members(cluster, limit=limit + 5)
            homologs = [m for m in members if accession not in m.get("_accessions", [])]
            for m in homologs:
                m.pop("_accessions", None)
            result = {
                "cluster": cluster,
                "cluster_url": f"https://www.uniprot.org/uniref/{cluster}",
                "identity": identity,
                "count": len(homologs),
                "homologs": homologs[:limit],
            }
        if self._cache is not None:
            await self._cache.set("uniref", f"{accession}:{identity}", result)
        return result

    async def _uniref_cluster(self, accession: str, identity: str) -> Optional[str]:
        cluster = await self._uniref_query(f"uniprot_id:{accession}", identity)
        if cluster:
            return cluster
        # The accession may have been demerged; UniRef suggests a replacement.
        alt = await self._suggested_accession(accession, identity)
        if alt:
            return await self._uniref_query(f"uniprot_id:{alt}", identity)
        return None

    async def _uniref_query(self, member_clause: str, identity: str) -> Optional[str]:
        params = {
            "query": f"{member_clause} AND identity:{identity}",
            "format": "json",
            "fields": "id",
            "size": "1",
        }
        resp = await self._get(f"{UNIREF}/search", params)
        if resp is None:
            return None
        try:
            results = resp.json().get("results", [])
        except ValueError:
            return None
        return results[0].get("id") if results else None

    async def _suggested_accession(self, accession: str, identity: str) -> Optional[str]:
        params = {
            "query": f"uniprot_id:{accession} AND identity:{identity}",
            "format": "json",
            "fields": "id",
            "size": "1",
        }
        resp = await self._get(f"{UNIREF}/search", params)
        if resp is None:
            return None
        try:
            suggestions = resp.json().get("suggestions", [])
        except ValueError:
            return None
        for s in suggestions:
            q = str(s.get("query", ""))
            # e.g. "uniprotkb:q5fvf1 identity:0.5" -> q5fvf1
            for tok in q.split():
                if tok.lower().startswith("uniprotkb:"):
                    return tok.split(":", 1)[1].upper()
        return None

    async def _uniref_members(self, cluster: str, *, limit: int) -> list[dict[str, Any]]:
        params = {
            "format": "json",
            "size": str(min(limit, 50)),
            "facetFilter": "member_id_type:uniprotkb_id",
        }
        resp = await self._get(f"{UNIREF}/{cluster}/members", params)
        if resp is None:
            return []
        try:
            results = resp.json().get("results", [])
        except ValueError:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for m in results:
            accs = m.get("accessions") or []
            acc = accs[0] if accs else m.get("memberId", "")
            if not acc or acc in seen:
                continue
            seen.add(acc)
            out.append(
                {
                    "accession": acc,
                    "id": m.get("memberId", ""),
                    "protein_name": m.get("proteinName", ""),
                    "organism": m.get("organismName", ""),
                    "length": m.get("sequenceLength"),
                    "url": f"https://www.uniprot.org/uniprotkb/{acc}",
                    "_accessions": accs,
                }
            )
        return out


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
