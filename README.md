# 🧬 Gene Literature Miner

Mine the scientific literature for the genes discussed in a publication, then pull
each gene's **nucleotide sequence**, **protein sequence**, **functional annotation**,
and **cross-species homologues** — all from open databases, no API key required.

**Example:** search *biofilm formation* in *Staphylococcus aureus* → the tool
finds the genes discussed in the literature (e.g. `icaA`, `sarA`, `agr`, `hla`…),
links each to its NCBI Gene page, and streams in the DNA/protein sequences,
GO/Pfam/KEGG annotation, and homologues across related species.

## How it works

| Step | Source | What it does |
|------|--------|--------------|
| 1. Literature search | **PubMed** and/or **Europe PMC** | Finds articles matching your topic (+ optional organism). |
| 2. Gene extraction | **PubTator3** (NCBI) | Annotates each paper's abstract — or its **full text** for open-access papers — and maps gene mentions to NCBI Gene IDs. |
| 3. Metadata + filter | **NCBI Gene** (`esummary`) | Adds official symbol/description/organism; drops off-organism hits. |
| 4. Nucleotide | **NCBI**, then an organism-specific database | Fetches the gene sequence, falling back to the authoritative database for that organism when NCBI has no record. |
| 5. Protein + annotation | **UniProt** | Amino-acid sequence + EC / keywords / GO / Pfam / KEGG. |
| 6. Orthologues (on demand) | **OrthoDB** | Cross-species orthologues from the gene's OrthoDB ortholog group. |

**Full-text mining:** for papers in PMC's open-access subset, PubTator annotates
the whole article rather than just the abstract, which surfaces noticeably more
genes. Paywalled papers are still covered at the abstract level. Toggle it off in
the search form for faster, abstract-only runs.

**Organism-specific nucleotide sources:** if NCBI has no usable record, the tool
routes to the database of record for that organism (by NCBI taxonomic lineage)
and labels each sequence with its source:

| Organism group | Database used |
|----------------|---------------|
| Bacteria, viruses, archaea | **BV-BRC** |
| Parasitic helminths (nematodes, flatworms) | **WormBase ParaSite** |
| Plants (incl. *Arabidopsis* → TAIR10) | **Ensembl Plants** |
| Fungi (incl. budding yeast → SGD) | **Ensembl Fungi** |
| Insects (incl. *Drosophila* → FlyBase) | **Ensembl Metazoa** |
| Vertebrates (human, mouse, zebrafish, …) | **Ensembl** |
| Other protists / eukaryotes | **Ensembl** |

The router is table-driven, so more organism databases can be slotted in per clade.

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

## Output

A sortable table of genes with the literature aliases, PMIDs, nucleotide and
protein sequences (each labelled with its source database), a **Function** column
(EC / family / keywords / Pfam / GO / KEGG), and a **Find orthologues** expander
per gene (OrthoDB). Export buttons:

- **Nucleotide FASTA** / **Protein FASTA**
- **CSV** (gene + sequence + protein summary)
- **Download all (zip)** — both FASTA files, the CSV, and an `annotations.csv`

## Notes & limits

- **Nucleotide sequences** come from NCBI first (exact gene region on the correct
  strand); if NCBI has no usable record the tool falls back to the organism's
  database of record (see the table above). Genes that no database can resolve are
  shown with a **why?** tooltip explaining the cause.
- **Gene recall** depends on PubTator coverage; raise *Max papers* and keep
  full-text mining on to surface more genes.
- **Orthologues** are OrthoDB ortholog groups (evolutionary orthology), scoped to
  the organism's taxonomic level where possible.
- The literature and sequence layers (`backend/ncbi.py`, `backend/europepmc.py`,
  `backend/bvbrc.py`, `backend/wormbase.py`, `backend/ensembl.py`) are isolated,
  so further databases can be plugged in without touching the rest of the pipeline.
