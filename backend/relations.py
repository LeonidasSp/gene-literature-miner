"""
PubTator3 relation-extraction API: precomputed gene<->disease and
gene<->chemical relations, mined from the literature by NCBI's BioREx model
(https://www.ncbi.nlm.nih.gov/research/pubtator3-api/relations). Free, no key.

Two relation types cover this well, picked empirically (see tests):
  - disease "associate"        -- the broad catch-all, well populated
  - chemical "negative_correlate" -- in practice the "inhibited/suppressed by"
    signal (for JAK1: ruxolitinib/tofacitinib/baricitinib; for EGFR: gefitinib;
    for BRCA1: olaparib -- real, approved drugs, not noise)

The index is looked up by a bare, case-sensitive symbol string with no species
scoping, so it is queried with the gene's official NCBI symbol (not a
literature-mention alias). Coverage is strong for well-studied human/mammalian
genes and essentially empty for bacterial/archaeal/viral ones -- confirmed by
querying a spread of AMR/biofilm gene symbols (mecA, icaA, oxyR, recA, blaKPC,
vanA, gyrA, katG, sarA, agr) and getting zero results for all of them, while
human genes are richly populated. Genes with nothing here just show no panel;
that is normal, not a failure.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import httpx

RELATIONS_URL = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/relations"
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

MAX_ITEMS = 6          # shown per category
_NUMERIC_ID = re.compile(r"^\d+$")  # a raw OMIM number with no mapped name


class RelationsClient:
    def __init__(self, cache=None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
            follow_redirects=True,
        )
        self._cache = cache
        # Kept low deliberately: bursting this endpoint (e.g. 4+ genes enriching
        # at once, 2 queries each) drew transient failures in testing, the same
        # way OrthoDB does above 2 concurrent requests (see orthodb.py).
        self._sem = asyncio.Semaphore(2)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def for_gene(self, symbol: str) -> dict[str, list[dict[str, Any]]]:
        """{"diseases": [...], "chemicals": [...]} for one official gene symbol.

        Each item is {"name": str, "publications": int}, sorted by publication
        count (most literature support first) and capped at MAX_ITEMS. Both
        lists are empty when PubTator3 has no relations for this exact symbol.
        """
        if not symbol:
            return {"diseases": [], "chemicals": []}
        diseases, chemicals = await asyncio.gather(
            self._query(symbol, "associate", "disease"),
            self._query(symbol, "negative_correlate", "chemical"),
        )
        return {"diseases": diseases[:MAX_ITEMS], "chemicals": chemicals[:MAX_ITEMS]}

    async def _query(self, symbol: str, type_: str, e2: str) -> list[dict[str, Any]]:
        cache_key = f"{symbol}|{type_}|{e2}"
        if self._cache is not None:
            cached = await self._cache.get("relations", cache_key)
            if cached is not None:
                return cached
        prefix = "@DISEASE_" if e2 == "disease" else "@CHEMICAL_"
        params = {"e1": f"@GENE_{symbol}", "type": type_, "e2": e2}
        async with self._sem:
            resp = await self._get(params)
        if resp is None:
            return []  # a transient outage is not "this gene has no data": never cache it
        try:
            raw = resp.json()
        except ValueError:
            return []  # an unreadable 200 (e.g. an HTML rate-limit page) is the same story
        out: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            other = item.get("source") if str(item.get("source", "")).startswith(prefix) \
                else item.get("target")
            if not isinstance(other, str) or not other.startswith(prefix):
                continue
            name = other[len(prefix):].replace("_", " ").strip()
            if not name or _NUMERIC_ID.match(name):  # unmapped OMIM code -- not presentable
                continue
            out.append({"name": name, "publications": item.get("publications") or 0})
        out.sort(key=lambda x: -x["publications"])
        if self._cache is not None:
            await self._cache.set("relations", cache_key, out)
        return out

    async def _get(self, params: dict[str, str]) -> Optional[httpx.Response]:
        for attempt in range(4):
            try:
                resp = await self._client.get(RELATIONS_URL, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                if resp.status_code in (400, 404):
                    return None
                if resp.status_code >= 500:
                    await asyncio.sleep(1.5 + attempt * 1.5)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError:
                await asyncio.sleep(0.4 * (attempt + 1))
        return None
