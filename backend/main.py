"""
Gene Literature Miner — FastAPI backend.

Pipeline for a query like "biofilm Staphylococcus aureus":
  1. esearch PubMed              -> PMIDs
  2. PubTator3 gene annotations  -> candidate NCBI Gene IDs + mentions
  3. esummary (Gene db)          -> symbol/description/organism  (+ organism filter)
  4. efetch genomic region       -> nucleotide sequence (FASTA)
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ncbi import NCBIClient, is_locus_tag

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Public-deployment guards: keep one user from getting the server's IP blocked by
# NCBI. Tunable via env vars.
MAX_CONCURRENT_SEARCHES = int(os.environ.get("MAX_CONCURRENT_SEARCHES", "2"))
PER_IP_MIN_INTERVAL = float(os.environ.get("PER_IP_MIN_INTERVAL", "3.0"))  # seconds

client: NCBIClient
_search_gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_last_seen: dict[str, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = NCBIClient()
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="Gene Literature Miner", lifespan=lifespan)


@app.middleware("http")
async def throttle(request: Request, call_next):
    """Simple per-IP throttle on the search endpoint."""
    if request.url.path == "/api/search":
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"))
        now = time.monotonic()
        last = _last_seen.get(ip, 0.0)
        if now - last < PER_IP_MIN_INTERVAL:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests — please wait a few seconds."},
            )
        _last_seen[ip] = now
        if len(_last_seen) > 10000:  # bound memory
            _last_seen.clear()
    return await call_next(request)


class SearchRequest(BaseModel):
    query: str = Field(..., description="Free-text topic, e.g. 'biofilm formation'")
    organism: str = Field("", description="Optional organism filter, e.g. 'Staphylococcus aureus'")
    max_papers: int = Field(50, ge=1, le=200)
    min_mentions: int = Field(1, ge=1, description="Keep genes mentioned at least this many times")
    max_genes_with_sequence: int = Field(30, ge=1, le=100)


def _build_term(query: str, organism: str) -> str:
    term = query.strip()
    if organism.strip():
        term = f"({term}) AND \"{organism.strip()}\"[Organism]"
    return term


def _organism_matches(scientific: str, wanted: str) -> bool:
    if not wanted.strip():
        return True
    scientific = scientific.lower()
    wanted = wanted.lower().strip()
    return wanted in scientific or scientific in wanted


@app.post("/api/search")
async def search(req: SearchRequest) -> dict[str, Any]:
    async with _search_gate:  # cap concurrent NCBI-bound work
        return await _run_search(req)


async def _run_search(req: SearchRequest) -> dict[str, Any]:
    term = _build_term(req.query, req.organism)
    pmids = await client.search_pubmed(term, retmax=req.max_papers)
    if not pmids:
        return {
            "query": req.query,
            "organism": req.organism,
            "pubmed_term": term,
            "paper_count": 0,
            "genes": [],
            "message": "No PubMed articles matched this query.",
        }

    raw_genes = await client.genes_from_pubtator(pmids)
    if not raw_genes:
        return {
            "query": req.query,
            "organism": req.organism,
            "pubmed_term": term,
            "paper_count": len(pmids),
            "genes": [],
            "message": "PubMed articles found, but PubTator returned no gene annotations.",
        }

    # Keep genes above the mention threshold, then look up metadata.
    candidate_ids = [
        gid for gid, e in raw_genes.items() if e["count"] >= req.min_mentions
    ]
    summaries = await client.gene_summaries(candidate_ids)

    genes: list[dict[str, Any]] = []
    for gid in candidate_ids:
        summ = summaries.get(gid)
        if not summ:
            continue
        organism = (summ.get("organism") or {}).get("scientificname", "")
        if not _organism_matches(organism, req.organism):
            continue
        entry = raw_genes[gid]
        ncbi_name = summ.get("nomenclaturesymbol") or summ.get("name") or ""
        lit_term = _top_mention(entry["mentions"])
        # Show the literature term when the NCBI record is only a locus tag.
        if ncbi_name and not is_locus_tag(ncbi_name):
            symbol = ncbi_name
        else:
            symbol = lit_term or ncbi_name or gid
        # Terms to try when re-resolving a sequence, best signal first: a clean
        # official symbol, the literature term, then the NCBI description phrase.
        description = summ.get("description", "")
        candidate_symbols = []
        if ncbi_name and not is_locus_tag(ncbi_name):
            candidate_symbols.append(ncbi_name)
        if lit_term:
            candidate_symbols.append(lit_term)
        if description:
            candidate_symbols.append(description)
        genes.append(
            {
                "gene_id": gid,
                "symbol": symbol,
                "ncbi_name": ncbi_name,
                "aliases": sorted(entry["mentions"], key=lambda k: -entry["mentions"][k])[:5],
                "description": summ.get("description", ""),
                "organism": organism,
                "mention_count": entry["count"],
                "paper_count": len(entry["pmids"]),
                "pmids": sorted(entry["pmids"], key=int, reverse=True),
                "gene_url": f"https://www.ncbi.nlm.nih.gov/gene/{gid}",
                "sequence": None,
                "_summary": summ,
                "_candidates": candidate_symbols,
            }
        )

    # Highest-signal genes first.
    genes.sort(key=lambda g: (g["mention_count"], g["paper_count"]), reverse=True)

    # Fetch sequences for the top N (concurrent but rate-limited inside client).
    to_fetch = genes[: req.max_genes_with_sequence]

    async def _attach(g: dict[str, Any]) -> None:
        try:
            seq = await client.resolve_sequence(
                summary=g["_summary"],
                candidate_symbols=g["_candidates"],
                organism=req.organism,
            )
            g["sequence"] = seq
            # If the sequence came from a re-resolved canonical record, point the
            # gene link there too.
            if seq and seq.get("gene_url"):
                g["gene_url"] = seq["gene_url"]
        except Exception:
            g["sequence"] = None

    await asyncio.gather(*(_attach(g) for g in to_fetch))
    for g in genes:  # strip internal fields
        g.pop("_summary", None)
        g.pop("_candidates", None)

    return {
        "query": req.query,
        "organism": req.organism,
        "pubmed_term": term,
        "paper_count": len(pmids),
        "gene_count": len(genes),
        "genes": genes,
    }


def _top_mention(mentions: dict[str, int]) -> str:
    if not mentions:
        return "?"
    return max(mentions, key=lambda k: mentions[k])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve the single-page frontend.
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
