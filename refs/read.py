"""Read a stored source page: structured metadata plus an LLM byline reading with verbatim-quote checks."""
import difflib, gzip, hashlib, html as htmllib, json, re, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import extruct, requests, trafilatura
from bs4 import BeautifulSoup
from .config import HTML_DIR, LLM_CACHE, PROMPT_VERSION, READER_MODEL, http_post, read_secret
from .db import connect, dumps

ARTICLE_TYPES = {"newsarticle", "article", "reportagenews", "analysisnewsarticle", "opinionnewsarticle", "blogposting",
                 "backgroundnewsarticle", "reviewnewsarticle", "liveblogposting", "review", "report", "webpage"}
META_AUTHOR_KEYS = ("author", "article:author", "parsely-author", "sailthru.author", "dc.creator", "dcterms.creator",
                    "byl", "cxenseparse:author", "og:article:author")


# ---------- text helpers ----------

def norm(s):
    return unicodedata.normalize("NFKC", re.sub(r"\s+", " ", s or "")).strip()


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower()) if not unicodedata.combining(c))


def title_sim(cited, headline):
    a = fold(norm(re.sub(r"\[\[|\]\]|''|<.*?>", "", cited or "")))
    b = fold(norm(htmllib.unescape(headline or "")))
    if not a or not b:
        return None
    if a in b or b in a:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b[:len(a) + 40]).ratio(), 2)


def load_html(sha):
    with gzip.open(HTML_DIR / f"{sha}.html.gz", "rb") as f:
        return f.read().decode("utf-8", "replace")


# ---------- structured metadata ----------

def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _types(o):
    t = o.get("@type", "")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _ld_names(o):
    a = o.get("author") or o.get("creator")
    out = []
    for x in a if isinstance(a, list) else [a]:
        if isinstance(x, str):
            out.append(("?", x))
        elif isinstance(x, dict):
            t = x.get("@type")
            t = (t[0] if isinstance(t, list) and t else t) or "?"
            nm = x.get("name")
            if isinstance(nm, list):
                nm = nm[0] if nm else ""
            if not nm and (x.get("givenName") or x.get("familyName")):
                nm = f"{x.get('givenName', '')} {x.get('familyName', '')}"
            if nm:
                out.append((str(t), norm(str(nm))))
    return out


def metadata(html, url=""):
    m = {"ld_authors": [], "meta_authors": [], "ld_pub": "", "ld_mod": "", "meta_pub": "", "headline": ""}
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata", "opengraph"], uniform=True, errors="ignore")
    except Exception:
        data = {}
    for o in _walk(data.get("json-ld", []) + data.get("microdata", [])):
        ts = _types(o)
        if not ts & ARTICLE_TYPES or ("webpage" in ts and "author" not in o):
            continue
        if "author" in o or "creator" in o or "datePublished" in o:
            m["ld_authors"] += _ld_names(o)
            m["ld_pub"] = m["ld_pub"] or str(o.get("datePublished") or "")
            m["ld_mod"] = m["ld_mod"] or str(o.get("dateModified") or "")
            h = o.get("headline")
            m["headline"] = m["headline"] or norm(str(h[0] if isinstance(h, list) and h else h or ""))
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("meta"):
        k = (t.get("name") or t.get("property") or t.get("itemprop") or "").lower()
        v = norm(t.get("content", ""))
        if v and k in META_AUTHOR_KEYS and not v.startswith("http"):
            m["meta_authors"].append((k, v))
        if v and k in ("article:published_time", "parsely-pub-date", "pubdate", "publishdate") and not m["meta_pub"]:
            m["meta_pub"] = v
        if v and k == "og:title" and not m["headline"]:
            m["headline"] = v
    if not m["headline"] and soup.title:
        m["headline"] = norm(soup.title.get_text())
    m["headline"] = htmllib.unescape(m["headline"])
    return m


def page_headline(html):
    return metadata(html)["headline"]


