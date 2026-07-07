"""
Europe PMC client — an alternative/additional literature source.

Unlike Google Scholar/Scopus, Europe PMC has a genuine open REST API (no key)
and covers preprints and full text, not just PubMed abstracts. We use it purely
to widen the set of PMIDs; gene extraction still flows through the existing
PubTator3 path, so only articles with a PMID are useful here.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"


class EuropePMCClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_pmids(self, term: str, retmax: int = 50) -> list[str]:
        """
        Return PMIDs for a query. Restricts to MED (records that have a PMID),
        so results plug straight into the PubTator pipeline.
        """
        pmids: list[str] = []
        cursor = "*"
        page = min(retmax, 100)
        query = f"({term}) AND SRC:MED"
        while len(pmids) < retmax:
            params = {
                "query": query,
                "format": "json",
                "resultType": "idlist",
                "pageSize": str(page),
                "cursorMark": cursor,
            }
            resp = await self._get(params)
            if resp is None:
                break
            try:
                data = resp.json()
            except ValueError:
                break
            results = data.get("resultList", {}).get("result", [])
            if not results:
                break
            for r in results:
                pmid = r.get("pmid")
                if pmid and pmid not in pmids:
                    pmids.append(pmid)
            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return pmids[:retmax]

    async def _get(self, params: dict) -> Optional[httpx.Response]:
        for attempt in range(3):
            try:
                resp = await self._client.get(EUROPEPMC, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError:
                await asyncio.sleep(0.4 * (attempt + 1))
        return None
