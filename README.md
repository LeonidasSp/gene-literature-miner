---
title: Gene Literature Miner
emoji: 🧬
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 🧬 Gene Literature Miner

Mine the scientific literature for the genes discussed on a topic, then pull each
gene's **nucleotide sequence**, **protein sequence**, **functional annotation**,
and **cross-species homologues** — all from open databases, no API key required.

**Example:** search *biofilm formation* in *Staphylococcus aureus* → the tool
finds the genes discussed in the literature (e.g. `icaA`, `sarA`, `agr`, `hla`…),
links each to its NCBI Gene page, and streams in the DNA/protein sequences,
GO/Pfam/KEGG annotation, and homologues across related species.

## How it works

| Step | Source | What it does |
|------|--------|--------------|
| 1. Literature search | **PubMed** and/or **Europe PMC** | Finds articles matching your topic (+ optional organism). |
| 2. Gene extraction | **PubTator3** (NCBI) | Reads the abstracts and maps gene mentions to NCBI Gene IDs. |
| 3. Metadata + filter | **NCBI Gene** (`esummary`) | Adds official symbol/description/organism; drops off-organism hits. |
| 4. Nucleotide | **NCBI Nucleotide** (`efetch`) | Fetches the exact gene region on the correct strand as FASTA. |
| 5. Protein + annotation | **UniProt** | Amino-acid sequence + EC / keywords / GO / Pfam / KEGG. |
| 6. Homologues (on demand) | **UniRef** or **OrthoDB** | Cross-species homologues by sequence cluster or ortholog group. |

Results **stream in gene-by-gene**, so the table appears immediately and fills as
each gene resolves. Every data source is free and needs no key.

## Run it

### Docker (recommended, no Python needed)

```bash
cd gene_literature_miner
docker compose up -d --build   # then open http://localhost:8000
docker compose down            # stop
```

### Local Python (3.10+)

```bash
cd gene_literature_miner/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Usage

Enter a **topic** (e.g. `biofilm formation`), optionally an **organism**
(`Staphylococcus aureus`), and hit **Search**.

| Control | Description |
|---------|-------------|
| Topic / query | Free-text search term. |
| Organism | Optional filter — keeps only genes whose NCBI record matches the species. |
| Literature source | `PubMed`, `Europe PMC`, or `Both` (Europe PMC adds preprints + full text). |
| Max papers | How many articles to scan (higher surfaces more genes; try 100–200). |
| Min mentions | Drop genes mentioned fewer than this many times. |
| Homologues | Choose `UniRef50/90/100` (sequence identity) or `OrthoDB` (orthology). |

## Output

A sortable table of genes with the literature aliases, PMIDs, nucleotide and
protein sequences, a **Function** column (EC / family / keywords / Pfam / GO /
KEGG), and a **Find homologues** expander per gene. Export buttons:

- **Nucleotide FASTA** / **Protein FASTA**
- **CSV** (gene + sequence + protein summary)
- **Download all (zip)** — both FASTA files, the CSV, and an `annotations.csv`

## Configuration

All optional, via environment variables:

| Variable | Purpose |
|----------|---------|
| `NCBI_API_KEY` | Raises the NCBI rate limit from 3 → 10 req/s (get a free key at NCBI). |
| `NCBI_EMAIL` | Contact address sent with NCBI requests (etiquette). |
| `PER_IP_MIN_INTERVAL`, `MAX_CONCURRENT_SEARCHES` | Abuse guards for public hosting. |
| `UNIREF_IDENTITY`, `CACHE_PATH`, `CACHE_TTL` | Default homology cluster and lookup cache. |

## Notes & limits

- **Sequences** come from the gene's genomic coordinates (exact region/strand);
  records without coordinates, or regions over 60 kb, are shown without a
  nucleotide sequence but keep their Gene-page link. A **why?** tooltip explains
  any missing sequence or protein.
- **PubTator coverage** is per-abstract and can be sparse — raise *Max papers*
  to surface more genes.
- **Homologues:** UniRef clusters group by sequence identity; OrthoDB groups by
  evolutionary orthology (often the better cross-species set).
- The literature layer (`backend/ncbi.py`, `backend/europepmc.py`) is isolated,
  so further sources can be added without touching the sequence code.
