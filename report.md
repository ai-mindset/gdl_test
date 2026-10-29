# Gov Delivery Library — testing feedback

**Tested:** 27 October 2026 · **Reviewed:** 28 October 2026

**Service address:** https://publicdigital-delivery-compass.netlify.app/mcp (trial key)

GDL returned useful guidance passages with clear source links and dates. Showing search terms and warning when guidance has been replaced helps readers check results. The main improvement is clearer signalling when results miss the question.

## Test coverage and results

The [recorded run](logs/run.json) tested search, source information, coverage, change notes, connection setup and invalid inputs: **43 cases, 46 requests**. **41 passed automated checks; two failed** because `limit=0` and `days=0` were accepted despite an advertised minimum of 1. Expected errors count as passes in tests of invalid requests. Usefulness and completeness were assessed separately.

- MCP, the protocol AI tools use to connect, worked for setup and listing four tools. Input-definition checks were limited; this was not a full protocol audit. Responses delivered in parts were tested offline only.
- Missing questions and unknown sources or tools produced errors. Inputs exceeding limits were reduced to those limits. One repeated query returned identical results.
- Half the requests finished within **408.1 milliseconds**; the slowest took **543.2 milliseconds**. This short run made one request at a time and is not a performance benchmark. [README.md](README.md) explains the timing calculations.

Evidence: [metrics](metrics.json), [saved responses](logs/) and [suite](suite.json).

## Suggested improvements, in priority order

**1. Make clear when results do not answer the question.** We asked separate questions about NHS technology assessment and ordering coffee. In each case, GDL reported no matches for a key search term, then returned unrelated guidance. These notices help; consider also making clear that the returned passages do not answer the question. The coverage tool already explains exclusions and warns that no result does not mean no rule applies. Evidence: [E1](logs/E1.json), [E6](logs/E6.json), [F2](logs/F2.json).

A query about AI in project reviews missed the requested authority. A rephrased query limited to its Gate Review 1 guide found relevant AI and machine learning material in the library. **Both the wording and source filter changed**, so we cannot tell which helped. A follow-up should keep the question unchanged and vary only the filter. Evidence: [B4](logs/B4.json), [B4s](logs/B4s.json).

**2. Reserve conflict labels for claims that may contradict each other.** B2 pairs two requirements that both say to follow the same security standard; X80 pairs guidance on managing data with COVID research precautions. The warning that suggestions are unverified is helpful. Consider labelling these as related passages unless a specific contradiction can be identified. Evidence: [B2](logs/B2.json), [X80](logs/X80.json).

**3. Flag other passages about replaced processes.** A1/A4 quoted the former Government Digital Service spending-approval process from *Assurance for agile delivery of digital services*, without a warning. Other results helpfully identified which guidance had been replaced. Consider adding a warning and linking to the current process. The [Service Manual](https://www.gov.uk/service-manual/agile-delivery/getting-approval-to-spend-money-on-a-service) records the change on 1 April 2026; Treasury approval remains necessary in specified cases. The cited PDF page and text remain unchecked. Evidence: [A1](logs/A1.json), [A4](logs/A4.json), [saved official-page check](verification/sources/checks.json).

**4. Handle inputs according to the published rules.** Zero values produced six search results and a 90-day change window instead of errors. Reject values outside the advertised limits, or revise the rules and explain when a value has been replaced. Evidence: [tool definitions](logs/G2.json), [G5a](logs/G5a.json), [G7a](logs/G7a.json).

## Verification and limits

All **287 unique external links** returned a successful web response (HTTP 200). Checks found 215 named webpage sections; 34 links addressed whole documents. **27 text-highlight targets and 11 PDF page targets remain unverified.** A working link to the expected section does not establish that its content supports a claim. Evidence: [link checks](logs/links.json).

Five sampled quotations matched official pages after allowing for formatting differences. All 17 defined source checks passed, but they do not verify every passage, label or interpretation. Gate Review 1's AI examples guide reviewers; they do not require a separate AI review for every project. Evidence: [checks and saved pages](verification/sources/checks.json), [publisher's explanation](https://www.gov.uk/government/publications/gate-review-1-business-justification/gate-1-review-business-justification#tailoring-the-gate-review).

We did not test how AI assistants use results, check the whole library or establish repeatability across all queries. This review used saved evidence without rerunning the service. [README.md](README.md) gives instructions. Recorded responses are in [logs/](logs/); source checks accompany the report.
