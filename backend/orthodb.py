"""
OrthoDB client — ortholog-group homologues (an alternative to UniRef).

UniRef groups by sequence identity; OrthoDB groups by evolutionary orthology,
which is usually what a biologist means by "homologues across species". We find
the ortholog group by name (optionally scoped to a taxonomic level) and return
its member genes, one entry per record with organism + gene id + description.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from typing import Any, Optional

import httpx

ORTHODB = "https://data.orthodb.org/current"
CONTACT_EMAIL = os.environ.get("NCBI_EMAIL", "le.spathis@gmail.com")
TOOL_NAME = "gene-literature-miner"

# A few common bacterial levels so name searches can be scoped when the organism
# is recognised (OrthoDB "level" = an NCBI taxid). Falls back to unscoped search.
_LEVEL_HINTS = {
    "staphylococcus": 1279,
    "streptococcus": 1301,
    "escherichia": 561,
    "pseudomonas": 286,
    "bacillus": 1386,
    "mycobacterium": 1763,
    "salmonella": 590,
    "klebsiella": 570,
    "acinetobacter": 469,
    "enterococcus": 1350,
    "clostridium": 1485,
}


class OrthoDBClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def orthologs(
        self, *, name: str, organism: str, limit: int = 25
    ) -> dict[str, Any]:
        """
        Find the best ortholog group for a gene/protein name and return members.

        Returns {"group": id, "group_name": ..., "count": N, "homologs": [...]}
        with each homologue: gene_id, organism, description.
        """
        if not name.strip():
            return {"group": None, "homologs": []}
        level = _level_for(organism)
        group = await self._find_group(name.strip(), level)
        if not group:
            return {"group": None, "homologs": []}
        members = await self._group_members(group["id"], limit=limit)
        return {
            "group": group["id"],
            "group_name": group.get("name", ""),
            "group_url": f"https://www.orthodb.org/?query={group['id']}",
            "level_name": group.get("level_name", ""),
            "count": len(members),
            "homologs": members[:limit],
        }

    async def _find_group(
        self, name: str, level: Optional[int]
    ) -> Optional[dict[str, Any]]:
        params: dict[str, Any] = {"query": name, "limit": 5}
        if level:
            params["level"] = level
        resp = await self._get(f"{ORTHODB}/search", params)
        if resp is None and level:  # retry unscoped
            resp = await self._get(f"{ORTHODB}/search", {"query": name, "limit": 5})
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        rows = data.get("bigdata") or []
        if not rows:
            ids = data.get("data") or []
            return {"id": ids[0]} if ids else None
        # Prefer the group with the most genes (broadest coverage).
        best = max(rows, key=lambda r: _to_int(r.get("gene_count")))
        return {
            "id": best.get("id"),
            "name": best.get("name", ""),
            "level_name": best.get("level_name", ""),
        }

    async def _group_members(self, group_id: str, *, limit: int) -> list[dict[str, Any]]:
        resp = await self._get(
            f"{ORTHODB}/tab", {"id": group_id, "limit": min(limit + 10, 100)}
        )
        if resp is None:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        reader = csv.DictReader(io.StringIO(resp.text), delimiter="\t")
        for row in reader:
            gid = (row.get("pub_gene_id") or "").strip()
            org = (row.get("organism_name") or "").strip()
            key = f"{org}|{gid}"
            if not gid or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "gene_id": gid,
                    "organism": org,
                    "description": (row.get("description") or "").strip(),
                }
            )
        return out

    async def _get(self, url: str, params: dict) -> Optional[httpx.Response]:
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError:
                await asyncio.sleep(0.5 * (attempt + 1))
        return None


def _level_for(organism: str) -> Optional[int]:
    genus = (organism or "").strip().lower().split(" ")[0]
    return _LEVEL_HINTS.get(genus)


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
