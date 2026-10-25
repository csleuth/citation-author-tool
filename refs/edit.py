"""Build exact new wikitext for approved author additions. DRY RUN ONLY in step 2: nothing is posted.

Guards (every one must hold, or the citation is skipped with a reason):
- the original template text occurs in the live page (identical copies are all updated the same way);
- the template still has no author parameter with a value;
- the new template parses to the old parameters, unchanged, plus only the added author parameters
  (empty name slots that don't fit the names being added are removed: they display nothing either way);
- the page outside the replaced spans is byte-identical;
- the page has no {{nobots}} / {{bots|deny=all}} and is not edit-protected.
"""
import difflib, json, re
from urllib.parse import urlsplit
from collections import Counter
from datetime import datetime, timezone
import mwparserfromhell as mw
from .config import DATA, RULES_VERSION, wiki_api
from .db import connect, dumps
from .scan import AUTHOR, COMMENT, NOBOTS, norm_url, parse_citations

PARTICLES = {"de", "van", "von", "da", "del", "der", "la", "le", "bin", "al", "el", "du", "dos", "di", "ter", "den"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


# Semi-protected and extended-confirmed pages are fine (the account is extended-confirmed; until 21 Oct 2026
# extended-confirmed pages were skipped too). Full (admin-only) and template-editor protection always block.
# The account's actual rights are also checked at send time (intestactions).
BLOCKING = {"sysop", "templateeditor"}

# Author-name slots an editor may leave empty in a citation skeleton ('|last3= |first3='). Empty ones that don't
# fit the names being added may be removed. Other author-like keys (host, interviewer, credits, people) are left alone.
NAME_SLOT = re.compile(r"^(author\d*|authors|vauthors|last\d*|first\d*|surname\d*|given\d*|author\d*-last|author-last\d*|"
                       r"author\d*-first|author-first\d*)$")


class Skip(Exception):
    pass


class AlreadyDone(Skip):
    """Someone has added an author to the citation on Wikipedia since it was scanned."""


# ---------- names and style ----------

def split_name(name):
    """'Jane A. Smith' -> ('Smith', 'Jane A.'); 'Ludwig van Beethoven' -> ('van Beethoven', 'Ludwig');
    'Martin Luther King Jr.' -> ('King', 'Martin Luther Jr.') (CS1 puts generational suffixes in |first=)."""
    w = name.split()
    suffix = []
    if len(w) > 2 and w[-1].lower().rstrip(",") in SUFFIXES:
        suffix = [w.pop()]
        w[-1] = w[-1].rstrip(",")
    k = len(w) - 1
    while k > 1 and w[k - 1].lower() in PARTICLES:
        k -= 1
    return " ".join(w[k:]), " ".join(w[:k] + suffix)


def article_style(templates):
    """Name-parameter style used by the article's existing authored citations (CS1 templates only)."""
    c = Counter()
    for t in templates:
        keys = {str(p.name).strip().lower() for p in t.params if str(p.value).strip()}
        if "vauthors" in keys:
            c["vauthors"] += 1
        elif keys & {"last1", "surname1", "author1-last"}:
            c["last1"] += 1
        elif keys & {"last", "surname", "author-last"}:
            c["last"] += 1
        elif keys & {"author", "author1"}:
            c["author"] += 1
    return c


def check_split(name, last, first):
    """A reviewer-chosen (last, first) may only regroup the name's own words: nothing added, dropped or changed."""
    if not last.strip() or sorted(name.split()) != sorted((last + " " + first).split()):
        raise Skip(f"name split {last!r} / {first!r} does not use exactly the words of {name!r}")
    return last.strip(), " ".join(first.split())


def choose_params(names, style, splits=None):
    """CS1 parameter list for the names, following the article's dominant style (WP:CITEVAR).
    splits: optional reviewer-chosen [(last, first), ...] per name, e.g. for family-name-first names ('Choi Won-suk')."""
    if style["vauthors"] > max(style["last"], style["last1"], style["author"]):
        raise Skip("article uses |vauthors= style; needs manual formatting")
    use_author = style["author"] > style["last"] + style["last1"]
    numbered = len(names) > 1 or style["last1"] > style["last"]
    out = []
    for i, n in enumerate(names, 1):
        sfx = str(i) if numbered else ""
        if use_author:
            out.append((f"author{sfx}", n))
        else:
            last, first = check_split(n, *splits[i - 1]) if splits else split_name(n)
            out += [(f"last{sfx}", last), (f"first{sfx}", first)]
    return out


# ---------- template surgery ----------

def _pipe_offset(t, i):
    """Character offset of the '|' that starts parameter i in str(t)."""
    return 2 + len(str(t.name)) + sum(1 + len(str(p)) for p in t.params[:i])


def _ws(s):
    m = re.match(r"^(\s*)(.*?)(\s*)$", s, re.S)
    return m.group(1), m.group(3)


def add_author_params(raw, params):
    """Insert params before the first named parameter, copying that parameter's spacing.
    Returns the new template text. Only inserts; never touches existing parameters except empty name slots:
    slots that exactly match the parameters being added are filled in place; slots that don't fit
    ('|last3= |first4=' for one author) are removed, keeping any that match, and the rest is added as usual."""
    code = mw.parse(raw)
    tops = code.filter_templates(recursive=False)
    if len(tops) != 1 or str(tops[0]) != raw:
        raise Skip("template text did not parse as a single template")
    t = tops[0]
    for p in t.params:
        k, v = str(p.name).strip().lower(), str(p.value).strip()
        if AUTHOR.match(k) and v and not COMMENT.match(v):
            raise Skip(f"template already has |{k}=")
        if AUTHOR.match(k) and v and COMMENT.match(v):
            raise Skip(f"|{k}= holds a comment (e.g. 'not stated'); leave as is")
    blanks = [p for p in t.params if NAME_SLOT.match(str(p.name).strip().lower())]
    wanted = [k for k, _ in params]
    if blanks:
        blank_keys = [str(p.name).strip().lower() for p in blanks]
        if sorted(blank_keys) != sorted(wanted):
            # Keep the first empty slot for each wanted key when all of them are there; remove every other empty slot.
            keep = {k: next(p for p in blanks if str(p.name).strip().lower() == k) for k in wanted} \
                if set(wanted) <= set(blank_keys) else {}
            out = raw
            for p in sorted(blanks, key=lambda p: t.params.index(p), reverse=True):  # from the end: offsets stay valid
                if p not in keep.values():
                    pos = _pipe_offset(t, t.params.index(p))
                    out = out[:pos] + out[pos + 1 + len(str(p)):]
            return add_author_params(out, params)
        vals = dict(params)
        filled = [p for p in t.params if p.showkey and str(p.value).strip()]
        lead = re.match(r"^[ \t]*", str(filled[0].value)).group(0) if filled else ""
        out = raw
        # Fill from the end so earlier offsets stay valid. A blank value is whitespace only:
        # the value goes after the usual leading space (if any), the rest is kept as the separator.
        for p in sorted(blanks, key=lambda p: t.params.index(p), reverse=True):
            i = t.params.index(p)
            start = _pipe_offset(t, i) + 1 + len(str(p.name)) + 1
            b = str(p.value)
            lb = lead if b.startswith(lead) and len(b) > len(lead) else ""
            out = out[:start] + lb + vals[str(p.name).strip().lower()] + b[len(lb):] + out[start + len(b):]
        return out
    named = [i for i, p in enumerate(t.params) if p.showkey]
    if not named:
        raise Skip("template has no named parameters")
    # Authors go first, as in the CS1 documentation and TemplateData order (what VisualEditor writes).
    i = named[0]
    ref = t.params[i]
    pos = _pipe_offset(t, i)
    # Separator: the one between parameters (after the first one's value), not the one after the
    # template name, which often differs ('{{cite web|url=x |title=T}}'); a lone parameter has only the latter.
    sep_src = str(ref.value) if i + 1 < len(t.params) else raw[:pos]
    before_pipe = re.search(r"\s*$", sep_src).group(0)
    if not str(ref.value).strip():  # '| date = \n': the space is the empty value's padding, not part of the line break
        before_pipe = re.sub(r"[ \t]+\n", "\n", before_pipe)
    name_lead, name_trail = _ws(str(ref.name))
    # The value's leading space comes from the first parameter that has a value ('|date= |title=T': from title).
    lead_src = next((p for p in t.params[i:] if p.showkey and str(p.value).strip()), ref)
    val_lead = re.match(r"^\s*", str(lead_src.value)).group(0)
    if "\n" in val_lead:  # value starts on a new line: keep one-line values for the inserted params
        val_lead = " " if val_lead.startswith(" ") else ""
    chunk = "".join(f"|{name_lead}{k}{name_trail}={val_lead}{v}{before_pipe}" for k, v in params)
    return raw[:pos] + chunk + raw[pos:]


def verify_template(old, new, params):
    to, tn = mw.parse(old).filter_templates(recursive=False)[0], mw.parse(new).filter_templates(recursive=False)[0]
    po = {str(p.name).strip(): str(p.value).strip() for p in to.params}
    pn = {str(p.name).strip(): str(p.value).strip() for p in tn.params}
    added = dict(params)
    removed = {k for k, v in po.items() if not v and k not in pn and k not in added and NAME_SLOT.match(k.lower())}
    if str(to.name).strip() != str(tn.name).strip():
        raise Skip("template name changed")
    for k, v in po.items():
        if k in added:
            if v:
                raise Skip(f"|{k}= was not blank")
        elif k not in removed and pn.get(k) != v:
            raise Skip(f"|{k}= changed")
    for k, v in added.items():
        if pn.get(k) != v:
            raise Skip(f"|{k}= not set as intended")
    if set(pn) - set(po) - set(added):
        raise Skip("unexpected extra parameters")
    if len(tn.params) != len(to.params) + len(set(added) - set(po)) - sum(str(p.name).strip() in removed for p in to.params):
        raise Skip("parameter count mismatch (duplicate keys?)")


def missing(text, raw, names=()):
    """Why a scanned citation's exact text is no longer on the page, as the Skip to raise."""
    url = next((c["url"] for c in parse_citations(raw)), "")
    same = [c for c in parse_citations(text) if url and c["url"] and norm_url(c["url"]) == norm_url(url)]
    for c in same:
        t = mw.parse(c["raw"]).filter_templates(recursive=False)[0]
        got = [f"|{str(p.name).strip()}={p.value.strip_code().strip()}" for p in t.params
               if AUTHOR.match(str(p.name).strip().lower()) and p.value.strip_code().strip()]
        if got:
            same_names = names and all(w.lower() in " ".join(got).lower() for n in names for w in n.split())
            return AlreadyDone(f"the citation now credits {'the same author' if same_names else 'an author'} ({' '.join(got)})")
    return Skip("the citation was edited on Wikipedia since the scan" if same else "the citation is no longer on the page")


def apply_to_page(text, items):
    """items: [{citation_id, raw, names}]. Returns (new_text, results). Pure function."""
    cs1 = [t for t in mw.parse(text).filter_templates() if re.sub(r"\s+", " ", str(t.name).strip().lower().replace("_", " "))
           .startswith(("cite ", "citation"))]
    style = article_style(cs1)
    new, results = text, []
    for it in items:
        try:
            n = new.count(it["raw"])
            if n == 0:
                raise missing(text, it["raw"], it["names"])
            params = choose_params(it["names"], style, it.get("split"))
            new_raw = add_author_params(it["raw"], params)
            verify_template(it["raw"], new_raw, params)
            new = new.replace(it["raw"], new_raw)
            results.append(dict(citation_id=it["citation_id"], status="ok", occurrences=n, old=it["raw"], new=new_raw,
                                params=params, style=dict(style), names=it["names"], source=publication(it["raw"])))
        except Skip as e:
            results.append(dict(citation_id=it["citation_id"], status="skip", reason=str(e), old=it["raw"],
                                already=isinstance(e, AlreadyDone)))
    # Whole-page guard: reverting every replacement must give back the original text exactly.
    back = new
    for r in results:
        if r["status"] == "ok":
            back = back.replace(r["new"], r["old"])
    if back != text:
        raise Skip("page text outside the replaced citations would change")
    return new, results


SOURCE_KEYS = ("work", "website", "newspaper", "magazine", "periodical", "journal", "publisher")
SUMMARY_MAX = 300  # characters with the author list; Wikipedia cuts summaries at 500 codepoints
SUMMARY_MAX_CITATIONS = 5


def publication(raw):
    """Plain-text name of the cited publication ('[[BBC]]' -> 'BBC'), else the URL's domain, else ''."""
    t = mw.parse(raw).filter_templates(recursive=False)[0]
    vals = {str(p.name).strip().lower(): p.value for p in t.params}
    for k in SOURCE_KEYS:
        if k in vals:
            name = " ".join(vals[k].strip_code().split())
            if name:
                return name
    url = vals["url"].strip_code().strip() if "url" in vals else ""
    return urlsplit(url).netloc.removeprefix("www.") if "://" in url else ""


def edit_summary(ok):
    """ok: the page's applied results. 'Add author to 1 citation, from the source's byline: Jane Smith (The Guardian)'.
    The author list is left off when it would make the summary long; the opening stays the same."""
    n = len(ok)
    authors = "authors" if sum(len(r["names"]) for r in ok) > 1 else "author"
    head = f"Add {authors} to 1 citation, from the source's byline" if n == 1 else \
        f"Add {authors} to {n} citations, from the sources' bylines"
    detail = "; ".join(", ".join(r["names"]) + (f" ({r['source']})" if r["source"] else "") for r in ok)
    full = f"{head}: {detail}"
    return full if n <= SUMMARY_MAX_CITATIONS and len(full) <= SUMMARY_MAX else head


# ---------- dry run against live pages ----------

def live_pages(pageids):
    out = {}
    for i in range(0, len(pageids), 50):
        r = wiki_api(dict(action="query", prop="revisions|info", rvprop="content|ids", rvslots="main",
                          inprop="protection", pageids="|".join(map(str, pageids[i:i + 50])),
                          format="json", formatversion=2, maxlag=5), post=True)
        for pg in r["query"]["pages"]:
            if "revisions" in pg:
                prot = [p for p in pg.get("protection", []) if p["type"] == "edit"]
                out[pg["pageid"]] = dict(title=pg["title"], revid=pg["revisions"][0]["revid"],
                                         text=pg["revisions"][0]["slots"]["main"]["content"], protection=prot,
                                         ns=pg["ns"], redirect=pg.get("redirect", False))
    return out


def dryrun(sample, run, out_dir=None):
    con = connect()
    rows = con.execute("""SELECT p.citation_id, p.names, c.pageid, c.raw FROM proposals p JOIN citations c ON c.id=p.citation_id
                          JOIN samples s ON s.citation_id=c.id AND s.name=? WHERE p.run=? AND p.tier='HIGH'""", (sample, run)).fetchall()
    by_page = {}
    for r in rows:
        by_page.setdefault(r["pageid"], []).append(dict(citation_id=r["citation_id"], raw=r["raw"], names=json.loads(r["names"])))
    pages = live_pages(sorted(by_page))
    out_dir = out_dir or DATA / "dryrun" / run
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tally, reasons, report, log = Counter(), Counter(), [], []
    for pid, items in by_page.items():
        pg = pages.get(pid)
        if pg is None or pg["ns"] != 0 or pg["redirect"]:
            res = [dict(citation_id=i["citation_id"], status="skip", reason="page missing, moved or not an article") for i in items]
            new = None
        elif NOBOTS.search(pg["text"]):
            res = [dict(citation_id=i["citation_id"], status="skip", reason="{{nobots}} on page") for i in items]
            new = None
        elif {p["level"] for p in pg["protection"]} & BLOCKING:
            res = [dict(citation_id=i["citation_id"], status="skip", reason="fully or template-protected") for i in items]
            new = None
        else:
            try:
                new, res = apply_to_page(pg["text"], items)
            except Skip as e:
                new, res = None, [dict(citation_id=i["citation_id"], status="skip", reason=str(e)) for i in items]
        ok = [r for r in res if r["status"] == "ok"]
        prot = ",".join(sorted({p["level"] for p in pg["protection"]})) if pg else ""
        for r in res:
            r["protection"] = prot
            tally[r["status"]] += 1
            if r["status"] == "skip":
                reasons[r["reason"]] += 1
        if ok and new is not None:
            diff = "".join(difflib.unified_diff(pg["text"].splitlines(True), new.splitlines(True),
                                                f"{pg['title']} @ {pg['revid']}", "dry run", n=0))
            (out_dir / f"{pid}.diff").write_text(f"Edit summary: {edit_summary(ok)}\n" + "".join(
                f"\n- {r['old']}\n+ {r['new']}\n" for r in ok) + "\n" + diff)
        log.append(
                    (pid, pg["revid"] if pg else None, dumps([r["citation_id"] for r in ok]), edit_summary(ok) if ok else "",
                     "dry-run built" if ok else "nothing to do", dumps(res), RULES_VERSION, now))
        report += [dict(pageid=pid, title=pg["title"] if pg else "", **{k: r.get(k) for k in ("citation_id", "status", "reason", "params", "protection", "occurrences")})
                   for r in res]
    # One short write at the end (another run may be holding the database between its commits).
    wcon = connect(timeout=900)
    wcon.executemany("INSERT INTO edits(pageid, base_revid, citation_ids, summary, status, detail, dry_run, rules_version, created_at) "
                     "VALUES(?,?,?,?,?,?,1,?,?)", log)
    wcon.commit()
    (out_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"dry run ({run}): {len(by_page)} articles, {sum(tally.values())} high-tier citations -> "
          f"{tally['ok']} edits built, {tally['skip']} skipped")
    for k, v in reasons.most_common():
        print(f"  skip: {k}: {v}")
    print(f"diffs in {out_dir}")
