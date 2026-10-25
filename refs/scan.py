"""Parse citation templates from article wikitext and mark which ones are eligible (v1 scope)."""
import hashlib, re, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit
import mwparserfromhell as mw
from .config import wiki_api
from .db import connect

CITE = {"cite news", "cite web", "cite magazine", "cite newspaper", "cite website", "web cite", "cite periodical"}
ALIAS = {"cite newspaper": "cite news", "cite website": "cite web", "web cite": "cite web", "cite periodical": "cite magazine"}
AUTHOR = re.compile(r"^(author\d*|authors|last\d*|first\d*|surname\d*|given\d*|author\d*-last|author-last\d*|author\d*-first|"
                    r"author-first\d*|vauthors|people|host\d*|credits|interviewer\d*)$")
COMMENT = re.compile(r"^<!--.*?-->$", re.S)
NOBOTS = re.compile(r"\{\{\s*(nobots|bots\s*\|\s*deny\s*=\s*all)\s*\}\}", re.I)


def norm_url(url):
    u = re.sub(r"\s.*", "", url.strip())
    p = urlsplit(u)
    host = p.netloc.lower().removeprefix("www.")
    return host + p.path.rstrip("/") + ("?" + p.query if p.query else "")


def parse_citations(wikitext):
    for t in mw.parse(wikitext).filter_templates():
        name = re.sub(r"\s+", " ", str(t.name).strip().lower().replace("_", " "))
        if name not in CITE:
            continue
        params = {str(p.name).strip().lower(): str(p.value).strip() for p in t.params}
        filled = {k: v for k, v in params.items() if v and not COMMENT.match(v)}
        url = re.sub(r"\s.*", "", filled.get("url", ""))
        yield dict(
            tmpl=ALIAS.get(name, name), raw=str(t), url=url,
            domain=urlsplit(url).netloc.lower().removeprefix("www.") if url.startswith("http") else "",
            title=filled.get("title", ""),
            work=next((filled[k] for k in ("work", "newspaper", "website", "magazine", "publisher") if k in filled), ""),
            date=filled.get("date", ""), access_date=filled.get("access-date", filled.get("accessdate", "")),
            archive_url=filled.get("archive-url", filled.get("archiveurl", "")),
            has_author=any(AUTHOR.match(k) for k in filled),
            author_comment=any(AUTHOR.match(k) and COMMENT.match(v) for k, v in params.items() if v))


def fetch_wikitext(pageids):
    out = {}
    params = dict(action="query", prop="revisions", rvprop="content|ids", rvslots="main",
                  pageids="|".join(map(str, pageids)), format="json", formatversion=2, maxlag=5)
    cont = {}
    while True:
        r = wiki_api({**params, **cont}, post=True)
        for pg in r.get("query", {}).get("pages", []):
            if "revisions" in pg:
                rv = pg["revisions"][0]
                out[pg["pageid"]] = (rv["revid"], rv["slots"]["main"]["content"])
        if "continue" not in r:
            return out
        cont = r["continue"]


def scan():
    con = connect()
    todo = [r["pageid"] for r in con.execute(
        "SELECT pageid FROM articles WHERE scanned_revid IS NULL OR scanned_revid != lastrevid")]
    print(f"scan: {len(todo)} articles to (re)scan")
    for i in range(0, len(todo), 50):
        for pageid, (revid, wt) in fetch_wikitext(todo[i:i + 50]).items():
            nobots = bool(NOBOTS.search(wt))
            for c in parse_citations(wt):
                h = hashlib.sha1(c["raw"].encode()).hexdigest()
                con.execute("""INSERT INTO citations(pageid, revid, tmpl, template_hash, raw, url, url_norm, domain, title, work,
                               date, access_date, archive_url, has_author, author_comment)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(pageid, template_hash) DO UPDATE SET revid=excluded.revid""",
                            (pageid, revid, c["tmpl"], h, c["raw"], c["url"], norm_url(c["url"]) if c["url"] else "",
                             c["domain"], c["title"], c["work"], c["date"], c["access_date"], c["archive_url"],
                             int(c["has_author"]), int(c["author_comment"])))
            con.execute("UPDATE articles SET scanned_revid=?, scanned_at=?, nobots=? WHERE pageid=?",
                        (revid, datetime.now(timezone.utc).isoformat(timespec="seconds"), int(nobots), pageid))
        con.commit()
        time.sleep(0.5)
    mark_eligible(con)


def mark_eligible(con):
    """v1 scope: cite news / cite magazine anywhere; cite web only on news-like domains.
    A domain is news-like if >= 30% of its (>= 5) citations use cite news or cite magazine."""
    use = defaultdict(Counter)
    for r in con.execute("SELECT domain, tmpl FROM citations WHERE domain != ''"):
        use[r["domain"]][r["tmpl"]] += 1
    newsy = {d for d, c in use.items()
             if sum(c.values()) >= 5 and (c["cite news"] + c["cite magazine"]) / sum(c.values()) >= 0.3}
    con.execute("UPDATE citations SET eligible = 0")
    rows = con.execute("""SELECT c.id, c.tmpl, c.domain FROM citations c JOIN articles a USING(pageid)
                          WHERE a.nobots = 0 AND c.has_author = 0 AND c.author_comment = 0 AND c.url LIKE 'http%'""").fetchall()
    ids = [r["id"] for r in rows if r["tmpl"] != "cite web" or r["domain"] in newsy]
    con.executemany("UPDATE citations SET eligible = 1 WHERE id = ?", [(i,) for i in ids])
    con.commit()
    n_url = con.execute("SELECT count(DISTINCT url_norm) FROM citations WHERE eligible = 1").fetchone()[0]
    print(f"eligible: {len(ids)} citations, {n_url} unique URLs, {len(newsy)} news-like domains")
