#!/usr/bin/env python3
"""Ingest Lag Profiler installer.

Loads the profiler pack into a Docker lab, a self-managed cluster, Elastic Cloud
or a Serverless project. Uses only the Python standard library.

    lagprof.py check     --target-file targets/docker.env
    lagprof.py install   --target-file targets/docker.env [--workflow per-space --space soc --shared-stats]
    lagprof.py status    --target-file targets/docker.env
    lagprof.py uninstall --target-file targets/docker.env [--space soc] [--delete-data --yes]
                         # keeps shared templates, pipelines and data while other copies remain
    lagprof.py lint                      # offline checks of the pack files
    lagprof.py render-workflow --workflow per-space --space soc   # write the YAML to build/

Connection settings come from a target file (KEY=VALUE lines) and the environment.
Environment variables win over the file. Values may use ${VAR}.

    LAGPROF_TARGET  docker | self-managed | cloud | serverless
    ES_URL, KIBANA_URL   or   CLOUD_ID (cloud)
    ES_API_KEY           or   ES_USERNAME + ES_PASSWORD
    CA_CERT              path to a CA certificate (optional)
    VERIFY_TLS           true (default) | false
    LAGPROF_WORKFLOW_API_KEY   API key of a dedicated user with the lagprof-workflow role
                         (per-space mode: lagprof-workflow-<space>).
                         The workflow is saved with it, so scheduled runs use that
                         user's (limited) privileges. Without it they use the installer's.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent
VERSION = (PACK_ROOT / "VERSION").read_text().strip()

TARGETS = ("docker", "self-managed", "cloud", "serverless")
MIN_VERSION = (9, 5, 6)
TESTED_VERSION = "9.5.6"
ROLE_NAME = "lagprof-workflow"

# Workflows management API (confirmed on 9.5.6):
#   GET  /api/workflows                       list
#   POST /api/workflows                       {"workflows": [{"yaml": ...}]}; id = workflow name
#   GET|PUT|DELETE /api/workflows/workflow/<id>   PUT takes {"yaml": ...}
#   POST /api/workflows/workflow/<id>/run     {"inputs": {}}
#   GET  /api/workflows/executions/<execution id>
# A newly created scheduled workflow runs once straight away.
WORKFLOWS_API = "/api/workflows"
# Serverless Kibana requires a version header on public APIs.
SERVERLESS_API_VERSION = "2023-10-31"

# Settings that Serverless rejects. Pack templates must not use them.
FORBIDDEN_SETTING_KEYS = ("number_of_shards", "number_of_replicas", "lifecycle.name",
                          "lifecycle.rollover_alias", "routing.allocation")

WORKFLOW_FILES = {
    "all-spaces": "kibana/workflows/lagprof-hourly-all-spaces.yaml",
    "per-space": "kibana/workflows/lagprof-hourly-per-space.yaml",
}
SPACE_ID_RE = re.compile(r"^[a-z0-9_-]+$")


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def parse_env_text(text: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, quotes and ${VAR} expansion."""
    environ = environ if environ is not None else {}
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ConfigError(f"line {lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            quoted, value = value[0], value[1:-1]
        else:
            quoted = ""
            value = re.sub(r"\s+#.*$", "", value)  # trailing comment
        if quoted != "'":
            value = re.sub(r"\$\{(\w+)\}",
                           lambda m: out.get(m.group(1), environ.get(m.group(1), "")), value)
        out[key] = value
    return out


def decode_cloud_id(cloud_id: str) -> tuple[str, str | None]:
    """Turn an Elastic Cloud ID into (elasticsearch_url, kibana_url)."""
    _, sep, encoded = cloud_id.partition(":")
    if not sep:
        encoded = cloud_id
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"CLOUD_ID could not be decoded: {exc}") from exc
    parts = decoded.split("$")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ConfigError("CLOUD_ID is not in the expected host$es$kibana form")
    host, port = parts[0], "443"
    if ":" in host:
        host, port = host.rsplit(":", 1)
    suffix = "" if port == "443" else f":{port}"
    es_url = f"https://{parts[1]}.{host}{suffix}"
    kb_url = f"https://{parts[2]}.{host}{suffix}" if len(parts) > 2 and parts[2] else None
    return es_url, kb_url


@dataclass
class Config:
    target: str
    es_url: str
    kibana_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    ca_cert: str = ""
    verify_tls: bool = True
    workflow_api_key: str = ""

    @property
    def auth_header(self) -> str:
        if self.api_key:
            return f"ApiKey {self.api_key}"
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"


def load_config(target_file: str | None, environ: dict[str, str] | None = None,
                target_override: str | None = None) -> Config:
    environ = dict(os.environ if environ is None else environ)
    values: dict[str, str] = {}
    if target_file:
        path = Path(target_file)
        if not path.is_absolute() and not path.exists():
            path = PACK_ROOT / target_file
        if not path.exists():
            raise ConfigError(f"target file not found: {target_file}")
        values = parse_env_text(path.read_text(), environ)
    keys = ("LAGPROF_TARGET", "ES_URL", "KIBANA_URL", "CLOUD_ID", "ES_API_KEY",
            "ES_USERNAME", "ES_PASSWORD", "CA_CERT", "VERIFY_TLS", "LAGPROF_WORKFLOW_API_KEY")
    for k in keys:
        if environ.get(k):
            values[k] = environ[k]

    target = target_override or values.get("LAGPROF_TARGET", "")
    if target not in TARGETS:
        raise ConfigError(f"LAGPROF_TARGET must be one of {', '.join(TARGETS)} (got {target!r})")

    es_url, kb_url = values.get("ES_URL", ""), values.get("KIBANA_URL", "")
    if values.get("CLOUD_ID"):
        cloud_es, cloud_kb = decode_cloud_id(values["CLOUD_ID"])
        es_url = es_url or cloud_es
        kb_url = kb_url or (cloud_kb or "")
    if not es_url or not kb_url:
        raise ConfigError("set ES_URL and KIBANA_URL (or CLOUD_ID for Elastic Cloud)")

    api_key = values.get("ES_API_KEY", "")
    user, pw = values.get("ES_USERNAME", ""), values.get("ES_PASSWORD", "")
    if not api_key and not (user and pw):
        raise ConfigError("set ES_API_KEY, or ES_USERNAME and ES_PASSWORD")
    if target in ("cloud", "serverless") and not api_key:
        raise ConfigError(f"{target} targets need ES_API_KEY")

    ca = values.get("CA_CERT", "")
    if ca and not Path(ca).is_absolute():
        ca = str(PACK_ROOT / ca)
    return Config(
        target=target, es_url=es_url.rstrip("/"), kibana_url=kb_url.rstrip("/"),
        api_key=api_key, username=user, password=pw, ca_cert=ca,
        verify_tls=values.get("VERIFY_TLS", "true").lower() not in ("false", "0", "no"),
        workflow_api_key=values.get("LAGPROF_WORKFLOW_API_KEY", ""),
    )


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, method: str, url: str, body: object):
        self.status, self.method, self.url, self.body = status, method, url, body
        super().__init__(f"{method} {url} -> {status}: {str(body)[:300]}")


