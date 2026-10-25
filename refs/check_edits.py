"""Read-only check of the live edits already sent: were they reverted, and are the added names still there?

No login and no edit: only public API reads (revision tags, current wikitext). Results are appended to
edit_checks, one row per sent citation per run; nothing else in the database is written.
"""
import json
from collections import Counter
from datetime import datetime, timezone
import mwparserfromhell as mw
from .config import wiki_api
from .db import connect, dumps
from .read import fold
from .scan import AUTHOR, fetch_wikitext, norm_url, parse_citations

SENT = ("success-verified", "success-unverified", "success-reconciled", "success-NOT-VERIFIED")
SCHEMA = """CREATE TABLE IF NOT EXISTS edit_checks(
  id INTEGER PRIMARY KEY, edit_id INTEGER, citation_id INTEGER, pageid INTEGER, newrevid INTEGER,
  outcome TEXT, detail TEXT, checked_at TEXT)"""
ORDER = ("kept", "reverted", "changed", "gone", "page missing")


def sent_edits(con):
    """Every live edit that saved, with the names approved for each of its citations."""
    out = []
    for e in con.execute(f"SELECT * FROM edits WHERE dry_run=0 AND status IN ({','.join('?' * len(SENT))}) ORDER BY id", SENT):
        d = json.loads(e["detail"])
        cits = []
        for cid in json.loads(e["citation_ids"]):
            c = con.execute("SELECT url, url_norm FROM citations WHERE id=?", (cid,)).fetchone()
            dec = con.execute("SELECT run, params FROM decisions WHERE citation_id=? AND kind='live' AND label='approve' "
                              "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            names = []
            if dec and dec["params"]:  # a reviewer-chosen split: [[last, first], ...]
                names = [f"{first} {last}".strip() for last, first in json.loads(dec["params"])]
            elif dec:
                p = con.execute("SELECT names FROM proposals WHERE citation_id=? AND run=?", (cid, dec["run"])).fetchone()
                names = json.loads(p["names"]) if p else []
            cits.append(dict(citation_id=cid, url=c["url"] if c else "", url_norm=c["url_norm"] if c else "", names=names))
        title = d.get("title") or (con.execute("SELECT title FROM articles WHERE pageid=?", (e["pageid"],)).fetchone() or [""])[0]
        out.append(dict(edit_id=e["id"], pageid=e["pageid"], title=title, newrevid=d.get("newrevid"), citations=cits))
    return out


def reverted_revids(revids):
    """Revision ids (of the given ones) that Wikipedia has tagged mw-reverted, plus ids it no longer has (deleted)."""
    reverted, missing = set(), set()
    for i in range(0, len(revids), 50):
        batch = revids[i:i + 50]
        r = wiki_api(dict(action="query", prop="revisions", revids="|".join(map(str, batch)),
                          rvprop="ids|tags", format="json", formatversion=2, maxlag=5))
        for pg in r.get("query", {}).get("pages", []):
            for rv in pg.get("revisions", []):
                if "mw-reverted" in rv.get("tags", []):
                    reverted.add(rv["revid"])
        bad = r.get("query", {}).get("badrevids", [])
        missing |= {b["revid"] for b in (bad.values() if isinstance(bad, dict) else bad)}
    return reverted, missing


def author_text(raw):
    """The citation's filled author parameters, folded, as one string."""
    t = mw.parse(raw).filter_templates(recursive=False)[0]
    vals = [p.value.strip_code().strip() for p in t.params if AUTHOR.match(str(p.name).strip().lower())]
    return " ".join(fold(v) for v in vals if v)  # strip_code() has already dropped comments


def citation_state(text, cit):
    """'kept' if a citation with this URL still credits every approved name, 'changed' if it is there without
    them, 'gone' if no citation on the page has this URL. Also returns what the page's citation now says."""
    found = [c for c in parse_citations(text) if c["url"] and norm_url(c["url"]) == cit["url_norm"]]
    if not found:
        return "gone", ""
    wanted = [fold(n).split() for n in cit["names"]]
    now = [author_text(c["raw"]) for c in found]
    for a in now:
        words = set(a.replace(",", " ").split())
        if wanted and all(all(w in words for w in n) for n in wanted):
            return "kept", a
    return "changed", " | ".join(x for x in now if x) or "(no author)"


def check(edits, reverted, missing, texts):
    """Pure: one result per sent citation. texts: {pageid: current wikitext} (absent if the page is gone)."""
    out = []
    for e in edits:
        for cit in e["citations"]:
            if e["pageid"] not in texts:
                outcome, now = "page missing", ""
            else:
                outcome, now = citation_state(texts[e["pageid"]], cit)
            if e["newrevid"] in reverted or e["newrevid"] in missing:
                outcome = "reverted"  # the names may be back since; 'now' says what the page holds today
            out.append(dict(edit_id=e["edit_id"], citation_id=cit["citation_id"], pageid=e["pageid"], title=e["title"],
                            newrevid=e["newrevid"], outcome=outcome, names=cit["names"], url=cit["url"], now=now,
                            revision_deleted=e["newrevid"] in missing))
    return out


def run_check():
    con = connect()
    con.execute(SCHEMA)
    edits = sent_edits(con)
    if not edits:
        print("check-edits: no live edits to check")
        return
    print(f"check-edits: {len(edits)} live edits, {sum(len(e['citations']) for e in edits)} citations (read-only)")
    print("looking up reverts...", flush=True)
    reverted, missing = reverted_revids([e["newrevid"] for e in edits if e["newrevid"]])
    pageids = sorted({e["pageid"] for e in edits})
    texts = {}
    for i in range(0, len(pageids), 50):
        print(f"reading pages {i + 1}-{min(i + 50, len(pageids))} of {len(pageids)}...", flush=True)
        texts.update({pid: wt for pid, (_, wt) in fetch_wikitext(pageids[i:i + 50]).items()})
    print("checking citations (under a minute)...", flush=True)
    results = check(edits, reverted, missing, texts)
    print()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany("INSERT INTO edit_checks(edit_id, citation_id, pageid, newrevid, outcome, detail, checked_at) VALUES(?,?,?,?,?,?,?)",
                    [(r["edit_id"], r["citation_id"], r["pageid"], r["newrevid"], r["outcome"],
                      dumps(dict(names=r["names"], now=r["now"], url=r["url"], revision_deleted=r["revision_deleted"])), now)
                     for r in results])
    con.commit()
    report(results)


def report(results):
    tally = Counter(r["outcome"] for r in results)
    print(" · ".join(f"{tally[k]} {k}" for k in ORDER if tally[k]))
    for kind in ORDER[1:]:
        rows = [r for r in results if r["outcome"] == kind]
        if not rows:
            continue
        print(f"\n{kind.upper()} ({len(rows)})")
        for r in rows:
            print(f"  {r['title']}: {', '.join(r['names'])}")
            if kind != "gone":
                print(f"    now: {r['now'] or '-'}")
            print(f"    https://en.wikipedia.org/w/index.php?diff=prev&oldid={r['newrevid']}")
            if kind in ("reverted", "changed"):
                print(f"    https://en.wikipedia.org/w/index.php?curid={r['pageid']}&action=history")
    if tally["gone"]:
        print("\n'gone' means the citation's URL is no longer on the page, usually ordinary article churn.")
