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
import re
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bvbrc import BVBRCClient
from cache import Cache
from ensembl import EnsemblClient
from europepmc import EuropePMCClient
from ncbi import NCBIClient, is_locus_tag
from orthodb import OrthoDBClient
from uniprot import UniProtClient
import veupathdb
import wormbase
from wormbase import is_helminth

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Public-deployment guards: keep one user from getting the server's IP blocked by
# NCBI. Tunable via env vars.
MAX_CONCURRENT_SEARCHES = int(os.environ.get("MAX_CONCURRENT_SEARCHES", "2"))
PER_IP_MIN_INTERVAL = float(os.environ.get("PER_IP_MIN_INTERVAL", "3.0"))  # seconds

client: NCBIClient
uniprot: UniProtClient
europepmc: EuropePMCClient
orthodb: OrthoDBClient
bvbrc: BVBRCClient
ensembl: EnsemblClient
cache: Cache
_search_gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_last_seen: dict[str, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, uniprot, europepmc, orthodb, bvbrc, ensembl, cache
    cache = Cache()
    client = NCBIClient()
    uniprot = UniProtClient(cache=cache)
    europepmc = EuropePMCClient()
    orthodb = OrthoDBClient()
    bvbrc = BVBRCClient(cache=cache)
    ensembl = EnsemblClient(cache=cache)
    try:
        yield
    finally:
        await client.aclose()
        await uniprot.aclose()
        await europepmc.aclose()
        await orthodb.aclose()
        await bvbrc.aclose()
        await ensembl.aclose()
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
    full_text: bool = Field(True, description="Mine full text of open-access papers, not just abstracts")


def _build_term(query: str, organism: str) -> str:
    term = query.strip()
    if organism.strip():
        term = f"({term}) AND \"{organism.strip()}\"[Organism]"
    return term


_ORG_SPLIT = re.compile(r"[\s.]+")


def _parse_org(name: str) -> tuple[str, str]:
    """(genus, species-epithet) from an organism string, tolerating abbreviations:
    'E. coli', 'e.coli', 'E coli' all -> ('e', 'coli')."""
    parts = [p for p in _ORG_SPLIT.split((name or "").strip().lower()) if p]
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _organism_match_level(scientific: str, wanted: str) -> int:
    """
    How well an NCBI Gene record's organism matches the requested organism:
      2 = exact species / strain / genus-only-query hit
      1 = same genus, different species
      0 = no match

    Tolerant of abbreviated binomials ('E. coli', 'B. pahangi') and genus-only
    queries ('Brugia'). The caller keeps tier-1 (genus) matches only when no
    tier-2 (exact-species) match exists, so precision is preserved when the
    species actually is present (asking 'E. coli' won't drag in E. albertii),
    while a species with no database entry still falls back to its genus
    (asking 'B. pahangi' surfaces the B. malayi genes that do exist).
    """
    w = (wanted or "").strip().lower()
    if not w:
        return 2
    s = (scientific or "").strip().lower()
    if not s:
        return 0
    if w == s or w in s:  # exact, or a strain whose name contains the wanted one
        return 2
    wg, we = _parse_org(w)
    sg, se = _parse_org(s)
    if not wg or not sg:
        return 0
    genus_match = (
        wg == sg
        or (len(wg) == 1 and sg.startswith(wg))   # 'e' abbreviates 'escherichia'
        or (len(sg) == 1 and wg.startswith(sg))
    )
    if not genus_match:
        return 0
    if not we:            # genus-only query -> any same-genus gene is a full hit
        return 2
    return 2 if we == se else 1


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
) -> tuple[list[dict[str, Any]], str, int, Optional[str], Optional[str]]:
    """Steps 1-3: literature -> PubTator -> Gene-db metadata. No enrichment yet.

    Returns (genes, pubmed_term, paper_count, message, note). `note` is an
    informational aside (organism normalised, or genus-level fallback used).
    """
    note: Optional[str] = None
    # Normalise the organism up front. When NCBI Taxonomy recognises it we use the
    # canonical name for both the literature search and the gene filter ('E. coli'
    # -> 'Escherichia coli'); when it doesn't, we keep the raw text, which PubMed's
    # [Organism] tag still maps for abbreviated binomials like 'B. pahangi'.
    if req.organism.strip():
        resolved = await client.resolve_organism(req.organism)
        if resolved and resolved.get("name"):
            if resolved["name"].lower() != req.organism.strip().lower():
                note = f"organism '{req.organism.strip()}' matched as {resolved['name']}"
            req.organism = resolved["name"]

    term = _build_term(req.query, req.organism)
    pmids = await _collect_pmids(req)
    if not pmids:
        return [], term, 0, "No articles matched this query.", note

    raw_genes = await client.genes_from_pubtator(pmids, full_text=req.full_text)
    if not raw_genes:
        return [], term, len(pmids), (
            "Articles found, but PubTator returned no gene annotations."
        ), note

    candidate_ids = [
        gid for gid, e in raw_genes.items() if e["count"] >= req.min_mentions
    ]
    summaries = await client.gene_summaries(candidate_ids)

    scored: list[tuple[int, dict[str, Any]]] = []
    for gid in candidate_ids:
        summ = summaries.get(gid)
        if not summ:
            continue
        organism = (summ.get("organism") or {}).get("scientificname", "")
        level = _organism_match_level(organism, req.organism)
        if level == 0:
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
        scored.append((level, {
            "gene_id": gid,
            "symbol": symbol,
            "ncbi_name": ncbi_name,
            "aliases": sorted(entry["mentions"], key=lambda k: -entry["mentions"][k])[:5],
            "description": description,
            "organism": organism,
            "taxid": str((summ.get("organism") or {}).get("taxid") or ""),
            "mention_count": entry["count"],
            "paper_count": len(entry["pmids"]),
            "pmids": sorted(entry["pmids"], key=int, reverse=True),
            "gene_url": f"https://www.ncbi.nlm.nih.gov/gene/{gid}",
            "sequence": None,
            "protein": None,
            "reason": None,
            "_summary": summ,
            "_candidates": candidate_symbols,
        }))

    # Two-pass: prefer exact-species/strain hits; fall back to same-genus genes
    # only when the exact species isn't present in the databases.
    if any(lvl == 2 for lvl, _ in scored):
        genes = [g for lvl, g in scored if lvl == 2]
    else:
        genes = [g for _, g in scored]
        if genes and req.organism.strip():
            fallback = "no exact-species match; showing same-genus genes"
            note = f"{note}; {fallback}" if note else fallback

    genes.sort(key=lambda g: (g["mention_count"], g["paper_count"]), reverse=True)
    return genes, term, len(pmids), None, note