class Client:
    def __init__(self, cfg: Config, serverless: bool | None = None):
        self.cfg = cfg
        self.serverless = cfg.target == "serverless" if serverless is None else serverless
        self.timeout = 60  # seconds per request
        if cfg.ca_cert:
            self.ctx = ssl.create_default_context(cafile=cfg.ca_cert)
        else:
            self.ctx = ssl.create_default_context()
        if not cfg.verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _send(self, base: str, method: str, path: str, body=None, headers=None,
              raw: bytes | None = None, allow: tuple[int, ...] = ()) -> tuple[int, object]:
        url = base + path
        hdrs = {"Authorization": self.cfg.auth_header, "Accept": "application/json"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        except urllib.error.URLError as err:
            raise HttpError(0, method, url, f"connection failed: {err.reason}") from err
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload.decode(errors="replace")
        if status >= 400 and status not in allow:
            raise HttpError(status, method, url, parsed)
        return status, parsed

    def as_workflow_owner(self) -> "Client":
        """A client that authenticates with the workflow owner's API key, if one is set."""
        if not self.cfg.workflow_api_key:
            return self
        import copy
        cfg = copy.copy(self.cfg)
        cfg.api_key, cfg.username, cfg.password = self.cfg.workflow_api_key, "", ""
        return Client(cfg, serverless=self.serverless)

    def es(self, method: str, path: str, body=None, allow: tuple[int, ...] = ()):
        return self._send(self.cfg.es_url, method, path, body, allow=allow)

    def kb(self, method: str, path: str, body=None, allow: tuple[int, ...] = (),
           raw: bytes | None = None, content_type: str | None = None):
        headers = {"kbn-xsrf": "lagprof"}
        if self.serverless:
            headers["elastic-api-version"] = SERVERLESS_API_VERSION
        if content_type:
            headers["Content-Type"] = content_type
        return self._send(self.cfg.kibana_url, method, path, body, headers, raw, allow)


def space_prefix(space: str | None) -> str:
    return "" if not space or space == "default" else f"/s/{space}"


def version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums[:3])


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

class Reporter:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lines: list[tuple[str, str, str, str]] = []

    def line(self, status: str, kind: str, name: str, detail: str = "") -> None:
        self.lines.append((status, kind, name, detail))
        tail = f"  {detail}" if detail else ""
        print(f"{status:<10} {kind:<18} {name}{tail}", file=self.stream)

    def note(self, text: str) -> None:
        print(text, file=self.stream)


# --------------------------------------------------------------------------------------
# Check
# --------------------------------------------------------------------------------------

@dataclass
class CheckResult:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def run_check(client: Client, rep: Reporter, fix_license: bool = False) -> CheckResult:
    res = CheckResult()
    cfg = client.cfg

    # Elasticsearch reachable, version and flavor
    try:
        _, root = client.es("GET", "/")
    except HttpError as err:
        res.blocking.append(f"Elasticsearch not reachable: {err}")
        rep.line("BLOCKED", "elasticsearch", cfg.es_url, str(err))
        return res
    ver = root.get("version", {})
    number, flavor = ver.get("number", "?"), ver.get("build_flavor", "default")
    res.info.update(version=number, flavor=flavor)
    detected = "serverless" if flavor == "serverless" else "stateful"
    if (cfg.target == "serverless") != (detected == "serverless"):
        msg = f"target file says {cfg.target}, but the cluster looks {detected}"
        res.warnings.append(msg)
        rep.line("WARN", "target", cfg.target, msg)
    client.serverless = detected == "serverless"
    rep.line("ok", "elasticsearch", cfg.es_url, f"version {number}, flavor {flavor}")

    if detected != "serverless":
        if version_tuple(number) < MIN_VERSION:
            msg = f"version {number} is older than {TESTED_VERSION}"
            res.blocking.append(msg)
            rep.line("BLOCKED", "version", number, msg)
        elif cfg.target == "docker" and number != TESTED_VERSION:
            msg = f"lab should run {TESTED_VERSION}, found {number}"
            res.warnings.append(msg)
            rep.line("WARN", "version", number, msg)

        # License
        _, lic = client.es("GET", "/_license")
        ltype = (lic or {}).get("license", {}).get("type", "unknown")
        lstatus = (lic or {}).get("license", {}).get("status", "unknown")
        if ltype in ("enterprise", "trial") and lstatus == "active":
            rep.line("ok", "license", ltype)
        elif ltype == "basic" and cfg.target == "docker" and fix_license:
            client.es("POST", "/_license/start_trial?acknowledge=true")
            rep.line("fixed", "license", "trial", "started a 30-day trial in the lab")
        else:
            msg = f"Workflows needs an Enterprise or trial license (found {ltype}, {lstatus})"
            res.blocking.append(msg)
            rep.line("BLOCKED", "license", ltype, msg)
    else:
        rep.line("skipped", "license", "serverless", "no license on Serverless")

    # Kibana reachable
    try:
        _, status = client.kb("GET", "/api/status")
        level = ((status or {}).get("status", {}).get("overall", {}) or {}).get("level", "?")
        rep.line("ok", "kibana", cfg.kibana_url, f"status {level}")
    except HttpError as err:
        res.blocking.append(f"Kibana not reachable: {err}")
        rep.line("BLOCKED", "kibana", cfg.kibana_url, str(err))
        return res

    # Workflows API: without it there is nothing to run the profiler.
    try:
        code, _ = client.kb("GET", WORKFLOWS_API, allow=(400, 403, 404))
        if code < 400:
            rep.line("ok", "workflows", WORKFLOWS_API)
            res.info["workflows_api"] = True
        else:
            msg = (f"Workflows isn't available here (HTTP {code}). It needs 9.4 or later with an "
                   "Enterprise or trial license, and a user with Workflows privileges.")
            res.blocking.append(msg)
            rep.line("BLOCKED", "workflows", WORKFLOWS_API, msg)
    except HttpError as err:
        res.blocking.append(str(err))
        rep.line("BLOCKED", "workflows", WORKFLOWS_API, str(err))

    # event.ingested coverage in the last hour
    query = ("FROM logs-* | WHERE @timestamp >= NOW() - 1 hour "
             "| STATS total = COUNT(*), measured = COUNT(event.ingested)")
    try:
        code, out = client.es("POST", "/_query", {"query": query}, allow=(400, 404))
        if code >= 400:
            rep.line("WARN", "data", "logs-*", "no logs-* data found yet")
            res.warnings.append("no logs-* data found yet")
        else:
            total, measured = (out.get("values") or [[0, 0]])[0]
            pct = (measured / total * 100) if total else 0.0
            status_word = "ok" if total and pct >= 95 else "WARN"
            detail = f"{total} events in the last hour, {pct:.1f}% have event.ingested"
            rep.line(status_word, "data", "logs-*", detail)
            if status_word == "WARN":
                res.warnings.append(detail)
    except HttpError as err:
        rep.line("WARN", "data", "logs-*", str(err))
        res.warnings.append(str(err))
    return res


