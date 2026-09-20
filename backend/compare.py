"""
Multi-organism comparison: turn per-organism literature results plus OrthoDB
lookups into one gene-by-organism matrix. Pure Python (no network), so the
interesting decisions can be unit-tested.

Two different questions are kept apart on purpose, because mixing them is what
makes naive comparisons misleading:

  * literature -- was the gene named in the papers scanned for that organism?
  * orthology  -- does the organism have a member of the gene's OrthoDB ortholog
                  group at all, whether or not the papers happen to name it?

So every (gene group, organism) cell is in exactly one state:

  literature  the papers scanned for this organism name the gene (counts shown)
  ortholog    not named in those papers, but OrthoDB lists a member in the organism
  absent      OrthoDB was asked and lists no member in the organism
  unknown     could not be checked (no OrthoDB group for the gene, or OrthoDB
              did not answer) -- never treated as "absent"
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

MAX_TOP_GENES = 30

LITERATURE, ORTHOLOG, ABSENT, UNKNOWN = "literature", "ortholog", "absent", "unknown"


def parse_organisms(values: list[str], max_n: int) -> list[str]:
    """Organism names from form input: split on newlines, commas or semicolons,
    trim, drop blanks and case-insensitive duplicates (order kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for part in re.split(r"[\n\r,;]+", value or ""):
            name = " ".join(part.split())
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    if len(out) < 2:
        raise ValueError("Enter at least two organisms to compare.")
    if len(out) > max_n:
        raise ValueError(f"Compare at most {max_n} organisms at a time.")
    return out


def is_sparse(papers: int, genes_found: int) -> bool:
    """Few genes out of many papers. That is the mark of a species whose gene
    mentions PubTator links to NCBI records poorly (seen for several staphylococci:
    2 genes from 30 papers), so "the papers don't name it" says little about it."""
    return papers >= 10 and genes_found < 0.25 * papers


def top_genes(genes: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """The k most-mentioned genes (ties: more papers first)."""
    k = max(1, min(int(k), MAX_TOP_GENES))
    ranked = sorted(genes, key=lambda g: (g.get("mention_count", 0), g.get("paper_count", 0)), reverse=True)
    return ranked[:k]


def _group_key(org: str, gene: dict[str, Any]) -> tuple:
    """Which row a gene belongs to. Genes OrthoDB placed in the same ortholog
    group share a row across organisms. Genes it could not place stay separate
    across organisms -- a shared *name* alone is not evidence of orthology -- but
    within ONE organism, records with the same official symbol (e.g. a resistance
    gene on several plasmids, each its own record) are one row."""
    og = gene.get("og")
    if og and og.get("id"):
        return ("og", og["id"])
    name = (gene.get("ncbi_name") or "").strip().lower()
    return ("sym", org, name) if name else ("gene", org, gene.get("gene_id"))


def _pick_symbol(genes: list[dict[str, Any]]) -> str:
    """The most-discussed gene's shown symbol (that is the name the papers use)."""
    best = max(genes, key=lambda g: (g.get("mention_count", 0), g.get("paper_count", 0)))
    return best.get("symbol") or best.get("ncbi_name") or str(best.get("gene_id", ""))


def _literature_member(genes: list[dict[str, Any]]) -> dict[str, Any]:
    """One organism's cell when the papers name the gene. Several gene records of
    one organism can share a group (strains, duplicates): counts are combined."""
    genes = sorted(genes, key=lambda g: (g.get("mention_count", 0), g.get("paper_count", 0)), reverse=True)
    top = genes[0]
    pmids = set()
    for g in genes:
        pmids.update(g.get("pmids") or [])
    return {
        "state": LITERATURE,
        "gene_id": top.get("gene_id"),
        "gene_ids": [g.get("gene_id") for g in genes],
        "symbol": top.get("symbol"),
        "mentions": sum(g.get("mention_count", 0) for g in genes),
        "papers": len(pmids) if pmids else max(g.get("paper_count", 0) for g in genes),
        "gene_url": top.get("gene_url"),
        "species": sorted({g.get("organism") for g in genes if g.get("organism")}),
        "snippets": (top.get("snippets") or [])[:2],
    }


def _presence_member(presence: Any) -> dict[str, Any]:
    """A cell for an organism the papers don't name the gene in."""
    if isinstance(presence, dict):
        if presence.get("count", 0) > 0:
            return {"state": ORTHOLOG, "count": presence["count"], "example": presence.get("example")}
        return {"state": ABSENT}
    return {"state": UNKNOWN}  # not looked up, or OrthoDB did not answer


def build_comparison(
    organisms: list[dict[str, Any]],
    genes_by_org: dict[str, list[dict[str, Any]]],
    presence: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    """The matrix.

    `organisms`: display-ordered dicts with at least "label".
    `genes_by_org`: label -> genes; a gene may carry "og" ({"id","name",...}) when
      OrthoDB placed it in an ortholog group.
    `presence`: (group id, organism label) -> {"count", "example"} when OrthoDB was
      asked, or "unavailable" if it could not be reached; absent keys = not asked.
    """
    labels = [o["label"] for o in organisms]
    buckets: dict[tuple, dict[str, list[dict[str, Any]]]] = {}
    for label in labels:
        for gene in genes_by_org.get(label, []):
            buckets.setdefault(_group_key(label, gene), {}).setdefault(label, []).append(gene)

    groups: list[dict[str, Any]] = []
    for key, by_org in buckets.items():
        all_genes = [g for gs in by_org.values() for g in gs]
        og = next((g["og"] for g in all_genes if g.get("og")), None)
        members: dict[str, dict[str, Any]] = {}
        for label in labels:
            if label in by_org:
                members[label] = _literature_member(by_org[label])
            elif og:
                members[label] = _presence_member(presence.get((og["id"], label)))
            else:
                members[label] = {"state": UNKNOWN}
        states = [m["state"] for m in members.values()]
        n_lit = states.count(LITERATURE)
        n_present = n_lit + states.count(ORTHOLOG)
        n_absent = states.count(ABSENT)
        top = max(all_genes, key=lambda g: (g.get("mention_count", 0), g.get("paper_count", 0)))
        groups.append({
            "symbol": _pick_symbol(all_genes),
            "description": top.get("description") or "",
            "og": ({"id": og["id"], "name": og.get("name", ""), "level_name": og.get("level_name", "")} if og else None),
            # Why a gene has no group (no_protein / no_group / unavailable), so a
            # "not checked" cell can say what was missing instead of just "?".
            "og_status": None if og else top.get("og_status"),
            "members": members,
            "n_literature": n_lit,
            "n_present": n_present,
            "n_absent": n_absent,
            "n_unknown": states.count(UNKNOWN),
            "conservation": (
                "all" if n_present == len(labels)
                else "some_absent" if n_absent
                else "unchecked"
            ),
            "mentions": sum(g.get("mention_count", 0) for g in all_genes),
        })

    groups.sort(key=lambda g: (-g["n_literature"], -g["n_present"], -g["mentions"], g["symbol"].lower()))
    for i, g in enumerate(groups):
        g["id"] = f"g{i}"

    summary = {
        "groups": len(groups),
        "present_in_all": sum(1 for g in groups if g["conservation"] == "all"),
        "missing_somewhere": sum(1 for g in groups if g["conservation"] == "some_absent"),
        "unchecked": sum(1 for g in groups if g["conservation"] == "unchecked"),
        "discussed_in_all": sum(1 for g in groups if g["n_literature"] == len(labels)),
        "per_organism": {
            label: {
                "discussed": sum(1 for g in groups if g["members"][label]["state"] == LITERATURE),
                "ortholog_only": sum(1 for g in groups if g["members"][label]["state"] == ORTHOLOG),
                "absent": sum(1 for g in groups if g["members"][label]["state"] == ABSENT),
                "unknown": sum(1 for g in groups if g["members"][label]["state"] == UNKNOWN),
            }
            for label in labels
        },
    }
    return {"organisms": organisms, "groups": groups, "summary": summary}


def common_taxid(genes: list[dict[str, Any]]) -> Optional[str]:
    """The most common NCBI taxid among gene records (fallback when an organism
    name itself could not be resolved)."""
    ids = [str(g.get("taxid")) for g in genes if g.get("taxid")]
    return Counter(ids).most_common(1)[0][0] if ids else None
