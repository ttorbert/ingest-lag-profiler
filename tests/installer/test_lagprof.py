"""Unit tests for scripts/lagprof.py. No cluster needed: a small fake Elasticsearch
and Kibana server answers the calls the installer makes."""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402


# --------------------------------------------------------------------------------------
# Fake cluster
# --------------------------------------------------------------------------------------

class FakeCluster:
    def __init__(self, version="9.5.6", flavor="default", license_type="trial"):
        self.version, self.flavor, self.license_type = version, flavor, license_type
        self.store: dict[str, dict] = {}       # path -> stored body
        self.data_streams_in_use = {"lagprof-results"}
        self.requests: list[tuple[str, str]] = []
        self.workflows_api = True
        self.workflows: dict[str, dict] = {}


def make_handler(fake: FakeCluster):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return raw

        def _send(self, code, obj=None):
            payload = json.dumps(obj if obj is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def handle_any(self, method):
            path = self.path.split("?")[0]
            body = self._body() if method in ("PUT", "POST") else None
            fake.requests.append((method, self.path))
            if path.startswith("/s/"):  # Kibana space prefix
                path = "/" + path.split("/", 3)[3]

            if path == "/" and method == "GET":
                return self._send(200, {"version": {"number": fake.version, "build_flavor": fake.flavor}})
            if path == "/_license":
                return self._send(200, {"license": {"type": fake.license_type, "status": "active"}})
            if path == "/_license/start_trial":
                fake.license_type = "trial"
                return self._send(200, {"acknowledged": True, "trial_was_started": True})
            if path == "/_security/_authenticate":
                auth = self.headers.get("Authorization", "")
                return self._send(200, {"username": "lagprof-owner" if auth.startswith("ApiKey owner") else "elastic"})
            if path.startswith("/api/workflows") and method in ("POST", "PUT"):
                fake.workflow_auth = self.headers.get("Authorization", "")
            if path == "/api/status":
                return self._send(200, {"status": {"overall": {"level": "available"}}})
            if path == "/api/workflows":
                if not fake.workflows_api:
                    return self._send(404, {})
                if method == "POST":
                    import re as _re
                    created = []
                    for w in body["workflows"]:
                        name = _re.search(r"(?m)^name:\s*(\S+)", w["yaml"]).group(1)
                        # Like Kibana: IDs of deleted workflows aren't reused.
                        fake.id_counter = getattr(fake, "id_counter", 0) + 1
                        wid = f"{name}-{fake.id_counter}"
                        fake.workflows[wid] = {"id": wid, "name": name, "yaml": w["yaml"], "valid": True}
                        created.append(fake.workflows[wid])
                    return self._send(200, {"created": created})
                return self._send(200, {"results": list(fake.workflows.values())})
            if path.startswith("/api/workflows/workflow/"):
                wid = path.rsplit("/", 1)[1]
                if wid not in fake.workflows:
                    return self._send(404, {})
                if method == "PUT":
                    fake.workflows[wid].update(yaml=body["yaml"])
                elif method == "DELETE":
                    del fake.workflows[wid]
                    return self._send(200, {})
                return self._send(200, fake.workflows[wid])
            if path == "/_query":
                return self._send(200, {"columns": [], "values": [[1000, 990]]})
            if path == "/lagprof-runs/_search":
                return self._send(404, {"error": "index_not_found_exception"})
            if path.startswith("/_data_stream/") and "*" not in path:
                if method == "PUT":
                    fake.store[path] = {}
                    return self._send(200, {"acknowledged": True})
                if method == "GET":
                    return self._send(200, {"data_streams": [{}]}) if path in fake.store else self._send(404, {})
            if path.startswith("/lagprof-*,") and path.endswith("/_mapping") and method == "PUT":
                fake.store[path] = body  # mapping update on existing lagprof-* indices
                return self._send(200, {"acknowledged": True})
            if path.endswith("/_mapping") and path[:-len("/_mapping")] in fake.store:
                idx = path[:-len("/_mapping")]
                return self._send(200, {idx.lstrip("/"): {"mappings": fake.store[idx]}})
            if path in ("/lagprof-rules", "/lagprof-rule-streams"):
                if method == "PUT":
                    fake.store[path] = {}
                    fake.index_creates = getattr(fake, "index_creates", 0) + 1
                    return self._send(200, {"acknowledged": True})
                if method == "DELETE":
                    fake.store.pop(path, None)
                    return self._send(200, {"acknowledged": True})
                return self._send(200, {}) if path in fake.store else self._send(404, {})

            if path.startswith("/api/dashboards/"):
                key = path
                if method == "PUT":
                    fake.store[key] = body
                    return self._send(200, {"id": path.rsplit("/", 1)[1], "data": body})
                if method == "DELETE":
                    return self._send(204 if fake.store.pop(key, None) is not None else 404, {})
                return self._send(200, {"data": fake.store[key]}) if key in fake.store else self._send(404, {})
            for prefix, wrap in (("/_component_template/", "component_templates"),
                                 ("/_index_template/", "index_templates"),
                                 ("/_ingest/pipeline/", None),
                                 ("/api/security/role/", None),
                                 ("/api/alerting/rule/", None)):
                if path.startswith(prefix):
                    name = path[len(prefix):]
                    key = prefix + name
                    if method in ("PUT", "POST"):
                        fake.store[key] = body
                        return self._send(200, {"acknowledged": True})
                    if method == "DELETE":
                        if key not in fake.store:
                            return self._send(404, {})
                        if prefix == "/_index_template/" and name in fake.data_streams_in_use:
                            return self._send(400, {"error": "in use"})
                        del fake.store[key]
                        return self._send(200, {"acknowledged": True})
                    if key not in fake.store:
                        return self._send(404, {})
                    stored = fake.store[key]
                    if wrap == "component_templates":
                        return self._send(200, {wrap: [{"name": name, "component_template": stored}]})
                    if wrap == "index_templates":
                        return self._send(200, {wrap: [{"name": name, "index_template": stored}]})
                    if prefix == "/_ingest/pipeline/":
                        return self._send(200, {name: stored})
                    return self._send(200, stored)
            return self._send(404, {"error": f"fake: no route {method} {path}"})

        def do_GET(self):
            self.handle_any("GET")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_POST(self):
            self.handle_any("POST")

        def do_DELETE(self):
            self.handle_any("DELETE")

    return Handler


class FakeServerCase(unittest.TestCase):
    fake_kwargs: dict = {}
    target = "docker"

    def setUp(self):
        self.fake = FakeCluster(**self.fake_kwargs)
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.cfg = lagprof.Config(target=self.target, es_url=url, kibana_url=url,
                                  username="elastic", password="pw", api_key="k" if self.target in ("cloud", "serverless") else "")
        self.client = lagprof.Client(self.cfg)
        self.out = io.StringIO()
        self.rep = lagprof.Reporter(self.out)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def statuses(self, kind=None):
        return [(s, k, n) for s, k, n, _ in self.rep.lines if kind is None or k == kind]


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_env_parsing_comments_quotes_and_expansion(self):
        text = """
        # comment
        LAGPROF_TARGET=docker
        export ES_URL="https://localhost:9200"
        ES_PASSWORD=${ELASTIC_PASSWORD}   # from the lab .env
        LITERAL='${NOT_EXPANDED}'
        CA_CERT=scripts/lab/certs/ca/ca.crt
        """
        vals = lagprof.parse_env_text(text, {"ELASTIC_PASSWORD": "s3cret"})
        self.assertEqual(vals["ES_URL"], "https://localhost:9200")
        self.assertEqual(vals["ES_PASSWORD"], "s3cret")
        self.assertEqual(vals["LITERAL"], "${NOT_EXPANDED}")

    def test_cloud_id_decoding(self):
        encoded = base64.b64encode(b"us-east-1.aws.found.io$abc123$def456").decode()
        es, kb = lagprof.decode_cloud_id(f"my-deployment:{encoded}")
        self.assertEqual(es, "https://abc123.us-east-1.aws.found.io")
        self.assertEqual(kb, "https://def456.us-east-1.aws.found.io")

    def test_cloud_id_with_port(self):
        encoded = base64.b64encode(b"example.com:9243$es$kb").decode()
        es, kb = lagprof.decode_cloud_id(f"x:{encoded}")
        self.assertEqual(es, "https://es.example.com:9243")
        self.assertEqual(kb, "https://kb.example.com:9243")

    def test_bad_cloud_id(self):
        with self.assertRaises(lagprof.ConfigError):
            lagprof.decode_cloud_id("x:not-base64-$$$")

    def test_cloud_target_requires_api_key(self):
        env = {"LAGPROF_TARGET": "cloud", "ES_URL": "https://e", "KIBANA_URL": "https://k",
               "ES_USERNAME": "u", "ES_PASSWORD": "p"}
        with self.assertRaises(lagprof.ConfigError):
            lagprof.load_config(None, env)

    def test_cloud_id_fills_urls(self):
        encoded = base64.b64encode(b"host.io$es1$kb1").decode()
        cfg = lagprof.load_config(None, {"LAGPROF_TARGET": "cloud", "CLOUD_ID": f"d:{encoded}",
                                         "ES_API_KEY": "abc"})
        self.assertEqual(cfg.es_url, "https://es1.host.io")
        self.assertEqual(cfg.kibana_url, "https://kb1.host.io")
        self.assertEqual(cfg.auth_header, "ApiKey abc")

    def test_invalid_target(self):
        with self.assertRaises(lagprof.ConfigError):
            lagprof.load_config(None, {"LAGPROF_TARGET": "laptop"})

    def test_docker_target_file_loads(self):
        cfg = lagprof.load_config("targets/docker.env", {"ELASTIC_PASSWORD": "pw"})
        self.assertEqual(cfg.target, "docker")
        self.assertEqual(cfg.password, "pw")
        self.assertTrue(cfg.ca_cert.endswith("scripts/lab/certs/ca/ca.crt"))


class WorkflowRenderTests(unittest.TestCase):
    def test_all_spaces_unchanged(self):
        name, text = lagprof.render_workflow("all-spaces")
        self.assertEqual(name, "lagprof-hourly-all-spaces.yaml")
        self.assertIn("for_each_space", text)

    def test_per_space_fills_settings(self):
        name, text = lagprof.render_workflow("per-space", "soc", shared_stats=False)
        self.assertEqual(name, "lagprof-hourly-space-soc.yaml")
        self.assertIn("name: lagprof-hourly-space-soc", text)
        self.assertRegex(text, r"(?m)^\s*space_id: soc\b")
        self.assertRegex(text, r"(?m)^\s*write_shared_stats: false\b")
        self.assertNotIn("CHANGEME", text)

    def test_per_space_needs_valid_space(self):
        for bad in (None, "", "Bad Space", "../x"):
            with self.assertRaises(lagprof.ConfigError):
                lagprof.render_workflow("per-space", bad)


class LintTests(unittest.TestCase):
    def test_pack_passes_lint(self):
        rep = lagprof.Reporter(io.StringIO())
        self.assertEqual(lagprof.lint(rep), 0, rep.stream.getvalue())

    def test_lint_catches_lookup_column_clash(self):
        real = lagprof.lookup_template_fields
        try:
            lagprof.lookup_template_fields = lambda: real() | {"stream_p99"}
            rep = lagprof.Reporter(io.StringIO())
            self.assertEqual(lagprof.lint(rep), 1)
            self.assertIn("stream_p99", rep.stream.getvalue())
        finally:
            lagprof.lookup_template_fields = real

    def test_prejoin_columns(self):
        q = "FROM x | EVAL a = 1, b == 2 | STATS n = COUNT(*) BY c // d = e\n| LOOKUP JOIN y ON c | EVAL late = 1"
        self.assertEqual(lagprof.prejoin_columns(q), {"a", "n"})

    def test_role_rendering(self):
        role = lagprof.render_role(["soc"])
        self.assertEqual(role["kibana"][0]["spaces"], ["soc"])
        self.assertNotIn("_lagprof_notes", role)
        self.assertEqual(role["metadata"]["lagprof_version"], lagprof.VERSION)

    def test_version_tuple(self):
        self.assertEqual(lagprof.version_tuple("9.5.6"), (9, 5, 6))
        self.assertEqual(lagprof.version_tuple("9.6.0-SNAPSHOT"), (9, 6, 0))
        self.assertLess(lagprof.version_tuple("9.4.2"), lagprof.MIN_VERSION)


class CheckTests(FakeServerCase):
    def test_docker_basic_license_starts_trial_when_fixing(self):
        self.fake.license_type = "basic"
        res = lagprof.run_check(self.client, self.rep, fix_license=True)
        self.assertEqual(res.blocking, [])
        self.assertIn(("fixed", "license", "trial"), self.statuses())

    def test_basic_license_blocks_without_fix(self):
        self.fake.license_type = "basic"
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("license" in b for b in res.blocking))

    def test_old_version_blocks(self):
        self.fake.version = "9.4.1"
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("older" in b for b in res.blocking))

    def test_missing_workflows_blocks(self):
        self.fake.workflows_api = False
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("Workflows" in b for b in res.blocking))