# --------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------

def load_json(rel: str | Path) -> dict:
    return json.loads((PACK_ROOT / rel).read_text())


def strip_notes(body: dict) -> dict:
    return {k: v for k, v in body.items() if not k.startswith("_lagprof")}


def fingerprint(path: str | None, extra: str = "") -> str:
    """Pack version plus a hash of the resource's source, e.g. '0.1.0-dev+3f2a9c01be'.

    Installed resources carry it, so a changed file is updated even when the pack
    version hasn't moved (as during development)."""
    import hashlib
    data = (PACK_ROOT / path).read_bytes() if path else b""
    return f"{VERSION}+{hashlib.sha256(data + extra.encode()).hexdigest()[:10]}"


def marker_version(marker: str | None) -> str:
    return (marker or "").split("+", 1)[0]


def with_meta(body: dict, marker: str) -> dict:
    body = strip_notes(body)
    meta = dict(body.get("_meta") or {})
    meta.update(lagprof_version=VERSION, lagprof_fingerprint=marker, managed_by="lagprof")
    body["_meta"] = meta
    return body


def role_spaces(opts_workflow: str, space: str | None) -> list[str]:
    return ["*"] if opts_workflow == "all-spaces" or not space else [space]


def render_role(spaces: list[str], marker: str | None = None) -> dict:
    body = strip_notes(load_json("elasticsearch/security/lagprof-workflow-role.json"))
    for entry in body.get("kibana", []):
        if entry.get("spaces") == "__SPACES__":
            entry["spaces"] = spaces
    body["metadata"] = {"lagprof_version": VERSION, "managed_by": "lagprof"}
    if marker:
        body["metadata"]["lagprof_fingerprint"] = marker
    return body


def render_workflow(mode: str, space: str | None = None, shared_stats: bool = False) -> tuple[str, str]:
    """Return (output file name, YAML text) with per-space settings filled in."""
    if mode not in WORKFLOW_FILES:
        raise ConfigError(f"--workflow must be one of {', '.join(WORKFLOW_FILES)}")
    text = (PACK_ROOT / WORKFLOW_FILES[mode]).read_text()
    if mode == "all-spaces":
        return "lagprof-hourly-all-spaces.yaml", text
    if not space or not SPACE_ID_RE.match(space):
        raise ConfigError("per-space mode needs --space with a valid space ID (a-z, 0-9, _ and -)")
    text = text.replace("name: lagprof-hourly-space-CHANGEME", f"name: lagprof-hourly-space-{space}", 1)
    text, n1 = re.subn(r"(?m)^(\s*space_id:\s*)\S+", rf"\g<1>{space}", text, count=1)
    text, n2 = re.subn(r"(?m)^(\s*write_shared_stats:\s*)\S+",
                       rf"\g<1>{'true' if shared_stats else 'false'}", text, count=1)
    if not (n1 and n2):
        raise ConfigError("per-space workflow file is missing space_id or write_shared_stats")
    return f"lagprof-hourly-space-{space}.yaml", text


@dataclass
class Resource:
    kind: str
    name: str
    path: str | None = None


def scoped(name: str, space: str | None) -> str:
    """Per-space installs get their own role, dashboard and alert rules.

    Kibana dashboard and alert rule IDs are unique across all spaces, and one shared
    role would be rewritten to the last space installed, so per-space copies must not
    share them."""
    return f"{name}-{space}" if space else name