def meta_block(m):
    lines = [f"jsonld.author[{t}]: {n}" for t, n in m["ld_authors"]] + [f"meta[{k}]: {v}" for k, v in m["meta_authors"]]
    lines += [f"{k}: {m[k]}" for k in ("ld_pub", "ld_mod", "meta_pub", "headline") if m.get(k)]
    return "\n".join(lines) or "(none)"


# ---------- visible text: article head plus article tail ----------

def visible_text(html):
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form"]):
        t.decompose()
    for t in soup.find_all(attrs={"role": ["navigation", "contentinfo", "complementary"]}):
        t.decompose()
    t = re.sub(r"[ \t\xa0]+", " ", soup.get_text("\n"))
    return unicodedata.normalize("NFKC", re.sub(r"\s*\n\s*", "\n", t)).strip()


def article_head(text, cited_title, headline, size=4000):
    low = text.lower()
    pos = -1
    for probe in (headline, cited_title):
        p = (probe or "").lower()[:40]
        if len(p) > 12:
            hits = [m.start() for m in re.finditer(re.escape(p), low)]
            if hits:
                pos = hits[1] if len(hits) > 1 else hits[0]
                break
    start = max(0, pos - 800) if pos >= 0 else 0
    return text[start:start + size]


def article_tail(html, size=1500):
    main = trafilatura.extract(html, include_comments=False, favor_precision=True) or ""
    return unicodedata.normalize("NFKC", main[-size:]) if main else ""


# ---------- LLM reader ----------

