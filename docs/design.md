# Design and accuracy

## The problem

The most-viewed English Wikipedia articles contain tens of thousands of news citations with a URL but no author. Most of them should stay that way: the source is unreachable, the URL no longer shows the cited article, or the piece has no individual byline (wire copy, "staff", an organisation). In a random sample of 200 such citations, about a quarter had a named author that could be identified correctly.

So the hard part is not finding names. It is **not proposing wrong ones**:

- the page at the URL may now be a homepage, a section page or a different story;
- pages carry other people's names (related-article lists, "Edited by", photo credits, people quoted);
- metadata often names the outlet, a desk or an agency as the "author", or disagrees with the visible byline.

## How a citation is judged

1. **Fetch.** Live page with an identifying User-Agent; otherwise the Wayback snapshot nearest the citation's `access-date` (or its `|archive-url=`). No browser User-Agent.
2. **Metadata (deterministic).** JSON-LD/microdata authors and dates (`extruct`) and author meta tags (`author`, `article:author`, `parsely-author`, `dc.creator`, …).
3. **LLM reader** (`refs/read.py`). It gets the citation, the metadata, and two parts of the page's visible text: the start, from the headline on, and the end of the article. It answers in a strict JSON schema, at temperature 0, with `store=false`:
   - is this the cited article (yes / no / unclear)?
   - byline kind: named people, agency or wire, staff or desk, organisation, none;
   - each credited person with a role (author / contributor / other), an **exact quote** from the visible text and, if present, an exact quote from the metadata.

   Code then checks that every quote is a literal substring of what the model was shown and that the name appears inside the quote. Answers are cached by page hash, model and prompt version, so a run can be replayed.
4. **Tier rules** (`refs/decide.py`, pure functions):

   | Tier | Meaning |
   |---|---|
   | HIGH | Same article; every credited author is a usable person name; each has a verified visible quote **and** independent structured-metadata corroboration; headline similar to the cited title; no extra people in the metadata; the live and archive versions agree |
   | REVIEW | A named person, but at least one of those conditions fails |
   | LEAVE | No individual author (wire, staff, organisation, initials only, pseudonym or handle) |
   | WRONG_PAGE | The reader says the page is not the cited article |
   | UNREACHABLE | No usable copy of the page |

5. **Blind checker** (`checker/`). A second model, with its own prompt written only from [labelling_spec.md](labelling_spec.md), labels the same pages without seeing the pipeline's answers. The review app only offers an edit when the checker names **exactly** the same people, full names included.
6. **Human review.** The app shows the page, the quotes, the metadata and the exact diff. Before each edit it re-reads the live article, requires the revision that was reviewed, and checks `{{nobots}}` and protection. Afterwards it reads the saved revision back to verify the edit.

## Results so far

- **Feasibility sample (200 citations):** 34 met the HIGH rule; all 34 were correct on manual checking.
- **Control set (citations that already had authors):** the pipeline proposed authors for 82 fetchable pages. 78 matched Wikipedia exactly and 1 was a superset; 1 exposed an existing Wikipedia error (`|first=Los Angeles |last=Times`); 2 were unresolved conflicts. In none was the pipeline clearly wrong.
- **Live review:** of 400 HIGH proposals that the checker agreed with, the reviewer approved 386. The 14 rejections were mostly cases that couldn't be verified (paywalls, blocked pages) or had already been fixed by another editor; 2 were marked as the wrong byline.

## Why use an LLM at all?

A deterministic version was tested against the same data: metadata person names, the same name filters and title gate, and a check that every name appears in the visible text. Replayed over 3,400 citations:

| | LLM pipeline | Deterministic only |
|---|---|---|
| HIGH proposals | 623 | 673 |
| Exact match with the blind checker (gold set) | 622 / 622 | 591 / 605 |
| Reviewer-approved names it reproduced at HIGH | 386 / 386 | 354 / 386 |

The deterministic version finds about as many authors, but roughly 7% of its HIGH proposals were wrong, compared with well under 1% for the LLM pipeline. Its errors:

- **Organisations and desks taken as people:** "Redacción Clarín", "Contributed Content", "Lloyd's List", "TBS Report".
- **Formatting:** "By Gary Smith", run-together capitalised bylines.
- **Errors that need someone to read the page:** a different article whose title passed the similarity gate; an editor credited as the author; metadata listing more or fewer people than the byline; metadata naming the outlet while the visible byline names a reporter.

Better filters could fix the first two. The last group needs reading comprehension, which is what the LLM is for. It costs about $0.25 per 1,000 citations. The deterministic checks stay in place as a second witness and as hard gates.

Caveat: the gold set and the human labels were selected through the LLM pipeline, which favours it. Names the deterministic version found that the LLM missed were never labelled.

## Wikipedia conventions followed

- Only missing information is added. Existing authors and citation style are never changed (WP:CITEVAR); no cosmetic edits.
- `|last=`/`|first=` (numbered when the article's other citations are) for names that split cleanly; `|author=` otherwise. Never two people in one `|author=`.
- New parameters go before the first named parameter, copying its spacing; existing parameters are untouched.
- Wire, staff and desk credits are left empty.
- A comment such as `|author=<!--Not stated-->` is treated as a deliberate "no byline" and left alone.
