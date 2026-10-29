# GDL test runner

Test the Gov Delivery Library trial service and save evidence for the feedback in [report.md](report.md) and [digest.txt](digest.txt). The runner uses Python 3.13+ built-in modules, **uv** to manage Python, and **.venv**, the workspace's isolated Python environment.

## Python setup

For first-time setup, run ./harness.py --help. Commands create .venv from local python3 when needed, without downloading Python. If uv is missing, the program asks before installing it into local .tools; declining or providing no input uses local python3 instead. Failed setup stops the command. Importing modules does not start setup.

Run commands from the repository root:

~~~sh
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B -m unittest -v
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B harness.py run --logs runs/recheck
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B harness.py links --logs runs/recheck
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B harness.py metrics --logs runs/recheck
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B verify_sources.py --output runs/source-check
~~~

Choose unused output directories. Working reruns under runs/ are ignored by Git. Review new results before replacing the published evidence; keep one published set. Existing directories are never overwritten by run. Selecting --only B7 also runs its B2/B3 prerequisites; missing source identifiers produce a logged skip and failure.

## Code checks

Sync the locked development tools, then run Ruff for style and ty for types:

~~~sh
uv sync --locked --python .venv/bin/python --no-python-downloads --link-mode=copy
uv run --locked --no-python-downloads --python .venv/bin/python -- ruff check .
uv run --locked --no-python-downloads --python .venv/bin/python -- ruff format --check .
uv run --locked --no-python-downloads --python .venv/bin/python -- ty check
~~~

pyproject.toml defines metadata, the dev dependency group and Python 3.13+ support. uv.lock records the resolved tool versions. The project runs as repository-local scripts, without a package build step.

## Evidence and provenance

The repository publishes one evidence set for **27 October 2026**:

- **logs/:** 43 cases covering 46 requests, saved once per case with inputs, responses, headers and timings.
- **logs/run.json:** service address, start time, Python version, case identifiers and hashes of the code and test definitions used. The current runner may differ from the recorded version; copied code and test files are not retained.
- **suite.json:** test definitions; its hash matches the recorded run.
- **metrics.json:** summary of the published responses, including 41 automated passes and two failures.
- **logs/links.json:** checks of the 287 external links returned by that run.
- **verification/sources/:** 17 official-source checks and one saved copy of each checked page.
- **verification/regression-tests.json:** results of the latest offline tests of the runner.

The raw responses and source checks let readers check the report's claims. Offline tests use the published responses; they do not need verification/. Source-page copies contain distinct pages, not repeated versions.

Without --logs, measurements and link checks use the published logs/. Recalculate the summary from that evidence without contacting the service:

~~~sh
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B harness.py metrics
~~~

For a quotation check, supply a URL and text; --output saves the result:

~~~sh
uv run --locked --no-python-downloads --python .venv/bin/python -- python -B harness.py checktext 'https://www.gov.uk/government/publications/the-green-book--2/the-green-book-2026#optimism-bias' 'Practitioners must account for optimism bias by making explicit adjustments at the outset of an appraisal.' --output runs/green-book-check.json
~~~

Text comparisons allow for spacing and typography differences, decode webpage character codes, and omit scripts and styles. Browser appearance, PDF page contents and text-highlight targets remain unchecked.

## Interpreting results

- **run:** exit code 0 if automated checks pass; 1 for failure or skipped cases. Both zero-input tests failed on 27 October. Expected errors pass tests of invalid requests.
- **links / verify_links.py:** 0 if all links and targets are verified; 1 for a failed download, missing section or wrong file type; 2 for incomplete checks.
- **checktext:** 0 if text matches after allowing for formatting differences, 1 if absent, 2 if inconclusive.
- **verify_sources.py:** 0 if all defined checks pass, 1 otherwise.

Automated passes do not establish relevance, completeness or whether a rule applies. The suite includes manual review criteria. Checks of input definitions are limited.

MCP (Model Context Protocol) defines how AI tools connect. G1 checks setup, acknowledgement and tool listing. Live responses used JSON, a structured data format; offline tests also cover responses delivered in parts.

GDL_URL changes the address for new requests. Recorded results retain their original address. Percentiles select sorted index round(p/100 * (n-1)), using Python's rounding. The recorded p50 is 406.8 ms; the report uses the median, the average of the two middle times, which is 408.1 ms.