class ServerlessCheckTests(FakeServerCase):
    fake_kwargs = {"version": "8.11.0", "flavor": "serverless", "license_type": "basic"}
    target = "serverless"

    def test_serverless_skips_version_and_license(self):
        res = lagprof.run_check(self.client, self.rep)
        self.assertEqual(res.blocking, [])
        self.assertIn(("skipped", "license", "serverless"), self.statuses())
        self.assertFalse(any(p == "/_license" for _, p in self.fake.requests))


class InstallTests(FakeServerCase):
    def test_install_is_idempotent(self):
        opts = lagprof.InstallOptions()
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        first = {s for s, k, n in self.statuses() if s != "WARN"}
        self.assertEqual(first, {"created"})

        self.rep.lines.clear()
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0)
        second = {s for s, k, n in self.statuses() if s != "WARN"}
        self.assertEqual(second, {"unchanged"})

    def test_templates_get_version_meta_and_no_notes(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        tmpl = self.fake.store["/_index_template/lagprof-results"]
        self.assertEqual(tmpl["_meta"]["lagprof_version"], lagprof.VERSION)
        role = self.fake.store["/api/security/role/lagprof-workflow"]
        self.assertEqual(role["kibana"][0]["spaces"], ["*"])
        rule = self.fake.store["/api/alerting/rule/lagprof-miss-rate"]
        self.assertNotIn("_lagprof_notes", rule)
        self.assertNotIn("id", rule)
        self.assertTrue(any(tag.startswith(f"lagprof-version:{lagprof.VERSION}+") for tag in rule["tags"]))

    def test_component_template_installed_before_index_templates(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p for m, p in self.fake.requests if m == "PUT"]
        first_index = next(i for i, p in enumerate(puts) if p.startswith("/_index_template/"))
        comp = puts.index("/_component_template/lagprof-common")
        self.assertLess(comp, first_index)

    def test_component_fields_added_to_existing_indices(self):
        # Pack updates can add fields; existing indices need them too, or ES|QL panels fail.
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p for m, p in self.fake.requests if m == "PUT"]
        sync = [p for p in puts if p.startswith("/lagprof-*,-lagprof-rule-streams/_mapping")]
        self.assertEqual(len(sync), 1, puts)
        self.assertGreater(puts.index(sync[0]), puts.index("/_component_template/lagprof-common"))
        body = self.fake.store["/lagprof-*,-lagprof-rule-streams/_mapping"]
        self.assertIn("reason", body["properties"])

    def test_data_streams_created_empty_after_templates(self):
        # Dashboard panels fail with "Unknown index" until each data stream exists.
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p for m, p in self.fake.requests if m == "PUT"]
        made = sorted(p for p in puts if p.startswith("/_data_stream/"))
        self.assertEqual(made, ["/_data_stream/lagprof-host-lag", "/_data_stream/lagprof-results",
                                "/_data_stream/lagprof-runs", "/_data_stream/lagprof-stream-stats"])
        last_template = max(i for i, p in enumerate(puts) if p.startswith("/_index_template/"))
        self.assertTrue(all(puts.index(p) > last_template for p in made))

    def test_per_space_role_and_space_prefixed_rules(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        self.assertEqual(self.fake.store["/api/security/role/lagprof-workflow"]["kibana"][0]["spaces"], ["soc"])
        self.assertTrue(any(p.startswith("/s/soc/api/alerting/rule/") for _, p in self.fake.requests))
        self.assertTrue((ROOT / "build/lagprof-hourly-space-soc.yaml").exists())

    def test_workflow_imported_then_unchanged_then_updated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("created", "workflow", "lagprof-hourly-all-spaces"), self.statuses())
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("unchanged", "workflow", "lagprof-hourly-all-spaces"), self.statuses())
        wid = next(iter(self.fake.workflows))
        self.fake.workflows[wid]["yaml"] = "name: lagprof-hourly-all-spaces\n# edited"
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("updated", "workflow", "lagprof-hourly-all-spaces"), self.statuses())

    def test_reinstall_after_uninstall_does_not_duplicate(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertEqual(len(self.fake.workflows), 1, self.fake.workflows.keys())

    def test_workflow_saved_with_owner_key(self):
        self.cfg.workflow_api_key = "owner-key"
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertEqual(self.fake.workflow_auth, "ApiKey owner-key")
        self.assertFalse(any(s == "WARN" and k == "workflow" for s, k, n in self.statuses()))

    def test_warns_without_owner_key(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertTrue(any(s == "WARN" and k == "workflow" for s, k, n in self.statuses()))

    def test_manual_workflow_when_import_disabled(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions(import_workflow=False))
        self.assertIn(("manual", "workflow", "lagprof-hourly-all-spaces.yaml"), self.statuses())

    def test_uninstall_deletes_workflow(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        self.assertIn(("deleted", "workflow", "lagprof-hourly-all-spaces"), self.statuses())
        self.assertEqual(self.fake.workflows, {})

    def test_outdated_resource_is_updated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.fake.store["/_ingest/pipeline/lagprof-stamp"]["_meta"]["lagprof_fingerprint"] = "0.0.1+0000000000"
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("updated", "pipeline", "lagprof-stamp"), self.statuses())

    def test_changed_file_is_updated_without_version_bump(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        real = lagprof.fingerprint
        try:
            lagprof.fingerprint = lambda path, extra="": real(path, extra) + ("x" if path and path.endswith("lagprof-stamp.json") else "")
            self.rep.lines.clear()
            lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        finally:
            lagprof.fingerprint = real
        self.assertIn(("updated", "pipeline", "lagprof-stamp"), self.statuses())
        self.assertIn(("unchanged", "pipeline", "lagprof-rules-parse"), self.statuses())

    def test_outdated_lookup_index_is_recreated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        # An index built from the old shared mapping, with result fields in it.
        self.fake.store["/lagprof-rule-streams"] = {"properties": {
            "lag_p99_s": {"type": "double"}, "data_stream": {"properties": {"dataset": {"type": "keyword"}}}}}
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("recreated", "index", "lagprof-rule-streams"), self.statuses())
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("unchanged", "index", "lagprof-rule-streams"), self.statuses())

    def test_uninstall_keeps_templates_in_use(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        self.assertIn(("kept", "index_template", "lagprof-results"), self.statuses())
        self.assertIn(("deleted", "pipeline", "lagprof-stamp"), self.statuses())
        self.assertIn(("deleted", "role", "lagprof-workflow"), self.statuses())

    def test_status_reports_missing_then_current(self):
        lagprof.status(self.client, self.rep)
        self.assertIn(("missing", "pipeline", "lagprof-stamp"), self.statuses())
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.status(self.client, self.rep)
        self.assertIn(("current", "pipeline", "lagprof-stamp"), self.statuses())


if __name__ == "__main__":
    unittest.main()
