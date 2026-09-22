"""End-to-end tests of /api/compare/stream through the real FastAPI app, with the
network layer (NCBI, UniProt, OrthoDB) replaced by fakes so failures can be
staged on demand. Run from the repo root: python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception as exc:  # a future Starlette may need a different HTTP client package
    raise unittest.SkipTest(f"FastAPI TestClient is unavailable here ({exc}); endpoint tests skipped")

import main  # noqa: E402
from orthodb import OrthoDBUnavailable  # noqa: E402

# ------------------------------------------------------------------ fake world
SPECIES = {  # typed name -> (canonical name, species taxid, lineage)
    "s. aureus": ("Staphylococcus aureus", "1280", "cellular organisms; Bacteria; Firmicutes"),
    "staphylococcus aureus": ("Staphylococcus aureus", "1280", "cellular organisms; Bacteria; Firmicutes"),
    "s. epidermidis": ("Staphylococcus epidermidis", "1282", "cellular organisms; Bacteria; Firmicutes"),
    "e. coli": ("Escherichia coli", "562", "cellular organisms; Bacteria; Proteobacteria"),
    "escherichia coli": ("Escherichia coli", "562", "cellular organisms; Bacteria; Proteobacteria"),
    "homo sapiens": ("Homo sapiens", "9606", "cellular organisms; Eukaryota; Metazoa"),
    "brugia malayi": ("Brugia malayi", "6279", "cellular organisms; Eukaryota; Metazoa; Nematoda"),
    "wuchereria bancrofti": ("Wuchereria bancrofti", "6293", "cellular organisms; Eukaryota; Metazoa; Nematoda"),
}
LINEAGE = {v[1]: v[2] for v in SPECIES.values()}


def make_gene(gid, symbol, organism, taxid, mentions, papers=2):
    return {"gene_id": gid, "symbol": symbol, "ncbi_name": symbol, "description": f"{symbol} protein",
            "organism": organism, "taxid": taxid, "mention_count": mentions, "paper_count": papers,
            "pmids": [str(i) for i in range(papers)], "gene_url": f"https://ncbi/gene/{gid}",
            "snippets": [{"pmid": "1", "section": "Abstract", "title": "t", "text": symbol, "marks": [[0, 1]]}],
            "aliases": [symbol], "_summary": {"secret": "internal"}, "_candidates": [symbol]}


GENES = {
    "Staphylococcus aureus": [make_gene("1", "icaA", "Staphylococcus aureus", "1280", 20),
                              make_gene("2", "sarA", "Staphylococcus aureus", "1280", 10),
                              make_gene("3", "mystery", "Staphylococcus aureus", "1280", 4),
                              make_gene("5", "orphan", "Staphylococcus aureus", "1280", 2)],
    "Staphylococcus epidermidis": [make_gene("11", "icaA", "Staphylococcus epidermidis", "1282", 6)],
    "Escherichia coli": [make_gene("21", "fimH", "Escherichia coli", "562", 9)],
    "Homo sapiens": [make_gene("31", "TP53", "Homo sapiens", "9606", 50)],
    "Brugia malayi": [make_gene("41", "Bm1", "Brugia malayi", "6279", 8)],
    "Wuchereria bancrofti": [],
}
GROUP_OF_ACCESSION = {"ACC1": "G_ICA", "ACC2": "G_SAR", "ACC11": "G_ICA", "ACC21": "G_FIM", "ACC31": "G_P53"}
MEMBERS = {  # (group, species taxid) -> member count
    ("G_ICA", "562"): 0, ("G_SAR", "1282"): 2, ("G_SAR", "562"): 0,
    ("G_FIM", "1280"): 0, ("G_FIM", "1282"): 0,
}


class FakeNCBI:
    async def resolve_organism(self, text):
        hit = SPECIES.get(text.strip().lower())
        return {"name": hit[0], "taxid": hit[1], "rank": "species"} if hit else None

    async def species_of(self, taxid):
        for name, tid, _ in SPECIES.values():
            if tid == str(taxid):
                return {"taxid": tid, "name": name}
        return None

    async def lineage(self, taxid):
        return LINEAGE.get(str(taxid), "")


class FakeUniProt:
    async def protein_for_gene(self, *, gene_id, candidates, organism):
        return None if gene_id == "3" else {"accession": f"ACC{gene_id}"}


class FakeOrthoDB:
    def __init__(self):
        self.levels, self.member_calls, self.fail_members, self.fail_groups = [], [], False, set()
        self.orthologs_kwargs = None

    async def group_for_accession(self, accession, level):
        self.levels.append(level)
        if accession in self.fail_groups:
            raise OrthoDBUnavailable("down")
        gid = GROUP_OF_ACCESSION.get(accession)
        return {"id": gid, "name": f"family {gid}", "level_name": "Bacteria"} if gid else None

    async def members_in_species(self, group_id, taxid):
        self.member_calls.append((group_id, str(taxid)))
        if self.fail_members:
            raise OrthoDBUnavailable("down")
        n = MEMBERS.get((group_id, str(taxid)), 1)
        return {"count": n, "example": {"gene_id": "x", "description": "d", "organism": "o"} if n else None}

    async def orthologs(self, **kw):
        self.orthologs_kwargs = kw
        return {"group": "G", "homologs": []}


def sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, _, data = block.partition("\ndata: ")
        events.append((name.removeprefix("event: "), json.loads(data)))
    return events


class CompareEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tc = TestClient(main.app)
        cls.tc.__enter__()  # runs the app's startup (builds the real clients once)
        cls.real = {k: getattr(main, k) for k in ("client", "uniprot", "orthodb", "_collect_genes")}

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.real.items():
            setattr(main, k, v)
        cls.tc.__exit__(None, None, None)

    def setUp(self):
        self.orthodb = FakeOrthoDB()
        self.fail_scan = set()

        async def fake_collect(req):
            if req.organism in self.fail_scan:
                raise RuntimeError("boom (internal detail)")
            return [dict(g) for g in GENES.get(req.organism, [])], "term", 12, None, None, {}

        main.client, main.uniprot, main.orthodb = FakeNCBI(), FakeUniProt(), self.orthodb
        main._collect_genes = fake_collect
        main._search_gate = asyncio.Semaphore(2)
        main._last_seen.clear()

    def get(self, orgs, **params):
        main._last_seen.clear()
        q = [("query", params.pop("query", "biofilm formation"))] + [("organism", o) for o in orgs]
        q += list(params.items())
        return self.tc.get("/api/compare/stream", params=q)

    def matrix(self, orgs, **params):
        resp = self.get(orgs, **params)
        self.assertEqual(resp.status_code, 200, resp.text)
        events = sse(resp.text)
        return events, dict(events)["matrix"]

    # ------------------------------------------------------------ happy path
    def test_full_run_states_and_stage_order(self):
        events, m = self.matrix(["S. aureus", "S. epidermidis", "E. coli"])
        names = [n for n, _ in events]
        self.assertEqual(names[0], "meta")
        self.assertEqual(names[-2:], ["matrix", "done"])
        self.assertEqual(names.count("organism"), 3)
        self.assertLess(max(i for i, n in enumerate(names) if n == "organism"),
                        min(i for i, n in enumerate(names) if n == "stage"))
        self.assertEqual(set(self.orthodb.levels), {2})  # bacteria -> OrthoDB level 2

        by_sym = {g["symbol"]: g for g in m["groups"]}
        state = lambda sym, org: by_sym[sym]["members"][org]["state"]
        self.assertEqual(state("icaA", "Staphylococcus aureus"), "literature")
        self.assertEqual(state("icaA", "Staphylococcus epidermidis"), "literature")  # merged via the same group
        self.assertEqual(state("icaA", "Escherichia coli"), "absent")
        self.assertEqual(state("sarA", "Staphylococcus epidermidis"), "ortholog")
        self.assertEqual(state("sarA", "Escherichia coli"), "absent")
        self.assertEqual(state("mystery", "Staphylococcus epidermidis"), "unknown")  # no protein -> not checked
        self.assertEqual(by_sym["icaA"]["members"]["Staphylococcus aureus"]["mentions"], 20)
        self.assertEqual(len([g for g in m["groups"] if g["symbol"] == "icaA"]), 1)

    def test_only_uncovered_organisms_are_looked_up(self):
        self.matrix(["S. aureus", "S. epidermidis", "E. coli"])
        asked = set(self.orthodb.member_calls)
        self.assertIn(("G_ICA", "562"), asked)
        self.assertNotIn(("G_ICA", "1280"), asked)  # literature already covers these two
        self.assertNotIn(("G_ICA", "1282"), asked)
        self.assertEqual(len(self.orthodb.member_calls), len(asked))  # nothing asked twice

    def test_organism_summaries(self):
        _, m = self.matrix(["S. aureus", "E. coli"])
        sa = next(o for o in m["organisms"] if o["label"] == "Staphylococcus aureus")
        self.assertEqual((sa["papers"], sa["genes_found"], sa["genes_compared"], sa["orthology_checked"]), (12, 4, 4, True))
        self.assertEqual(m["summary"]["groups"], len(m["groups"]))

    def test_genes_per_organism_limits_what_is_compared(self):
        _, m = self.matrix(["S. aureus", "E. coli"], genes_per_organism=1)
        self.assertEqual({g["symbol"] for g in m["groups"]}, {"icaA", "fimH"})  # the top gene of each
        sa = next(o for o in m["organisms"] if o["label"] == "Staphylococcus aureus")
        self.assertEqual((sa["genes_found"], sa["genes_compared"]), (4, 1))

    def test_internal_fields_are_not_sent(self):
        resp = self.get(["S. aureus", "E. coli"])
        self.assertNotIn("secret", resp.text)
        self.assertNotIn("_candidates", resp.text)

    # ------------------------------------------------------------ validation
    def test_needs_two_organisms(self):
        resp = self.get(["S. aureus"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least two", resp.json()["detail"])

    def test_needs_a_topic(self):
        resp = self.get(["S. aureus", "E. coli"], query="   ")
        self.assertEqual(resp.status_code, 400)

    def test_too_many_organisms(self):
        resp = self.get(["a", "b", "c", "d", "e", "f"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at most", resp.json()["detail"])

    def test_same_organism_under_two_names(self):
        resp = self.get(["E. coli", "Escherichia coli"])
        # ("E. coli" and "Escherichia coli" differ as typed, so this reaches the resolver.)
        events = sse(resp.text)
        self.assertEqual(events[0][0], "error")
        self.assertIn("same organism", events[0][1]["detail"])

    # ------------------------------------------------------------ degraded paths
    def test_mixed_domains_skip_orthology_with_a_notice(self):
        events, m = self.matrix(["S. aureus", "Homo sapiens"])
        self.assertTrue(any("different domains" in n for n in m["notices"]))
        self.assertEqual(self.orthodb.member_calls, [])
        self.assertNotIn("stage", [n for n, _ in events])
        for g in m["groups"]:
            self.assertNotEqual(g["conservation"], "some_absent")  # nothing was checked, so nothing is "absent"

    def test_helminths_are_compared_at_the_eukaryote_level(self):
        # The taxonomy classifier calls nematodes "helminth"; for orthology they are eukaryotes.
        _, m = self.matrix(["Brugia malayi", "Wuchereria bancrofti"])
        self.assertEqual(m["notices"], [])
        self.assertEqual(m["level"], "Eukaryota")
        self.assertEqual(set(self.orthodb.levels), {2759})  # Brugia's gene was looked up, at the eukaryote level

    def test_sparse_organisms_are_flagged(self):
        _, m = self.matrix(["S. aureus", "S. epidermidis"])
        flags = {o["label"]: o["sparse"] for o in m["organisms"]}
        # the fake scan reports 12 papers for everyone: 4 genes is enough, 1 gene is not
        self.assertEqual(flags, {"Staphylococcus aureus": False, "Staphylococcus epidermidis": True})

    def test_orthodb_outage_gives_unknown_never_absent(self):
        self.orthodb.fail_members = True
        _, m = self.matrix(["S. aureus", "S. epidermidis", "E. coli"])
        sar = next(g for g in m["groups"] if g["symbol"] == "sarA")
        self.assertEqual(sar["members"]["Staphylococcus epidermidis"]["state"], "unknown")
        self.assertEqual(sar["members"]["Escherichia coli"]["state"], "unknown")
        self.assertEqual(m["summary"]["missing_somewhere"], 0)

    def test_a_gene_whose_group_lookup_failed_is_kept_but_unchecked(self):
        self.orthodb.fail_groups = {"ACC2"}
        _, m = self.matrix(["S. aureus", "S. epidermidis"])
        sar = next(g for g in m["groups"] if g["symbol"] == "sarA")
        self.assertIsNone(sar["og"])
        self.assertEqual(sar["members"]["Staphylococcus epidermidis"]["state"], "unknown")

    def test_unchecked_genes_say_why(self):
        self.orthodb.fail_groups = {"ACC2"}
        _, m = self.matrix(["S. aureus", "S. epidermidis"])
        why = {g["symbol"]: g["og_status"] for g in m["groups"]}
        self.assertEqual(why["mystery"], "no_protein")   # UniProt had no protein for it
        self.assertEqual(why["orphan"], "no_group")      # protein found, but OrthoDB has no group for it
        self.assertEqual(why["sarA"], "unavailable")     # OrthoDB did not answer
        self.assertIsNone(why["icaA"])                   # checked fine

    def test_one_failing_scan_does_not_sink_the_others(self):
        self.fail_scan = {"Escherichia coli"}
        events, m = self.matrix(["S. aureus", "E. coli"])
        ecoli = next(o for o in m["organisms"] if o["label"] == "Escherichia coli")
        self.assertIn("failed", ecoli["error"])
        self.assertNotIn("internal detail", json.dumps(events))  # the raw exception text stays server-side
        self.assertTrue(any(g["symbol"] == "icaA" for g in m["groups"]))

    def test_compare_is_rate_limited_like_search(self):
        main._last_seen.clear()
        first = self.tc.get("/api/compare/stream", params=[("query", "x"), ("organism", "S. aureus"), ("organism", "E. coli")])
        second = self.tc.get("/api/compare/stream", params=[("query", "x"), ("organism", "S. aureus"), ("organism", "E. coli")])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    # ------------------------------------------------------------ orthologue lookup
    def test_homologs_uses_the_accession_and_domain_level(self):
        main._last_seen.clear()
        resp = self.tc.get("/api/homologs", params={"name": "yeeJ", "organism": "Escherichia coli",
                                                    "accession": "P76347", "taxid": "562"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self.orthodb.orthologs_kwargs["accession"], self.orthodb.orthologs_kwargs["level"]), ("P76347", 2))

    def test_homologs_without_an_accession_still_works(self):
        resp = self.tc.get("/api/homologs", params={"name": "yeeJ", "organism": "Escherichia coli"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.orthodb.orthologs_kwargs["level"])


if __name__ == "__main__":
    unittest.main()