def _public_gene(g: dict[str, Any]) -> dict[str, Any]:
    """Copy without internal fields, for sending to the client."""
    return {k: v for k, v in g.items() if not k.startswith("_")}


async def _enrich_gene(g: dict[str, Any], req: SearchRequest) -> dict[str, Any]:
    """Fetch nucleotide sequence + UniProt protein for one gene, concurrently."""

    async def _seq() -> Optional[dict[str, Any]]:
        try:
            seq = await client.resolve_sequence(
                summary=g["_summary"],
                candidate_symbols=g["_candidates"],
                organism=req.organism,
            )
        except Exception:
            seq = None
        if seq:
            return seq
        # NCBI came up empty — try an organism-specific database.
        return await _fallback_sequence(g, req)

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


async def _fallback_sequence(
    g: dict[str, Any], req: SearchRequest
) -> Optional[dict[str, Any]]:
    """
    Try the most authoritative organism-specific nucleotide database when NCBI
    has nothing, routed by the organism's taxonomic lineage / genus:

      bacteria / viruses / archaea  -> BV-BRC
      parasitic helminths           -> WormBase ParaSite  (via Ensembl mirror)
      protozoan parasites / vectors -> VEuPathDB component (via Ensembl mirror)
      other eukaryotes              -> Ensembl (plants=TAIR, fungi=SGD,
                                        insects=FlyBase, vertebrates, protists)

    WormBase ParaSite and VEuPathDB sequences are retrieved through Ensembl's
    reliable REST (which mirrors those assemblies and native gene IDs) but
    labelled and linked to the source database of record.
    """
    organism = g.get("organism") or req.organism
    if not organism.strip():
        return None
    gene = g.get("ncbi_name") or g.get("symbol") or ""
    if is_locus_tag(gene):  # a locus tag is a poor cross-database key
        gene = g.get("symbol") or ""
    symbol = g.get("symbol") or gene
    name = g.get("description") or g.get("symbol") or ""

    try:
        lineage = await client.lineage(g.get("taxid", ""))
    except Exception:
        lineage = ""
    group = _classify(lineage, organism)

    async def _try(coro):
        try:
            return await coro
        except Exception:
            return None

    if group in ("bacteria", "virus", "archaea"):
        return await _try(bvbrc.fetch_sequence(gene=gene, name=name, organism=organism))

    if group == "helminth":
        return await _try(ensembl.fetch_sequence(
            symbol=symbol, name=name, organism=organism,
            database=wormbase.LABEL, url_builder=wormbase.gene_url,
        ))

    veupath = veupathdb.component_for(organism)
    if veupath:
        label, url_builder = veupath
        return await _try(ensembl.fetch_sequence(
            symbol=symbol, name=name, organism=organism,
            database=label, url_builder=url_builder,
        ))

    # Any other eukaryote (plant, fungus, vertebrate, insect, …).
    return await _try(ensembl.fetch_sequence(symbol=symbol, name=name, organism=organism))


