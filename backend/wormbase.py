"""
WormBase ParaSite routing — the database of record for parasitic helminths
(nematodes and flatworms).

WormBase ParaSite's own REST service is intermittently unavailable, but Ensembl
Metazoa mirrors the same helminth genome assemblies and native WormBase gene IDs
(e.g. `WBGene00227131`). So helminth sequences are retrieved through the reliable
Ensembl REST backend and branded as WormBase ParaSite, with each gene linked to
its native WormBase ParaSite record page.

This module is pure routing (genus membership + label + gene-page URL);
retrieval is delegated to the Ensembl client.
"""
from __future__ import annotations

LABEL = "WormBase ParaSite"

# Helminth genera WormBase ParaSite covers. Extend freely.
HELMINTH_GENERA = {
    "brugia", "wuchereria", "onchocerca", "loa", "schistosoma", "ascaris",
    "trichuris", "necator", "ancylostoma", "strongyloides", "haemonchus",
    "echinococcus", "taenia", "fasciola", "trichinella", "dirofilaria",
    "toxocara", "enterobius", "clonorchis", "opisthorchis", "dracunculus",
    "hymenolepis", "heligmosomoides", "nippostrongylus", "teladorsagia",
    "ostertagia", "trichostrongylus", "cooperia", "angiostrongylus",
}


def is_helminth(organism: str) -> bool:
    genus = (organism or "").strip().lower().split(" ")[0]
    return genus in HELMINTH_GENERA


def gene_url(gene_id: str) -> str:
    return f"https://parasite.wormbase.org/Gene/Summary?g={gene_id}"