def pack_resources(space: str | None = None) -> list[Resource]:
    """The pack's resources. space is set for a per-space install (see scoped())."""
    res: list[Resource] = []
    for kind, folder in (("component_template", "elasticsearch/component-templates"),
                         ("index_template", "elasticsearch/index-templates"),
                         ("pipeline", "elasticsearch/ingest-pipelines")):
        for p in sorted((PACK_ROOT / folder).glob("*.json")):
            res.append(Resource(kind, p.stem, str(p.relative_to(PACK_ROOT))))
    # Created up front so the miss-rate query and dashboard work before the first run.
    res.append(Resource("index", "lagprof-rules"))
    res.append(Resource("index", "lagprof-rule-streams"))
    # Data streams are otherwise created by the first write. Until then ES|QL reports
    # "Unknown index", so every dashboard panel would show an error on a new install.
    for p in sorted((PACK_ROOT / "elasticsearch/index-templates").glob("*.json")):
        body = load_json(p.relative_to(PACK_ROOT))
        if "data_stream" in body:
            res.append(Resource("data_stream", body["index_patterns"][0]))
    res.append(Resource("role", scoped(ROLE_NAME, space), "elasticsearch/security/lagprof-workflow-role.json"))
    res.append(Resource("workflow", "lagprof-hourly"))
    res.append(Resource("dashboard", scoped("lagprof-overview", space), "kibana/dashboards/lagprof-overview.json"))
    for p in sorted((PACK_ROOT / "kibana/alerting").glob("*.json")):
        res.append(Resource("alert_rule", scoped(load_json(p.relative_to(PACK_ROOT))["id"], space),
                            str(p.relative_to(PACK_ROOT))))
    return res


ES_PATHS = {
    "component_template": "/_component_template/",
    "index_template": "/_index_template/",
    "pipeline": "/_ingest/pipeline/",
}


def installed_version(client: Client, r: Resource, space: str | None) -> str | None:
    """Return the installed pack version, '' if present but unversioned, None if missing."""
    if r.kind in ES_PATHS:
        code, out = client.es("GET", ES_PATHS[r.kind] + r.name, allow=(404,))
        if code == 404 or not out:
            return None
        if r.kind == "component_template":
            items = out.get("component_templates", [])
            meta = items[0]["component_template"].get("_meta", {}) if items else None
        elif r.kind == "index_template":
            items = out.get("index_templates", [])
            meta = items[0]["index_template"].get("_meta", {}) if items else None
        else:
            meta = (out.get(r.name) or {}).get("_meta", {})
        return None if meta is None else meta.get("lagprof_fingerprint", meta.get("lagprof_version", ""))
    if r.kind == "role":
        code, out = client.kb("GET", f"/api/security/role/{r.name}", allow=(404,))
        if code == 404:
            return None
        md = out.get("metadata") or {}
        return md.get("lagprof_fingerprint", md.get("lagprof_version", ""))
    if r.kind == "alert_rule":
        code, out = client.kb("GET", f"{space_prefix(space)}/api/alerting/rule/{r.name}", allow=(404,))
        if code == 404:
            return None
        tags = [t for t in out.get("tags", []) if t.startswith("lagprof-version:")]
        return tags[0].split(":", 1)[1] if tags else ""
    if r.kind == "dashboard":
        code, out = client.kb("GET", f"{space_prefix(space)}/api/dashboards/{r.name}", allow=(404,))
        if code == 404:
            return None
        m = re.search(r"pack v([^,)\s]+)(?:, ([0-9a-f]{10}))?\)", (out.get("data") or {}).get("description", ""))
        if not m:
            return ""
        return f"{m.group(1)}+{m.group(2)}" if m.group(2) else m.group(1)
    return None


# Indices whose documents carry lagprof.space.id. Stream stats and host lag come from
# raw logs, which aren't divided by space, so a per-space dashboard shows them unfiltered.
SPACE_SCOPED_INDICES = ("lagprof-results", "lagprof-rules", "lagprof-runs")


def scope_query(esql: str, space: str) -> str:
    """Add a space filter after the FROM line of a query on a space-scoped index."""
    first, sep, rest = esql.partition("\n")
    m = re.match(r"\s*FROM\s+(\S+)\s*$", first)
    if not m or m.group(1) not in SPACE_SCOPED_INDICES:
        return esql
    return f'{first}\n| WHERE lagprof.space.id == "{space}"{sep}{rest}'


def _scope_panels(obj, space: str):
    if isinstance(obj, dict):
        if obj.get("type") == "esql" and isinstance(obj.get("query"), str):
            obj["query"] = scope_query(obj["query"], space)
        for v in obj.values():
            _scope_panels(v, space)
    elif isinstance(obj, list):
        for v in obj:
            _scope_panels(v, space)


def render_dashboard(path: str, marker: str, space: str | None = None) -> dict:
    """Dashboard body. A per-space copy's panels only show that space's rules and runs."""
    body = load_json(path)
    ver, _, h = marker.partition("+")
    body["description"] = re.sub(r"pack v[^)]*\)", f"pack v{ver}, {h})", body.get("description", ""))
    if space:
        body["title"] = f"{body['title']} ({space})"
        _scope_panels(body.get("panels", []), space)
    return body


def marker_extra(r: "Resource", spaces: list[str], space: str | None) -> str:
    """Extra input to a resource's fingerprint, for resources rendered differently per install.

    The role depends on its spaces. A per-space dashboard is filtered to its space, so its
    marker includes the space and older, unfiltered copies are updated on the next install."""
    if r.kind == "role":
        return ",".join(spaces)
    if r.kind == "dashboard" and space:
        return f"space={space}"
    return ""


def render_alert_rule(path: str, space: str | None = None) -> dict:
    """Alert rule body. A per-space copy only covers its own space's rules and workflow."""
    body = strip_notes(load_json(path))
    if space:
        esql = body["params"]["esqlQuery"]["esql"]
        first, _, rest = esql.partition("\n")
        body["params"]["esqlQuery"]["esql"] = f'{first}\n| WHERE lagprof.space.id == "{space}"\n{rest}'
        body["name"] = f"{body['name']} ({space})"
    return body


@dataclass
class InstallOptions:
    workflow: str = "all-spaces"
    space: str | None = None
    shared_stats: bool = False
    import_workflow: bool = True
    force: bool = False

    @property
    def kibana_space(self) -> str | None:
        return self.space if self.workflow == "per-space" else None


