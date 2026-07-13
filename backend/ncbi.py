"""
NCBI E-utilities + PubTator3 client for the Gene Literature Miner.

All requests are polite: they include a `tool` and `email` identifier and are
rate-limited to stay under NCBI's guideline of 3 requests/second when no API
key is configured (10/second when NCBI_API_KEY is set).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBTATOR = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"

TOOL_NAME = "gene-literature-miner"
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
API_KEY = os.environ.get("NCBI_API_KEY", "").strip()

# Rate limit: 10/s with a key, 3/s without. Leave a little headroom.
_MIN_INTERVAL = 0.11 if API_KEY else 0.34


class RateLimiter:
    """Serialises access so successive NCBI calls are spaced by _MIN_INTERVAL."""

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


class NCBIClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
        )
        self._limiter = RateLimiter(_MIN_INTERVAL)
        self._tax_cache: dict[str, str] = {}
        self._org_cache: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _common(self) -> dict[str, str]:
        params = {"tool": TOOL_NAME, "email": CONTACT_EMAIL}
        if API_KEY:
            params["api_key"] = API_KEY
        return params

    async def _get(self, url: str, params: dict[str, Any], *, rate_limited: bool = True) -> httpx.Response:
        if rate_limited:
            await self._limiter.wait()
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:  # too many requests -> back off
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError,) as exc:  # transient network/5xx
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"NCBI request failed: {url} ({last_exc})")

    # ------------------------------------------------------------------ PubMed
    async def search_pubmed(self, term: str, retmax: int = 50) -> list[str]:
        params = {
            **self._common(),
            "db": "pubmed",
            "term": term,
            "retmax": str(retmax),
            "retmode": "json",
            "sort": "relevance",
        }
        resp = await self._get(f"{EUTILS}/esearch.fcgi", params)
        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])

    # --------------------------------------------------------------- PubTator3
    async def genes_from_pubtator(
        self, pmids: list[str], *, full_text: bool = False
    ) -> dict[str, dict[str, Any]]:
        """
        Return {gene_id: {"mentions": set(text), "pmids": set(pmid), "count": int}}
        by asking PubTator3 for gene annotations on the given PMIDs.

        When `full_text` is set, PubTator annotates the full body of any article
        available in PMC's open-access subset (not just the abstract), which
        surfaces more genes; paywalled articles still fall back to their abstract.
        Full-text responses are much larger, so they are fetched in smaller batches.
        """
        genes: dict[str, dict[str, Any]] = {}
        batch_size = 20 if full_text else 100
        for i in range(0, len(pmids), batch_size):
            batch = pmids[i : i + batch_size]
            params = {"pmids": ",".join(batch)}
            if full_text:
                params["full"] = "true"
            try:
                resp = await self._get(
                    f"{PUBTATOR}/publications/export/biocjson", params
                )
            except RuntimeError:
                continue
            for doc in _iter_bioc_docs(resp.text):
                pmid = _doc_pmid(doc)
                for ann in _iter_annotations(doc):
                    infons = ann.get("infons", {})
                    if str(infons.get("type", "")).lower() != "gene":
                        continue
                    ident = infons.get("identifier") or infons.get("normalized_id")
                    if not ident:
                        continue
                    text = (ann.get("text") or "").strip()
                    for gid in _split_ids(str(ident)):
                        if not gid.isdigit():
                            continue
                        entry = genes.setdefault(
                            gid, {"mentions": {}, "pmids": set(), "count": 0}
                        )
                        entry["count"] += 1
                        if text:
                            entry["mentions"][text] = entry["mentions"].get(text, 0) + 1
                        if pmid:
                            entry["pmids"].add(pmid)
        return genes

    # ------------------------------------------------------------------ Gene db
    async def gene_summaries(self, gene_ids: list[str]) -> dict[str, dict[str, Any]]:
        """esummary on the Gene database, batched. Returns {gene_id: summary}."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(gene_ids), 200):
            batch = gene_ids[i : i + 200]
            params = {
                **self._common(),
                "db": "gene",
                "id": ",".join(batch),
                "retmode": "json",
            }
            resp = await self._get(f"{EUTILS}/esummary.fcgi", params)
            result = resp.json().get("result", {})
            for uid in result.get("uids", []):
                out[uid] = result[uid]
        return out

    async def lineage(self, taxid: str) -> str:
        """Full taxonomic lineage string for a taxid (cached), for source routing."""
        taxid = str(taxid or "").strip()
        if not taxid:
            return ""
        if taxid in self._tax_cache:
            return self._tax_cache[taxid]
        params = {**self._common(), "db": "taxonomy", "id": taxid, "retmode": "xml"}
        try:
            resp = await self._get(f"{EUTILS}/efetch.fcgi", params)
        except RuntimeError:
            return ""
        text = resp.text
        lin = ""
        m = re.search(r"<Lineage>(.*?)</Lineage>", text, re.DOTALL)
        if m:
            lin = m.group(1).strip()
        # Append the taxon's own name so single-rank checks still work.
        n = re.search(r"<ScientificName>(.*?)</ScientificName>", text, re.DOTALL)
        if n:
            lin = f"{lin}; {n.group(1).strip()}"
        self._tax_cache[taxid] = lin
        return lin

    async def resolve_organism(self, text: str) -> Optional[dict[str, str]]:
        """
        Resolve a user-typed organism to a canonical NCBI Taxonomy scientific name
        + taxid. Handles abbreviations and common names that Taxonomy knows
        ('E. coli' -> Escherichia coli; "baker's yeast" -> Saccharomyces
        cerevisiae). Returns None when it can't resolve — the caller then keeps the
        raw text, which PubMed's [Organism] tag still maps for abbreviated
        binomials like 'B. pahangi'. Cached; soft-fails to None on any error.
        """
        text = (text or "").strip()
        if not text:
            return None
        key = text.lower()
        if key in self._org_cache:
            return self._org_cache[key]
        result: Optional[dict[str, str]] = None
        try:
            ids = (await self._get(
                f"{EUTILS}/esearch.fcgi",
                {**self._common(), "db": "taxonomy", "term": text,
                 "retmode": "json", "retmax": "1"},
            )).json().get("esearchresult", {}).get("idlist", [])
            if ids:
                taxid = ids[0]
                rec = (await self._get(
                    f"{EUTILS}/esummary.fcgi",
                    {**self._common(), "db": "taxonomy", "id": taxid,
                     "retmode": "json"},
                )).json().get("result", {}).get(taxid, {})
                name = rec.get("scientificname", "")
                if name:
                    result = {"name": name, "taxid": taxid,
                              "rank": rec.get("rank", "")}
        except (RuntimeError, ValueError, KeyError, TypeError):
            result = None
        self._org_cache[key] = result
        return result

    async def search_gene_db(self, term: str, retmax: int = 20) -> list[str]:
        params = {
            **self._common(),
            "db": "gene",
            "term": term,
            "retmax": str(retmax),
            "retmode": "json",
        }
        resp = await self._get(f"{EUTILS}/esearch.fcgi", params)
        return resp.json().get("esearchresult", {}).get("idlist", [])

    async def resolve_sequence(
        self,
        *,
        summary: dict[str, Any],
        candidate_symbols: list[str],
        organism: str,
    ) -> Optional[dict[str, Any]]:
        """
        Get a gene's nucleotide sequence as FASTA.

        Strategy:
          1. Extract the exact region from the gene's own genomic coordinates.
          2. If that record has no usable coordinates (e.g. its assembly was
             suppressed), re-resolve the gene *name* + organism to a canonical
             RefSeq Gene record that does have coordinates, and use that.
        """
        seq = await self._seq_from_summary(summary)
        if seq:
            seq["source"] = "gene genomic coordinates"
            return seq

        seen: set[str] = set()
        for sym in candidate_symbols:
            sym = (sym or "").strip()
            key = sym.lower()
            if not sym or key in seen:
                continue
            seen.add(key)
            term = _build_gene_query(sym, organism)
            try:
                ids = await self.search_gene_db(term, retmax=20)
            except RuntimeError:
                continue
            if not ids:
                continue
            summaries = await self.gene_summaries(ids)
            best = _best_coord_summary(summaries)
            if not best:
                continue
            best_uid, best_summary = best
            seq = await self._seq_from_summary(best_summary)
            if seq:
                seq["source"] = f"resolved by name '{sym}'"
                seq["resolved_gene_id"] = best_uid
                seq["resolved_symbol"] = best_summary.get("name", "")
                seq["gene_url"] = f"https://www.ncbi.nlm.nih.gov/gene/{best_uid}"
                return seq
        return None

    async def _seq_from_summary(self, summary: dict[str, Any]) -> Optional[dict[str, Any]]:
        """efetch the exact gene region from a Gene-db summary's coordinates."""
        g = _coords_from_summary(summary)
        if not g:
            return None
        chraccver, chrstart, chrstop = g
        lo, hi = min(chrstart, chrstop), max(chrstart, chrstop)
        if hi - lo + 1 > 60000:  # guard against pulling a whole operon/genome
            return None
        strand = 1 if chrstart <= chrstop else 2
        params = {
            **self._common(),
            "db": "nuccore",
            "id": chraccver,
            "seq_start": str(lo + 1),
            "seq_stop": str(hi + 1),
            "strand": str(strand),
            "rettype": "fasta",
            "retmode": "text",
        }
        resp = await self._get(f"{EUTILS}/efetch.fcgi", params)
        fasta = resp.text.strip()
        if not fasta.startswith(">"):
            return None
        header, seq = _split_fasta(fasta)
        if not seq:
            return None
        return {
            "accession": chraccver,
            "region": f"{lo + 1}-{hi + 1}",
            "strand": "+" if strand == 1 else "-",
            "length": len(seq),
            "header": header,
            "sequence": seq,
            "url": f"https://www.ncbi.nlm.nih.gov/nuccore/{chraccver}",
            "database": "NCBI",
        }


