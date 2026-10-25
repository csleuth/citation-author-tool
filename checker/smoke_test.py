#!/usr/bin/env python3
"""Offline smoke test: text extraction, capping, quote verification and the
driver's output format, with no API call.

Writes a synthetic snapshot to data/check/smoke/ and checks the results.
Run from the project root:  .venv/bin/python checker/smoke_test.py
"""

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check  # noqa: E402

SMOKE_DIR = Path("data/check/smoke")

HTML = """<!doctype html>
<html><head>
<meta charset="utf-8"><title>Council approves new bridge | Example Gazette</title>
<meta name="author" content="Hidden Metaperson">
<script>var author = "Script Person";</script>
<style>.byline{color:red}</style>
<script type="application/ld+json">{"author":{"name":"Jsonld Person"}}</script>
</head><body>
<header><a href="/">Example Gazette</a><nav><ul><li>News</li><li>Sport</li></ul></nav></header>
<!-- byline: Comment Person -->
<main><article>
<h1>Council approves new bridge over the Avon</h1>
<div class="byline">By <a href="/au/jane">Jane Smith</a> and <a href="/au/raj">RAJ PATEL</a>,&nbsp;Local
  Democracy Reporters</div>
<time>16 October 2026</time>
<p>The council voted 7-2 on Tuesday. "It is a great day," said Councillor <b>Tom Jones</b>.</p>
<div style="display: none">Written by Invisible Person</div>
<div hidden>By Hidden Attr</div>
<p>Work starts next year.<br>Edited by Ed Itor</p>
<p>Ünïcödé check: Zoë Ñúñez</p>
</article>
<aside><h2>More from the Gazette</h2><ul><li>Fire at mill <span>By Other Writer</span></li></ul></aside>
</main>
<footer>&copy; 2026 Example Gazette. Reporting by Jane Smith in Bristol.</footer>
</body></html>
"""


