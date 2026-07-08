"""
Ensembl REST client — organism-specific nucleotide source for eukaryotes.

Ensembl's unified REST API (https://rest.ensembl.org) serves the authoritative
gene models for a huge range of eukaryotes, drawing each from the community
database of record:

  * plants (incl. Arabidopsis, from the TAIR10 assembly)  — Ensembl Plants
  * fungi (incl. budding yeast, SGD systematic names)      — Ensembl Fungi
  * insects (incl. Drosophila, FlyBase IDs)                — Ensembl Metazoa
  * vertebrates (human, mouse, zebrafish/ZFIN, ...)        — Ensembl
  * protists                                               — Ensembl Protists

So one client reaches TAIR/SGD/FlyBase/etc. gene sequences through a single
uniform API. Gene lookup is by external symbol; the sequence is the genomic
region for the resolved stable ID.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

REST = os.environ.get("ENSEMBL_REST", "https://rest.ensembl.org")
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

# Nicer source labels for well-known species (the community DB Ensembl mirrors).
_SOURCE_LABELS = {
    "arabidopsis_thaliana": "Ensembl Plants (TAIR)",
    "oryza_sativa": "Ensembl Plants",
    "zea_mays": "Ensembl Plants",
    "saccharomyces_cerevisiae": "Ensembl Fungi (SGD)",
    "schizosaccharomyces_pombe": "Ensembl Fungi (PomBase)",
    "drosophila_melanogaster": "Ensembl Metazoa (FlyBase)",
    "caenorhabditis_elegans": "Ensembl Metazoa (WormBase)",
    "danio_rerio": "Ensembl (ZFIN)",
    "mus_musculus": "Ensembl (MGI)",
    "rattus_norvegicus": "Ensembl (RGD)",
    "homo_sapiens": "Ensembl",
}


class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._last < self._min_interval:
                await asyncio.sleep(self._min_interval - (now - self._last))
            self._last = time.monotonic()


class EnsemblClient:
    def __init__(self, cache: Any = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={
                "User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        self._limiter = _RateLimiter(0.08)  # Ensembl allows ~15 req/s
        self._cache = cache

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_sequence(
        self, *, symbol: str, name: str, organism: str
    ) -> Optional[dict[str, Any]]:
        species = (organism or "").strip().lower().replace(" ", "_")
        if not species or not (symbol or name):
            return None
        key = f"{symbol}|{name}|{species}".lower()
        if self._cache is not None:
            cached = await self._cache.get("ensembl", key)
            if cached is not None:
                return cached or None

        result = await self._resolve(symbol, name, species)

        if self._cache is not None:
            await self._cache.set("ensembl", key, result or "")
        return result

    async def _resolve(
        self, symbol: str, name: str, species: str
    ) -> Optional[dict[str, Any]]:
        # Try the clean symbol, then the first token of a descriptive name.
        candidates = []
        for t in (symbol, (name or "").split(" ")[0]):
            t = (t or "").strip()
            if t and t.lower() not in {c.lower() for c in candidates}:
                candidates.append(t)
        gene_id = None
        for term in candidates:
            gene_id = await self._gene_id(species, term)
            if gene_id:
                break
        if not gene_id:
            return None
        seq = await self._sequence(gene_id)
        if not seq:
            return None
        label = _SOURCE_LABELS.get(species, "Ensembl")
        pretty = species.replace("_", " ")
        return {
            "accession": gene_id,
            "region": "",
            "strand": "",
            "length": len(seq),
            "header": f"{gene_id} {symbol} [{pretty}]".strip(),
            "sequence": seq.upper(),
            "url": f"https://www.ensembl.org/id/{quote(gene_id)}",
            "source": f"{label} ({pretty})",
            "database": label,
        }

    async def _gene_id(self, species: str, symbol: str) -> Optional[str]:
        data = await self._get(f"{REST}/xrefs/symbol/{quote(species)}/{quote(symbol)}")
        if isinstance(data, list):
            for x in data:
                if x.get("type") == "gene" and x.get("id"):
                    return x["id"]
        return None

    async def _sequence(self, gene_id: str) -> Optional[str]:
        data = await self._get(f"{REST}/sequence/id/{quote(gene_id)}?type=genomic")
        if isinstance(data, dict):
            return data.get("seq")
        return None

    async def _get(self, url: str) -> Any:
        await self._limiter.wait()
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}content-type=application/json"
        for attempt in range(3):
            try:
                resp = await self._client.get(url)
                if resp.status_code in (400, 404):
                    return None
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(0.4 * (attempt + 1))
        return None
