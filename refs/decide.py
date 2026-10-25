"""Pure tiering rules (v1, v2, v3). Replayable offline from sources + readings.

Tiers: HIGH, REVIEW, LEAVE (no individual author / unusable name), WRONG_PAGE, UNREACHABLE.
"""
import json, re
from collections import Counter
from .config import RULES_VERSION
from .db import connect, dumps
from .read import fold, title_sim

GENERIC = re.compile(
    r"\b(staff|desk|news|newsroom|editor(s|ial)?|team|correspondents?|agenc(y|ies)|agence|agencia|presse|reporters?|admin|"
    r"administrator|bureau|wire|wires|press|online|digital|web|contributors?|guest|sports?|media|multim[eé]dia|service|limited|"
    r"ltd|inc|llc|group|network|channel|magazine|times|post|journal|daily|tribune|herald|reuters|associated|afp|bbc|cnn|"
    r"pti|ians|ani|ap|upi|dpa|efe|tass|xinhua|notimex|unknown|anonymous|various|fifa|uefa|nba|nfl|mlb|wwe|espn|sky)\b", re.I)
PARTICLES = {"de", "van", "von", "da", "del", "der", "la", "le", "bin", "al", "el", "du", "dos", "di", "ter", "den"}


HONORIFIC = re.compile(r"^(mr|mrs|ms|miss|mx|dr|sir|dame|lady|lord|prof|professor|captain|capt|uncle|aunt|the)\.?\s", re.I)
NAME_PREFIX = re.compile(r"^(Mc|Mac|Fitz|De|Di|Da|Du|La|Le|Van|Von)[A-Z]")


def is_handle(n):
    """v2: pseudonyms and handles that are not real personal names (e.g. 'Mr X', 'JohnDoe99', 'the_critic')."""
    toks = n.split()
    if HONORIFIC.match(n) and len(toks) <= 2:
        return True
    if any(re.search(r"[_@#\d]", t) for t in toks):
        return True
    if any(re.search(r"[a-z][A-Z]", t) and not NAME_PREFIX.match(t) for t in toks):
        return True
    return len(toks) >= 2 and len(toks[-1]) == 1  # single-letter surname without a period ('Mr X', 'Agent K')


# v3: a given name that ends like an English word ('Caffeinated Clint') and is (nearly) never a given name in
# Wikipedia's existing citations is likely a pen name. The corpus check keeps names such as Ahmed, Sterling and Ming.
WORDLIKE = re.compile(r"(ed|ing|ous|ful|less|ness|able|ible|ive|ist|ish)$")
GIVEN_KEY = re.compile(r"(first|given|author-first)\d*|author\d+-first")


def known_given_names(con):
    """First-word counts of given names in the corpus's authored citations (|first=..., or the first word of a full |author=)."""
    import mwparserfromhell as mw
    out = Counter()
    for (raw,) in con.execute("SELECT raw FROM citations WHERE has_author=1"):
        tops = mw.parse(raw).filter_templates(recursive=False)
        for p in tops[0].params if tops else []:
            k = str(p.name).strip().lower()
            words = p.value.strip_code().split()
            if GIVEN_KEY.fullmatch(k) and words or re.fullmatch(r"author\d*", k) and len(words) >= 2:
                out[fold(words[0]).strip(".")] += 1
    return out


def wordlike_given(name, given):
    tok = fold(name.split()[0]).strip(".")
    return given.get(tok, 0) < 2 and bool(WORDLIKE.search(tok))


def fix_case(name, sources):
    """v2: if the name matches a source string apart from case, use the source's mixed-case form."""
    for src in sources:
        i = (src or "").lower().find(name.lower())
        if i >= 0:
            form = src[i:i + len(name)]
            if not form.isupper() and not form.islower():
                return form
    return name


def is_initial(tok):
    return len(tok.strip(".")) <= 1


