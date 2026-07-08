"""
WormBase ParaSite client — organism-specific nucleotide source for parasitic
helminths (nematodes and flatworms), which NCBI's Gene database often covers
poorly.

Uses the standard Ensembl-style REST API (https://parasite.wormbase.org/rest):
  1. `/info/genomes/taxonomy/{taxon}` — resolve the organism to a production name
     (species + BioProject, e.g. `brugia_malayi_prjna10729`).
  2. `/lookup/symbol/{species}/{symbol}` (or `/xrefs/symbol/...`) — gene stable ID.
  3. `/sequence/id/{id}?type=genomic` — the nucleotide sequence.

Every call soft-fails (returns None) so a WormBase outage or an unresolved gene
never breaks a search — the gene is simply shown without a WBPS sequence.

NOTE: this integration was written while the WormBase ParaSite service was
returning HTTP 500 for all requests, so it could not be verified end-to-end; it
follows the documented REST contract and should work once the service recovers.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

REST = os.environ.get("WBPS_REST", "https://parasite.wormbase.org/rest")
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

# Genera WormBase ParaSite covers (helminths). Used to route only relevant
# organisms here; extend freely.
HELMINTH_GENERA = {
    "brugia", "wuchereria", "onchocerca", "loa", "schistosoma", "ascaris",
    "trichuris", "necator", "ancylostoma", "strongyloides", "haemonchus",
    "echinococcus", "taenia", "fasciola", "trichinella", "dirofilaria",
    "toxocara", "enterobius", "clonorchis", "opisthorchis", "dracunculus",
    "hymenolepis", "heligmosomoides", "nippostrongylus", "teladorsagia",
    "ostertagia", "trichostrongylus", "cooperia", "angiostrongylus",
}


def is_helminth(organism: str) -> bool:
    genus = (organism or "").strip().lower().split(" ")[0]
    return genus in HELMINTH_GENERA


class WormBaseParasiteClient:
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
        self, *, symbol: str, name: str, organism: str
    ) -> Optional[dict[str, Any]]:
        if not organism.strip() or not (symbol or name):
            return None
        key = f"{symbol}|{organism}".lower()
        if self._cache is not None:
            cached = await self._cache.get("wbps", key)
            if cached is not None:
                return cached or None

        result = await self._resolve(symbol, organism)

        if self._cache is not None:
            await self._cache.set("wbps", key, result or "")
        return result

    async def _resolve(self, symbol: str, organism: str) -> Optional[dict[str, Any]]:
        species = await self._production_name(organism)
        if not species:
            return None
        gene_id = await self._gene_id(species, symbol)
        if not gene_id:
            return None
        seq = await self._sequence(gene_id)
        if not seq:
            return None
        return {
            "accession": gene_id,
            "region": "",
            "strand": "",
            "length": len(seq),
            "header": f"{gene_id} {symbol} [{organism}]".strip(),
            "sequence": seq.upper(),
            "url": f"https://parasite.wormbase.org/Gene/Summary?g={quote(gene_id)}",
            "source": f"WormBase ParaSite ({species})",
            "database": "WormBase ParaSite",
        }

    async def _production_name(self, organism: str) -> Optional[str]:
        genus = organism.strip().split(" ")[0]
        data = await self._get(f"{REST}/info/genomes/taxonomy/{quote(genus)}")
        if not isinstance(data, list) or not data:
            return None
        wanted = organism.strip().lower()
        best = None
        for g in data:
            name = g.get("name") or ""
            sci = str(g.get("species") or g.get("scientific_name") or "").lower()
            if sci and (sci in wanted or wanted in sci):
                return name
            best = best or name
        return best

    async def _gene_id(self, species: str, symbol: str) -> Optional[str]:
        if not symbol:
            return None
        hit = await self._get(f"{REST}/lookup/symbol/{quote(species)}/{quote(symbol)}")
        if isinstance(hit, dict) and hit.get("id"):
            return hit["id"]
        xrefs = await self._get(f"{REST}/xrefs/symbol/{quote(species)}/{quote(symbol)}")
        if isinstance(xrefs, list):
            for x in xrefs:
                if x.get("id"):
                    return x["id"]
        return None

    async def _sequence(self, gene_id: str) -> Optional[str]:
        data = await self._get(f"{REST}/sequence/id/{quote(gene_id)}?type=genomic")
        if isinstance(data, dict):
            return data.get("seq")
        if isinstance(data, list) and data:
            return data[0].get("seq")
        return None

    async def _get(self, url: str) -> Any:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}content-type=application/json"
        for attempt in range(2):
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