SYSTEM = """You verify bibliographic details of a cited web page for a Wikipedia citation.
You receive: the existing citation, METADATA extracted from the page's HTML (JSON-LD/meta tags), the START of the page's visible text and the END of the article's main text.
Rules:
- Use ONLY the given material. Never guess or use outside knowledge. If unsure, say so.
- same_article: does the page appear to be the article the citation describes (same story; headline may have been edited)? Answer "no" for homepages, section/index pages, error pages, paywall/consent pages without the article, or a different story.
- authors: only the people credited as writing THIS article (its byline). Exclude names from related-article lists, recommendations, photo credits, editors ("edited by"), people merely quoted or mentioned, and author bios of other articles. "Additional reporting by" contributors: role contributor.
- For each author give visible_quote: an exact, character-for-character substring of the VISIBLE TEXT sections (about 3-12 words) containing the name as credited (e.g. "By Jane Smith and John Doe"), or "" if not visible; and metadata_quote: an exact substring of METADATA containing the name, or "".
- Wire services, news agencies, "Staff", desks, or the outlet's own name are NOT people: report them via byline_kind and agency, not authors.
- date_published: original publication date of this article (YYYY-MM-DD), not the updated/modified date, not today's date, not a date in a related link. Give date_quote as an exact substring of VISIBLE TEXT or METADATA. If only an updated date is shown, use date_kind "updated_only"."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["same_article", "same_article_reason", "byline_kind", "authors", "agency", "date_published",
                       "date_quote", "date_kind", "confidence", "notes"],
          "properties": {
              "same_article": {"type": "string", "enum": ["yes", "no", "unclear"]},
              "same_article_reason": {"type": "string"},
              "byline_kind": {"type": "string", "enum": ["named_people", "agency_or_wire", "staff_or_desk", "organization", "none_found"]},
              "authors": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                          "required": ["name", "role", "visible_quote", "metadata_quote"],
                          "properties": {"name": {"type": "string"}, "role": {"type": "string", "enum": ["author", "contributor", "other"]},
                                         "visible_quote": {"type": "string"}, "metadata_quote": {"type": "string"}}}},
              "agency": {"type": "string"}, "date_published": {"type": "string"}, "date_quote": {"type": "string"},
              "date_kind": {"type": "string", "enum": ["published", "updated_only", "none"]},
              "confidence": {"type": "string", "enum": ["high", "medium", "low"]}, "notes": {"type": "string"}}}

_key = None
FRESH = False  # True: ignore cached answers (the 24-hour stability rerun must re-ask the model)


def _ask(user):
    global _key
    h = hashlib.sha256((READER_MODEL + PROMPT_VERSION + SYSTEM + user).encode()).hexdigest()[:24]
    cp = LLM_CACHE / f"{h}.json"
    if cp.exists() and not FRESH:
        return json.loads(cp.read_text())
    _key = _key or read_secret("OPENAI_API_KEY")
    body = dict(model=READER_MODEL, input=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                reasoning={"effort": "none"}, temperature=0, store=False,
                text={"format": {"type": "json_schema", "name": "byline", "strict": True, "schema": SCHEMA}})
    for i in range(4):  # rate limits, server errors and network errors wait 1, 2, 4 s (or what Retry-After asks, up to a minute)
        try:
            r = http_post("https://api.openai.com/v1/responses", gap=0.2, headers={"Authorization": "Bearer " + _key}, json=body, timeout=120)
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(2 ** i)
            continue
        if r.status_code not in (429, 500, 502, 503) or i == 3:
            break
        ra = r.headers.get("retry-after", "")
        time.sleep(min(float(ra), 60) if ra.replace(".", "", 1).isdigit() else 2 ** i)
    if not r.ok:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:200]}")
    j = r.json()
    txt = "".join(c.get("text", "") for o in j.get("output", []) if o.get("type") == "message" for c in o.get("content", []))
    res = {"answer": json.loads(txt), "usage": j.get("usage", {})}
    LLM_CACHE.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(res, ensure_ascii=False))
    return res


def ws(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def verbatim(quote, text):
    return bool(quote) and ws(quote) in ws(text)


def read_one(c, src):
    html = load_html(src["html_sha"])
    m = metadata(html, src["snapshot_url"])
    head = article_head(visible_text(html), c["title"], m["headline"])
    tail = article_tail(html)
    mb = meta_block(m)
    user = (f"CITATION:\n{c['raw'][:1500]}\n\nPAGE URL: {src['snapshot_url']}\n\nMETADATA:\n{mb}\n\n"
            f"VISIBLE TEXT - PAGE START (excerpt):\n{head}\n\nVISIBLE TEXT - ARTICLE END (excerpt):\n{tail or '(not available)'}")
    res = _ask(user)
    a = res["answer"]
    visible = head + "\n" + tail
    for au in a["authors"]:
        au["vis_ok"] = verbatim(au["visible_quote"], visible) and fold(au["name"]) in fold(au["visible_quote"])
        au["meta_ok"] = verbatim(au["metadata_quote"], mb) and fold(au["name"]) in fold(au["metadata_quote"])
    a["date_ok"] = verbatim(a["date_quote"], visible + "\n" + mb)
    return a, m, res.get("usage", {})


def read(sample, run, workers=6, fresh=False):
    global FRESH
    FRESH = fresh
    con = connect()
    rows = [dict(r) for r in con.execute("""
        SELECT c.*, s.via, s.snapshot_url, s.html_sha FROM citations c
        JOIN samples sm ON sm.citation_id = c.id JOIN sources s ON s.url_norm = c.url_norm AND s.run = ?
        WHERE sm.name = ? AND s.via != 'fail'
          AND c.id NOT IN (SELECT citation_id FROM readings WHERE run = ?)""", (run, sample, run))]
    print(f"read: {len(rows)} pages for run {run}")

    def work(r):
        try:
            return r, read_one(r, r), None
        except Exception as e:
            return r, None, f"{type(e).__name__}: {str(e)[:150]}"

    tin = tout = errs = 0
    with ThreadPoolExecutor(workers) as ex:
        for r, out, err in ex.map(work, rows):
            if err:
                errs += 1
                print("  error", r["id"], err)
                continue
            a, m, u = out
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
            con.execute("INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?,?,?,?)",
                        (r["id"], run, r["html_sha"], READER_MODEL, PROMPT_VERSION, dumps(a), dumps(m), dumps(u),
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
            con.commit()
    print(f"read: done, {errs} errors, new tokens in/out {tin}/{tout} (~${tin / 1e6 * 0.10 + tout / 1e6 * 0.50:.3f}; cached reads cost nothing)")