def name_class(n, site_tokens):
    """'full' | 'review_only' (single-initial given names, non-Latin, ALL CAPS) | 'initials' | 'not_person'."""
    n = n.strip()
    toks = n.replace(".", ". ").split()
    if not n or len(n) > 60 or re.search(r"\d|@|/|https?:|www\.|\.com", n):
        return "not_person"
    if not re.search(r"[A-Za-z]", n):
        return "review_only" if len(n) >= 2 else "not_person"  # non-Latin script, e.g. Hangul
    if is_handle(n):
        return "handle"
    if GENERIC.search(n) or fold(n).replace(" ", "") in site_tokens:
        return "not_person"
    if all(is_initial(t) for t in toks):
        return "initials"
    words = n.split()
    if len(words) < 2 or len(words) > 5:
        return "not_person"
    if not all(w[0].isupper() or w.lower() in PARTICLES or w[0] in "'’(" for w in words):
        return "not_person"
    if any(is_initial(t) for t in toks[:-1]) or n.isupper() or HONORIFIC.match(n):
        return "review_only"
    return "full"


def site_tokens(c):
    from urllib.parse import urlsplit
    d = urlsplit(c["url"]).netloc.lower().removeprefix("www.")
    return {d.split(".")[0], fold(re.sub(r"\[\[|\]\]|''", "", c["work"] or "")).replace(" ", "")}


def propose_params(names, numbered):
    """CS1 parameters for display: last/first for clean Latin names, author= otherwise."""
    params = []
    for i, n in enumerate(names, 1):
        sfx = str(i) if numbered or len(names) > 1 else ""
        w = n.split()
        if re.search(r"[A-Za-z]", n) and 2 <= len(w) <= 4 and not n.isupper():
            k = len(w) - 1
            while k > 1 and w[k - 1].lower() in PARTICLES:
                k -= 1
            params += [(f"last{sfx}", " ".join(w[k:])), (f"first{sfx}", " ".join(w[:k]))]
        else:
            params.append((f"author{sfx}", n))
    return params


def _flip(s):
    """'smith, jane' -> 'jane smith' (a single 'Last, First' name in metadata)."""
    p = [x.strip() for x in s.split(",")]
    return f"{p[1]} {p[0]}" if len(p) == 2 and all(len(x.split()) <= 2 for x in p) and p[1] else s


def _usable_surnames(a, st):
    out = []
    for x in a["authors"]:
        if x["role"] == "author" and name_class(x["name"], st) in ("full", "review_only"):
            out.append(fold(x["name"]).split()[-1])
    return sorted(out)


