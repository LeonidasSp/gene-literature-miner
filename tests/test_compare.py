"""Unit tests for the multi-organism comparison logic (no network needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import compare  # noqa: E402
from compare import ABSENT, LITERATURE, ORTHOLOG, UNKNOWN  # noqa: E402


def gene(gid, symbol, mentions=5, papers=2, og=None, pmids=None, organism="X", desc="", snippets=None):
    g = {"gene_id": gid, "symbol": symbol, "ncbi_name": symbol, "mention_count": mentions,
         "paper_count": papers, "pmids": pmids or [], "organism": organism, "description": desc,
         "gene_url": f"https://ncbi/gene/{gid}", "snippets": snippets or []}
    if og:
        g["og"] = {"id": og, "name": f"family {og}", "level_name": "Bacteria"}
    return g


ORGS = [{"label": "A"}, {"label": "B"}, {"label": "C"}]


class ParseOrganisms(unittest.TestCase):
    def test_splits_on_newlines_commas_semicolons(self):
        self.assertEqual(
            compare.parse_organisms(["S. aureus\nE. coli, K. pneumoniae;  P. aeruginosa"], 6),
            ["S. aureus", "E. coli", "K. pneumoniae", "P. aeruginosa"],
        )

    def test_repeated_form_values_and_dedupe_ignoring_case_and_spacing(self):
        self.assertEqual(compare.parse_organisms(["E. coli", "e. coli", "  S.   aureus "], 6), ["E. coli", "S. aureus"])

    def test_needs_two(self):
        with self.assertRaises(ValueError):
            compare.parse_organisms(["E. coli"], 5)
        with self.assertRaises(ValueError):
            compare.parse_organisms(["", "  ", ","], 5)

    def test_limit(self):
        with self.assertRaises(ValueError) as ctx:
            compare.parse_organisms(["a", "b", "c", "d"], 3)
        self.assertIn("at most 3", str(ctx.exception))


class IsSparse(unittest.TestCase):
    def test_thresholds(self):
        self.assertTrue(compare.is_sparse(30, 2))     # S. epidermidis in real data
        self.assertTrue(compare.is_sparse(30, 1))     # S. haemolyticus in real data
        self.assertTrue(compare.is_sparse(30, 5))     # 5 < 7.5
        self.assertFalse(compare.is_sparse(30, 34))
        self.assertFalse(compare.is_sparse(30, 8))    # 8 >= 7.5
        self.assertFalse(compare.is_sparse(4, 0))     # too few papers to say anything
        self.assertTrue(compare.is_sparse(10, 2))


class TopGenes(unittest.TestCase):
    def test_orders_by_mentions_then_papers_and_clamps(self):
        genes = [gene("1", "a", 5, 1), gene("2", "b", 9, 1), gene("3", "c", 5, 3)]
        self.assertEqual([g["gene_id"] for g in compare.top_genes(genes, 2)], ["2", "3"])
        self.assertEqual(len(compare.top_genes(genes, 0)), 1)  # at least one
        self.assertEqual(len(compare.top_genes(genes * 20, 999)), compare.MAX_TOP_GENES)


class BuildComparison(unittest.TestCase):
    def test_states_for_a_gene_discussed_in_one_organism(self):
        by_org = {"A": [gene("1", "icaA", 20, 4, og="G1")], "B": [], "C": []}
        presence = {("G1", "B"): {"count": 2, "example": {"gene_id": "b1"}},
                    ("G1", "C"): {"count": 0, "example": None}}
        res = compare.build_comparison(ORGS, by_org, presence)
        (grp,) = res["groups"]
        self.assertEqual(grp["members"]["A"]["state"], LITERATURE)
        self.assertEqual(grp["members"]["A"]["mentions"], 20)
        self.assertEqual(grp["members"]["B"]["state"], ORTHOLOG)
        self.assertEqual(grp["members"]["B"]["count"], 2)
        self.assertEqual(grp["members"]["C"]["state"], ABSENT)
        self.assertEqual(grp["conservation"], "some_absent")
        self.assertEqual((grp["n_literature"], grp["n_present"], grp["n_absent"]), (1, 2, 1))

    def test_genes_in_the_same_orthogroup_merge_across_organisms(self):
        by_org = {"A": [gene("1", "icaA", 20, og="G1")], "B": [gene("9", "IcaA", 6, og="G1")], "C": []}
        res = compare.build_comparison(ORGS, by_org, {("G1", "C"): {"count": 1, "example": None}})
        (grp,) = res["groups"]
        self.assertEqual(grp["symbol"], "icaA")  # the most-discussed gene's name
        self.assertEqual({k: v["state"] for k, v in grp["members"].items()},
                         {"A": LITERATURE, "B": LITERATURE, "C": ORTHOLOG})
        self.assertEqual(grp["conservation"], "all")
        self.assertEqual(res["summary"]["present_in_all"], 1)
        self.assertEqual(res["summary"]["discussed_in_all"], 0)

    def test_gene_without_an_orthogroup_is_unknown_elsewhere_never_absent(self):
        by_org = {"A": [gene("1", "mystery", 10)], "B": [], "C": []}
        (grp,) = compare.build_comparison(ORGS, by_org, {})["groups"]
        self.assertIsNone(grp["og"])
        self.assertEqual([grp["members"][o]["state"] for o in "ABC"], [LITERATURE, UNKNOWN, UNKNOWN])
        self.assertEqual(grp["conservation"], "unchecked")

    def test_two_unmapped_genes_with_the_same_symbol_are_not_merged(self):
        by_org = {"A": [gene("1", "sameName", 10)], "B": [gene("2", "sameName", 8)], "C": []}
        groups = compare.build_comparison(ORGS, by_org, {})["groups"]
        self.assertEqual(len(groups), 2)  # a shared name alone is not evidence of orthology

    def test_records_with_the_same_official_symbol_in_one_organism_are_one_row(self):
        # e.g. a resistance gene that has a separate gene record per plasmid
        by_org = {"A": [gene("1", "blaOXA-48", 43, 3, pmids=["1", "2", "3"]),
                        gene("2", "blaOXA-48", 18, 2, pmids=["3", "4"])], "B": [], "C": []}
        groups = compare.build_comparison(ORGS, by_org, {})["groups"]
        self.assertEqual(len(groups), 1)
        cell = groups[0]["members"]["A"]
        self.assertEqual((cell["mentions"], cell["papers"], cell["gene_ids"]), (61, 4, ["1", "2"]))

    def test_the_reason_a_gene_has_no_group_is_passed_on(self):
        a = gene("1", "x", 9)
        a["og_status"] = "no_protein"
        b = gene("2", "y", 5, og="G1")
        b["og_status"] = "should be ignored: it has a group"
        groups = {g["symbol"]: g for g in compare.build_comparison(ORGS, {"A": [a, b], "B": [], "C": []}, {})["groups"]}
        self.assertEqual(groups["x"]["og_status"], "no_protein")
        self.assertIsNone(groups["y"]["og_status"])

    def test_orthodb_failure_is_unknown_not_absent(self):
        by_org = {"A": [gene("1", "x", og="G1")], "B": [], "C": []}
        res = compare.build_comparison(ORGS, by_org, {("G1", "B"): "unavailable"})
        m = res["groups"][0]["members"]
        self.assertEqual((m["B"]["state"], m["C"]["state"]), (UNKNOWN, UNKNOWN))  # C was never asked

    def test_several_gene_records_of_one_organism_are_combined(self):
        by_org = {"A": [gene("1", "icaA", 10, 3, og="G1", pmids=["1", "2", "3"]),
                        gene("2", "icaA", 4, 2, og="G1", pmids=["3", "4"])], "B": [], "C": []}
        cell = compare.build_comparison(ORGS, by_org, {})["groups"][0]["members"]["A"]
        self.assertEqual(cell["mentions"], 14)
        self.assertEqual(cell["papers"], 4)  # union of PMIDs, not 3 + 2
        self.assertEqual(cell["gene_ids"], ["1", "2"])
        self.assertEqual(cell["gene_id"], "1")  # the most-discussed record represents it

    def test_ranking_and_ids(self):
        by_org = {
            "A": [gene("1", "everywhere", 5, og="G1"), gene("2", "onlyA", 50, og="G2")],
            "B": [gene("3", "everywhere", 5, og="G1")],
            "C": [gene("4", "everywhere", 5, og="G1")],
        }
        groups = compare.build_comparison(ORGS, by_org, {("G2", "B"): {"count": 0}, ("G2", "C"): {"count": 0}})["groups"]
        self.assertEqual([g["symbol"] for g in groups], ["everywhere", "onlyA"])  # discussed in more organisms first
        self.assertEqual([g["id"] for g in groups], ["g0", "g1"])

    def test_summary_counts_and_per_organism(self):
        by_org = {"A": [gene("1", "a", og="G1"), gene("2", "b", og="G2")], "B": [gene("3", "a", og="G1")], "C": []}
        presence = {("G1", "C"): {"count": 3}, ("G2", "B"): {"count": 0}, ("G2", "C"): {"count": 1}}
        s = compare.build_comparison(ORGS, by_org, presence)["summary"]
        self.assertEqual((s["groups"], s["present_in_all"], s["missing_somewhere"]), (2, 1, 1))
        self.assertEqual(s["per_organism"]["B"], {"discussed": 1, "ortholog_only": 0, "absent": 1, "unknown": 0})
        self.assertEqual(s["per_organism"]["C"]["ortholog_only"], 2)

    def test_snippets_travel_with_the_cell_but_are_capped(self):
        snips = [{"pmid": str(i)} for i in range(5)]
        by_org = {"A": [gene("1", "x", snippets=snips)], "B": [], "C": []}
        cell = compare.build_comparison(ORGS, by_org, {})["groups"][0]["members"]["A"]
        self.assertEqual(len(cell["snippets"]), 2)

    def test_no_genes_at_all(self):
        res = compare.build_comparison(ORGS, {}, {})
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["summary"]["groups"], 0)

    def test_private_gene_fields_never_reach_the_output(self):
        g = gene("1", "x")
        g["_summary"] = {"secret": "internal"}
        g["_candidates"] = ["a", "b"]
        res = compare.build_comparison(ORGS, {"A": [g], "B": [], "C": []}, {})
        self.assertNotIn("secret", repr(res))
        self.assertNotIn("_candidates", repr(res))


class CommonTaxid(unittest.TestCase):
    def test_most_common(self):
        self.assertEqual(compare.common_taxid([{"taxid": "5"}, {"taxid": "7"}, {"taxid": "7"}, {}]), "7")
        self.assertIsNone(compare.common_taxid([{}, {"taxid": ""}]))


if __name__ == "__main__":
    unittest.main()