import re

_LOCUS_TAG_RE = re.compile(r"_")


def is_locus_tag(name: str) -> bool:
    """Heuristic: 'AT695_RS04810', 'SAP033A_029' are locus tags; 'icaA' is not."""
    if not name:
        return True
    return bool(_LOCUS_TAG_RE.search(name)) and any(ch.isdigit() for ch in name)


def _build_gene_query(term: str, organism: str) -> str:
    """
    Build a Gene-db query for a candidate. Single-token symbols are matched
    precisely on [Gene Name]; multi-word descriptions are matched as a RefSeq
    phrase (so literature aliases fall back to the NCBI description vocabulary).
    """
    org_clause = f' AND "{organism.strip()}"[Organism]' if organism.strip() else ""
    if " " in term or "-" in term:  # descriptive phrase
        return f'"{term}"[All Fields]{org_clause} AND alive[prop] AND srcdb refseq[prop]'
    return f'{term}[Gene Name]{org_clause} AND alive[prop]'


def _coords_from_summary(summary: dict[str, Any]) -> Optional[tuple[str, int, int]]:
    """Return (chraccver, chrstart, chrstop) from genomicinfo or locationhist."""
    for source in (summary.get("genomicinfo"), summary.get("locationhist")):
        for g in source or []:
            acc = g.get("chraccver")
            if not acc:
                continue
            try:
                return acc, int(g["chrstart"]), int(g["chrstop"])
            except (KeyError, ValueError, TypeError):
                continue
    return None


