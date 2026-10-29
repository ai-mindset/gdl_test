#!/usr/bin/env python3
"""GDL evidence harness. Run via uv and this directory's .venv; see README.md."""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path

BASE = Path(__file__).resolve().parent
SUITE = BASE / "suite.json"
LOGS = BASE / "logs"
DEFAULT_URL = (
    "https://publicdigital-delivery-compass.netlify.app/mcp?key=gdl-trial-2026"
)
API_URL = os.environ.get("GDL_URL") or DEFAULT_URL
UA = "gdl-clitest/0.2 (python-urllib)"
UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)
ACCEPT = "application/json, text/event-stream"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
TIMEOUT_API, TIMEOUT_PAGE, SLEEP_S = 45, 25, 0.5
SKIP_HOSTS = {"publicdigital-delivery-compass.netlify.app"}


def utcnow():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def ensure_environment(base=BASE):
    """Before CLI work, prefer uv and require the project-local .venv.

    Installation is opt-in. Declining (including EOF) uses a local python3
    installation, without installing packages or downloading Python.
    Imports have no bootstrap side effects, allowing isolated unit tests.
    """
    base = Path(base).resolve()
    envdir = base / ".venv"
    bindir = "Scripts" if os.name == "nt" else "bin"
    envpython = envdir / bindir / ("python.exe" if os.name == "nt" else "python")
    local_uv = base / ".tools" / ("uv.exe" if os.name == "nt" else "uv")
    uv = shutil.which("uv") or (str(local_uv) if local_uv.is_file() else None)
    if not uv:
        try:
            answer = input(
                "uv is missing. Install it locally in .tools from "
                "https://astral.sh/uv/install.sh? [y/N] "
            )
        except (EOFError, OSError):
            answer = "n"
        if answer.strip().lower() in {"y", "yes"}:
            if os.name == "nt":
                raise RuntimeError(
                    "Install uv using the official Windows installer, then retry."
                )
            shell = shutil.which("sh")
            if not shell:
                raise RuntimeError(
                    "Cannot run the approved uv installer: sh was not found."
                )
            with urllib.request.urlopen(
                "https://astral.sh/uv/install.sh", timeout=30
            ) as response:
                installer = response.read()
            install_env = dict(
                os.environ, UV_INSTALL_DIR=str(base / ".tools"), UV_NO_MODIFY_PATH="1"
            )
            subprocess.run([shell], input=installer, env=install_env, check=True)
            uv = str(local_uv) if local_uv.is_file() else shutil.which("uv")
            if not uv:
                raise RuntimeError(
                    "uv installer finished but no uv executable was found."
                )
        else:
            print(
                "uv installation declined; using local python3 with .venv.",
                file=sys.stderr,
            )
    if not envpython.is_file():
        if envdir.exists():
            raise RuntimeError(
                ".venv exists but has no Python executable; repair it before retrying."
            )
        python3 = shutil.which("python3")
        if not python3:
            raise RuntimeError(
                "No local python3 installation found; no harness command was run."
            )
        if uv:
            subprocess.run(
                [
                    uv,
                    "venv",
                    "--python",
                    python3,
                    "--no-python-downloads",
                    "--cache-dir",
                    str(base / ".uv-cache"),
                    str(envdir),
                ],
                check=True,
            )
        else:
            subprocess.run(
                [python3, "-m", "venv", "--without-pip", str(envdir)], check=True
            )
    if Path(sys.prefix).resolve() == envdir and sys.prefix != sys.base_prefix:
        return
    script = str(Path(sys.argv[0]).resolve())
    if uv:
        os.environ.setdefault("UV_CACHE_DIR", str(base / ".uv-cache"))
        os.execv(
            uv,
            [
                uv,
                "run",
                "--no-project",
                "--no-sync",
                "--no-python-downloads",
                "--python",
                str(envpython),
                "--",
                str(envpython),
                script,
                *sys.argv[1:],
            ],
        )
    else:
        os.execv(str(envpython), [str(envpython), script, *sys.argv[1:]])


