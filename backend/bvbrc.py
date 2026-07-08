"""
BV-BRC (Bacterial and Viral Bioinformatics Resource Center) client.

An organism-specific nucleotide source used as a fallback when NCBI has no
usable record. BV-BRC (formerly PATRIC) has very broad bacterial + viral genome
coverage, so it often finds a gene NCBI's Gene database can't resolve.

Two-step lookup against the public Data API (https://www.bv-brc.org/api/):
  1. `genome_feature` — find a CDS by gene symbol (or product name), scoped to
     the organism. Returns each feature's `na_sequence_md5`.
  2. `feature_sequence` — fetch the actual nucleotide sequence for that md5.
No key required.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

BASE = "https://www.bv-brc.org/api"
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"


class BVBRCClient:
    def __init__(self, cache: Any = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={
                "User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        self._cache = cache

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_sequence(
        self, *, gene: str, name: str, organism: str
    ) -> Optional[dict[str, Any]]:
        """Best CDS nucleotide sequence for a gene in an organism, or None."""
        if not organism.strip():
            return None
        key = f"{gene}|{name}|{organism}".lower()
        if self._cache is not None:
            cached = await self._cache.get("bvbrc", key)
            if cached is not None:
                return cached or None

        result = await self._resolve(gene, name, organism)

        if self._cache is not None:
            await self._cache.set("bvbrc", key, result or "")
        return result

    async def _resolve(
        self, gene: str, name: str, organism: str
    ) -> Optional[dict[str, Any]]:
        feats: list[dict[str, Any]] = []
        g = (gene or "").strip()
        # 1. Exact gene-symbol match, but only for real single-token symbols.
        if g and " " not in g and "-" not in g:
            feats = await self._features(f"eq(gene,{_tok(g)})", organism)
        # 2. Otherwise (or if that missed) match the product/name as a phrase.
        phrases: list[str] = []
        for t in (name, gene):
            t = (t or "").strip()
            if t and t.lower() not in {p.lower() for p in phrases}:
                phrases.append(t)
        for phrase in phrases:
            if feats:
                break
            feats = await self._features(f"keyword({_kw(phrase)})", organism)
        feat = _best_feature(feats, organism)
        if not feat:
            return None
        md5 = feat.get("na_sequence_md5")
        seq = await self._sequence_by_md5(md5) if md5 else None
        if not seq:
            return None
        patric_id = feat.get("patric_id")
        acc = patric_id or feat.get("refseq_locus_tag") or feat.get("feature_id") or md5
        genome = feat.get("genome_name", "")
        product = feat.get("product", "")
        url = (
            f"https://www.bv-brc.org/view/Feature/{quote(str(patric_id))}"
            if patric_id
            else "https://www.bv-brc.org/"
        )
        return {
            "accession": acc,
            "region": "",
            "strand": "",
            "length": len(seq),
            "header": f"{acc} {product} [{genome}]".strip(),
            "sequence": seq.upper(),
            "url": url,
            "source": f"BV-BRC ({genome})",
            "database": "BV-BRC",
        }

    async def _features(self, clause: str, organism: str) -> list[dict[str, Any]]:
        rql = (
            f"and({clause},eq(feature_type,CDS),keyword({_kw(organism)}))"
            "&select(patric_id,feature_id,refseq_locus_tag,genome_name,gene,"
            "product,na_length,na_sequence_md5)"
            "&sort(-na_length)&limit(15)"
        )
        data = await self._get(f"{BASE}/genome_feature/?{rql}")
        return data if isinstance(data, list) else []

    async def _sequence_by_md5(self, md5: str) -> Optional[str]:
        data = await self._get(f"{BASE}/feature_sequence/?eq(md5,{_tok(md5)})&limit(1)")
        if isinstance(data, list) and data:
            return data[0].get("sequence")
        return None

    async def _get(self, url: str) -> Any:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}http_accept=application/json"
        for attempt in range(3):
            try:
                resp = await self._client.get(url)
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(0.4 * (attempt + 1))
        return None


def _tok(s: str) -> str:
    """Encode a bare RQL token value (no spaces expected)."""
    return quote(str(s), safe="")


def _kw(s: str) -> str:
    """A quoted RQL keyword() argument, URL-encoded."""
    return quote(f'"{s}"', safe="")


def _best_feature(
    feats: list[dict[str, Any]], organism: str
) -> Optional[dict[str, Any]]:
    """Prefer a feature from the requested organism with a sequence md5, longest."""
    org = organism.lower()
    scored = []
    for f in feats:
        if not f.get("na_sequence_md5"):
            continue
        genome = str(f.get("genome_name", "")).lower()
        match = 1 if all(w in genome for w in org.split()[:2]) else 0
        has_id = 1 if f.get("patric_id") else 0
        scored.append((match, has_id, int(f.get("na_length") or 0), f))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return scored[0][3]