def installed_workflows(client: Client) -> list[dict] | None:
    """Profiler workflows installed in any space: [{space, name, id, shared_stats}].

    None if workflows can't be listed (for example, the Workflows API is off)."""
    code, spaces = client.kb("GET", "/api/spaces/space", allow=(403, 404))
    ids = [s["id"] for s in spaces] if code == 200 and isinstance(spaces, list) else ["default"]
    found, listed = [], False
    for sid in ids:
        code, out = client.kb("GET", f"{space_prefix(sid)}{WORKFLOWS_API}?size=1000", allow=(400, 403, 404))
        if code != 200:
            continue
        listed = True
        for w in (out or {}).get("results", []):
            name = str(w.get("name", ""))
            if name.startswith("lagprof-hourly"):
                shared = bool(re.search(r"(?m)^\s*write_shared_stats:\s*true\b", w.get("yaml") or ""))
                found.append({"space": sid, "name": name, "id": w.get("id"), "shared_stats": shared})
    return found if listed else None


def mode_conflict(installed: list[dict], opts: "InstallOptions") -> str | None:
    """The two workflow versions are alternatives; running both profiles every space twice."""
    if opts.workflow == "per-space":
        other = [w for w in installed if w["name"] == "lagprof-hourly-all-spaces"]
        if other:
            return ("the all-spaces workflow is installed; install one version, not both. "
                    "Run uninstall first (history is kept), or use --force")
    else:
        other = [w for w in installed if w["name"].startswith("lagprof-hourly-space-")]
        if other:
            spaces = ", ".join(sorted({w["space"] for w in other}))
            return (f"per-space workflows are installed (spaces: {spaces}); install one version, not both. "
                    "Run uninstall --space <id> for each first (history is kept), or use --force")
    return None


def shared_stats_conflict(client: Client, space: str, installed: list[dict] | None = None) -> str | None:
    """Return another space whose installed copy already writes shared stats, if any.

    Uses the installed workflows when they can be listed, so a removed copy doesn't
    block. Otherwise falls back to the last 7 days of run history."""
    if installed is not None:
        others = [w["space"] for w in installed
                  if w["shared_stats"] and w["name"].startswith("lagprof-hourly-space-") and w["space"] != space]
        return others[0] if others else None
    body = {"size": 1, "_source": ["lagprof.space.id"],
            "query": {"bool": {"filter": [{"term": {"lagprof.wrote_shared_stats": True}},
                                          {"range": {"@timestamp": {"gte": "now-7d"}}}],
                               "must_not": [{"term": {"lagprof.space.id": space}}]}}}
    code, out = client.es("POST", "/lagprof-runs/_search", body, allow=(404,))
    if code == 404:
        return None
    hits = out.get("hits", {}).get("hits", [])
    return hits[0]["_source"]["lagprof"]["space"]["id"] if hits else None


def install(client: Client, rep: Reporter, opts: InstallOptions) -> int:
    space = opts.kibana_space
    installed = installed_workflows(client) if opts.import_workflow else None
    if installed is not None and not opts.force:
        why = mode_conflict(installed, opts)
        if why:
            rep.line("BLOCKED", "workflow", opts.workflow, why)
            return 1
    if opts.workflow == "per-space" and opts.shared_stats and not opts.force:
        other = shared_stats_conflict(client, opts.space or "", installed)
        if other:
            rep.line("BLOCKED", "workflow", opts.space or "",
                     f"space {other!r} already writes shared stats; use --force to override")
            return 1
    failures = 0
    for r in pack_resources(space):
        try:
            failures += install_one(client, rep, r, opts, space)
        except HttpError as err:
            rep.line("FAILED", r.kind, r.name, str(err))
            failures += 1
    return 1 if failures else 0


def install_one(client: Client, rep: Reporter, r: Resource, opts: InstallOptions,
                space: str | None) -> int:
    if r.kind == "workflow":
        return install_workflow(client, rep, opts)
    if r.kind == "index":
        code, _ = client.es("GET", f"/{r.name}", allow=(404,))
        if code == 404:
            client.es("PUT", f"/{r.name}")  # settings and mappings come from the index template
            rep.line("created", r.kind, r.name, "empty, from its index template")
            return 0
        if r.name == LOOKUP_INDEX:
            _, m = client.es("GET", f"/{r.name}/_mapping")
            installed = mapped_fields(next(iter(m.values()))["mappings"])
            extra = installed - lookup_template_fields()
            if extra:
                # Rebuilt by every workflow run, so recreating it loses nothing lasting.
                client.es("DELETE", f"/{r.name}")
                client.es("PUT", f"/{r.name}")
                rep.line("recreated", r.kind, r.name,
                         f"mapping was out of date ({len(extra)} extra fields); the next workflow run refills it")
                return 0
        rep.line("unchanged", r.kind, r.name, "exists")
        return 0
    if r.kind == "data_stream":
        code, _ = client.es("GET", f"/_data_stream/{r.name}", allow=(404,))
        if code == 404:
            client.es("PUT", f"/_data_stream/{r.name}")
            rep.line("created", r.kind, r.name, "empty, so dashboard panels work before the first run")
        else:
            rep.line("unchanged", r.kind, r.name, "exists")
        return 0
    if r.kind == "dashboard" and not (PACK_ROOT / (r.path or "")).exists():
        rep.line("skipped", r.kind, r.name, "run kibana/dashboards/build.py first")
        return 0

    spaces = role_spaces(opts.workflow, opts.space)
    marker = fingerprint(r.path, marker_extra(r, spaces, space))
    current = installed_version(client, r, space)
    if current == marker and not opts.force:
        rep.line("unchanged", r.kind, r.name, f"v{VERSION}")
        return 0
    verb = "created" if current is None else "updated"

    if r.kind in ES_PATHS:
        client.es("PUT", ES_PATHS[r.kind] + r.name, with_meta(load_json(r.path), marker))
        if r.kind == "component_template":
            sync_existing_mappings(client, rep, r)
    elif r.kind == "dashboard":
        # Dashboards API (9.5+): PUT creates with this ID or replaces it.
        client.kb("PUT", f"{space_prefix(space)}/api/dashboards/{r.name}", render_dashboard(r.path, marker, space))
    elif r.kind == "role":
        client.kb("PUT", f"/api/security/role/{r.name}", render_role(spaces, marker))
    elif r.kind == "alert_rule":
        body = render_alert_rule(r.path, space)
        body["tags"] = [t for t in body.get("tags", []) if not t.startswith("lagprof-version:")]
        body["tags"].append(f"lagprof-version:{marker}")
        body.pop("id")
        rule_id = r.name
        base = f"{space_prefix(space)}/api/alerting/rule/{rule_id}"
        if current is None:
            client.kb("POST", base, body)
        else:
            update = {k: body[k] for k in ("name", "schedule", "params", "tags", "actions") if k in body}
            client.kb("PUT", base, update)
    was = ""
    if current:
        was = " (content changed)" if marker_version(current) == VERSION else f" (was v{marker_version(current)})"
    rep.line(verb, r.kind, r.name, f"v{VERSION}{was}")
    return 0