def sse_messages(lines):
    """Assemble SSE events, including multiline data, before parsing JSON."""
    data = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield json.loads("\n".join(data))
                except json.JSONDecodeError:
                    pass
            data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value.removeprefix(" "))
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass


def is_rpc_response(message, rid=None):
    return (
        isinstance(message, dict)
        and "id" in message
        and (rid is None or message["id"] == rid)
        and ("result" in message or "error" in message)
    )


def parse_body(raw, ctype, rid=None):
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype.lower():
        for message in sse_messages(text.splitlines()):
            if is_rpc_response(message, rid):
                return message
        return {"_unparsed_sse": text[:1500]}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_unparsed": text[:1500]}


def request(url, payload=None, accept=ACCEPT, timeout=TIMEOUT_API, headers=None):
    """Capture one distinct HTTP call. SSE stops at the matching RPC response."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdr = {"Content-Type": "application/json", "Accept": accept, "User-Agent": UA}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr)
    out = {
        "call_id": str(uuid.uuid4()),
        "requested_at": utcnow(),
        "request": payload,
        "request_headers": hdr,
    }
    t0 = time.perf_counter()
    try:
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response as r:
            content_type = r.headers.get("Content-Type", "")
            out.update(
                status=r.status,
                content_type=content_type,
                final_url=r.url,
                response_headers=dict(r.headers),
            )
            if "text/event-stream" in content_type.lower():
                rid = payload.get("id") if isinstance(payload, dict) else None
                out["body"] = {
                    "_unparsed_sse": "stream ended without the matching response"
                }
                events = []
                for message in sse_messages(iter(r.readline, b"")):
                    if is_rpc_response(message, rid):
                        out["body"] = message
                        break
                    events.append(message)
                out["preceding_sse_messages"] = events
            else:
                raw = r.read()
                out["raw_body"] = raw.decode("utf-8", "replace")
                out["body_sha256"] = hashlib.sha256(raw).hexdigest()
                out["body"] = parse_body(raw, content_type)
    except (OSError, ValueError, HTTPException) as error:
        out.update(status=0, error=f"{type(error).__name__}: {error}")
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def rpc(method, params, rid, **kw):
    payload = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        payload["id"] = rid
    if params is not None:
        payload["params"] = params
    return request(API_URL, payload, **kw)


def call(name, args, rid, **kw):
    return rpc("tools/call", {"name": name, "arguments": args}, rid, **kw)


def classify(res):
    payload = res.get("request") or {}
    if (
        res.get("status") == 202
        and payload.get("method", "").startswith("notifications/")
        and "id" not in payload
        and not res.get("raw_body")
    ):
        return "notification_accepted"
    if res.get("status") != 200:
        return f"http_{res.get('status', 0)}"
    b = res.get("body")
    if not isinstance(b, dict) or b.get("jsonrpc") != "2.0":
        return "invalid_response"
    if "id" not in b or ("id" in payload and b["id"] != payload["id"]):
        return "invalid_response"
    if ("result" in b) == ("error" in b):
        return "invalid_response"
    if "error" in b:
        e = b["error"]
        return (
            "rpc_error"
            if (
                isinstance(e, dict)
                and isinstance(e.get("code"), int)
                and isinstance(e.get("message"), str)
            )
            else "invalid_response"
        )
    r = b["result"]
    if not isinstance(r, dict):
        return "invalid_response"
    if isinstance(r.get("content"), list):
        return "tool_isError" if r.get("isError") else "tool_ok"
    if isinstance(r.get("tools"), list):
        return "tools_list"
    return "rpc_result"


def tool_text(res):
    b = res.get("body") or {}
    r = (b.get("result") or {}) if isinstance(b, dict) else {}
    return (
        "\n".join(
            c["text"]
            for c in r.get("content", [])
            if isinstance(c, dict)
            and c.get("type") == "text"
            and isinstance(c.get("text"), str)
        )
        if isinstance(r, dict)
        else ""
    )


def result_object(res):
    body = res.get("body")
    result = body.get("result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else {}


def advertised_tools(res):
    tools = result_object(res).get("tools")
    return tools if isinstance(tools, list) else []


def schema_sanity(schema):
    """Limited structural checks, explicitly not full JSON Schema validation."""
    issues = []
    valid_types = {"object", "array", "string", "number", "integer", "boolean", "null"}

    def visit(s, path):
        if not isinstance(s, dict):
            issues.append(path + ": expected schema object")
            return
        types = s.get("type", [])
        types = types if isinstance(types, list) else [types]
        if any(not isinstance(t, str) or t not in valid_types for t in types):
            issues.append(path + ": unrecognised type")
        for name in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if name in s and (
                not isinstance(s[name], (int, float)) or isinstance(s[name], bool)
            ):
                issues.append(path + ": " + name + " must be numeric")
        props = s.get("properties", {})
        if not isinstance(props, dict):
            issues.append(path + ": properties must be an object")
            props = {}
        required = s.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(k, str) for k in required)
            or len(set(required)) != len(required)
        ):
            issues.append(path + ": required must contain unique strings")
        for name, sub in props.items():
            visit(sub, path + "." + name)
        if "items" in s:
            visit(s["items"], path + "[]")

    visit(schema, "schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        issues.append("top-level type must be object for an MCP tool")
    return issues


def entry_check(entry):
    responses = entry.get("responses", [])
    if entry.get("skipped") or not responses:
        return False
    outcomes = [classify(r) for r in responses]
    if entry.get("kind") == "repeat":
        return entry.get("identical") is True and outcomes == ["tool_ok"]
    if entry.get("kind") == "dedupe":  # old G8 holds both responses
        return outcomes == ["tool_ok", "tool_ok"] and (
            responses[0]["body"]["result"] == responses[1]["body"]["result"]
        )
    if entry.get("kind") == "initialize":
        return entry.get("lifecycle_pass") is True
    if entry.get("kind") == "meta":
        advertised = advertised_tools(responses[0])
        return (
            outcomes == ["tools_list"]
            and bool(advertised)
            and all(
                isinstance(t, dict) and not schema_sanity(t.get("inputSchema"))
                for t in advertised
            )
        )
    allowed = entry.get("auto_expect_error")
    if allowed:
        if outcomes[0] in {"rpc_error", "tool_isError"}:
            return outcomes[0] in allowed
        if outcomes != ["tool_ok"] or "tool_ok" not in allowed:
            return False
        clamp = entry.get("clamp_check")
        if not clamp:
            return False  # success alone never proves a clamp
        text = tool_text(responses[0])
        if clamp["kind"] == "sections":
            n = len(re.findall(r"^## \d+\. ", text, re.MULTILINE))
            return 1 <= n <= clamp["maximum"]
        if clamp["kind"] == "days":
            return text.startswith(f"# Change notes, last {clamp['maximum']} days\n")
        return False
    return all(o == "tool_ok" for o in outcomes)


def logwrite(entry, logdir):
    logdir = Path(logdir)
    write_json(logdir / (entry["id"] + ".json"), entry)


def load_entries(logdir=LOGS):
    entries = []
    for path in sorted(Path(logdir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("responses"), list):
            entries.append(data)
        elif isinstance(data, dict) and "request" in data and "status" in data:
            entries.append(
                {
                    "id": path.stem,
                    "kind": "call",
                    "category": "followup",
                    "legacy_format": True,
                    "responses": [data],
                }
            )
    return entries


def unique_responses(entries):
    seen, out = set(), []
    for e in entries:
        for r in e.get("responses", []):
            # Legacy G8 embeds an exact copy of A1. New calls have unique IDs.
            key = r.get("call_id") or json.dumps(r, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                out.append((e["id"], r))
    return out


def select_entries(suite, only):
    by_id = {e["id"]: e for e in suite}
    if len(by_id) != len(suite):
        raise ValueError("Duplicate suite IDs")
    if only and any(not any(k.startswith(prefix) for k in by_id) for prefix in only):
        raise ValueError("An --only prefix matches no suite entry")
    selected = {
        e["id"] for e in suite if not only or any(e["id"].startswith(p) for p in only)
    }
    pending = list(selected)
    while pending:
        e = by_id[pending.pop()]
        deps = e.get("from", []) if e.get("kind") in {"chain", "repeat"} else []
        for dep in deps:
            if dep not in by_id:
                raise ValueError("Missing prerequisite: " + dep)
            if dep not in selected:
                selected.add(dep)
                pending.append(dep)
    ordered, visiting, done = [], set(), set()

    def add(key):
        if key in done:
            return
        if key in visiting:
            raise ValueError("Cyclic prerequisites: " + key)
        visiting.add(key)
        e = by_id[key]
        for dep in e.get("from", []) if e.get("kind") in {"chain", "repeat"} else []:
            add(dep)
        visiting.remove(key)
        done.add(key)
        ordered.append(copy.deepcopy(e))

    for e in suite:
        if e["id"] in selected:
            add(e["id"])
    return ordered


def run_initialize(entry, rid):
    """An isolated initialize -> initialized -> tools/list lifecycle probe."""
    r = rpc(
        "initialize",
        {
            "protocolVersion": PROTOCOLS[0],
            "capabilities": {},
            "clientInfo": {"name": "gdl-clitest", "version": "0.2"},
        },
        rid,
    )
    entry["responses"] = [r]
    result = result_object(r)
    version = result.get("protocolVersion")
    capabilities = result.get("capabilities")
    if (
        classify(r) != "rpc_result"
        or version not in PROTOCOLS
        or not isinstance(capabilities, dict)
        or not isinstance(capabilities.get("tools"), dict)
        or not isinstance(result.get("serverInfo"), dict)
    ):
        entry["lifecycle_pass"] = False
        return
    headers = {"MCP-Protocol-Version": version}
    session = next(
        (
            v
            for k, v in r.get("response_headers", {}).items()
            if k.lower() == "mcp-session-id"
        ),
        None,
    )
    if session:
        headers["Mcp-Session-Id"] = session
    n = rpc("notifications/initialized", None, None, headers=headers)
    entry["responses"].append(n)
    if classify(n) != "notification_accepted":
        entry["lifecycle_pass"] = False
        return
    listing = rpc("tools/list", None, rid + 1, headers=headers)
    entry["responses"].append(listing)
    entry["negotiated_protocol"] = version
    entry["lifecycle_pass"] = classify(listing) == "tools_list"


def run(only=None, logdir=None, suite_path=SUITE):
    suite = json.loads(Path(suite_path).read_text(encoding="utf-8"))
    entries = select_entries(suite, only)  # validate before creating anything
    if not entries:
        raise ValueError("No entries selected")
    logdir = (
        Path(logdir)
        if logdir
        else BASE / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    logdir.mkdir(parents=True, exist_ok=False)  # do not overwrite published evidence
    write_json(
        logdir / "run.json",
        {
            "started": utcnow(),
            "endpoint": API_URL.split("?")[0],
            "suite_sha256": hashlib.sha256(Path(suite_path).read_bytes()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "requested_prefixes": only,
            "entry_ids": [e["id"] for e in entries],
            "python": sys.version,
            "environment": sys.prefix,
        },
    )
    print(f"Running {len(entries)} entries; evidence: {logdir}")
    completed = {}
    for i, entry in enumerate(entries, 1):
        rid, kind = 100 + i * 10, entry.get("kind", "call")
        entry["responses"] = []
        if kind == "meta":
            res = rpc("tools/list", None, rid)
            entry["responses"] = [res]
            advertised = advertised_tools(res)
            entry["schema_sanity"] = {
                t.get("name", "(unnamed)")
                if isinstance(t, dict)
                else f"(entry {j})": schema_sanity(
                    t.get("inputSchema") if isinstance(t, dict) else None
                )
                for j, t in enumerate(advertised)
            }
        elif kind == "initialize":
            run_initialize(entry, rid)
        elif kind == "chain":
            src_id = None
            for fid in entry.get("from", []):
                for prior in completed.get(fid, {}).get("responses", []):
                    if classify(prior) != "tool_ok":
                        continue
                    m = re.search(
                        r"^Source:[^\n]*" + chr(96) + r"([a-z0-9][a-z0-9-]*)" + chr(96),
                        tool_text(prior),
                        re.MULTILINE,
                    )
                    if m:
                        src_id = m.group(1)
                        entry["resolved_from"] = fid
                        break
                if src_id:
                    break
            if src_id:
                entry["resolved_source"] = src_id
                entry["responses"] = [call("get_source", {"source_id": src_id}, rid)]
                time.sleep(SLEEP_S)
                entry["responses"].append(
                    call(
                        "search_guidance",
                        {
                            "question": entry["follow_up_question"],
                            "source_id": src_id,
                            "limit": 5,
                        },
                        rid + 1,
                    )
                )
            else:
                entry["skipped"] = "No successful prerequisite supplied a Source id"
        elif kind == "repeat":
            original = completed.get(entry["from"][0], {})
            if (
                original.get("responses")
                and classify(original["responses"][0]) == "tool_ok"
            ):
                res = call(original["tool"], original["args"], rid)
                entry["responses"] = [res]
                entry["compared_to"] = original["id"]
                valid = classify(res) == "tool_ok"
                entry["identical"] = (
                    (
                        res["body"]["result"]
                        == original["responses"][0]["body"]["result"]
                    )
                    if valid
                    else None
                )
                entry["text_identical"] = (
                    (tool_text(res) == tool_text(original["responses"][0]))
                    if valid
                    else None
                )
            else:
                entry["skipped"] = (
                    "Repeat prerequisite was not a successful tool result"
                )
                entry["identical"] = None
        elif kind == "raw":
            payload = dict(entry["payload"], id=rid)
            entry["responses"] = [request(API_URL, payload)]
        else:
            entry["responses"] = [call(entry["tool"], entry.get("args", {}), rid)]
        entry["outcomes"] = [classify(r) for r in entry["responses"]]
        entry["auto_pass"] = entry_check(entry)
        entry["manual_review_required"] = bool(entry.get("review_criteria"))
        logwrite(entry, logdir)
        completed[entry["id"]] = entry
        print(
            f"[{i}/{len(entries)}] {entry['id']}: {'PASS' if entry['auto_pass'] else 'FAIL'} "
            + (entry.get("skipped") or ", ".join(entry["outcomes"])),
            flush=True,
        )
        time.sleep(SLEEP_S)
    write_json(logdir / "metrics.json", metrics(logdir))
    return {
        "logs": str(logdir),
        "passed": all(e["auto_pass"] for e in entries),
        "entries": len(entries),
    }


def urls_in_response(res):
    texts = [tool_text(res)]
    body = res.get("body")
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        texts.append(str(error.get("message", "")))
    for u in re.findall(r"https?://[^\s\"'<>)\]]+", "\n".join(texts)):
        u = u.rstrip(".,;")
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
        if host not in SKIP_HOSTS:
            yield u


def collect_urls(logdir=LOGS):
    return sorted(
        {
            u
            for _, r in unique_responses(load_entries(logdir))
            for u in urls_in_response(r)
        }
    )


class PageText(HTMLParser):
    BLOCKS = frozenset(
        {
            "p",
            "div",
            "section",
            "li",
            "ul",
            "ol",
            "table",
            "tr",
            "td",
            "th",
            "br",
            "hr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "article",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.excluded = []
        self.parts, self.anchors = [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.anchors.add(attrs["id"])
        if tag == "a" and attrs.get("name"):
            self.anchors.add(attrs["name"])
        if self.excluded or tag in {"script", "style", "template", "head"}:
            if tag not in {"br", "hr", "meta", "link", "img", "input", "source", "wbr"}:
                self.excluded.append(tag)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.excluded:
            if tag in self.excluded:
                index = len(self.excluded) - 1 - self.excluded[::-1].index(tag)
                self.excluded = self.excluded[:index]
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.excluded:
            self.parts.append(data)


def normalize(s):
    s = unicodedata.normalize("NFKC", s)
    return re.sub(
        r"\s+",
        " ",
        s.translate(
            str.maketrans({"“": '"', "”": '"', "’": "'", "‘": "'", "–": "-", "—": "-"})
        ),
    ).strip()


def page_text(html):
    parser = PageText()
    parser.feed(html)
    parser.close()
    return normalize("".join(parser.parts))


def fetch_raw(url, user_agent=UA, timeout=TIMEOUT_PAGE):
    bare = urllib.parse.urlsplit(url)._replace(fragment="").geturl()
    req = urllib.request.Request(
        bare, headers={"User-Agent": user_agent, "Accept": "*/*"}
    )
    t0 = time.perf_counter()
    out = {"requested_at": utcnow()}
    try:
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response as r:
            raw = r.read()
            out.update(
                status=r.status,
                final=r.url,
                ctype=r.headers.get("Content-Type", ""),
                html=raw.decode("utf-8", "replace"),
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
    except (OSError, ValueError, HTTPException) as error:
        out.update(status=0, error=f"{type(error).__name__}: {error}", html="")
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def fetch_page(url):
    res = fetch_raw(url)
    if res.get("status") == 403:
        res = fetch_raw(url, UA_BROWSER)
        res["browser_ua_retry"] = True
    return res


def check_link(url, fetched=None):
    res = fetched if fetched is not None else fetch_page(url)
    frag = urllib.parse.urlsplit(url).fragment
    rec = {
        "url": url,
        "status": res.get("status"),
        "final_url": res.get("final"),
        "checked_at": res.get("requested_at"),
        "latency_ms": res.get("latency_ms"),
        "document_available": res.get("status") == 200,
        "fragment_verified": None,
    }
    for key in ("sha256", "error", "ctype", "browser_ua_retry"):
        if key in res:
            rec[key] = res[key]
    if not rec["document_available"]:
        rec.update(ok=False, verification="fetch_failed")
    elif not frag:
        rec.update(ok=True, verification="document_available", fragment_kind="none")
    elif re.fullmatch(r"page=\d+", frag):
        pdf = "application/pdf" in res.get("ctype", "").lower()
        page = int(frag[5:])
        rec.update(
            pdf_page=page,
            fragment_kind="pdf_page",
            pdf_content_type=pdf,
            ok=None if pdf and page >= 1 else False,
            verification="pdf_page_unverified"
            if pdf and page >= 1
            else "invalid_pdf_link",
        )
    elif ":~:" in frag:
        rec.update(
            ok=None, fragment_kind="text_highlight", verification="highlight_unverified"
        )
    elif "html" not in res.get("ctype", "").lower():
        rec.update(ok=None, fragment_kind="unknown", verification="fragment_unverified")
    else:
        parser = PageText()
        parser.feed(res.get("html", ""))
        found = urllib.parse.unquote(frag) in parser.anchors
        rec.update(
            ok=found,
            fragment_verified=found,
            fragment_kind="html_anchor",
            verification="anchor_found" if found else "anchor_missing",
        )
    return rec


def links(logdir=LOGS, output=None):
    urls, cache, results = collect_urls(logdir), {}, []
    output = Path(output) if output else Path(logdir) / "links.json"
    for i, url in enumerate(urls, 1):
        bare = urllib.parse.urlsplit(url)._replace(fragment="").geturl()
        if bare not in cache:
            cache[bare] = fetch_page(bare)
            time.sleep(0.15)
        rec = check_link(url, cache[bare])
        results.append(rec)
        write_json(output, results)
        print(
            f"[{i}/{len(urls)}] {rec['status']} {rec['verification']} {url[:100]}",
            flush=True,
        )
    if not urls:
        write_json(output, [])
    return results


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return round(s[k], 1)


def response_stats(responses):
    lat = [
        r["latency_ms"]
        for _, r in responses
        if isinstance(r.get("latency_ms"), (int, float))
    ]
    outcomes, tools = {}, {}
    for _, r in responses:
        name = ((r.get("request") or {}).get("params") or {}).get("name")
        if name:
            tools[name] = tools.get(name, 0) + 1
        out = classify(r)
        outcomes[out] = outcomes.get(out, 0) + 1
    return {
        "http_calls": len(responses),
        "latency_observations": len(lat),
        "latency_ms": {
            "min": min(lat) if lat else None,
            "p50": pct(lat, 50),
            "p95": pct(lat, 95),
            "max": max(lat) if lat else None,
        },
        "outcomes": outcomes,
        "tools": tools,
    }


def link_summary(records):
    legacy = any("document_available" not in r for r in records)
    kinds = {}
    for r in records:
        kind = r.get("fragment_kind")
        if not kind:
            kind = (
                "pdf_page"
                if "pdf_page" in r
                else "text_highlight"
                if r.get("highlight_fragment")
                else "html_anchor"
                if urllib.parse.urlsplit(r["url"]).fragment
                else "none"
            )
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "checked": len(records),
        "http_200": sum(r.get("status") == 200 for r in records),
        "fragment_kinds": kinds,
        "html_anchors_found": sum(
            r.get("fragment_verified", r.get("anchor_found")) is True for r in records
        ),
        "fully_verified": sum(
            r.get("ok") is True and (r.get("fragment_kind") in {"none", "html_anchor"})
            for r in records
        )
        if not legacy
        else None,
        "failed": [r["url"] for r in records if r.get("ok") is False],
        "unverified_fragments": sum(r.get("ok") is None for r in records)
        if not legacy
        else kinds.get("pdf_page", 0) + kinds.get("text_highlight", 0),
        "legacy_check_format": legacy,
    }


def metrics(logdir=LOGS, link_file=None):
    entries = load_entries(logdir)
    suite = {e["id"]: e for e in json.loads(SUITE.read_text(encoding="utf-8"))}
    manifest_path = Path(logdir) / "run.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    rs = unique_responses(entries)
    endpoints = sorted(
        {
            urllib.parse.urlsplit(r["final_url"])
            ._replace(query="", fragment="")
            .geturl()
            for _, r in rs
            if r.get("final_url")
        }
    )
    endpoint = manifest.get("endpoint") or (
        endpoints[0] if len(endpoints) == 1 else None
    )
    baseline = [(key, r) for key, r in rs if not key.startswith("X")]
    followups = [(key, r) for key, r in rs if key.startswith("X")]
    suite_entries = [
        e
        for e in entries
        if e.get("kind") not in {"repeat", "dedupe"} and not e["id"].startswith("X")
    ]
    checks = {e["id"]: entry_check(dict(suite.get(e["id"], {}), **e)) for e in entries}
    m = {
        "generated": utcnow(),
        "endpoint": endpoint,
        "observed_endpoints": endpoints,
        "evidence_directory": str(Path(logdir).relative_to(BASE))
        if Path(logdir).is_relative_to(BASE)
        else str(logdir),
        "logged_entries": len(entries),
        "baseline_entries": len(suite_entries),
        "current_suite_entries": len(suite),
        "run_suite_entries": len(manifest["entry_ids"])
        if "entry_ids" in manifest
        else None,
        "mechanical_rules": "Stored entry rules, supplemented by the current suite for legacy clamp checks",
        "repeat_entries": sum(e.get("kind") in {"repeat", "dedupe"} for e in entries),
        "followup_entries": sum(e["id"].startswith("X") for e in entries),
        "stored_response_occurrences": sum(
            len(e.get("responses", [])) for e in entries
        ),
        "legacy_deduplication": "Exact repeated legacy records counted once; new calls use call_id",
        **response_stats(rs),
        "baseline": response_stats(baseline),
        "followups": response_stats(followups),
        "mechanical_checks": checks,
        "manual_review_required": "Mechanical success does not assess relevance or completeness",
    }
    urls = set(collect_urls(logdir))
    lp = Path(link_file) if link_file else Path(logdir) / "links.json"
    if lp.exists():
        records = json.loads(lp.read_text(encoding="utf-8"))
        m["links"] = {
            **link_summary(records),
            "extracted_urls": len(urls),
            "unchecked_urls": sorted(urls - {r["url"] for r in records}),
        }
    repeat = next((e for e in entries if e.get("kind") in {"repeat", "dedupe"}), None)
    if repeat:
        valid = all(classify(r) == "tool_ok" for r in repeat["responses"]) and bool(
            repeat["responses"]
        )
        if repeat.get("kind") == "dedupe":
            valid = valid and len(repeat["responses"]) == 2
            identical = (
                (
                    repeat["responses"][0]["body"]["result"]
                    == repeat["responses"][1]["body"]["result"]
                )
                if valid
                else None
            )
        else:
            identical = repeat.get("identical") if valid else None
        m["repeat_comparison"] = {
            "id": repeat["id"],
            "valid": valid,
            "parsed_result_equal": identical,
            "scope": "one A1 repeat",
        }
    return m


def text_check(url, text, fetched=None):
    res = fetched if fetched is not None else fetch_page(url)
    html = "html" in res.get("ctype", "").lower()
    found = (
        (normalize(text) in page_text(res.get("html", "")))
        if (res.get("status") == 200 and html and bool(normalize(text)))
        else None
    )
    return {
        "url": url,
        "text": text,
        "checked_at": res.get("requested_at"),
        "status": res.get("status"),
        "found": found,
        "page_sha256": res.get("sha256"),
        "method": "HTML text substring after entity, typography and whitespace normalisation; scripts/styles excluded",
        "limitation": "Not byte-for-byte equality or browser layout/CSS validation",
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="new run directory; never overwrite logs")
    r.add_argument("--only", nargs="+")
    r.add_argument("--logs", type=Path)
    for name in ("links", "metrics"):
        sp = sub.add_parser(name)
        sp.add_argument("--logs", type=Path, default=LOGS)
        sp.add_argument("--output", type=Path)
        if name == "metrics":
            sp.add_argument("--links-file", type=Path)
    c = sub.add_parser("checktext")
    c.add_argument("url")
    c.add_argument("text")
    c.add_argument("--output", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "run":
        try:
            result = run(a.only, a.logs)
        except (ValueError, FileExistsError) as error:
            p.error(str(error))
        return 0 if result["passed"] else 1
    if a.cmd == "links":
        if not load_entries(a.logs):
            p.error("No response evidence found in --logs")
        records = links(a.logs, a.output)
        print(json.dumps(link_summary(records), indent=2))
        return (
            1
            if any(r["ok"] is False for r in records)
            else 2
            if any(r["ok"] is None for r in records)
            else 0
        )
    if a.cmd == "metrics":
        if not load_entries(a.logs):
            p.error("No response evidence found in --logs")
        value = metrics(a.logs, a.links_file)
        write_json(
            a.output
            or (BASE / "metrics.json" if a.logs == LOGS else a.logs / "metrics.json"),
            value,
        )
        print(json.dumps(value, indent=2))
        return 0
    rec = text_check(a.url, a.text)
    if a.output:
        write_json(a.output, rec)
    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["found"] is True else 1 if rec["found"] is False else 2


if __name__ == "__main__":
    try:
        ensure_environment()
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"Startup/IO error: {error}", file=sys.stderr)
        raise SystemExit(2)
