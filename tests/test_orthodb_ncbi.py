"""Tests for the OrthoDB lookups and NCBI taxonomy parsing (no network needed)."""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import httpx  # noqa: E402
import orthodb  # noqa: E402
from ncbi import species_from_taxonomy_xml  # noqa: E402
from orthodb import OrthoDBClient, OrthoDBUnavailable  # noqa: E402


class MemoryCache:
    def __init__(self):
        self.data = {}

    async def get(self, ns, key):
        return self.data.get((ns, key))

    async def set(self, ns, key, value):
        self.data[(ns, key)] = value


def make_client(handler, cache=None):
    """An OrthoDBClient wired to a fake transport. Its real constructor would
    build an HTTPS client (loading the CA bundle, ~1s each time), so it is given
    the mock-transport client from the start instead."""
    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **_kw: real(transport=httpx.MockTransport(handler))
    try:
        return OrthoDBClient(cache=cache)
    finally:
        httpx.AsyncClient = real


def run(coro):
    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)
    asyncio.sleep = no_sleep  # skip the retry back-offs
    try:
        return asyncio.run(coro)
    finally:
        asyncio.sleep = real_sleep


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)


class GroupForAccession(unittest.TestCase):
    def test_finds_the_group_and_sends_accession_and_level(self):
        seen = []

        def handler(req):
            seen.append(dict(req.url.params))
            return json_response({"data": ["8320584at2"], "count": 1, "bigdata": [
                {"id": "8320584at2", "name": "invasin", "level_name": "Bacteria", "gene_count": 1086}]})
        g = run(make_client(handler).group_for_accession("P76347", 2))
        self.assertEqual(g, {"id": "8320584at2", "name": "invasin", "level_name": "Bacteria"})
        self.assertEqual((seen[0]["query"], seen[0]["level"]), ("P76347", "2"))

    def test_none_when_orthodb_has_no_group_and_that_answer_is_cached(self):
        calls = []

        def handler(req):
            calls.append(1)
            return json_response({"data": None, "count": 0, "bigdata": None})
        cache = MemoryCache()
        client = make_client(handler, cache)

        async def twice():
            return [await client.group_for_accession("P0A6Z0", 2), await client.group_for_accession("P0A6Z0", 2)]
        self.assertEqual(run(twice()), [None, None])
        self.assertEqual(len(calls), 1)  # "no group" is a definitive answer: not asked again

    def test_outage_raises_and_is_not_cached(self):
        cache = MemoryCache()
        client = make_client(lambda req: httpx.Response(500, text="oops"), cache)
        with self.assertRaises(OrthoDBUnavailable):
            run(client.group_for_accession("P76347", 2))
        self.assertEqual(cache.data, {})  # a transient failure must not look like "no group"

    def test_unreadable_answer_counts_as_unavailable(self):
        client = make_client(lambda req: httpx.Response(200, text="<html>rate limited</html>"))
        with self.assertRaises(OrthoDBUnavailable):
            run(client.group_for_accession("P76347", 2))

    def test_blank_accession(self):
        self.assertIsNone(run(make_client(lambda r: json_response({})).group_for_accession("  ", 2)))


RECORDS = {"data": [
    {"organism": {"id": "562_0", "name": "Escherichia coli"},
     "genes": [{"gene_id": {"id": "AOI93_RS27405", "param": "x"}, "description": "hypothetical protein"},
               {"gene_id": {"id": "b2"}, "description": "second"}]},
    {"organism": {"id": "83333_0", "name": "Escherichia coli K-12"},
     "genes": [{"gene_id": {"id": "b1978"}, "description": "inverse autotransporter adhesin"}]},
]}


class MembersInSpecies(unittest.TestCase):
    def test_counts_members_over_all_strains(self):
        seen = []

        def handler(req):
            seen.append(dict(req.url.params))
            return json_response(RECORDS)
        res = run(make_client(handler).members_in_species("8320584at2", 562))
        self.assertEqual(res["count"], 3)
        self.assertEqual(res["example"], {"gene_id": "AOI93_RS27405", "description": "hypothetical protein",
                                          "organism": "Escherichia coli"})
        self.assertEqual((seen[0]["id"], seen[0]["species"]), ("8320584at2", "562"))

    def test_zero_members_means_absent_and_is_cached(self):
        calls = []

        def handler(req):
            calls.append(1)
            return json_response({"data": [], "status": "ok"})
        cache = MemoryCache()
        client = make_client(handler, cache)

        async def twice():
            return [await client.members_in_species("G", 287), await client.members_in_species("G", 287)]
        first, second = run(twice())
        self.assertEqual(first, {"count": 0, "example": None})
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)

    def test_outage_raises(self):
        with self.assertRaises(OrthoDBUnavailable):
            run(make_client(lambda req: httpx.Response(503)).members_in_species("G", 562))

    def test_summarise_handles_odd_shapes(self):
        self.assertEqual(orthodb._summarise_members([]), {"count": 0, "example": None})
        self.assertEqual(orthodb._summarise_members([{"genes": [{"gene_id": "plain", "description": None}]}])["count"], 1)