def _classify(lineage: str, organism: str) -> str:
    """Map an NCBI lineage string to a source-routing group."""
    lin = (lineage or "").lower()
    org = (organism or "").lower()
    if "viruses" in lin:
        return "virus"
    if "bacteria" in lin:
        return "bacteria"
    if "archaea" in lin:
        return "archaea"
    if "nematoda" in lin or "platyhelminthes" in lin or is_helminth(org):
        return "helminth"
    return "eukaryote"


def _reason(seq: Optional[dict], protein: Optional[dict]) -> Optional[str]:
    """Explain a missing sequence/protein rather than showing a bare blank."""
    notes = []
    if not seq:
        notes.append(
            "no nucleotide sequence (not found in NCBI or the organism-specific "
            "databases, usually a naming/vocabulary mismatch)"
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
        genes, term, paper_count, message, note = await _collect_genes(req)
        if not genes:
            return {
                "query": req.query, "organism": req.organism, "pubmed_term": term,
                "paper_count": paper_count, "genes": [], "message": message,
                "note": note,
            }
        to_fetch = genes[: req.max_genes_with_sequence]
        await asyncio.gather(*(_enrich_gene(g, req) for g in to_fetch))
        return {
            "query": req.query, "organism": req.organism, "pubmed_term": term,
            "paper_count": paper_count, "gene_count": len(genes),
            "genes": [_public_gene(g) for g in genes], "note": note,
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
    full_text: bool = True,
) -> StreamingResponse:
    """Server-Sent Events: emit gene metadata first, then enrich row-by-row."""
    req = SearchRequest(
        query=query, organism=organism, max_papers=max(1, min(max_papers, 200)),
        min_mentions=max(1, min_mentions),
        max_genes_with_sequence=max(1, min(max_genes_with_sequence, 100)),
        source=source, full_text=full_text,
    )

    async def gen():
        async with _search_gate:
            try:
                genes, term, paper_count, message, note = await _collect_genes(req)
                yield _sse("meta", {
                    "query": req.query, "organism": req.organism,
                    "pubmed_term": term, "paper_count": paper_count,
                    "gene_count": len(genes), "source": req.source,
                    "note": note,
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
    name: str = "",
    organism: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    """Cross-species orthologues from OrthoDB (ortholog group for a gene name)."""
    limit = max(1, min(limit, 50))
    try:
        result = await orthodb.orthologs(name=name, organism=organism, limit=limit)
    except Exception:
        return {"homologs": [], "message": "Orthologue lookup failed.", "source": "orthodb"}
    result["source"] = "orthodb"
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
        loc = (
            f"{s.get('accession')}:{s.get('region')}({s.get('strand')})"
            if s.get("region") else f"{s.get('accession')}"
        )
        parts.append(
            f">{g.get('symbol')}|GeneID:{g.get('gene_id')}|{g.get('organism')}"
            f"|{loc}|{s.get('database', 'NCBI')}"
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
        "seq_database", "seq_accession", "seq_region", "seq_strand", "seq_length_bp",
        "uniprot_accession", "uniprot_reviewed", "protein_name", "protein_length_aa",
        "gene_url", "pmids",
    ])
    for g in genes:
        s = g.get("sequence") or {}
        p = g.get("protein") or {}
        w.writerow([
            g.get("symbol"), g.get("gene_id"), g.get("organism"), g.get("description"),
            g.get("mention_count"), g.get("paper_count"),
            s.get("database", ""), s.get("accession", ""), s.get("region", ""),
            s.get("strand", ""), s.get("length", ""),
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