def expect(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    return bool(cond)


def main() -> int:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    snap = SMOKE_DIR / "smoke.html.gz"
    snap.write_bytes(gzip.compress(HTML.encode("utf-8")))

    text, truncated = check.load_snapshot_text(snap)
    (SMOKE_DIR / "smoke_text.txt").write_text(text)
    ok = True
    print("---- extracted text ----\n" + text + "\n------------------------")
    ok &= expect(not truncated, "short page not truncated")
    ok &= expect("By Jane Smith and RAJ PATEL, Local Democracy Reporters" in text.splitlines(),
                 "byline on one line: source newline and nbsp are spaces, no space before comma")
    ok &= expect("said Councillor Tom Jones." in text, "inline tags joined as rendered")
    ok &= expect("Reporting by Jane Smith in Bristol." in text, "footer byline kept")
    ok &= expect("Example Gazette" in text.splitlines()[0], "header kept")
    ok &= expect("By Other Writer" in text, "sidebar kept (model must ignore it)")
    for hidden in ["Hidden Metaperson", "Script Person", "Jsonld Person", "Comment Person",
                   "Invisible Person", "Hidden Attr", "color:red", "| Example Gazette"]:
        ok &= expect(hidden not in text, f"hidden/non-visible text dropped: {hidden!r}")
    ok &= expect("Work starts next year.\nEdited by Ed Itor" in text, "<br> becomes a line break")
    ok &= expect("Zoë Ñúñez" in text, "charset decoded")
    latin = HTML.replace('<meta charset="utf-8">', '<meta charset="iso-8859-1">').encode("utf-8")
    ok &= expect("Zoë Ñúñez" in check.html_to_text(latin), "stored UTF-8 read as UTF-8 despite a Latin-1 meta tag")

    # Cap: keeps start and end.
    long_text = "START " + "x" * 200_000 + " END"
    capped, was_cut = check.cap_text(long_text)
    ok &= expect(was_cut and capped.startswith("START") and capped.endswith("END")
                 and len(capped) <= check.MAX_TEXT_CHARS + 100, "cap keeps start and end")

    # Verification with fabricated model outputs.
    good = check.verify_label({
        "is_cited_article": "yes",
        "authors": [
            {"name": "Jane Smith", "quote": "By Jane Smith and RAJ PATEL"},
            {"name": "RAJ PATEL", "quote": "By Jane   Smith and\nRAJ PATEL"},  # whitespace differs
        ],
        "no_author_reason": "", "no_author_quote": "", "notes": "RAJ PATEL is ALL CAPS.",
    }, text)
    ok &= expect([a["quote_verified"] for a in good["authors"]] == [True, True],
                 "real quotes verify (whitespace-insensitive)")
    ok &= expect(good["notes"] == "RAJ PATEL is ALL CAPS.", "no spurious notes")

    bad = check.verify_label({
        "is_cited_article": "yes",
        "authors": [
            {"name": "Jane Smith", "quote": "Written by Jane Smith"},       # not on page
            {"name": "Raj Patel", "quote": "By Jane Smith and RAJ PATEL"},  # case differs
        ],
        "no_author_reason": "staff/desk", "no_author_quote": "", "notes": "",
    }, text)
    ok &= expect(len(bad["authors"]) == 2, "failed authors are kept, not dropped")
    ok &= expect([a["quote_verified"] for a in bad["authors"]] == [False, False],
                 "fabricated quote and mismatched name both fail")
    ok &= expect("quote not found" in bad["notes"] and "name not in quote" in bad["notes"],
                 "failures recorded in notes")
    ok &= expect(bad["no_author_reason"] == "", "reason cleared when authors present")

    inconsistent = check.verify_label({
        "is_cited_article": "no",
        "authors": [{"name": "Jane Smith", "quote": "By Jane Smith"}],
        "no_author_reason": "", "no_author_quote": "", "notes": "Homepage.",
    }, text)
    ok &= expect(inconsistent["authors"] == [] and "Jane Smith" in inconsistent["notes"],
                 "authors on a non-article are moved to notes, not lost")

    wire = check.verify_label({
        "is_cited_article": "yes", "authors": [], "no_author_reason": "wire/agency",
        "no_author_quote": "By Associated Press", "notes": "",
    }, text)
    ok &= expect(wire["no_author_quote_verified"] is False and "[verify]" in wire["notes"],
                 "no_author_quote is verified too")

    # Driver path without the API: a stub client returns a canned label.
    class Stub:
        def label(self, msg):
            assert "Cited title: Council approves new bridge" in msg and "<<<PAGE TEXT" in msg
            return {"is_cited_article": "yes",
                    "authors": [{"name": "Jane Smith", "quote": "By Jane Smith and RAJ PATEL"},
                                {"name": "RAJ PATEL", "quote": "By Jane Smith and RAJ PATEL"}],
                    "no_author_reason": "", "no_author_quote": "", "notes": ""}

    case = {"case_id": "smoke-1", "cited_title": "Council approves new bridge",
            "url": "https://example.org/news/bridge", "snapshot_path": str(snap),
            "snapshot_url": "https://example.org/news/bridge"}
    rec = check.label_case(case, Stub(), "stub", Path("."))
    missing = check.label_case({**case, "case_id": "smoke-2", "snapshot_path": "data/check/smoke/nope.gz"},
                               Stub(), "stub", Path("."))
    out = SMOKE_DIR / "smoke_labels.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (rec, missing)))
    ok &= expect(check.done_case_ids(out) == {"smoke-1", "smoke-2"}, "resume reads case_ids back")
    spec_keys = {"case_id", "is_cited_article", "authors", "no_author_reason", "notes",
                 "model", "checker_version"}
    ok &= expect(spec_keys <= rec.keys(), "output has all spec fields")
    ok &= expect(missing["is_cited_article"] == "unclear" and "missing" in missing["notes"],
                 "missing snapshot labelled unclear with a note")
    print(json.dumps(rec, ensure_ascii=False))

    # Response parsing on a canned Responses API body.
    c = check.Client("sk-test", "stub", 1)
    parsed = c._parse({"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(
            {"is_cited_article": "no", "authors": [], "no_author_reason": "",
             "no_author_quote": "", "notes": "x"})}]}],
        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40},
                  "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 5}}})
    ok &= expect(parsed["is_cited_article"] == "no" and c.usage["cached"] == 40
                 and c.usage["reasoning"] == 5, "Responses API body parsed, usage counted")
    ok &= expect("sk-[redacted]" in check.scrub("bad key sk-abc123XYZ789", "zzz"), "key scrubbed from errors")

    # Full CLI run with the API stubbed: write, then resume skips done cases.
    cases_path = SMOKE_DIR / "smoke_cases.jsonl"
    cases_path.write_text(json.dumps(case) + "\n" + json.dumps({**case, "case_id": "smoke-3"}) + "\n")
    cli_out = SMOKE_DIR / "smoke_cli_labels.jsonl"
    cli_out.unlink(missing_ok=True)
    check.read_api_key = lambda: "sk-test"
    check.Client.label = lambda self, msg: Stub().label(msg)
    argv = ["check.py", "--cases", str(cases_path), "--out", str(cli_out), "--model", "gpt-6-luna"]
    sys.argv = argv + ["--limit", "1"]
    rc1 = check.main()
    sys.argv = argv
    rc2 = check.main()
    rc3 = check.main()
    lines = cli_out.read_text().splitlines()
    ok &= expect(rc1 == rc2 == rc3 == 0 and [json.loads(l)["case_id"] for l in lines] == ["smoke-1", "smoke-3"],
                 "CLI: --limit, append, resume without duplicates")
    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
