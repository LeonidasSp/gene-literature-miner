"""
VEuPathDB routing — the authoritative resource for eukaryotic pathogens
(protozoan parasites, oomycetes, vectors). VEuPathDB is a family of component
sites (PlasmoDB, TriTrypDB, ToxoDB, …), each the database of record for a clade.

VEuPathDB's own WDK web service has no simple gene-name search and a fragile
sequence endpoint, but Ensembl Protists mirrors the same genome assemblies and
native gene IDs (e.g. `PF3D7_...`). So we resolve the sequence through the
reliable Ensembl REST backend and brand the result as the VEuPathDB component,
linking each gene to its native VEuPathDB record page.

This module is pure routing (genus -> component + gene-page URL); retrieval is
delegated to the Ensembl client.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# genus -> (component display name, site subdomain, project path segment)
_GENUS_TO_COMPONENT: dict[str, tuple[str, str, str]] = {
    # PlasmoDB — malaria parasites
    "plasmodium": ("PlasmoDB", "plasmodb.org", "plasmo"),
    # TriTrypDB — kinetoplastids
    "trypanosoma": ("TriTrypDB", "tritrypdb.org", "tritrypdb"),
    "leishmania": ("TriTrypDB", "tritrypdb.org", "tritrypdb"),
    "leptomonas": ("TriTrypDB", "tritrypdb.org", "tritrypdb"),
    "crithidia": ("TriTrypDB", "tritrypdb.org", "tritrypdb"),
    # ToxoDB — apicomplexa (coccidia)
    "toxoplasma": ("ToxoDB", "toxodb.org", "toxo"),
    "neospora": ("ToxoDB", "toxodb.org", "toxo"),
    "eimeria": ("ToxoDB", "toxodb.org", "toxo"),
    "sarcocystis": ("ToxoDB", "toxodb.org", "toxo"),
    "cystoisospora": ("ToxoDB", "toxodb.org", "toxo"),
    # CryptoDB
    "cryptosporidium": ("CryptoDB", "cryptodb.org", "cryptodb"),
    # PiroplasmaDB
    "babesia": ("PiroplasmaDB", "piroplasmadb.org", "piro"),
    "theileria": ("PiroplasmaDB", "piroplasmadb.org", "piro"),
    # GiardiaDB
    "giardia": ("GiardiaDB", "giardiadb.org", "giardiadb"),
    "spironucleus": ("GiardiaDB", "giardiadb.org", "giardiadb"),
    # TrichDB
    "trichomonas": ("TrichDB", "trichdb.org", "trichdb"),
    # AmoebaDB
    "entamoeba": ("AmoebaDB", "amoebadb.org", "amoeba"),
    "acanthamoeba": ("AmoebaDB", "amoebadb.org", "amoeba"),
    "naegleria": ("AmoebaDB", "amoebadb.org", "amoeba"),
    # MicrosporidiaDB
    "encephalitozoon": ("MicrosporidiaDB", "microsporidiadb.org", "micro"),
    "enterocytozoon": ("MicrosporidiaDB", "microsporidiadb.org", "micro"),
    "nosema": ("MicrosporidiaDB", "microsporidiadb.org", "micro"),
    # VectorBase — arthropod disease vectors
    "anopheles": ("VectorBase", "vectorbase.org", "vectorbase"),
    "aedes": ("VectorBase", "vectorbase.org", "vectorbase"),
    "culex": ("VectorBase", "vectorbase.org", "vectorbase"),
    "ixodes": ("VectorBase", "vectorbase.org", "vectorbase"),
    "rhodnius": ("VectorBase", "vectorbase.org", "vectorbase"),
    "glossina": ("VectorBase", "vectorbase.org", "vectorbase"),
}


def component_for(
    organism: str,
) -> Optional[tuple[str, Callable[[str], str]]]:
    """
    (label, url_builder) for a VEuPathDB-covered organism, else None.

    `label` is e.g. "VEuPathDB (PlasmoDB)"; `url_builder(gene_id)` yields the
    native VEuPathDB gene-record page.
    """
    genus = (organism or "").strip().lower().split(" ")[0]
    comp = _GENUS_TO_COMPONENT.get(genus)
    if not comp:
        return None
    name, sub, proj = comp
    label = f"VEuPathDB ({name})"

    def _url(gene_id: str, _sub: str = sub, _proj: str = proj) -> str:
        return f"https://{_sub}/{_proj}/app/record/gene/{gene_id}"

    return label, _url


def is_veupath(organism: str) -> bool:
    return component_for(organism) is not None
