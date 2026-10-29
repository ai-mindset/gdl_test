"""Offline regression checks for the peer-review findings."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

import harness as h


def response(text="", rid=1, status=200, error=None):
    body = {"jsonrpc": "2.0", "id": rid}
    body.update(
        {"error": error}
        if error is not None
        else {"result": {"content": [{"type": "text", "text": text}]}}
    )
    return {
        "status": status,
        "body": body,
        "latency_ms": 1,
        "request": {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "search_guidance", "arguments": {}},
        },
    }


class ResponseTests(unittest.TestCase):
    def test_http_failure_cannot_be_tool_success(self):
        self.assertEqual(h.classify(response(status=500)), "http_500")

    def test_id_mismatch_and_invalid_envelopes(self):
        r = response()
        r["body"]["id"] = 2
        self.assertEqual(h.classify(r), "invalid_response")
        for body in [
            [],
            None,
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        ]:
            self.assertEqual(h.classify(dict(r, body=body)), "invalid_response")

    def test_sse_skips_notification_and_other_request_id(self):
        raw = (
            b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            b'data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n'
            b'data: {"jsonrpc":"2.0","id":1,\n'
            b'data: "result":{"content":[]}}\n\n'
        )
        self.assertEqual(
            h.parse_body(raw, "text/event-stream", 1)["result"], {"content": []}
        )

    def test_sse_request_reads_until_matching_response(self):
        class Stream(io.BytesIO):
            status = 200
            url = "https://example.test/mcp"

            def __init__(self, data):
                super().__init__(data)
                self.headers = {"Content-Type": "text/event-stream"}

            def read(self, *args):
                raise AssertionError("must not read the entire SSE body")

        raw = (
            b'data: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\nunclosed stream'
        )
        with patch.object(h.urllib.request, "urlopen", return_value=Stream(raw)):
            r = h.request("https://example.test/mcp", {"jsonrpc": "2.0", "id": 1})
        self.assertEqual(h.classify(r), "tool_ok")
        self.assertEqual(r["request_headers"]["Accept"], h.ACCEPT)

    def test_network_and_truncated_response_failures_are_recorded(self):
        for operation in [h.request, h.fetch_raw]:
            for error in [
                h.urllib.error.URLError("offline"),
                TimeoutError("timed out"),
                IncompleteRead(b"partial"),
            ]:
                with self.subTest(
                    operation=operation.__name__, error=type(error).__name__
                ):
                    with patch.object(h.urllib.request, "urlopen", side_effect=error):
                        result = operation("https://example.test/mcp")
                    self.assertEqual(result["status"], 0)
                    self.assertIn(type(error).__name__, result["error"])
                    self.assertGreaterEqual(result["latency_ms"], 0)

    def test_http_errors_preserve_status_url_and_response_body(self):
        url = "https://example.test/mcp"
        body = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"busy"}}'
        for operation in [h.request, h.fetch_raw]:
            with self.subTest(operation=operation.__name__):
                headers = Message()
                headers["Content-Type"] = "application/json"
                error = h.urllib.error.HTTPError(
                    url, 503, "Unavailable", headers, io.BytesIO(body)
                )
                with patch.object(h.urllib.request, "urlopen", side_effect=error):
                    result = operation(url)
                self.assertEqual(result["status"], 503)
                if operation is h.request:
                    self.assertEqual(result["final_url"], url)
                    self.assertEqual(result["body"]["error"]["message"], "busy")
                else:
                    self.assertEqual(result["final"], url)
                    self.assertEqual(result["html"], body.decode())

    def test_success_does_not_prove_clamp(self):
        suite = json.loads(h.SUITE.read_text())
        for key in ["G5b", "G7b"]:
            e = next(e for e in suite if e["id"] == key)
            self.assertFalse(h.entry_check(dict(e, responses=[response("")])))
        e = next(e for e in suite if e["id"] == "G5b")
        self.assertFalse(
            h.entry_check(
                dict(
                    e,
                    responses=[
                        response("".join(f"## {i}. Heading\n" for i in range(1, 12)))
                    ],
                )
            )
        )
        self.assertTrue(
            h.entry_check(dict(e, responses=[response("## 1. Relevant\n")]))
        )
        e = next(e for e in suite if e["id"] == "G7b")
        self.assertFalse(
            h.entry_check(
                dict(e, responses=[response("# Change notes, last 999 days\n")])
            )
        )
        self.assertTrue(
            h.entry_check(
                dict(e, responses=[response("# Change notes, last 730 days\n")])
            )
        )

    def test_errors_do_not_prove_repeatability(self):
        r = response(error={"code": -32603, "message": "failure"})
        self.assertFalse(
            h.entry_check({"kind": "dedupe", "responses": [r, r], "identical": True})
        )

    def test_schema_check_is_limited_but_catches_bad_types_and_bounds(self):
        self.assertTrue(
            h.schema_sanity(
                {
                    "type": "object",
                    "properties": {"x": {"type": "nonsense", "minimum": "one"}},
                }
            )
        )
        self.assertFalse(
            h.schema_sanity(
                {
                    "type": "object",
                    "properties": {"x": {"type": "integer", "minimum": 1}},
                }
            )
        )
        self.assertTrue(h.schema_sanity([]))

    def test_lifecycle_notification_and_versioned_request(self):
        calls = []

        def rpc(method, params, rid, **kw):
            calls.append((method, params, rid, kw))
            if method == "initialize":
                return {
                    "status": 200,
                    "body": {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "protocolVersion": h.PROTOCOLS[0],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "test", "version": "1"},
                        },
                    },
                    "response_headers": {"Mcp-Session-Id": "test-session"},
                }
            if method == "notifications/initialized":
                return {
                    "status": 202,
                    "raw_body": "",
                    "request": {"jsonrpc": "2.0", "method": method},
                }
            return {
                "status": 200,
                "body": {"jsonrpc": "2.0", "id": rid, "result": {"tools": []}},
            }

        entry = {}
        with patch.object(h, "rpc", side_effect=rpc):
            h.run_initialize(entry, 100)
        self.assertTrue(entry["lifecycle_pass"])
        self.assertIsNone(calls[1][2])
        self.assertEqual(
            calls[2][3]["headers"],
            {"MCP-Protocol-Version": h.PROTOCOLS[0], "Mcp-Session-Id": "test-session"},
        )

    def test_failed_initialization_stops_lifecycle(self):
        entry = {}
        with patch.object(h, "rpc", return_value=response(status=401)) as rpc:
            h.run_initialize(entry, 100)
        self.assertFalse(entry["lifecycle_pass"])
        self.assertEqual(rpc.call_count, 1)

    def test_malformed_initialization_is_retained_without_crashing(self):
        for result in [
            None,
            [],
            {"protocolVersion": h.PROTOCOLS[0], "capabilities": None},
        ]:
            entry = {}
            r = {"status": 200, "body": {"jsonrpc": "2.0", "id": 100, "result": result}}
            with patch.object(h, "rpc", return_value=r):
                h.run_initialize(entry, 100)
            self.assertFalse(entry["lifecycle_pass"])
            self.assertEqual(entry["responses"], [r])


class EvidenceTests(unittest.TestCase):
    def test_published_counts_and_latency_are_reconciled(self):
        m = h.metrics(h.BASE / "logs")
        self.assertEqual(m["logged_entries"], 43)
        self.assertEqual(m["http_calls"], 46)
        self.assertEqual(m["baseline"]["http_calls"], 44)
        self.assertEqual(m["followups"]["http_calls"], 2)
        self.assertEqual(m["latency_ms"]["max"], 543.2)
        self.assertEqual(m["latency_ms"]["p95"], 505.5)
        self.assertEqual(m["outcomes"]["tool_ok"], 39)
        self.assertEqual(sum(m["mechanical_checks"].values()), 41)
        self.assertEqual(
            {key for key, passed in m["mechanical_checks"].items() if not passed},
            {"G5a", "G7a"},
        )

    def test_followup_urls_are_included(self):
        urls = h.collect_urls(h.BASE / "logs")
        self.assertEqual(len(urls), 287)
        self.assertIn(
            "https://www.gov.uk/government/publications/data-ethics-framework/"
            "data-and-ai-ethics-framework#using-the-algorithmic-transparency-recording-standard",
            urls,
        )

    def test_recorded_endpoint_does_not_depend_on_current_environment(self):
        with patch.object(h, "API_URL", "https://different.example/mcp"):
            m = h.metrics(h.BASE / "logs")
        self.assertEqual(
            m["endpoint"], "https://publicdigital-delivery-compass.netlify.app/mcp"
        )
        self.assertEqual(m["run_suite_entries"], 43)

    def test_distinct_new_calls_are_not_deduplicated_by_body(self):
        a, b = response(), response()
        a["call_id"], b["call_id"] = "first", "second"
        entries = [{"id": "A", "responses": [a]}, {"id": "B", "responses": [b]}]
        self.assertEqual(len(h.unique_responses(entries)), 2)

    def test_legacy_embedded_repeat_is_counted_once(self):
        a = response()
        entries = [{"id": "A", "responses": [a]}, {"id": "G8", "responses": [a]}]
        self.assertEqual(len(h.unique_responses(entries)), 1)

    def test_skipped_repeat_metrics_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as d:
            h.write_json(
                Path(d) / "G8.json",
                {
                    "id": "G8",
                    "kind": "repeat",
                    "responses": [],
                    "skipped": "failed prerequisite",
                    "identical": None,
                },
            )
            m = h.metrics(Path(d))
        self.assertFalse(m["repeat_comparison"]["valid"])
        self.assertIsNone(m["repeat_comparison"]["parsed_result_equal"])


class TextAndLinkTests(unittest.TestCase):
    def check(self, html, text, ctype="text/html"):
        return h.text_check(
            "https://example.test", text, {"status": 200, "ctype": ctype, "html": html}
        )

    def test_hidden_script_and_style_do_not_match(self):
        self.assertFalse(
            self.check(
                "<SCRIPT>invented quote</SCRIPT><style>invented quote</style><p>Real text</p>",
                "invented quote",
            )["found"]
        )

    def test_entities_inline_punctuation_and_typography_match(self):
        self.assertTrue(
            self.check(
                "<p>Data &amp; AI: <a>Commercial</a>.</p>", "Data & AI: Commercial."
            )["found"]
        )
        self.assertTrue(
            self.check("<p>“AI”\n should work</p>", '"AI" should work')["found"]
        )

    def test_block_boundaries_do_not_concatenate_words(self):
        self.assertEqual(h.page_text("<p>First</p><p>Second</p>"), "First Second")

    def test_empty_quote_and_pdf_are_inconclusive(self):
        self.assertIsNone(self.check("<p>Text</p>", "")["found"])
        self.assertIsNone(self.check("%PDF", "quote", "application/pdf")["found"])

    def test_arbitrary_pdf_page_is_not_verified(self):
        r = h.check_link(
            "https://example.test/a.pdf#page=999999",
            {"status": 200, "ctype": "application/pdf", "html": "%PDF"},
        )
        self.assertIsNone(r["ok"])
        self.assertIsNone(r["fragment_verified"])

    def test_highlights_are_explicitly_unverified(self):
        r = h.check_link(
            "https://example.test/#:~:text=missing",
            {"status": 200, "ctype": "text/html", "html": "<p>Something else</p>"},
        )
        self.assertIsNone(r["ok"])
        self.assertEqual(r["verification"], "highlight_unverified")

    def test_encoded_anchor_and_missing_anchor(self):
        page = {"status": 200, "ctype": "text/html", "html": '<h2 id="an id">Text</h2>'}
        self.assertTrue(h.check_link("https://example.test/#an%20id", page)["ok"])
        self.assertFalse(h.check_link("https://example.test/#absent", page)["ok"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def suite(self, entries):
        path = self.base / "suite.json"
        h.write_json(path, entries)
        return path

    def test_existing_evidence_and_invalid_selection_preserved(self):
        logs = self.base / "logs"
        logs.mkdir()
        sentinel = logs / "evidence.txt"
        sentinel.write_text("keep")
        suite = self.suite([{"id": "A1", "tool": "search_guidance", "args": {}}])
        with self.assertRaises(FileExistsError):
            h.run(["A1"], logs, suite)
        with self.assertRaises(ValueError):
            h.run(["unknown"], self.base / "unused", suite)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((self.base / "unused").exists())

    def test_run_records_provenance_without_duplicate_snapshots(self):
        suite = self.suite(
            [{"id": "A1", "tool": "search_guidance", "args": {"question": "AI"}}]
        )
        output = self.base / "run"
        with (
            patch.object(h, "call", return_value=response("Guidance")),
            patch.object(h.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = h.run(None, output, suite)
        self.assertTrue(result["passed"])
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {"A1.json", "run.json", "metrics.json"},
        )
        manifest = json.loads((output / "run.json").read_text())
        self.assertEqual(manifest["entry_ids"], ["A1"])
        self.assertEqual(
            manifest["suite_sha256"], hashlib.sha256(suite.read_bytes()).hexdigest()
        )
        self.assertEqual(
            manifest["harness_sha256"],
            hashlib.sha256(Path(h.__file__).read_bytes()).hexdigest(),
        )
        self.assertIn("started", manifest)
        self.assertIn("endpoint", manifest)

    def test_chain_resolves_dependencies_and_logs_safe_skip(self):
        suite = self.suite(
            [
                {"id": "B2", "kind": "call", "tool": "search_guidance", "args": {}},
                {
                    "id": "B7",
                    "kind": "chain",
                    "from": ["B2"],
                    "follow_up_question": "AI",
                },
            ]
        )
        with (
            patch.object(h, "call", return_value=response("no source")),
            patch.object(h.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = h.run(["B7"], self.base / "run", suite)
        self.assertFalse(result["passed"])
        b7 = json.loads((self.base / "run" / "B7.json").read_text())
        self.assertEqual(b7["responses"], [])
        self.assertIn("skipped", b7)
        self.assertTrue((self.base / "run" / "B2.json").exists())

    def test_failed_check_exits_nonzero(self):
        with patch.object(h, "run", return_value={"passed": False}):
            self.assertEqual(h.main(["run"]), 1)

    def test_bad_links_and_incomplete_links_have_distinct_exit_codes(self):
        with (
            patch.object(h, "load_entries", return_value=[{}]),
            patch.object(h, "links", return_value=[{"url": "x", "ok": False}]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(h.main(["links"]), 1)
        with (
            patch.object(h, "load_entries", return_value=[{}]),
            patch.object(h, "links", return_value=[{"url": "x", "ok": None}]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(h.main(["links"]), 2)

    def test_two_failed_calls_cannot_produce_successful_repeat(self):
        suite = self.suite(
            [
                {"id": "A1", "tool": "search_guidance", "args": {}},
                {"id": "G8", "kind": "repeat", "from": ["A1"]},
            ]
        )
        with (
            patch.object(
                h,
                "call",
                return_value=response(error={"code": -32603, "message": "bad"}),
            ),
            patch.object(h.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = h.run(["G8"], self.base / "run", suite)
        self.assertFalse(result["passed"])
        g8 = json.loads((self.base / "run" / "G8.json").read_text())
        self.assertIsNone(g8["identical"])

    def test_malformed_meta_result_is_logged_as_failure(self):
        suite = self.suite([{"id": "G2", "kind": "meta"}])
        r = {"status": 200, "body": {"jsonrpc": "2.0", "id": 110, "result": None}}
        with (
            patch.object(h, "rpc", return_value=r),
            patch.object(h.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = h.run(None, self.base / "run", suite)
        self.assertFalse(result["passed"])
        self.assertEqual(
            json.loads((self.base / "run" / "G2.json").read_text())["responses"], [r]
        )

    def test_dependency_cycle_rejected_before_run(self):
        with self.assertRaises(ValueError):
            h.select_entries(
                [
                    {"id": "A", "kind": "repeat", "from": ["B"]},
                    {"id": "B", "kind": "repeat", "from": ["A"]},
                ],
                ["A"],
            )


class BootstrapTests(unittest.TestCase):
    def test_installed_uv_creates_local_environment_without_downloads(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                h.shutil,
                "which",
                side_effect=lambda x: {
                    "uv": "/usr/bin/uv",
                    "python3": "/usr/bin/python3",
                }.get(x),
            ),
            patch.object(h.subprocess, "run") as run,
            patch.object(h.os, "execv") as execute,
            patch("builtins.input") as prompt,
        ):
            h.ensure_environment(Path(d))
            self.assertIn("--no-python-downloads", run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][-1], str(Path(d) / ".venv"))
            self.assertEqual(execute.call_args.args[0], "/usr/bin/uv")
            prompt.assert_not_called()

    def test_declined_install_uses_local_python_venv_before_execution(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                h.shutil,
                "which",
                side_effect=lambda x: "/usr/bin/python3" if x == "python3" else None,
            ),
            patch("builtins.input", return_value="no"),
            patch.object(h.subprocess, "run") as run,
            patch.object(h.os, "execv") as execute,
            patch.object(h.urllib.request, "urlopen") as fetch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            h.ensure_environment(Path(d))
            self.assertEqual(
                run.call_args.args[0],
                [
                    "/usr/bin/python3",
                    "-m",
                    "venv",
                    "--without-pip",
                    str(Path(d) / ".venv"),
                ],
            )
            self.assertEqual(
                execute.call_args.args[0], str(Path(d) / ".venv" / "bin" / "python")
            )
            fetch.assert_not_called()

    def test_missing_local_python_stops_before_any_command(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(h.shutil, "which", return_value=None),
            patch("builtins.input", return_value="n"),
            patch.object(h.subprocess, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "No local python3"):
                h.ensure_environment(Path(d))
            run.assert_not_called()

    def test_no_input_is_not_installation_consent(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                h.shutil,
                "which",
                side_effect=lambda x: "/usr/bin/python3" if x == "python3" else None,
            ),
            patch("builtins.input", side_effect=EOFError),
            patch.object(h.subprocess, "run"),
            patch.object(h.os, "execv"),
            patch.object(h.urllib.request, "urlopen") as fetch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            h.ensure_environment(Path(d))
            fetch.assert_not_called()

    def test_bootstrap_restarts_the_original_entry_script(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                h.shutil,
                "which",
                side_effect=lambda x: {
                    "uv": "/usr/bin/uv",
                    "python3": "/usr/bin/python3",
                }.get(x),
            ),
            patch.object(h.subprocess, "run"),
            patch.object(h.os, "execv") as execute,
            patch.object(h.sys, "argv", ["verify_sources.py", "--output", "evidence"]),
        ):
            h.ensure_environment(Path(d))
            self.assertEqual(
                execute.call_args.args[1][-3:],
                [str(Path("verify_sources.py").resolve()), "--output", "evidence"],
            )

    def test_installer_runs_only_after_explicit_consent(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            events = []

            def consent(_):
                events.append("consent")
                return "yes"

            def command(args, **kwargs):
                events.append("install" if args == ["/bin/sh"] else "venv")
                if args == ["/bin/sh"]:
                    (base / ".tools").mkdir()
                    (base / ".tools" / "uv").touch()
                    self.assertEqual(
                        kwargs["env"]["UV_INSTALL_DIR"], str(base / ".tools")
                    )

            with (
                patch.object(
                    h.shutil,
                    "which",
                    side_effect=lambda x: {
                        "sh": "/bin/sh",
                        "python3": "/usr/bin/python3",
                    }.get(x),
                ),
                patch("builtins.input", side_effect=consent),
                patch.object(
                    h.urllib.request, "urlopen", return_value=io.BytesIO(b"installer")
                ),
                patch.object(h.subprocess, "run", side_effect=command),
                patch.object(h.os, "execv"),
            ):
                h.ensure_environment(base)
            self.assertEqual(events, ["consent", "install", "venv"])


if __name__ == "__main__":
    unittest.main()
