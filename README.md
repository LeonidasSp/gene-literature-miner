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

A web app that mines the literature for genes on a topic and pulls their
nucleotide sequences from NCBI.

**Example:** search *biofilm* in *Staphylococcus aureus* → the tool finds the
genes discussed in PubMed articles (e.g. `icaA`, `icaD`, `sarA`, `agr`…), links
each to its NCBI Gene page, and fetches the actual nucleotide sequence as FASTA.

## How it works

| Step | Source | What it does |
|------|--------|--------------|
| 1. Literature search | **PubMed** (E-utilities `esearch`) | Finds articles matching your topic (+ optional organism). |
| 2. Gene extraction | **PubTator3** (NCBI) | Reads the abstracts and annotates the genes, mapped to NCBI Gene IDs. |
| 3. Metadata + filter | **Gene db** (`esummary`) | Adds official symbol/description/organism; drops off-organism hits. |
| 4. Sequence | **Nucleotide db** (`efetch`) | Uses each gene's genomic coordinates to fetch the exact region/strand as FASTA. |

All data sources are free and need **no API key**. Google Scholar (no API) and
Scopus (paid key) were left out of v1 by design — see the note at the bottom.

## Run it

### Option A — Docker (recommended, no Python needed)

```bash
cd gene_literature_miner
docker compose up -d --build
```

Then open <http://localhost:8000>. Stop it with `docker compose down`.

### Option B — local Python (3.10+)

```bash
cd gene_literature_miner/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open <http://localhost:8000> in your browser.

Type a topic (e.g. `biofilm formation`), optionally an organism
(`Staphylococcus aureus`), and hit **Search**. Results are a sortable table with
gene links, PMIDs, and sequences; use **Export FASTA / CSV** to save them.

## Make it public (host it for others)

It's a single stateless Docker container (no database, no saved files), so any
container host works. Two easy, free-tier paths:

### Path A — Render (web UI, no CLI)
1. Put this `gene_literature_miner/` folder in its own **GitHub repo**.
2. On <https://render.com>: **New → Blueprint**, pick the repo. It reads
   [`render.yaml`](render.yaml) and builds the Dockerfile automatically.
3. In the service's **Environment**, set `NCBI_API_KEY` to your free key (below).
4. You get a public HTTPS URL like `https://gene-literature-miner.onrender.com`.
   (Free tier sleeps after ~15 min idle and wakes on the next request.)

### Path B — Fly.io (CLI, deploys straight from this folder, no GitHub)
```bash
cd gene_literature_miner
fly launch --copy-config --now      # uses fly.toml; pick a unique app name
fly secrets set NCBI_API_KEY=your_key_here
```
Gives an HTTPS URL like `https://<app>.fly.dev` and scales to zero when idle.

Other hosts (Railway, Google Cloud Run, any VPS) work the same way — they all
just run the Dockerfile and inject a `$PORT`, which the app honors.

### Before you go public — important
- **Get a free NCBI API key** (see below) and set `NCBI_API_KEY`. Once hosted,
  *every* visitor's NCBI/PubTator request comes from your one server IP; without
  a key you're capped at 3 req/s and NCBI can temporarily block the IP.
- The app has **no login**, so it includes light abuse protection: a per-IP
  throttle (`PER_IP_MIN_INTERVAL`, default 3 s) and a concurrency cap
  (`MAX_CONCURRENT_SEARCHES`, default 2). Tune via env vars.
- Set `NCBI_EMAIL` to a real contact address (NCBI etiquette).

## Optional: raise the NCBI rate limit

Without a key the tool is polite-limited to 3 requests/second. A free NCBI API
key raises this to 10/s and makes large searches faster:

```bash
export NCBI_API_KEY=your_key_here      # Windows PowerShell: $env:NCBI_API_KEY="..."
export NCBI_EMAIL=you@example.com
```

## Notes & limits

- **Sequence retrieval** uses the gene's genomic coordinates, so you get the
  exact gene region on the correct strand. Genes without genomic coordinates in
  their NCBI record, or regions larger than 60 kb, are shown without a sequence
  (the Gene page link is always provided).
- **PubTator coverage:** genes are only found if PubTator has annotated the
  article, and only genes it maps to an NCBI Gene ID are reported. Coverage on
  any single abstract can be sparse, so **raise "Max papers" (e.g. to 100–200)
  to surface more genes** for a topic.
- **Names:** bacterial genes are often mapped to a strain-specific locus tag
  (e.g. `AT695_RS04810`). The table shows the literature term as the headline
  name (e.g. `FnBPA`) with the NCBI record name alongside.
- **Scholar / Scopus:** Google Scholar has no official API and blocks scrapers;
  Scopus needs an institutional Elsevier key. Both can be added later — the
  literature layer in `backend/ncbi.py` is isolated so a new source plugs in
  without touching the sequence code.
```
