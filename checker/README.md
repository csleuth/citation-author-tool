# Checker

An independent second reader for the step 1 evaluation. It labels each case
against `docs/labelling_spec.md`: is the snapshot the cited article, and who is
credited as its author. It was written from that spec alone, without looking at
the main pipeline's prompt, rules or outputs, so it gives a blind second opinion.

## Run

From the project root:

```bash
.venv/bin/python checker/check.py --cases data/check/cases.jsonl --out data/check/checker_labels.jsonl
```

- The checker model is **gpt-6.1-sol** by default.
  `--model` overrides it, for example for a comparison run. Every output line
  records the model used.
- `--limit N` labels at most N cases that are not yet in `--out`.
- Other options: `--workers` (default 4), `--timeout` (default 300 s),
  `--root` (base directory for `snapshot_path`, default the current directory),
  and `--price-in` / `--price-cached` / `--price-out` (USD per 1M tokens) for
  the cost estimate.
- The OpenAI key comes only from the `OPENAI_API_KEY` line of
  secrets file (`$REFS_SECRETS_PATH`, default
  `~/.config/citation-author-tool/secrets`), read at run time. It is never printed, logged
  or written, and API error text is scrubbed of anything key-shaped.

The run is resumable. Output is appended one line per case and flushed after
each line. Cases whose `case_id` is already in `--out` are skipped. A case that
fails at the API, after retries on 408/409/429/5xx and network errors, is not
written, so the next run retries it. The script exits 1 if any case failed. At
the end it prints token usage and an estimated cost. The built-in prices for
gpt-6-luna, gpt-6.1-sol and gpt-6-astra come from third-party pricing pages
(Oct 2026); check them against OpenAI.

Offline self-test (no API call; writes under `data/check/smoke/`):

```bash
.venv/bin/python checker/smoke_test.py
```

## What the model sees

Only three things: the cited title, the URL, and the full visible text of the
snapshot. No other extracted metadata is used.

The full visible text is built from the stored HTML (gunzipped if needed) with
BeautifulSoup and lxml. Snapshots are stored as UTF-8, so they are decoded as UTF-8 even when the
page's own `<meta charset>` names another encoding (following it garbled accented letters).
- Removed: `<head>` (including `<title>` and meta tags), `script`, `style`,
  `template`, `svg`, `iframe`, media and form-select elements, comments, and
  elements hidden by the `hidden` attribute, `input type=hidden`, or an inline
  `display:none` / `visibility:hidden` style. JSON-LD and other hidden data
  never reach the model, in line with the spec rule that hidden-only names do
  not count.
- Kept: everything else in `<body>`, including the header, navigation,
  bylines, sidebars, related-article lists, `<noscript>` text and the footer.
  No article-extraction library is used, so bylines are not dropped.
- Layout: block elements (`p`, `div`, `li`, `td`, `h1`–`h6`, `header`,
  `footer` and so on) and `<br>` start new lines. Inline text is joined the way
  a browser renders it. Newlines in the HTML source count as spaces, so
  `<a>RAJ PATEL</a>, Local` becomes `RAJ PATEL, Local`. Whitespace runs collapse
  to one space and empty lines are dropped. A known simplification: line breaks
  inside `<pre>` become spaces too.
- Cap: 150,000 characters. Longer pages keep the first 110,000 and the last
  38,000 characters, with a marker in between saying how much was cut.

## Output

One JSON line per case. It has the spec's fields plus a few additions:

```json
{"case_id": "...", "is_cited_article": "yes|no|unclear",
 "authors": [{"name": "...", "quote": "...", "quote_verified": true}],
 "no_author_reason": "wire/agency|staff/desk|organisation|no byline|",
 "no_author_quote": "...", "no_author_quote_verified": true,
 "notes": "...", "text_chars": 12345, "text_truncated": false,
 "model": "...", "checker_version": "1.0+<sha8 of check.py>"}
```

- `no_author_quote` holds the credit relied on for "no individual author"
  (e.g. "By Associated Press"), which the spec asks for but its output format
  has no field for.
- `checker_version` changes whenever `check.py` changes, which includes the
  prompt, the schema and the text extraction.

## Verification

The model must answer with a strict JSON schema. After it answers, code checks
the result:

- Every author quote must be a substring of the text that was sent, after
  collapsing whitespace. The name must appear in its quote, case-sensitive.
- If a quote matches only when all spaces are ignored (for example, adjacent
  inline elements with no space between them), it still counts as verified,
  and notes say so.
- When a check fails, `quote_verified` is `false` and the reason is added to
  `notes` with a `[verify]` prefix. The author is still kept.
- The spec's field rules are enforced. If the model gives authors when
  `is_cited_article` is not `yes`, they are moved into `notes` with their
  quotes, under a `[consistency]` prefix. A reason given alongside authors is
  cleared and noted. A `yes` with neither authors nor a reason is flagged in
  notes.
- Missing snapshots and pages with no visible text are labelled `unclear`
  without calling the model, with an `[input]` note.

API settings: Responses API, `store: false`, `temperature: 0` where the model
accepts it. gpt-6.1-sol rejects `temperature` (confirmed by a live call on
2026-10-16), so it is never sent for that model, and calls rely on the
model's default sampling. For any other model, a rejected `temperature` makes
the request resend without it, and a note is printed. Strict `json_schema`
output works with gpt-6.1-sol. No `reasoning` parameter is sent.
The prompt is the `INSTRUCTIONS` constant in `check.py`.
