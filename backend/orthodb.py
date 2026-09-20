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


# OrthoDB "level" (an NCBI taxid) at which orthologous groups are compared, by
# domain of life. Broad levels put every organism of the domain in the same group
# space, which is what a cross-organism comparison needs.
DOMAIN_LEVELS = {"bacteria": 2, "archaea": 2157, "eukaryote": 2759}


class OrthoDBUnavailable(RuntimeError):
    """OrthoDB did not answer (as opposed to answering 'nothing found')."""


class OrthoDBClient:
    def __init__(self, cache: Any = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME} (mailto:{CONTACT_EMAIL})"},
            follow_redirects=True,
        )
        self._cache = cache

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------- exact, accession-based lookups
    async def group_for_accession(self, accession: str, level: int) -> Optional[dict[str, Any]]:
        """The ortholog group a UniProt protein belongs to at `level`, or None.

        Unlike a name search (which returns several fuzzy candidate groups), a
        UniProt accession names one protein, so its group is exact. Raises
        OrthoDBUnavailable if OrthoDB could not be reached, so a transient outage is
        never mistaken for "this protein has no group".
        """
        acc = (accession or "").strip()
        if not acc:
            return None
        key = f"{level}:{acc}"
        if self._cache is not None:
            cached = await self._cache.get("odb_group", key)
            if cached is not None:
                return cached or None
        resp = await self._get(f"{ORTHODB}/search", {"query": acc, "level": level, "limit": 5})
        if resp is None:
            raise OrthoDBUnavailable("OrthoDB did not respond")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OrthoDBUnavailable("OrthoDB returned an unreadable answer") from exc
        rows = data.get("bigdata") or []
        ids = data.get("data") or []
        group: Optional[dict[str, Any]] = None
        if rows and rows[0].get("id"):
            r = rows[0]
            group = {"id": r["id"], "name": r.get("name", ""), "level_name": r.get("level_name", "")}
        elif ids:
            group = {"id": ids[0], "name": "", "level_name": ""}
        if self._cache is not None:
            await self._cache.set("odb_group", key, group or "")
        return group

    async def members_in_species(self, group_id: str, taxid: int | str) -> dict[str, Any]:
        """Does an organism (species-level NCBI taxid; strains are included) have
        members of this ortholog group? Returns {"count": n, "example": {...}}.
        Raises OrthoDBUnavailable if OrthoDB could not be reached."""
        key = f"{group_id}:{taxid}"
        if self._cache is not None:
            cached = await self._cache.get("odb_members", key)
            if cached is not None:
                return cached
        resp = await self._get(f"{ORTHODB}/orthologs", {"id": group_id, "species": str(taxid)})
        if resp is None:
            raise OrthoDBUnavailable("OrthoDB did not respond")
        try:
            records = resp.json().get("data") or []
        except ValueError as exc:
            raise OrthoDBUnavailable("OrthoDB returned an unreadable answer") from exc
        result = _summarise_members(records)
        if self._cache is not None:
            await self._cache.set("odb_members", key, result)
        return result

    async def orthologs(
        self, *, name: str, organism: str, limit: int = 25,
        accession: str = "", level: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Find the ortholog group for a gene/protein and return its members.

        With a UniProt `accession` (and a domain `level`) the group is looked up
        exactly; otherwise -- or if that finds nothing -- the group is found by
        name, taking the broadest match.

        Returns {"group": id, "group_name": ..., "count": N, "homologs": [...]}
        with each homologue: gene_id, organism, description.
        """
        group = None
        if accession.strip() and level:
            try:
                group = await self.group_for_accession(accession, level)
            except OrthoDBUnavailable:
                group = None
        if not group:
            if not name.strip():
                return {"group": None, "homologs": []}
            group = await self._find_group(name.strip(), _level_for(organism))
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
        # See ncbi.py's _get: a 5xx gets a longer backoff than a genuine
        # client error or a dropped connection, since it's usually a brief,
        # self-clearing blip rather than a real failure.
        for attempt in range(5):
            try:
                resp = await self._client.get(url, params=params)
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


def _summarise_members(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Boil OrthoDB's per-species records down to {count, example}.

    Each record is one genome (a strain) with its member genes; `count` is the
    total number of member genes across them, `example` the first one.
    """
    count = 0
    example: Optional[dict[str, str]] = None
    for rec in records or []:
        genes = rec.get("genes") or []
        count += len(genes)
        if genes and example is None:
            g = genes[0]
            gid = g.get("gene_id")
            example = {
                "gene_id": str((gid.get("id") if isinstance(gid, dict) else gid) or ""),
                "description": str(g.get("description") or ""),
                "organism": str((rec.get("organism") or {}).get("name") or ""),
            }
    return {"count": count, "example": example}
