"""Unit tests for the mention-context snippets (no network needed).

Run from the repo root:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import snippets  # noqa: E402
from ncbi import absorb_document  # noqa: E402


def sentences(text):
    return [text[s:e] for s, e in snippets.split_sentences(text)]


class SplitSentences(unittest.TestCase):
    def test_plain_boundaries(self):
        self.assertEqual(
            sentences("The gene yeeJ was deleted. The mutant made less biofilm."),
            ["The gene yeeJ was deleted.", "The mutant made less biofilm."],
        )

    def test_species_initials_do_not_split(self):
        text = "E. coli K-12 and S. aureus were used. Cells grew overnight."
        self.assertEqual(
            sentences(text), ["E. coli K-12 and S. aureus were used.", "Cells grew overnight."]
        )

    def test_common_abbreviations_do_not_split(self):
        self.assertEqual(len(sentences("Smith et al. showed that yeeJ binds peptidoglycan.")), 1)
        self.assertEqual(len(sentences("See Fig. 2 for details, e.g. the mutant.")), 1)
        self.assertEqual(len(sentences("Pseudomonas sp. Strain PAO1 was grown.")), 1)

    def test_decimals_and_digits(self):
        self.assertEqual(
            sentences("Grown at 0.5 mM IPTG. Then washed."), ["Grown at 0.5 mM IPTG.", "Then washed."]
        )

    def test_leading_whitespace_is_skipped(self):
        (start, _), *_ = snippets.split_sentences("  Hello world. Next one.")
        self.assertEqual(start, 2)

    def test_no_terminal_punctuation(self):
        self.assertEqual(sentences("just a title"), ["just a title"])
        self.assertEqual(snippets.split_sentences(""), [])


class TrimAround(unittest.TestCase):
    def test_short_sentence_is_untouched(self):
        self.assertEqual(snippets.trim_around("yeeJ is here.", [(0, 4)]), ("yeeJ is here.", [[0, 4]]))

    def test_long_sentence_keeps_the_mention(self):
        s = "word " * 120 + "yeeJ" + " word" * 120
        pos = s.index("yeeJ")
        text, marks = snippets.trim_around(s, [(pos, pos + 4)])
        self.assertLessEqual(len(text), snippets.SENTENCE_LIMIT + 2)
        self.assertTrue(text.startswith("…") and text.endswith("…"))
        (m0, m1), = marks
        self.assertEqual(text[m0:m1], "yeeJ")


def ann(passage_text, passage_offset, word, gene_id="1", nth=0, text=None):
    """A PubTator-style gene annotation for the nth occurrence of `word`."""
    pos = -1
    for _ in range(nth + 1):
        pos = passage_text.index(word, pos + 1)
    return {
        "infons": {"type": "Gene", "identifier": gene_id},
        "text": word if text is None else text,
        "locations": [{"offset": passage_offset + pos, "length": len(word)}],
    }


def passage(offset, text, section=None, ptype="paragraph", anns=()):
    infons = {"type": ptype}
    if section:
        infons["section_type"] = section
    return {"infons": infons, "offset": offset, "text": text, "annotations": list(anns)}


class AbsorbDocument(unittest.TestCase):
    def build_doc(self):
        title = "YeeJ promotes biofilm formation"
        abstract = "We studied yeeJ in E. coli. YeeJ binds peptidoglycan. Other genes were flagged."
        results = "Deleting yeeJ reduced adhesion. The yeeJ mutant and the yeeJ complement differed."
        ref = "Smith J. The yeeJ protein. J Bact 2010."
        o1 = 0
        o2 = o1 + len(title) + 1
        o3 = o2 + len(abstract) + 1
        o4 = o3 + len(results) + 1
        return {
            "pmid": 111,
            "passages": [
                passage(o1, title, "TITLE", "front", [ann(title, o1, "YeeJ", "946498")]),
                passage(o2, abstract, "ABSTRACT", "abstract", [
                    ann(abstract, o2, "yeeJ", "946498"), ann(abstract, o2, "YeeJ", "946498")]),
                passage(o3, results, "RESULTS", "paragraph", [
                    ann(results, o3, "yeeJ", "946498", 0), ann(results, o3, "yeeJ", "946498", 1),
                    ann(results, o3, "yeeJ", "946498", 2)]),
                passage(o4, ref, "REF", "ref", [ann(ref, o4, "yeeJ", "946498")]),
            ],
        }

    def run_absorb(self, *docs):
        genes = {}
        for d in docs:
            absorb_document(genes, d)
        for e in genes.values():
            e["snippets"] = e.pop("_snips").pick()
        return genes

    def test_reference_list_is_not_counted_or_shown(self):
        g = self.run_absorb(self.build_doc())["946498"]
        self.assertEqual(g["count"], 6)  # 1 title + 2 abstract + 3 results; the REF mention is excluded
        for s in g["snippets"]:
            self.assertNotIn("J Bact", s["text"])

    def test_marks_point_at_the_gene(self):
        for s in self.run_absorb(self.build_doc())["946498"]["snippets"]:
            self.assertTrue(s["marks"])
            for a, b in s["marks"]:
                self.assertEqual(s["text"][a:b].lower(), "yeej")

    def test_order_labels_and_merging(self):
        snips = self.run_absorb(self.build_doc())["946498"]["snippets"]
        self.assertEqual(snips[0]["section"], "Abstract")  # most informative section first
        by_text = {s["text"]: s for s in snips}
        merged = by_text["The yeeJ mutant and the yeeJ complement differed."]
        self.assertEqual(len(merged["marks"]), 2)  # two mentions, one sentence -> one snippet
        self.assertEqual(merged["section"], "Results")
        self.assertEqual(merged["title"], "YeeJ promotes biofilm formation")
        self.assertEqual({s["pmid"] for s in snips}, {"111"})

    def test_spreads_over_papers_before_repeating_one(self):
        def doc(pmid, n):
            text = " ".join(f"Sentence {i} mentions yeeJ here." for i in range(n))
            anns = [ann(text, 0, "yeeJ", "5", i) for i in range(n)]
            return {"pmid": pmid, "passages": [passage(0, text, "RESULTS", anns=anns)]}
        snips = self.run_absorb(doc(1, 6), doc(2, 1))["5"]["snippets"]
        self.assertIn("2", {s["pmid"] for s in snips})  # the lone second paper is included
        self.assertEqual(snips[0]["pmid"], "1")
        self.assertEqual(snips[1]["pmid"], "2")  # ...and right after the first paper's best one
        self.assertEqual(len(snips), snippets.SECTION_POOL_MAX + 1)  # paper 1 is capped at 3 per section

    def test_section_headings_are_counted_but_not_shown(self):
        heading = "Prevalence and conservation of the yeeJ gene"
        body = "The yeeJ gene is common."
        d = {"pmid": 1, "passages": [
            passage(0, heading, "RESULTS", "title_2", [ann(heading, 0, "yeeJ", "8")]),
            passage(len(heading) + 1, body, "RESULTS", "paragraph", [ann(body, len(heading) + 1, "yeeJ", "8")]),
        ]}
        g = self.run_absorb(d)["8"]
        self.assertEqual(g["count"], 2)
        self.assertEqual([s["text"] for s in g["snippets"]], [body])

    def test_single_paper_is_spread_across_its_sections(self):
        # A gene named many times in one paper: Results is crowded, but the
        # abstract and discussion must still be represented.
        def para(offset, section, n):
            text = " ".join(f"{section} sentence {i} mentions yeeJ here." for i in range(n))
            return passage(offset, text, section, anns=[ann(text, offset, "yeeJ", "5", i) for i in range(n)]), len(text) + 1
        passages, off = [], 0
        for section, n in (("ABSTRACT", 3), ("RESULTS", 20), ("DISCUSS", 3)):
            p, size = para(off, section, n)
            passages.append(p)
            off += size
        snips = self.run_absorb({"pmid": 1, "passages": passages})["5"]["snippets"]
        self.assertEqual(len(snips), snippets.MAX_SNIPPETS)
        self.assertEqual({s["section"] for s in snips}, {"Abstract", "Results", "Discussion"})
        self.assertEqual(snips[0]["section"], "Abstract")

    def test_pool_is_bounded_per_section(self):
        text = " ".join(f"Sentence {i} mentions yeeJ here." for i in range(50))
        anns = [ann(text, 0, "yeeJ", "5", i) for i in range(50)]
        d = {"pmid": 1, "passages": [passage(0, text, "RESULTS", anns=anns)]}
        genes = {}
        absorb_document(genes, d)
        self.assertEqual(genes["5"]["count"], 50)  # every mention still counted
        self.assertEqual(len(genes["5"]["_snips"].pick()), snippets.SECTION_POOL_MAX)

    def test_mismatched_offset_is_counted_but_never_highlighted(self):
        text = "The yeeJ gene matters."
        bad = ann(text, 0, "yeeJ", "9")
        bad["locations"][0]["offset"] += 3  # points at the wrong words
        d = {"pmid": 1, "passages": [passage(0, text, "RESULTS", anns=[bad])]}
        g = self.run_absorb(d)["9"]
        self.assertEqual(g["count"], 1)
        self.assertEqual(g["snippets"], [])

    def test_abstract_only_document_without_section_types(self):
        title = "yeeJ in biofilms"
        abstract = "We show that yeeJ matters."
        d = {"pmid": 7, "passages": [
            passage(0, title, None, "title", [ann(title, 0, "yeeJ", "3")]),
            passage(len(title) + 1, abstract, None, "abstract", [ann(abstract, len(title) + 1, "yeeJ", "3")]),
        ]}
        snips = self.run_absorb(d)["3"]["snippets"]
        self.assertEqual({s["section"] for s in snips}, {"Title", "Abstract"})
        self.assertEqual(snips[0]["title"], "yeeJ in biofilms")

    def test_non_gene_and_non_numeric_ids_are_ignored(self):
        text = "The yeeJ gene."
        a = ann(text, 0, "yeeJ", "not-a-number")
        b = ann(text, 0, "yeeJ", "4")
        b["infons"]["type"] = "Species"
        d = {"pmid": 1, "passages": [passage(0, text, "RESULTS", anns=[a, b])]}
        self.assertEqual(self.run_absorb(d), {})


if __name__ == "__main__":
    unittest.main()
