"""Tests for the PubTator3 relations lookup (no network needed)."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import httpx  # noqa: E402
from relations import RelationsClient  # noqa: E402


class MemoryCache:
    def __init__(self):
        self.data = {}

    async def get(self, ns, key):
        return self.data.get((ns, key))

    async def set(self, ns, key, value):
        self.data[(ns, key)] = value


def make_client(handler, cache=None):
    """A RelationsClient wired to a fake transport (see test_orthodb_ncbi.py
    for why: the real constructor loading a CA bundle is slow to repeat)."""
    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **_kw: real(transport=httpx.MockTransport(handler))
    try:
        return RelationsClient(cache=cache)
    finally:
        httpx.AsyncClient = real


def run(coro):
    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)
    asyncio.sleep = no_sleep
    try:
        return asyncio.run(coro)
    finally:
        asyncio.sleep = real_sleep


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)


class ForGene(unittest.TestCase):
    def test_blank_symbol_makes_no_request(self):
        calls = []
        client = make_client(lambda req: calls.append(1) or json_response([]))
        self.assertEqual(run(client.for_gene("")), {"diseases": [], "chemicals": []})
        self.assertEqual(calls, [])

    def test_names_are_extracted_and_sorted_by_publications(self):
        def handler(req):
            params = dict(req.url.params)
            self.assertEqual(params["e1"], "@GENE_JAK1")
            if params["e2"] == "disease":
                self.assertEqual(params["type"], "associate")
                return json_response([
                    {"type": "associate", "source": "@DISEASE_Inflammation", "target": "@GENE_JAK1", "publications": 5},
                    {"type": "associate", "source": "@DISEASE_Neoplasms", "target": "@GENE_JAK1", "publications": 20},
                ])
            self.assertEqual(params["type"], "negative_correlate")
            return json_response([
                {"type": "negative_correlate", "source": "@CHEMICAL_ruxolitinib", "target": "@GENE_JAK1", "publications": 8},
            ])
        result = run(make_client(handler).for_gene("JAK1"))
        self.assertEqual(result["diseases"], [
            {"name": "Neoplasms", "publications": 20},
            {"name": "Inflammation", "publications": 5},
        ])
        self.assertEqual(result["chemicals"], [{"name": "ruxolitinib", "publications": 8}])

    def test_raw_omim_numeric_ids_are_dropped(self):
        def handler(req):
            return json_response([
                {"type": "associate", "source": "@DISEASE_601308", "target": "@GENE_TP53", "publications": 465},
                {"type": "associate", "source": "@DISEASE_Neoplasms", "target": "@GENE_TP53", "publications": 180},
            ])
        result = run(make_client(handler).for_gene("TP53"))
        self.assertEqual(result["diseases"], [{"name": "Neoplasms", "publications": 180}])

    def test_no_data_for_the_symbol_is_two_empty_lists(self):
        result = run(make_client(lambda req: json_response([])).for_gene("mecA"))
        self.assertEqual(result, {"diseases": [], "chemicals": []})

    def test_capped_at_max_items(self):
        many = [
            {"type": "associate", "source": f"@DISEASE_D{i}", "target": "@GENE_X", "publications": i}
            for i in range(20)
        ]
        result = run(make_client(lambda req: json_response(many)).for_gene("X"))
        self.assertEqual(len(result["diseases"]), 6)
        self.assertEqual(result["diseases"][0]["name"], "D19")  # highest publication count first

    def test_an_outage_is_swallowed_as_no_data_not_an_exception(self):
        result = run(make_client(lambda req: httpx.Response(500, text="oops")).for_gene("JAK1"))
        self.assertEqual(result, {"diseases": [], "chemicals": []})

    def test_unreadable_json_is_swallowed_as_no_data(self):
        result = run(make_client(lambda req: httpx.Response(200, text="<html>nope</html>")).for_gene("JAK1"))
        self.assertEqual(result, {"diseases": [], "chemicals": []})

    def test_an_outage_is_not_cached_as_no_data(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(500, text="oops")
        cache = MemoryCache()
        client = make_client(handler, cache)
        run(client.for_gene("EGFR"))
        after_first = len(calls)
        run(client.for_gene("EGFR"))
        # the second lookup retries over the network exactly as much as the
        # first: a transient failure must never look like a cached "no data"
        self.assertEqual(len(calls), after_first * 2)
        self.assertEqual(cache.data, {})

    def test_unreadable_json_is_not_cached_either(self):
        cache = MemoryCache()
        client = make_client(lambda req: httpx.Response(200, text="<html>rate limited</html>"), cache)
        run(client.for_gene("EGFR"))
        self.assertEqual(cache.data, {})

    def test_a_genuinely_empty_answer_is_cached(self):
        calls = []

        def handler(req):
            calls.append(1)
            return json_response([])
        cache = MemoryCache()
        client = make_client(handler, cache)
        run(client.for_gene("mecA"))
        run(client.for_gene("mecA"))
        self.assertEqual(len(calls), 2)  # not 4: the second lookup was served from cache

    def test_second_lookup_of_the_same_gene_is_cached(self):
        calls = []

        def handler(req):
            calls.append(1)
            return json_response([])
        cache = MemoryCache()
        client = make_client(handler, cache)

        async def twice():
            return [await client.for_gene("JAK1"), await client.for_gene("JAK1")]
        run(twice())
        self.assertEqual(len(calls), 2)  # one for disease, one for chemical -- not doubled on the second call


if __name__ == "__main__":
    unittest.main()
