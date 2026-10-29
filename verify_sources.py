#!/usr/bin/env python3
"""Save primary-source snapshots and check the report's factual statements."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import harness as h


def verify(output, cases_path=h.BASE / "source_checks.json"):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cached, results = {}, []
    for case in cases:
        url = case["url"]
        bare = h.urllib.parse.urlsplit(url)._replace(fragment="").geturl()
        if bare not in cached:
            fetched = h.fetch_page(bare)
            snapshot = hashlib.sha256(bare.encode()).hexdigest()[:16] + ".json"
            h.write_json(output / snapshot, dict(fetched, url=bare))
            cached[bare] = (fetched, snapshot)
        res, snapshot = cached[bare]
        rec = {"id": case["id"], "url": url, "snapshot": snapshot}
        if case["kind"] == "quote":
            rec.update(h.text_check(url, case["text"], res))
            rec["passed"] = rec["found"] is True
        else:
            rec.update(
                status=res.get("status"),
                checked_at=res.get("requested_at"),
                page_sha256=res.get("sha256"),
                path=case["path"],
            )
            try:
                value = json.loads(res.get("html", ""))
                for key in case["path"]:
                    value = value[key]
                rec["value"] = value
                rec["passed"] = (
                    res.get("status") == 200
                    and isinstance(value, str)
                    and value.startswith(case.get("starts_with", ""))
                    and case.get("contains", "") in value
                )
            except (ValueError, KeyError, TypeError):
                rec["passed"] = False
        results.append(rec)
        h.write_json(output / "checks.json", results)
        print(case["id"], "PASS" if rec["passed"] else "FAIL", flush=True)
    return all(r["passed"] for r in results)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = p.parse_args()
    return 0 if verify(args.output) else 1


if __name__ == "__main__":
    h.ensure_environment()
    sys.exit(main())
