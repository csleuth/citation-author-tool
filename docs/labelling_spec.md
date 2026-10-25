# Labelling spec: who wrote this cited news article?

This spec defines the ground truth for the step 1 gold set. Both the independent checker and the human reviewer label against it. It deliberately says nothing about how the main pipeline works.

## The task

Each case is one Wikipedia citation. You get:
- the title as cited on Wikipedia;
- the cited URL;
- a stored HTML snapshot of the source page (the same bytes the pipeline saw).

Answer two questions from the page itself.

1. **Is this page the cited article?** One of:
   - `yes`: the same story. Small headline edits are fine.
   - `no`: a homepage, section or index page, search page, error or "page not found" page, consent or paywall interstitial without the article, or a different story.
   - `unclear`: for example, a scanned newspaper page with no readable text.
2. **If yes: who is credited as the author of this article?** One of:
   - the named person or people credited as writing it, or
   - `no individual author`, with a short reason: `wire/agency`, `staff/desk`, `organisation`, or `no byline`.

## What counts as an author

**Counts:**
- People credited in the article's own byline: "By Jane Smith", "Jane Smith, Political Reporter", a name in the author slot under the headline, "Words: Jane Smith", "Written by …".
- All co-authors. Give the complete set.
- A byline at the end of the article ("Reporting by Jane Smith in Paris"), if it credits the writer of this story.
- A person credited in the byline even when an agency is also credited ("By Jane Smith and agencies"); list Jane Smith.

**Does not count:**
- Wire services and agencies: AP, Reuters, AFP, PTI, EFE, dpa and so on.
- "Staff", "Staff and agencies", "News Desk", "Web Desk", "Express News Service".
- The outlet's own name ("BBC Sport", "Billboard Staff").
- Editors ("Edited by …"), photographers and image credits, illustrators.
- "Additional reporting by" or "contributed reporting" credits when the article's byline names someone else or an agency.
- People who are only quoted or mentioned.
- Authors of related, recommended or "more from" articles.
- Author bios of other pieces.
- Names that appear only in hidden page data and nowhere visible on the page.
- Live pages (rolling updates): reporters named on individual updates, or editors of the page. Record `no individual author` unless one byline clearly covers the whole page.

**Names:** record each name as written on the page. Note in `notes` if it is ALL CAPS, initials only, or in a non-Latin script; still record it.

## Evidence

For every author, give a short quote of about 3–15 words, copied exactly from the page's visible text, containing the name as credited. For example: "By Jane Smith and John Doe".

For `no individual author`, quote the credit you relied on ("By Associated Press", "Guardian staff and agencies") if there is one; otherwise leave the quote empty.

## Checker interface

- **Input:** `data/check/cases.jsonl`. One JSON object per line:
  `{"case_id": "...", "cited_title": "...", "url": "...", "snapshot_path": "data/html/<sha>.html.gz", "snapshot_url": "<live or web.archive.org URL the bytes came from>"}`
- **Output:** `data/check/checker_labels.jsonl`. One line per case:

```json
{"case_id": "...",
 "is_cited_article": "yes|no|unclear",
 "authors": [{"name": "...", "quote": "..."}],
 "no_author_reason": "wire/agency|staff/desk|organisation|no byline|",
 "notes": "...",
 "model": "...", "checker_version": "..."}
```

When `authors` is non-empty, `no_author_reason` is `""`. When `is_cited_article` is not `yes`, `authors` is empty.
