"""
Gene Literature Miner — FastAPI backend.

Pipeline for a query like "biofilm Staphylococcus aureus":
  1. Literature search (PubMed and/or Europe PMC) -> PMIDs
  2. PubTator3 gene annotations  -> candidate NCBI Gene IDs + mentions
  3. esummary (Gene db)          -> symbol/description/organism (+ organism filter)
  4. Enrichment (streamed per gene):
       - efetch genomic region   -> nucleotide sequence (FASTA)
       - UniProt                 -> protein sequence + GO/Pfam/KEGG/EC annotations
  5. On demand: homologues (UniRef cluster or OrthoDB ortholog group)

Results stream to the browser gene-by-gene over Server-Sent Events so the table
appears immediately; a non-streaming POST /api/search is kept for exports and
programmatic use.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cache import Cache
from europepmc import EuropePMCClient
from ncbi import NCBIClient, is_locus_tag
from orthodb import OrthoDBClient
from uniprot import UniProtClient

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Public-deployment guards: keep one user from getting the server's IP blocked by
# NCBI. Tunable via env vars.
MAX_CONCURRENT_SEARCHES = int(os.environ.get("MAX_CONCURRENT_SEARCHES", "2"))
PER_IP_MIN_INTERVAL = float(os.environ.get("PER_IP_MIN_INTERVAL", "3.0"))  # seconds

client: NCBIClient
uniprot: UniProtClient
europepmc: EuropePMCClient
orthodb: OrthoDBClient
cache: Cache
_search_gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_last_seen: dict[str, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, uniprot, europepmc, orthodb, cache
    cache = Cache()
    client = NCBIClient()
    uniprot = UniProtClient(cache=cache)
    europepmc = EuropePMCClient()
    orthodb = OrthoDBClient()
    try:
        yield
    finally:
        await client.aclose()
        await uniprot.aclose()
        await europepmc.aclose()
        await orthodb.aclose()
        cache.close()


app = FastAPI(title="Gene Literature Miner", lifespan=lifespan)


@app.middleware("http")
async def throttle(request: Request, call_next):
    """Simple per-IP throttle on the search endpoints."""
    if request.url.path.startswith("/api/search"):
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
    source: str = Field("pubmed", description="Literature source: pubmed | europepmc | both")


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


async def _collect_pmids(req: SearchRequest) -> list[str]:
    """Gather PMIDs from the selected literature source(s)."""
    term = _build_term(req.query, req.organism)
    src = (req.source or "pubmed").lower()
    tasks = []
    if src in ("pubmed", "both"):
        tasks.append(client.search_pubmed(term, retmax=req.max_papers))
    if src in ("europepmc", "both"):
        epmc_q = req.query.strip()
        if req.organism.strip():
            epmc_q = f'({epmc_q}) AND "{req.organism.strip()}"'
        tasks.append(europepmc.search_pmids(epmc_q, retmax=req.max_papers))
    if not tasks:  # unknown source -> default to PubMed
        tasks.append(client.search_pubmed(term, retmax=req.max_papers))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged: list[str] = []
    seen: set[str] = set()
    for r in results:
        if isinstance(r, Exception):
            continue
        for pmid in r:
            if pmid not in seen:
                seen.add(pmid)
                merged.append(pmid)
    return merged[: req.max_papers * (2 if src == "both" else 1)]


async def _collect_genes(
    req: SearchRequest,
) -> tuple[list[dict[str, Any]], str, int, Optional[str]]:
    """Steps 1-3: literature -> PubTator -> Gene-db metadata. No enrichment yet."""
    term = _build_term(req.query, req.organism)
    pmids = await _collect_pmids(req)
    if not pmids:
        return [], term, 0, "No articles matched this query."

    raw_genes = await client.genes_from_pubtator(pmids)
    if not raw_genes:
        return [], term, len(pmids), (
            "Articles found, but PubTator returned no gene annotations."
        )

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
        symbol = ncbi_name if (ncbi_name and not is_locus_tag(ncbi_name)) else (
            lit_term or ncbi_name or gid
        )
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
                "description": description,
                "organism": organism,
                "mention_count": entry["count"],
                "paper_count": len(entry["pmids"]),
                "pmids": sorted(entry["pmids"], key=int, reverse=True),
                "gene_url": f"https://www.ncbi.nlm.nih.gov/gene/{gid}",
                "sequence": None,
                "protein": None,
                "reason": None,
                "_summary": summ,
                "_candidates": candidate_symbols,
            }
        )

    genes.sort(key=lambda g: (g["mention_count"], g["paper_count"]), reverse=True)
    return genes, term, len(pmids), None


def _public_gene(g: dict[str, Any]) -> dict[str, Any]:
    """Copy without internal fields, for sending to the client."""
    return {k: v for k, v in g.items() if not k.startswith("_")}


async def _enrich_gene(g: dict[str, Any], req: SearchRequest) -> dict[str, Any]:
    """Fetch nucleotide sequence + UniProt protein for one gene, concurrently."""

    async def _seq() -> Optional[dict[str, Any]]:
        try:
            return await client.resolve_sequence(
                summary=g["_summary"],
                candidate_symbols=g["_candidates"],
                organism=req.organism,
            )
        except Exception:
            return None

    async def _prot() -> Optional[dict[str, Any]]:
        try:
            return await uniprot.protein_for_gene(
                gene_id=g["gene_id"],
                candidates=g.get("_candidates", []),
                organism=g.get("organism") or req.organism,
            )
        except Exception:
            return None

    seq, protein = await asyncio.gather(_seq(), _prot())
    g["sequence"] = seq
    g["protein"] = protein
    if seq and seq.get("gene_url"):
        g["gene_url"] = seq["gene_url"]
    g["reason"] = _reason(seq, protein)
    return {
        "gene_id": g["gene_id"],
        "sequence": seq,
        "protein": protein,
        "reason": g["reason"],
        "gene_url": g["gene_url"],
    }


def _reason(seq: Optional[dict], protein: Optional[dict]) -> Optional[str]:
    """Explain a missing sequence/protein rather than showing a bare blank."""
    notes = []
    if not seq:
        notes.append(
            "no nucleotide sequence (record lacks genomic coordinates or the "
            "literature name didn't resolve to an NCBI Gene record)"
        )
    if not protein:
        notes.append(
            "no UniProt protein (no GeneID cross-reference and the name/organism "
            "search returned nothing)"
        )
    return "; ".join(notes) if notes else None


def _top_mention(mentions: dict[str, int]) -> str:
    if not mentions:
        return "?"
    return max(mentions, key=lambda k: mentions[k])


# ---------------------------------------------------------------- search (JSON)
@app.post("/api/search")
async def search(req: SearchRequest) -> dict[str, Any]:
    async with _search_gate:
        genes, term, paper_count, message = await _collect_genes(req)
        if not genes:
            return {
                "query": req.query, "organism": req.organism, "pubmed_term": term,
                "paper_count": paper_count, "genes": [], "message": message,
            }
        to_fetch = genes[: req.max_genes_with_sequence]
        await asyncio.gather(*(_enrich_gene(g, req) for g in to_fetch))
        return {
            "query": req.query, "organism": req.organism, "pubmed_term": term,
            "paper_count": paper_count, "gene_count": len(genes),
            "genes": [_public_gene(g) for g in genes],
        }


# --------------------------------------------------------------- search (stream)
def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/search/stream")
async def search_stream(
    query: str,
    organism: str = "",
    max_papers: int = 50,
    min_mentions: int = 1,
    max_genes_with_sequence: int = 30,
    source: str = "pubmed",
) -> StreamingResponse:
    """Server-Sent Events: emit gene metadata first, then enrich row-by-row."""
    req = SearchRequest(
        query=query, organism=organism, max_papers=max(1, min(max_papers, 200)),
        min_mentions=max(1, min_mentions),
        max_genes_with_sequence=max(1, min(max_genes_with_sequence, 100)),
        source=source,
    )

    async def gen():
        async with _search_gate:
            try:
                genes, term, paper_count, message = await _collect_genes(req)
                yield _sse("meta", {
                    "query": req.query, "organism": req.organism,
                    "pubmed_term": term, "paper_count": paper_count,
                    "gene_count": len(genes), "source": req.source,
                })
                if not genes:
                    yield _sse("done", {"message": message, "gene_count": 0})
                    return
                yield _sse("genes", {"genes": [_public_gene(g) for g in genes]})
                to_fetch = genes[: req.max_genes_with_sequence]
                tasks = [asyncio.create_task(_enrich_gene(g, req)) for g in to_fetch]
                for coro in asyncio.as_completed(tasks):
                    payload = await coro
                    yield _sse("enrich", payload)
                yield _sse("done", {"gene_count": len(genes)})
            except Exception as exc:  # surface, don't hang the client
                yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------- homologues
@app.get("/api/homologs")
async def homologs(
    accession: str = "",
    limit: int = 25,
    identity: str = "0.5",
    source: str = "uniref",
    name: str = "",
    organism: str = "",
) -> dict[str, Any]:
    """
    Cross-species homologues.

    source=uniref  -> the protein's UniRef cluster (needs `accession`); `identity`
                      selects UniRef50/90/100 (0.5 / 0.9 / 1.0).
    source=orthodb -> OrthoDB ortholog group (needs `name` + `organism`).
    """
    limit = max(1, min(limit, 50))
    try:
        if source.lower() == "orthodb":
            result = await orthodb.orthologs(name=name, organism=organism, limit=limit)
            result["source"] = "orthodb"
        else:
            result = await uniprot.homologs_for_protein(
                accession.strip(), limit=limit, identity=identity
            )
            result["source"] = "uniref"
    except Exception:
        return {"homologs": [], "message": "Homologue lookup failed.", "source": source}
    result["accession"] = accession
    return result


# ---------------------------------------------------------------------- bundle
class BundleRequest(BaseModel):
    query: str = ""
    organism: str = ""
    genes: list[dict[str, Any]] = []


@app.post("/api/bundle")
async def bundle(req: BundleRequest) -> Response:
    """Zip of nucleotide FASTA + protein FASTA + CSV + annotations for a result set."""
    genes = req.genes or []
    nucl = _nucleotide_fasta(genes)
    prot = _protein_fasta(genes)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _bundle_readme(req))
        if nucl:
            z.writestr("nucleotide.fasta", nucl)
        if prot:
            z.writestr("proteins.fasta", prot)
        z.writestr("genes.csv", _genes_csv(genes))
        z.writestr("annotations.csv", _annotations_csv(genes))
    buf.seek(0)
    fname = _safe_name(req.query or "genes") + "_bundle.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _wrap(seq: str, n: int = 70) -> str:
    return "\n".join(seq[i:i + n] for i in range(0, len(seq), n))


def _nucleotide_fasta(genes: list[dict[str, Any]]) -> str:
    parts = []
    for g in genes:
        s = g.get("sequence")
        if not s:
            continue
        parts.append(
            f">{g.get('symbol')}|GeneID:{g.get('gene_id')}|{g.get('organism')}"
            f"|{s.get('accession')}:{s.get('region')}({s.get('strand')})"
        )
        parts.append(_wrap(s.get("sequence", "")))
    return "\n".join(parts) + ("\n" if parts else "")


def _protein_fasta(genes: list[dict[str, Any]]) -> str:
    parts = []
    for g in genes:
        p = g.get("protein")
        if not p or not p.get("sequence"):
            continue
        parts.append(
            f">{g.get('symbol')}|{p.get('accession')}|{g.get('organism')}|{p.get('name')}"
        )
        parts.append(_wrap(p.get("sequence", "")))
    return "\n".join(parts) + ("\n" if parts else "")


def _genes_csv(genes: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "symbol", "gene_id", "organism", "description", "mentions", "papers",
        "seq_accession", "seq_region", "seq_strand", "seq_length_bp",
        "uniprot_accession", "uniprot_reviewed", "protein_name", "protein_length_aa",
        "gene_url", "pmids",
    ])
    for g in genes:
        s = g.get("sequence") or {}
        p = g.get("protein") or {}
        w.writerow([
            g.get("symbol"), g.get("gene_id"), g.get("organism"), g.get("description"),
            g.get("mention_count"), g.get("paper_count"),
            s.get("accession", ""), s.get("region", ""), s.get("strand", ""), s.get("length", ""),
            p.get("accession", ""), "yes" if p.get("reviewed") else ("no" if p else ""),
            p.get("name", ""), p.get("length", ""),
            g.get("gene_url"), " ".join(g.get("pmids") or []),
        ])
    return out.getvalue()


def _annotations_csv(genes: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "symbol", "uniprot_accession", "ec", "protein_families",
        "keywords", "go_terms", "pfam", "kegg",
    ])
    for g in genes:
        p = g.get("protein") or {}
        a = p.get("annotations") or {}
        w.writerow([
            g.get("symbol"), p.get("accession", ""),
            "; ".join(a.get("ec") or []),
            a.get("families") or "",
            "; ".join(a.get("keywords") or []),
            "; ".join(f"{x.get('id')} {x.get('term')}" for x in a.get("go") or []),
            "; ".join(f"{x.get('id')} {x.get('name')}" for x in a.get("pfam") or []),
            "; ".join(a.get("kegg") or []),
        ])
    return out.getvalue()


def _bundle_readme(req: BundleRequest) -> str:
    return (
        "Gene Literature Miner export\n"
        f"Query: {req.query}\nOrganism: {req.organism}\n"
        f"Genes: {len(req.genes or [])}\n\n"
        "Files:\n"
        "  nucleotide.fasta  - gene nucleotide sequences (NCBI)\n"
        "  proteins.fasta    - protein sequences (UniProt)\n"
        "  genes.csv         - gene/sequence/protein summary table\n"
        "  annotations.csv   - GO / Pfam / KEGG / EC / keyword annotations\n"
    )


def _safe_name(s: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in s).strip()
    return (keep.replace(" ", "_") or "genes")[:40]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve the single-page frontend.
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