def sync_existing_mappings(client: Client, rep: Reporter, r: Resource) -> None:
    """Add the component template's fields to lagprof-* indices that already exist.

    Templates only shape new backing indices, and ES|QL rejects a column that no index
    maps, so a dashboard panel reading a field added in a pack update would fail until
    the next rollover. Adding fields is always allowed. The lookup index keeps its own
    mapping: every field in it enters the join.
    """
    props = ((load_json(r.path).get("template") or {}).get("mappings") or {}).get("properties")
    if not props:
        return
    path = f"/lagprof-*,-{LOOKUP_INDEX}/_mapping?allow_no_indices=true&expand_wildcards=open,hidden"
    code, out = client.es("PUT", path, {"properties": props}, allow=(400,))
    if code == 400:
        rep.line("WARN", "mapping", "lagprof-*", f"could not add new fields to existing indices: {out}")


def workflow_id(text: str) -> str:
    m = re.search(r"(?m)^name:\s*(\S+)", text)
    if not m:
        raise ConfigError("workflow YAML has no name")
    return m.group(1)


def install_workflow(client: Client, rep: Reporter, opts: InstallOptions) -> int:
    fname, text = render_workflow(opts.workflow, opts.space, opts.shared_stats)
    out_dir = PACK_ROOT / "build"
    out_dir.mkdir(exist_ok=True)
    (out_dir / fname).write_text(text)
    rel = f"build/{fname}"
    if not opts.import_workflow:
        where = f"the {opts.space!r} space" if opts.workflow == "per-space" else "the Default space"
        rep.line("manual", "workflow", fname, f"import {rel} in Kibana > Workflows in {where}")
        return 0
    prefix = space_prefix(opts.kibana_space)
    name = workflow_id(text)
    owner = client.as_workflow_owner()
    if owner is client:
        rep.line("WARN", "workflow", name, "LAGPROF_WORKFLOW_API_KEY not set: scheduled runs will use "
                 "the installer's privileges. See README, 'Workflow identity'.")
    # Match by name: Kibana doesn't reuse a deleted workflow's ID, so a re-created
    # workflow gets an ID like "<name>-1".
    _, listing = client.kb("GET", f"{prefix}{WORKFLOWS_API}?size=1000")
    matches = [w for w in (listing or {}).get("results", []) if w.get("name") == name]
    if not matches:
        _, out = owner.kb("POST", f"{prefix}{WORKFLOWS_API}", {"workflows": [{"yaml": text}]})
        created = (out or {}).get("created") or [{}]
        if not created[0].get("valid", True):
            rep.line("FAILED", "workflow", name, "Kibana marked the imported workflow invalid")
            return 1
        rep.line("created", "workflow", name, f"id {created[0].get('id', '?')}; first scheduled run starts now")
        return 0
    if len(matches) > 1:
        rep.line("WARN", "workflow", name,
                 f"{len(matches)} workflows share this name ({', '.join(w['id'] for w in matches)}); "
                 "updating the first. Delete the extras so they don't all run.")
    wid = matches[0]["id"]
    _, current = client.kb("GET", f"{prefix}{WORKFLOWS_API}/workflow/{wid}")
    owner_name = _whoami(owner)
    owner_changes = owner is not client and (current or {}).get("lastUpdatedBy") != owner_name
    if (current or {}).get("yaml") == text and not opts.force and not owner_changes:
        rep.line("unchanged", "workflow", name, f"v{VERSION} (id {wid}, saved by {(current or {}).get('lastUpdatedBy')})")
        return 0
    _, out = owner.kb("PUT", f"{prefix}{WORKFLOWS_API}/workflow/{wid}", {"yaml": text})
    if not (out or {}).get("valid", True):
        rep.line("FAILED", "workflow", name, "Kibana marked the updated workflow invalid")
        return 1
    rep.line("updated", "workflow", name, f"v{VERSION} (id {wid}, saved by {owner_name})")
    return 0


def _whoami(client: Client) -> str:
    try:
        _, me = client.es("GET", "/_security/_authenticate")
        return (me or {}).get("username", "?")
    except HttpError:
        return "?"


def status(client: Client, rep: Reporter, space: str | None = None) -> int:
    for r in pack_resources(space):
        if r.kind == "workflow":
            # Per-space copies live in their own space.
            code, out = client.kb("GET", f"{space_prefix(space)}{WORKFLOWS_API}?size=1000", allow=(400, 404))
            names = [f"{w['name']} (id {w['id']})" for w in (out or {}).get("results", [])
                     if str(w.get("name", "")).startswith("lagprof-")] if code == 200 else []
            rep.line("present" if names else "missing", r.kind, ", ".join(names) or r.name,
                     "in this space" if names else "")
            continue
        if r.kind in ("index", "data_stream"):
            path = f"/_data_stream/{r.name}" if r.kind == "data_stream" else f"/{r.name}"
            code, _ = client.es("GET", path, allow=(404,))
            rep.line("missing" if code == 404 else "present", r.kind, r.name)
            continue
        try:
            v = installed_version(client, r, space)
        except HttpError as err:
            rep.line("error", r.kind, r.name, str(err))
            continue
        spaces = role_spaces("per-space" if space else "all-spaces", space)
        marker = fingerprint(r.path, marker_extra(r, spaces, space))
        if v is None:
            rep.line("missing", r.kind, r.name)
        elif v == marker:
            rep.line("current", r.kind, r.name, f"v{VERSION}")
        elif marker_version(v) == VERSION:
            rep.line("outdated", r.kind, r.name, f"v{VERSION}, but the pack file has changed since install")
        else:
            rep.line("outdated", r.kind, r.name, f"v{marker_version(v) or '?'} (pack is v{VERSION})")
    return 0