class OrthologsLookup(unittest.TestCase):
    def handler(self, log):
        def handler(req):
            path = req.url.path
            log.append((path, dict(req.url.params)))
            if path.endswith("/search"):
                q = req.url.params["query"]
                if q == "P76347":  # exact accession -> one group
                    return json_response({"data": ["EXACT"], "count": 1, "bigdata": [{"id": "EXACT", "name": "invasin", "level_name": "Bacteria"}]})
                return json_response({"data": ["BIG", "SMALL"], "count": 2, "bigdata": [
                    {"id": "BIG", "name": "Ig-like", "level_name": "Bacteria", "gene_count": 1953},
                    {"id": "SMALL", "name": "invasin", "level_name": "Bacteria", "gene_count": 10}]})
            return httpx.Response(200, text="pub_gene_id\torganism_name\tdescription\nb1\tE. coli\td1\n")
        return handler

    def test_accession_gives_the_exact_group_not_the_biggest_name_match(self):
        log = []
        res = run(make_client(self.handler(log)).orthologs(name="yeeJ", organism="Escherichia coli", accession="P76347", level=2))
        self.assertEqual(res["group"], "EXACT")

    def test_without_an_accession_the_name_search_is_the_fallback(self):
        res = run(make_client(self.handler([])).orthologs(name="yeeJ", organism="Escherichia coli"))
        self.assertEqual(res["group"], "BIG")

    def test_accession_without_a_level_is_ignored(self):
        log = []
        run(make_client(self.handler(log)).orthologs(name="yeeJ", organism="Escherichia coli", accession="P76347"))
        self.assertNotIn("P76347", [p.get("query") for _, p in log])

    def test_unknown_accession_falls_back_to_the_name_search(self):
        res = run(make_client(self.handler([])).orthologs(name="yeeJ", organism="Escherichia coli", accession="Q00000", level=2))
        self.assertEqual(res["group"], "BIG")


SPECIES_XML = """<TaxaSet><Taxon><TaxId>562</TaxId><ScientificName>Escherichia coli</ScientificName>
<Rank>species</Rank><Lineage>x</Lineage><LineageEx>
<Taxon><TaxId>561</TaxId><ScientificName>Escherichia</ScientificName><Rank>genus</Rank></Taxon>
</LineageEx></Taxon></TaxaSet>"""

STRAIN_XML = """<TaxaSet><Taxon><TaxId>83333</TaxId><ScientificName>Escherichia coli K-12</ScientificName>
<OtherNames></OtherNames><Rank>strain</Rank><Lineage>x</Lineage><LineageEx>
<Taxon><TaxId>131567</TaxId><ScientificName>cellular organisms</ScientificName><Rank>no rank</Rank></Taxon>
<Taxon><TaxId>561</TaxId><ScientificName>Escherichia</ScientificName><Rank>genus</Rank></Taxon>
<Taxon><TaxId>562</TaxId><ScientificName>Escherichia coli</ScientificName><Rank>species</Rank></Taxon>
</LineageEx></Taxon></TaxaSet>"""

GENUS_XML = """<TaxaSet><Taxon><TaxId>6278</TaxId><ScientificName>Brugia</ScientificName><Rank>genus</Rank>
<Lineage>x</Lineage><LineageEx>
<Taxon><TaxId>6231</TaxId><ScientificName>Nematoda</ScientificName><Rank>phylum</Rank></Taxon>
</LineageEx></Taxon></TaxaSet>"""


class SpeciesFromTaxonomyXml(unittest.TestCase):
    def test_a_species_is_itself(self):
        self.assertEqual(species_from_taxonomy_xml(SPECIES_XML), {"taxid": "562", "name": "Escherichia coli"})

    def test_a_strain_maps_up_to_its_species(self):
        self.assertEqual(species_from_taxonomy_xml(STRAIN_XML), {"taxid": "562", "name": "Escherichia coli"})

    def test_a_genus_has_no_species(self):
        self.assertIsNone(species_from_taxonomy_xml(GENUS_XML))

    def test_garbage(self):
        self.assertIsNone(species_from_taxonomy_xml(""))
        self.assertIsNone(species_from_taxonomy_xml("<html>error</html>"))


if __name__ == "__main__":
    unittest.main()