def _best_coord_summary(
    summaries: dict[str, dict[str, Any]]
) -> Optional[tuple[str, dict[str, Any]]]:
    """Pick the best Gene-db summary that has coordinates, preferring RefSeq."""
    best = None  # (rank, uid, summary)
    for uid, s in summaries.items():
        coords = _coords_from_summary(s)
        if not coords:
            continue
        acc = coords[0]
        status = str(s.get("status", ""))  # "0" = live
        rank = (
            2 if acc.startswith("NC_") else 1 if acc.startswith("NZ_") else 0,
            1 if status == "0" else 0,
        )
        if best is None or rank > best[0]:
            best = (rank, uid, s)
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------- parse helpers
def _iter_bioc_docs(text: str):
    """PubTator export may be a single JSON object, an array, or JSON-lines."""
    import json

    text = text.strip()
    if not text:
        return
    # Try whole-body parse first.
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            yield from obj
            return
        if isinstance(obj, dict):
            # PubTator3 wraps docs as {"PubTator3": [...]}; BioC uses "documents".
            for key in ("PubTator3", "documents"):
                if isinstance(obj.get(key), list):
                    yield from obj[key]
                    return
            # A single wrapping list under any key.
            list_vals = [v for v in obj.values() if isinstance(v, list)]
            if len(list_vals) == 1:
                yield from list_vals[0]
                return
            yield obj  # already a single document
            return
    except json.JSONDecodeError:
        pass
    # Fall back to JSON-lines.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _iter_annotations(doc: dict[str, Any]):
    for passage in doc.get("passages", []) or []:
        for ann in passage.get("annotations", []) or []:
            yield ann


def _doc_pmid(doc: dict[str, Any]) -> Optional[str]:
    pmid = doc.get("pmid") or doc.get("id")
    if pmid:
        return str(pmid)
    infons = doc.get("infons", {}) or {}
    if infons.get("pmid"):
        return str(infons["pmid"])
    return None


def _split_ids(ident: str) -> list[str]:
    for sep in (";", ",", "|"):
        ident = ident.replace(sep, " ")
    return [p for p in ident.split() if p]


def _split_fasta(fasta: str) -> tuple[str, str]:
    lines = fasta.splitlines()
    header = lines[0].lstrip(">").strip() if lines else ""
    seq = "".join(l.strip() for l in lines[1:])
    return header, seq