# Templates, pipelines, indices and data streams are shared by every installed workflow:
# the all-spaces version, or each per-space copy.
SHARED_KINDS = ("component_template", "index_template", "pipeline", "index", "data_stream")


def workflow_name(space: str | None) -> str:
    return f"lagprof-hourly-space-{space}" if space else "lagprof-hourly-all-spaces"


def other_workflows(client: Client, space: str | None) -> list[dict] | None:
    """Installed profiler workflows other than the one this uninstall removes.

    None if workflows can't be listed."""
    installed = installed_workflows(client)
    if installed is None:
        return None
    return [w for w in installed if w["name"] != workflow_name(space)]


def uninstall(client: Client, rep: Reporter, delete_data: bool, space: str | None = None) -> int:
    others = other_workflows(client, space)
    if others is None:
        rep.line("WARN", "workflow", "*", "couldn't list workflows to check for other copies; "
                 "removing shared resources too")
        others = []
    # While another copy is installed, its runs need the shared resources and data.
    shared_with = ", ".join(sorted(f"{w['name']} ({w['space']})" for w in others))
    if delete_data:
        if not others:
            client.es("DELETE", "/_data_stream/lagprof-*", allow=(404,))
            client.es("DELETE", "/lagprof-rules,lagprof-rule-streams?ignore_unavailable=true", allow=(404,))
            rep.line("deleted", "data", "lagprof-*", "data streams and rule indices")
        elif space:
            # Only this space's documents. Stream stats and host lag have no space and
            # are shared, so they stay.
            client.es("POST", "/lagprof-*/_delete_by_query?conflicts=proceed&refresh=true"
                      "&allow_no_indices=true&ignore_unavailable=true",
                      {"query": {"term": {"lagprof.space.id": space}}})
            rep.line("deleted", "data", "lagprof-*", f"documents for space {space!r}; "
                     f"shared stream and host stats kept for {shared_with}")
        else:
            rep.line("kept", "data", "lagprof-*", f"other workflows are installed: {shared_with}")
    failures = 0
    for r in reversed(pack_resources(space)):
        if others and r.kind in SHARED_KINDS:
            rep.line("kept", r.kind, r.name, f"shared with {shared_with}")
            continue
        try:
            if r.kind in ES_PATHS:
                code, out = client.es("DELETE", ES_PATHS[r.kind] + r.name, allow=(400, 404))
                if code == 404:
                    rep.line("absent", r.kind, r.name)
                elif code == 400:
                    rep.line("kept", r.kind, r.name, "in use (history kept; use --delete-data)")
                else:
                    rep.line("deleted", r.kind, r.name)
            elif r.kind == "role":
                code, _ = client.kb("DELETE", f"/api/security/role/{r.name}", allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind == "alert_rule":
                code, _ = client.kb("DELETE", f"{space_prefix(space)}/api/alerting/rule/{r.name}",
                                    allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind == "dashboard":
                code, _ = client.kb("DELETE", f"{space_prefix(space)}/api/dashboards/{r.name}", allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind in ("index", "data_stream"):
                if not delete_data:
                    rep.line("kept", r.kind, r.name, "data (use --delete-data)")
            elif r.kind == "workflow":
                prefix = space_prefix(space)
                code, out = client.kb("GET", f"{prefix}{WORKFLOWS_API}?size=1000", allow=(400, 404))
                # Only this install's workflow: a per-space copy, or the all-spaces version.
                wanted = workflow_name(space)
                found = [w for w in (out or {}).get("results", [])
                         if w.get("name") == wanted] if code == 200 else []
                for w in found:
                    client.kb("DELETE", f"{prefix}{WORKFLOWS_API}/workflow/{w['id']}")
                    rep.line("deleted", r.kind, w["name"], f"id {w['id']}")
                if not found:
                    rep.line("absent", r.kind, r.name)
            else:
                rep.line("manual", r.kind, r.name, "remove in Kibana")
        except HttpError as err:
            rep.line("FAILED", r.kind, r.name, str(err))
            failures += 1
    return 1 if failures else 0


# --------------------------------------------------------------------------------------
# Lint (offline)
# --------------------------------------------------------------------------------------

LOOKUP_INDEX = "lagprof-rule-streams"
LOOKUP_TEMPLATE = "elasticsearch/index-templates/lagprof-rule-streams.json"


def mapped_fields(mapping: dict, prefix: str = "") -> set[str]:
    """Leaf field paths in a mappings 'properties' tree."""
    out: set[str] = set()
    for name, spec in (mapping.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if "properties" in spec:
            out |= mapped_fields(spec, path + ".")
        else:
            out.add(path)
    return out


def lookup_template_fields() -> set[str]:
    return mapped_fields(load_json(LOOKUP_TEMPLATE)["template"]["mappings"])


def prejoin_columns(query: str) -> set[str]:
    """Column names a query creates (name = ...) before its LOOKUP JOIN."""
    head = query.split("LOOKUP JOIN", 1)[0]
    head = re.sub(r"//[^\n]*", "", head)
    return set(re.findall(r"(?<![=!<>\w.])([A-Za-z_][\w.]*)\s*=(?!=)", head))


def _walk_keys(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path
            yield from _walk_keys(v, path)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def lint(rep: Reporter) -> int:
    problems = 0

    def bad(kind, name, msg):
        nonlocal problems
        problems += 1
        rep.line("PROBLEM", kind, name, msg)

    for r in pack_resources():
        if not r.path or not r.path.endswith(".json"):
            continue
        try:
            body = load_json(r.path)
        except (OSError, ValueError) as err:
            bad(r.kind, r.name, f"invalid JSON: {err}")
            continue
        if r.kind in ("component_template", "index_template"):
            settings = (body.get("template") or {}).get("settings") or {}
            for key in _walk_keys(settings):
                if any(key.endswith(f) or f"{f}." in key for f in FORBIDDEN_SETTING_KEYS) or "lifecycle.name" in key:
                    bad(r.kind, r.name, f"setting {key} is rejected on Serverless")
        is_lookup = (((body.get("template") or {}).get("settings") or {}).get("index") or {}).get("mode") == "lookup"
        if r.kind == "index_template" and not is_lookup and "lagprof-common" not in body.get("composed_of", []):
            bad(r.kind, r.name, "does not use the lagprof-common component template")
        if r.kind == "index_template" and is_lookup and body.get("composed_of"):
            bad(r.kind, r.name, "lookup index must have its own mapping: every mapped field enters the join")
        if r.kind == "index_template" and "data_stream" in body:
            if not ((body.get("template") or {}).get("lifecycle") or {}).get("data_retention"):
                bad(r.kind, r.name, "data stream has no lifecycle data_retention")

    # A lookup field replaces any query column of the same name in LOOKUP JOIN.
    clash = prejoin_columns((PACK_ROOT / "esql" / "miss-rate.esql").read_text()) & lookup_template_fields()
    if clash:
        bad("esql", "miss-rate.esql", f"columns created before the join are also mapped in {LOOKUP_INDEX} "
            f"and would be replaced: {', '.join(sorted(clash))}")

    # The esql/ files are the readable copies of the queries inside the workflows.
    workflows = {m: normalize_ws((PACK_ROOT / p).read_text()) for m, p in WORKFLOW_FILES.items()}
    for qfile in sorted((PACK_ROOT / "esql").glob("*.esql")):
        q = normalize_ws(qfile.read_text())
        for mode, wf in workflows.items():
            expected = q
            if mode == "per-space" and qfile.stem == "miss-rate":
                expected = q.replace("| WHERE lagprof.rule.id IS NOT NULL",
                                     "| WHERE lagprof.space.id == ?space_id AND lagprof.rule.id IS NOT NULL")
            if expected not in wf:
                bad("esql", qfile.name, f"differs from the query in the {mode} workflow")

    for mode in WORKFLOW_FILES:
        try:
            render_workflow(mode, "lint-check", True)
        except ConfigError as err:
            bad("workflow", mode, str(err))

    if not problems:
        rep.line("ok", "lint", "pack", f"v{VERSION}")
    return 1 if problems else 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lagprof.py", description="Ingest Lag Profiler installer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def conn(p):
        p.add_argument("--target-file", help="KEY=VALUE file with connection settings")
        p.add_argument("--target", choices=TARGETS, help="override LAGPROF_TARGET")

    def wf(p):
        p.add_argument("--workflow", choices=tuple(WORKFLOW_FILES), default="all-spaces")
        p.add_argument("--space", help="space ID (per-space workflow)")
        p.add_argument("--shared-stats", action="store_true",
                       help="this per-space copy writes stream stats and host lag")

    p = sub.add_parser("check", help="check the target can run the profiler")
    conn(p)
    p.add_argument("--fix-license", action="store_true", help="start a trial (docker only)")

    p = sub.add_parser("install", help="install or update the pack")
    conn(p)
    wf(p)
    p.add_argument("--no-import-workflow", dest="import_workflow", action="store_false",
                   help="only write the workflow YAML to build/ for a manual import")
    p.add_argument("--force", action="store_true", help="reinstall even if versions match")

    p = sub.add_parser("status", help="show installed versions")
    conn(p)
    p.add_argument("--space")

    p = sub.add_parser("uninstall", help="remove the pack")
    conn(p)
    p.add_argument("--space")
    p.add_argument("--delete-data", action="store_true", help="also delete all lagprof-* data")
    p.add_argument("--yes", action="store_true", help="don't ask before deleting data")

    sub.add_parser("lint", help="offline checks of the pack files")

    p = sub.add_parser("render-workflow", help="write a ready-to-import workflow YAML to build/")
    wf(p)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rep = Reporter()
    try:
        if args.cmd == "lint":
            return lint(rep)
        if args.cmd == "render-workflow":
            fname, text = render_workflow(args.workflow, args.space, args.shared_stats)
            out = PACK_ROOT / "build" / fname
            out.parent.mkdir(exist_ok=True)
            out.write_text(text)
            rep.line("written", "workflow", f"build/{fname}")
            return 0

        cfg = load_config(args.target_file, target_override=args.target)
        client = Client(cfg)
        rep.note(f"lagprof v{VERSION} -> {cfg.target} ({cfg.es_url})")

        if args.cmd == "check":
            res = run_check(client, rep, fix_license=args.fix_license)
            return 1 if res.blocking else 0
        if args.cmd == "install":
            if args.workflow == "per-space" and not args.space:
                raise ConfigError("per-space mode needs --space")
            res = run_check(client, rep, fix_license=cfg.target == "docker")
            if res.blocking:
                rep.note("Not installing: fix the BLOCKED items above first.")
                return 1
            rep.note("")
            return install(client, rep, InstallOptions(
                workflow=args.workflow, space=args.space, shared_stats=args.shared_stats,
                import_workflow=args.import_workflow, force=args.force))
        if args.cmd == "status":
            return status(client, rep, args.space)
        if args.cmd == "uninstall":
            if args.delete_data and not args.yes:
                answer = input("Delete lagprof-* data, including history? If other profiler workflows "
                               "are installed, only this space's documents are deleted. Type 'delete': ")
                if answer.strip() != "delete":
                    rep.note("Cancelled.")
                    return 1
            return uninstall(client, rep, args.delete_data, args.space)
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except HttpError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
