#!/usr/bin/env python3
"""Check URL sets from several runs with one fetch per document; retain separate results."""

import argparse
import sys
import urllib.parse
from pathlib import Path

import harness as h


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--logs", nargs="+", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = p.parse_args()
    if len({d.name for d in args.logs}) != len(args.logs):
        p.error("Log directory names must be unique")
    args.output.mkdir(parents=True, exist_ok=False)
    sets = {d.name: set(h.collect_urls(d)) for d in args.logs}
    cache, checked = {}, {}
    for url in sorted(set().union(*sets.values())):
        bare = urllib.parse.urlsplit(url)._replace(fragment="").geturl()
        if bare not in cache:
            cache[bare] = h.fetch_page(bare)
        rec = h.check_link(url, cache[bare])
        checked[url] = rec
        if rec["ok"] is False:
            print("FAIL", rec["status"], url, flush=True)
        if len(checked) % 25 == 0:
            print(
                "Checked",
                len(checked),
                "URLs;",
                len(cache),
                "documents fetched",
                flush=True,
            )
        for name, urls in sets.items():
            h.write_json(
                args.output / (name + ".json"),
                [checked[u] for u in sorted(urls) if u in checked],
            )
    summaries = {}
    for name, urls in sets.items():
        records = [checked[u] for u in sorted(urls)]
        summaries[name] = dict(h.link_summary(records), extracted_urls=len(urls))
    h.write_json(args.output / "summary.json", summaries)
    print(summaries, flush=True)
    records = list(checked.values())
    return (
        1
        if any(r["ok"] is False for r in records)
        else 2
        if any(r["ok"] is None for r in records)
        else 0
    )


if __name__ == "__main__":
    h.ensure_environment()
    sys.exit(main())