def decide_one(c, src, rd, alt_src=None, alt_rd=None, given=None):
    """Return (tier, reason, names, evidence). alt_*: the other version (archive if the primary is live, or vice versa).
    given: known_given_names() counts (v3 pen-name check; skipped when None)."""
    if src is None or src["via"] == "fail":
        return "UNREACHABLE", "fetch failed", [], {}
    if rd is None:
        return "UNREACHABLE", "no reading", [], {}
    a, m = json.loads(rd["answer"]), json.loads(rd["metadata"])
    sim = title_sim(c["title"], src["headline"])
    ev = dict(via=src["via"], snapshot_url=src["snapshot_url"], headline=src["headline"], title_sim=sim,
              byline_kind=a["byline_kind"], agency=a["agency"], same_article=a["same_article"], notes=a["notes"],
              authors=[{k: x[k] for k in ("name", "role", "visible_quote", "metadata_quote", "vis_ok", "meta_ok")} for x in a["authors"]],
              meta_authors=m["ld_authors"] + m["meta_authors"])
    if a["same_article"] != "yes":
        return "WRONG_PAGE", f"reader: {a['same_article']}", [], ev
    st = site_tokens(c)
    sources = [n for _, n in m["ld_authors"]] + [v for _, v in m["meta_authors"]]
    for x in a["authors"]:
        x["name"] = fix_case(x["name"], sources + [x["visible_quote"], x["metadata_quote"]])
    authors = [x for x in a["authors"] if x["role"] == "author"]
    classes = {x["name"]: name_class(x["name"], st) for x in authors}
    usable = [x for x in authors if classes[x["name"]] in ("full", "review_only")]
    if not usable:
        why = ("pseudonym or handle" if any(v == "handle" for v in classes.values()) else
               "initials only" if any(v == "initials" for v in classes.values()) else a["byline_kind"])
        return "LEAVE", why, [], ev
    names = [x["name"] for x in usable]
    reasons = []
    if any(classes[x["name"]] == "handle" for x in authors):
        return "LEAVE", "pseudonym or handle among the credited names", [], ev
    if len(usable) < len(authors):
        reasons.append("some credited names are not usable person names")
    if any(classes[n] == "review_only" for n in names):
        reasons.append("name form needs review (initials, non-Latin or capitals)")
    if sim is None or sim < 0.5:
        reasons.append(f"headline differs from cited title (similarity {sim})")
    structured = {_flip(fold(n)) for t, n in m["ld_authors"] if t.lower() in ("person", "?")} | {_flip(fold(v)) for k, v in m["meta_authors"]}
    structured_people = {s for s in structured if name_class(s.title(), st) in ("full", "review_only")}
    for x in usable:
        if given is not None and len(x["name"].split()) >= 2 and wordlike_given(x["name"], given):
            reasons.append(f"{x['name']}: given name reads like a word, not a name (possible pen name)")
        if not x["vis_ok"]:
            reasons.append(f"{x['name']}: no verified visible byline quote")
        if not (x["meta_ok"] or any(fold(x["name"]) in s for s in structured)):
            reasons.append(f"{x['name']}: no structured-metadata corroboration")
    extra = structured_people - {fold(n) for n in names}
    if extra and not any(fold(n) in s or s in fold(n) for s in extra for n in names):
        reasons.append("metadata names other people: " + ", ".join(sorted(extra))[:120])
    if not reasons and alt_rd is not None:  # v2: live and archive must agree
        b = json.loads(alt_rd["answer"])
        ev["alt"] = dict(via=alt_src["via"], snapshot_url=alt_src["snapshot_url"], same_article=b["same_article"],
                         authors=[x["name"] for x in b["authors"] if x["role"] == "author"])
        if b["same_article"] != "yes":
            reasons.append(f"{alt_src['via']} version: reader says same article = {b['same_article']}")
        elif _usable_surnames(b, st) != sorted(fold(n).split()[-1] for n in names):
            reasons.append(f"{alt_src['via']} version credits different authors: {', '.join(ev['alt']['authors']) or 'none'}")
    return ("REVIEW" if reasons else "HIGH"), "; ".join(reasons), names, ev


def decide(sample, run):
    con = connect()
    rows = [dict(r) for r in con.execute(
        "SELECT c.* FROM citations c JOIN samples s ON s.citation_id = c.id WHERE s.name = ?", (sample,))]
    given = known_given_names(con)
    tally = Counter()
    for c in rows:
        src = con.execute("SELECT * FROM sources WHERE url_norm=? AND run=?", (c["url_norm"], run)).fetchone()
        rd = con.execute("SELECT * FROM readings WHERE citation_id=? AND run=?", (c["id"], run)).fetchone()
        alt_src = con.execute("SELECT * FROM sources WHERE url_norm=? AND run=? AND via != 'fail'", (c["url_norm"], run + ":alt")).fetchone()
        alt_rd = con.execute("SELECT * FROM readings WHERE citation_id=? AND run=?", (c["id"], run + ":alt")).fetchone()
        tier, reason, names, ev = decide_one(c, src, rd, alt_src, alt_rd, given)
        numbered = con.execute("SELECT count(*) FROM citations WHERE pageid=? AND raw LIKE '%last1%=%'", (c["pageid"],)).fetchone()[0] > \
            con.execute("SELECT count(*) FROM citations WHERE pageid=? AND has_author=1", (c["pageid"],)).fetchone()[0] / 2
        con.execute("INSERT OR REPLACE INTO proposals VALUES(?,?,?,?,?,?,?,?)",
                    (c["id"], run, RULES_VERSION, tier, reason, dumps(names),
                     dumps(propose_params(names, numbered) if names else []), dumps(ev)))
        tally[tier] += 1
    con.commit()
    print(f"decide ({sample}, run {run}, rules {RULES_VERSION}): " + ", ".join(f"{k} {v}" for k, v in tally.most_common()))
