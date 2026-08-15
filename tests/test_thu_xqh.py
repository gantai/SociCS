"""Tests against a local stand-in for the archive server.

The real host cannot be reached from CI, so the fake reproduces the behaviours
that actually matter: paginated index pages, real PDFs, plain 404s, and the
classic trap of a "not found" HTML page served with HTTP 200.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from thu_xqh import collections as collib, discover, ids as idlib  # noqa: E402
from thu_xqh.collections import Collection  # noqa: E402
from thu_xqh.cli import main  # noqa: E402
from thu_xqh.client import PoliteClient, RateLimiter  # noqa: E402
from thu_xqh.download import download_one, probe  # noqa: E402
from thu_xqh.manifest import ERROR, MISSING, OK, Entry, Manifest  # noqa: E402

PDF_BODY = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
TRUNCATED_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n"
NOT_FOUND_HTML = b"<html><body><h1>404</h1><p>the requested issue does not exist</p></body></html>"

EXISTING = {
    "xqh": {
        "0001": PDF_BODY,
        "0004": PDF_BODY,
        "1670": PDF_BODY,
        "1670Z22": PDF_BODY,
        "0900": TRUNCATED_PDF,
    },
    # A second journal, to prove nothing is hardcoded to XQH.
    "qhzk": {"0007": PDF_BODY, "0008": PDF_BODY},
}
# Ids each index knows about, in listing order.
INDEXED = {
    "XQH": ["0001", "0004", "0900", "1670", "1670Z22"],
    "QHZK": ["0007", "0008"],
}
PAGE_SIZE = 2

XQH = collib.XQH
QHZK = Collection(code="QHZK", name="清华周刊", sys_id="24", first=1, last=8,
                  first_year=1953, last_year=1954)

# When true the index links only to detail pages, never straight to a PDF --
# the shape the live site is suspected to use.
DETAIL_ONLY = {"value": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    reject_head = False
    hits: list = []

    def log_message(self, *args):
        pass

    # -- helpers ---------------------------------------------------------------

    def _send(self, status, body: bytes, ctype: str, head_only: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _route(self, head_only: bool):
        parts = urllib.parse.urlsplit(self.path)
        Handler.hits.append(self.path)

        if parts.path == "/robots.txt":
            return self._send(200, b"User-agent: *\nAllow: /\n", "text/plain", head_only)

        if parts.path.startswith("/swfPath/") and parts.path.endswith(".pdf"):
            _, _, code, filename = parts.path.split("/", 3)
            issue_id = filename[: -len(".pdf")]
            if issue_id == "0003":  # 200 + HTML: the trap
                return self._send(200, NOT_FOUND_HTML, "text/html", head_only)
            body = EXISTING.get(code, {}).get(issue_id)
            if body is None:
                return self._send(404, NOT_FOUND_HTML, "text/html", head_only)
            requested = self.headers.get("Range")
            if requested and requested.startswith("bytes=0-"):
                end = int(requested.split("-", 1)[1] or len(body) - 1)
                chunk = body[: end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(chunk)
                return None
            return self._send(200, body, "application/pdf", head_only)

        if parts.path == collib.INDEX_PATH:
            return self._send(200, self._index_page(parts.query), "text/html; charset=utf-8",
                              head_only)

        if parts.path in ("/", collib.FIRST_INDEX_PATH):
            return self._send(200, self._home_page(), "text/html; charset=utf-8", head_only)

        if parts.path.lower() == "/detaliswfinfo":
            return self._send(200, self._detail_page(parts.query),
                              "text/html; charset=utf-8", head_only)

        return self._send(404, b"nope", "text/plain", head_only)

    def _home_page(self) -> bytes:
        rows = "".join(
            f'<a href="{collib.INDEX_PATH}?sysId={c.sys_id}&amp;displayDBCode={c.code}'
            f'&amp;displayDBName={urllib.parse.quote(c.name)}">{c.name}</a>'
            for c in (XQH, QHZK)
        )
        return f"<html><body><nav>{rows}</nav></body></html>".encode("utf-8")

    def _detail_page(self, query: str) -> bytes:
        params = urllib.parse.parse_qs(query)
        code = (params.get("dbName", ["XQH"])[0]).upper()
        issue_id = params.get("issue", [""])[0]
        link = f'<a href="/swfPath/{code.lower()}/{issue_id}.pdf">full text</a>'
        return f"<html><body><h1>{issue_id}</h1>{link}</body></html>".encode("utf-8")

    def _index_page(self, query: str) -> bytes:
        params = urllib.parse.parse_qs(query)
        code = (params.get("displayDBCode", ["XQH"])[0]).upper()
        listed = INDEXED.get(code, [])
        page = int(params.get("page", ["1"])[0])
        start = (page - 1) * PAGE_SIZE
        chunk = listed[start : start + PAGE_SIZE]
        if DETAIL_ONLY["value"]:
            rows = "".join(
                f'<li><a href="/DetaliSwfInfo?dbName={code}&amp;sysID=13571{n}'
                f'&amp;issue={i}">issue</a></li>'
                for n, i in enumerate(chunk)
            )
        else:
            rows = "".join(
                f'<li><a href="/swfPath/{code.lower()}/{i}.pdf">issue {i}</a></li>'
                for i in chunk
            )
        base = {k: v[0] for k, v in params.items()}
        links = ""
        total_pages = max(1, (len(listed) + PAGE_SIZE - 1) // PAGE_SIZE)
        for n in range(1, total_pages + 1):
            nav = dict(base, page=str(n))
            links += (f'<a href="{collib.INDEX_PATH}?'
                      f'{urllib.parse.urlencode(nav)}">{n}</a> ')
        # Stray four-digit numbers, exactly like a real page has.
        noise = '<span class="count">1953</span><div id="0002x"></div>'
        return (f"<html><body><ul>{rows}</ul><nav>{links}</nav>{noise}"
                f"</body></html>").encode("utf-8")

    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        if Handler.reject_head:
            return self._send(405, b"", "text/plain", head_only=True)
        self._route(head_only=True)


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.reject_head = False
        Handler.hits = []
        DETAIL_ONLY["value"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def client(self, **kwargs):
        kwargs.setdefault("delay", 0.0)
        client = PoliteClient(self.base_url, **kwargs)
        self.addCleanup(client.close)
        return client


class TestIds(unittest.TestCase):
    def test_normalize_and_split(self):
        self.assertEqual(idlib.normalize("1670z22"), "1670Z22")
        self.assertEqual(idlib.normalize(" 0001 "), "0001")
        self.assertIsNone(idlib.normalize("123"))
        self.assertIsNone(idlib.normalize("abcd"))
        self.assertIsNone(idlib.normalize("1670ZZ2"))
        self.assertEqual(idlib.split_id("1670Z22"), (1670, "Z22"))
        self.assertEqual(idlib.split_id("0001"), (1, ""))
        self.assertTrue(idlib.is_supplement("1670Z22"))
        self.assertFalse(idlib.is_supplement("1670"))

    def test_range_filter_rejects_stray_numbers(self):
        self.assertTrue(idlib.in_range("1670"))
        self.assertFalse(idlib.in_range("1953"))  # a year, not an issue
        self.assertFalse(idlib.in_range("0000"))

    def test_base_range(self):
        r = idlib.base_range(1, 1670)
        self.assertEqual(len(r), 1670)
        self.assertEqual(r[0], "0001")
        self.assertEqual(r[-1], "1670")

    def test_suffix_spec(self):
        self.assertEqual(idlib.parse_suffix_spec("Z{1..3}"), ["Z1", "Z2", "Z3"])
        self.assertEqual(idlib.parse_suffix_spec("Z{01..03}"), ["Z01", "Z02", "Z03"])
        self.assertEqual(idlib.parse_suffix_spec("Z1,S1,Z1"), ["Z1", "S1"])
        with self.assertRaises(ValueError):
            idlib.parse_suffix_spec("Z{5..1}")

    def test_sorting_puts_supplement_after_its_base(self):
        self.assertEqual(
            idlib.sorted_ids(["1670Z22", "0002", "1670", "0001"]),
            ["0001", "0002", "1670", "1670Z22"],
        )

    def test_id_file_parsing(self):
        text = "0001\n# comment\n0002, 0003\n\n1670Z22 # trailing\ngarbage\n"
        self.assertEqual(
            list(idlib.iter_id_file(text)), ["0001", "0002", "0003", "1670Z22"]
        )


class TestExtraction(unittest.TestCase):
    def test_confirmed_vs_candidate(self):
        html = (
            '<a href="/swfPath/xqh/1670Z22.pdf">x</a>'
            '<a href="/QHHome/Detail?id=0004">y</a>'
            '<span>1953</span>'
        )
        confirmed, candidates = discover.extract_ids(html, XQH)
        self.assertEqual(confirmed, {"1670Z22"})
        self.assertEqual(candidates, {"0004"})  # 1953 is out of range, dropped

    def test_case_insensitive_href(self):
        confirmed, _ = discover.extract_ids('<a href="/SWFPATH/XQH/0001.PDF">x</a>', XQH)
        self.assertEqual(confirmed, {"0001"})


class TestRateLimiter(unittest.TestCase):
    def test_slow_down_is_monotonic(self):
        limiter = RateLimiter(1.0)
        first = limiter.slow_down()
        second = limiter.slow_down()
        self.assertGreater(first, 1.0)
        self.assertGreater(second, first)
        self.assertEqual(limiter.throttle_events, 2)

    def test_slow_down_respects_ceiling(self):
        limiter = RateLimiter(1.0, max_delay=4.0)
        for _ in range(10):
            limiter.slow_down()
        self.assertEqual(limiter.delay, 4.0)


class TestManifest(unittest.TestCase):
    def test_roundtrip_and_resume_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            manifest = Manifest(path)
            manifest.record(Entry(id="0001", status=OK, size=10))
            manifest.record(Entry(id="0002", status=MISSING))
            manifest.record(Entry(id="0003", status=ERROR))
            manifest.save()

            reloaded = Manifest.load(path)
            self.assertTrue(reloaded.is_done("0001"))
            self.assertTrue(reloaded.is_done("0002"))
            self.assertFalse(reloaded.is_done("0002", recheck_missing=True))
            self.assertFalse(reloaded.is_done("0003"))  # errors are retried
            self.assertFalse(reloaded.is_done("9999"))
            self.assertEqual(reloaded.counts()[OK], 1)

    def test_attempts_accumulate(self):
        manifest = Manifest("/dev/null")
        manifest.record(Entry(id="0001", status=ERROR))
        manifest.record(Entry(id="0001", status=OK))
        self.assertEqual(manifest.get("0001").attempts, 2)

    def test_corrupt_manifest_is_preserved_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            with open(path, "w") as handle:
                handle.write("{not json")
            manifest = Manifest.load(path)
            self.assertEqual(manifest.entries, {})
            self.assertTrue(os.path.exists(path + ".corrupt"))


class TestDownload(ServerTestCase):
    def test_downloads_a_real_pdf(self):
        client = self.client()
        entry = download_one(client, XQH, "0001", self.tmp.name)
        self.assertEqual(entry.status, OK)
        self.assertEqual(entry.size, len(PDF_BODY))
        self.assertTrue(entry.sha256)
        path = os.path.join(self.tmp.name, "0001.pdf")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), PDF_BODY)

    def test_downloads_a_supplement(self):
        entry = download_one(self.client(), XQH, "1670Z22", self.tmp.name)
        self.assertEqual(entry.status, OK)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "1670Z22.pdf")))

    def test_404_is_recorded_as_absent_with_no_file_left_behind(self):
        entry = download_one(self.client(), XQH, "0002", self.tmp.name)
        self.assertEqual(entry.status, MISSING)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_html_error_page_with_status_200_is_not_saved(self):
        entry = download_one(self.client(), XQH, "0003", self.tmp.name)
        self.assertEqual(entry.status, MISSING)
        self.assertIn("not a PDF", entry.note)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_pdf_without_eof_is_kept_but_flagged(self):
        entry = download_one(self.client(), XQH, "0900", self.tmp.name)
        self.assertEqual(entry.status, OK)
        self.assertIn("EOF", entry.note)

    def test_no_part_files_survive(self):
        client = self.client()
        for issue_id in ("0001", "0002", "0003"):
            download_one(client, XQH, issue_id, self.tmp.name)
        self.assertFalse([f for f in os.listdir(self.tmp.name) if f.endswith(".part")])


class TestProbe(ServerTestCase):
    def test_head_probe(self):
        client = self.client()
        self.assertIs(probe(client, XQH, "1670Z22"), True)
        self.assertIs(probe(client, XQH, "0002"), False)
        self.assertIs(probe(client, XQH, "0003"), False)  # HTML content type

    def test_falls_back_to_ranged_get_when_head_is_rejected(self):
        Handler.reject_head = True
        client = self.client()
        self.assertIs(probe(client, XQH, "1670Z22"), True)
        self.assertIs(probe(client, XQH, "0002"), False)
        self.assertTrue(any("Range" for h in Handler.hits))

    def test_probe_sweep_stops_after_misses(self):
        client = self.client()
        found = discover.probe_supplements(
            client, XQH, ["1670"], idlib.parse_suffix_spec("Z{1..30}"),
            stop_after_misses=3, log=lambda m: None,
        )
        self.assertEqual(found, [])
        # Z1, Z2, Z3 then give up -- not all thirty.
        self.assertLessEqual(len([h for h in Handler.hits if ".pdf" in h]), 6)

    def test_probe_sweep_finds_the_supplement(self):
        client = self.client()
        found = discover.probe_supplements(
            client, XQH, ["1670"], ["Z21", "Z22", "Z23"], log=lambda m: None
        )
        self.assertEqual(found, ["1670Z22"])


class TestCatalog(ServerTestCase):
    def test_catalog_walk_follows_pagination_and_finds_everything(self):
        catalog = discover.catalog_scan(
            self.client(), XQH, coverage_target=0.0,
            max_level="all", log=lambda m: None,
        )
        self.assertEqual(catalog.confirmed, set(INDEXED["XQH"]))
        self.assertEqual(catalog.supplements(), ["1670Z22"])
        self.assertGreater(catalog.pages_fetched, 1)

    def test_catalog_escalates_when_coverage_is_short(self):
        catalog = discover.catalog_scan(
            self.client(), Collection(code="XQH", name="新清华", first=1, last=5,
                                      first_year=1953, last_year=1954),
            coverage_target=0.99, max_level="month", log=lambda m: None,
        )
        # Coverage can never be met here, so it must have tried both levels.
        self.assertEqual(catalog.levels_tried, ["all", "month"])

    def test_catalog_stops_at_the_cheapest_sufficient_level(self):
        catalog = discover.catalog_scan(
            self.client(), XQH, coverage_target=0.0,
            max_level="year-month", log=lambda m: None,
        )
        self.assertEqual(catalog.levels_tried, ["all"])

    def test_budget_math(self):
        self.assertEqual(discover.probe_budget(1670, 30), 100200)


class TestCli(ServerTestCase):
    def run_cli(self, command, *args):
        return main([command, "--base-url", self.base_url, "--delay", "0", *args])

    def test_discover_then_download_then_verify(self):
        ids_file = os.path.join(self.tmp.name, "ids.txt")
        dest = os.path.join(self.tmp.name, "pdfs")

        code = self.run_cli(
            "discover", "--first", "1", "--last", "1670", "-q",
            "--out", ids_file, "--max-level", "all", "--coverage-target", "0",
        )
        self.assertEqual(code, 0)
        with open(ids_file, encoding="utf-8") as handle:
            listed = list(idlib.iter_id_file(handle.read()))
        self.assertIn("1670Z22", listed)
        self.assertIn("0001", listed)
        self.assertEqual(len(listed), 1671)  # 1670 base ids + one supplement

        # Download a slice, including the supplement and both flavours of absent.
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n0002\n0003\n1670Z22\n")

        code = self.run_cli("download", "-q", "--ids", subset, "--dest", dest)
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(os.listdir(dest)), ["0001.pdf", "1670Z22.pdf", "manifest.json"]
        )

        manifest = Manifest.load(os.path.join(dest, "manifest.json"))
        self.assertEqual(manifest.get("0001").status, OK)
        self.assertEqual(manifest.get("0002").status, MISSING)
        self.assertEqual(manifest.get("0003").status, MISSING)
        self.assertEqual(manifest.get("1670Z22").status, OK)

        self.assertEqual(self.run_cli("verify", "-q", "--dest", dest), 0)
        self.assertEqual(self.run_cli("status", "-q", "--dest", dest), 0)

    def test_rerun_is_a_no_op(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n0002\n")

        self.run_cli("download", "-q", "--ids", subset, "--dest", dest)
        Handler.hits = []
        self.run_cli("download", "-q", "--ids", subset, "--dest", dest)
        self.assertEqual([h for h in Handler.hits if ".pdf" in h], [])

    def test_force_refetches_settled_ids(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n")
        self.run_cli("download", "-q", "--ids", subset, "--dest", dest)
        Handler.hits = []
        self.run_cli("download", "-q", "--ids", subset, "--dest", dest, "--force")
        self.assertEqual(
            [h for h in Handler.hits if h.endswith(".pdf")], ["/swfPath/xqh/0001.pdf"]
        )

    def test_verify_reports_without_deleting(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n")
        self.run_cli("download", "-q", "--ids", subset, "--dest", dest)
        # Corrupt the file behind the manifest's back.
        with open(os.path.join(dest, "0001.pdf"), "wb") as handle:
            handle.write(b"<html>nope</html>")
        code = self.run_cli("verify", "-q", "--dest", dest)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(dest, "0001.pdf")))
        with open(os.path.join(dest, "retry-ids.txt"), encoding="utf-8") as handle:
            self.assertEqual(list(idlib.iter_id_file(handle.read())), ["0001"])

    def test_limit_caps_the_run(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n0004\n1670\n")
        self.run_cli("download", "-q", "--ids", subset, "--dest", dest, "--limit", "1")
        self.assertEqual(
            [f for f in os.listdir(dest) if f.endswith(".pdf")], ["0001.pdf"]
        )

    def test_max_requests_budget_stops_cleanly(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        subset = os.path.join(self.tmp.name, "subset.txt")
        with open(subset, "w", encoding="utf-8") as handle:
            handle.write("0001\n0004\n1670\n")
        code = self.run_cli(
            "download", "-q", "--max-requests", "2", "--ids", subset, "--dest", dest
        )
        self.assertEqual(code, 0)  # a budget stop is a clean stop
        manifest = Manifest.load(os.path.join(dest, "manifest.json"))
        self.assertEqual(manifest.get("0001").status, OK)  # progress kept

    def test_dry_run_touches_nothing(self):
        dest = os.path.join(self.tmp.name, "pdfs")
        Handler.hits = []
        code = self.run_cli("download", "-q", "--dest", dest, "--dry-run",
                            "--first", "1", "--last", "10")
        self.assertEqual(code, 0)
        self.assertEqual([h for h in Handler.hits if ".pdf" in h], [])


class TestRobots(ServerTestCase):
    def test_disallow_blocks_the_crawl(self):
        client = self.client()
        client._robots = None
        original = Handler.do_GET

        def blocking_get(handler):
            if handler.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /swfPath/\n"
                handler.send_response(200)
                handler.send_header("Content-Type", "text/plain")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return
            original(handler)

        Handler.do_GET = blocking_get
        self.addCleanup(setattr, Handler, "do_GET", original)
        self.assertFalse(client.robots_allows("/swfPath/xqh/0001.pdf"))


class TestCollections(unittest.TestCase):
    def test_pdf_dir_derives_from_code(self):
        self.assertEqual(Collection(code="qhzk").pdf_dir, "/swfPath/qhzk")
        self.assertEqual(Collection(code="QHZK").code, "QHZK")
        self.assertEqual(Collection(code="X", pdf_dir="swfPath/custom/").pdf_dir,
                         "/swfPath/custom")

    def test_pdf_path(self):
        self.assertEqual(XQH.pdf_path("1670Z22"), "/swfPath/xqh/1670Z22.pdf")
        self.assertEqual(QHZK.pdf_path("0007"), "/swfPath/qhzk/0007.pdf")

    def test_unknown_code_resolves_with_derived_defaults(self):
        registry = collib.load_registry("/nonexistent.json")
        resolved = collib.resolve("NEWJ", registry)
        self.assertEqual(resolved.pdf_dir, "/swfPath/newj")
        self.assertFalse(resolved.has_known_range)

    def test_overrides_win(self):
        registry = collib.load_registry("/nonexistent.json")
        resolved = collib.resolve("XQH", registry, first=5, last=9, sys_id="99")
        self.assertEqual((resolved.first, resolved.last, resolved.sys_id), (5, 9, "99"))
        self.assertEqual(resolved.name, "新清华")  # untouched by the override

    def test_registry_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "collections.json")
            collib.save_registry({"QHZK": QHZK}, path)
            loaded = collib.load_registry(path)
            self.assertIn("QHZK", loaded)
            self.assertIn("XQH", loaded)  # built-in survives
            self.assertEqual(loaded["QHZK"].name, "清华周刊")

    def test_reads_codes_out_of_links(self):
        html = (
            '<a href="/QHHome/SecondIndex?sysId=23&amp;displayDBCode=XQH'
            '&amp;displayDBName=%E6%96%B0%E6%B8%85%E5%8D%8E">x</a>'
            '<a href="/DetaliSwfInfo?dbName=QHZK&amp;sysID=135717">y</a>'
            '<a href="/about">z</a>'
        )
        found = collib.collections_in_html(html)
        self.assertEqual(set(found), {"XQH", "QHZK"})
        self.assertEqual(found["XQH"].name, "新清华")
        self.assertEqual(found["XQH"].sys_id, "23")


class TestCollectionDiscovery(ServerTestCase):
    def test_finds_both_journals_on_the_home_page(self):
        found = collib.discover_collections(self.client(), log=lambda m: None)
        self.assertEqual(set(found), {"XQH", "QHZK"})
        self.assertEqual(found["QHZK"].name, "清华周刊")
        self.assertEqual(found["QHZK"].sys_id, "24")

    def test_cli_lists_and_saves(self):
        registry = os.path.join(self.tmp.name, "collections.json")
        code = main(["collections", "--base-url", self.base_url, "--delay", "0",
                     "-q", "--registry", registry, "--save"])
        self.assertEqual(code, 0)
        loaded = collib.load_registry(registry)
        self.assertEqual(set(loaded), {"XQH", "QHZK"})
        # The built-in range for XQH must survive a save from the site.
        self.assertEqual((loaded["XQH"].first, loaded["XQH"].last), (1, 1670))

    def test_offline_listing_makes_no_requests(self):
        Handler.hits = []
        code = main(["collections", "--offline", "-q",
                     "--registry", "/nonexistent.json"])
        self.assertEqual(code, 0)
        self.assertEqual(Handler.hits, [])


class TestSecondCollection(ServerTestCase):
    def run_cli(self, command, *args):
        return main([command, "--base-url", self.base_url, "--delay", "0", *args])

    def test_discover_and_download_a_different_journal(self):
        ids_file = os.path.join(self.tmp.name, "qhzk.txt")
        dest = os.path.join(self.tmp.name, "qhzk")

        code = self.run_cli("discover", "-c", "QHZK", "-q", "--out", ids_file,
                            "--max-level", "all", "--coverage-target", "0")
        self.assertEqual(code, 0)
        with open(ids_file, encoding="utf-8") as handle:
            listed = list(idlib.iter_id_file(handle.read()))
        self.assertEqual(listed, ["0007", "0008"])

        code = self.run_cli("download", "-c", "QHZK", "-q",
                            "--ids", ids_file, "--dest", dest)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(f for f in os.listdir(dest) if f.endswith(".pdf")),
                         ["0007.pdf", "0008.pdf"])
        # It must have used /swfPath/qhzk/, not the XQH directory.
        self.assertTrue(any("/swfPath/qhzk/" in h for h in Handler.hits))
        self.assertFalse(any("/swfPath/xqh/" in h for h in Handler.hits))

    def test_download_without_ids_needs_a_known_range(self):
        code = self.run_cli("download", "-c", "QHZK", "-q",
                            "--dest", os.path.join(self.tmp.name, "d"))
        self.assertEqual(code, 1)

    def test_explicit_range_works_for_an_unknown_journal(self):
        dest = os.path.join(self.tmp.name, "qhzk")
        code = self.run_cli("download", "-c", "QHZK", "-q", "--first", "7", "--last", "8",
                            "--dest", dest)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(f for f in os.listdir(dest) if f.endswith(".pdf")),
                         ["0007.pdf", "0008.pdf"])

    def test_collections_keep_separate_manifests(self):
        root = os.path.join(self.tmp.name, "pdfs")
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)

        self.run_cli("download", "-c", "XQH", "-q", "--first", "1", "--last", "1")
        self.run_cli("download", "-c", "QHZK", "-q", "--first", "7", "--last", "7")
        self.assertTrue(os.path.exists(os.path.join("pdfs", "xqh", "0001.pdf")))
        self.assertTrue(os.path.exists(os.path.join("pdfs", "qhzk", "0007.pdf")))
        # Same id number in two journals must not collide.
        xqh = Manifest.load(os.path.join("pdfs", "xqh", "manifest.json"))
        qhzk = Manifest.load(os.path.join("pdfs", "qhzk", "manifest.json"))
        self.assertIn("0001", xqh.entries)
        self.assertNotIn("0001", qhzk.entries)


class TestDetailPages(ServerTestCase):
    def run_cli(self, command, *args):
        return main([command, "--base-url", self.base_url, "--delay", "0", *args])

    def test_index_with_only_detail_links_yields_nothing_by_default(self):
        DETAIL_ONLY["value"] = True
        catalog = discover.catalog_scan(
            self.client(), XQH, coverage_target=0.0, max_level="all",
            log=lambda m: None,
        )
        self.assertEqual(catalog.confirmed, set())
        self.assertEqual(len(catalog.detail_links), len(INDEXED["XQH"]))

    def test_following_detail_pages_recovers_the_ids(self):
        DETAIL_ONLY["value"] = True
        client = self.client()
        catalog = discover.catalog_scan(
            client, XQH, coverage_target=0.0, max_level="all", log=lambda m: None,
        )
        found = discover.follow_details(
            client, XQH, sorted(catalog.detail_links), log=lambda m: None
        )
        self.assertEqual(found, set(INDEXED["XQH"]))
        self.assertIn("1670Z22", found)

    def test_cli_follow_details(self):
        DETAIL_ONLY["value"] = True
        ids_file = os.path.join(self.tmp.name, "ids.txt")
        code = self.run_cli("discover", "-q", "--out", ids_file, "--only-discovered",
                            "--max-level", "all", "--coverage-target", "0",
                            "--follow-details")
        self.assertEqual(code, 0)
        with open(ids_file, encoding="utf-8") as handle:
            self.assertEqual(list(idlib.iter_id_file(handle.read())),
                             sorted(INDEXED["XQH"], key=idlib.sort_key))

    def test_max_details_caps_the_second_pass(self):
        DETAIL_ONLY["value"] = True
        client = self.client()
        catalog = discover.catalog_scan(
            client, XQH, coverage_target=0.0, max_level="all", log=lambda m: None,
        )
        found = discover.follow_details(
            client, XQH, sorted(catalog.detail_links)[:2], log=lambda m: None
        )
        self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
