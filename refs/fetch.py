"""Draw samples and fetch source pages: live with the identifying User-Agent, else Wayback (v1).

No browser User-Agent fallback. A Wayback snapshot is also tried when the live page's headline
does not match the cited title (likely a redirect or a different story).
"""
import gzip, hashlib, random, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dateutil import parser as dateparser
from .config import HTML_DIR, http_get
from .db import connect, dumps
from .read import page_headline, title_sim


def draw_sample(name, n, seed, exclude_file=None):
    """One citation per unique eligible URL, excluding URLs listed in exclude_file (normalised)."""
    con = connect()
    if con.execute("SELECT 1 FROM samples WHERE name=?", (name,)).fetchone():
        raise SystemExit(f"sample {name} already exists")
    excluded = set(open(exclude_file).read().split()) if exclude_file else set()
    by_url = {}
    for r in con.execute("SELECT id, url_norm FROM citations WHERE eligible=1 ORDER BY id"):
        by_url.setdefault(r["url_norm"], []).append(r["id"])
    urls = sorted(u for u in by_url if u not in excluded)
    rng = random.Random(seed)
    pick = rng.sample(urls, n)
    con.executemany("INSERT INTO samples VALUES(?,?)", [(name, rng.choice(by_url[u])) for u in pick])
    con.commit()
    print(f"sample {name}: {n} of {len(urls)} eligible URLs (seed {seed}, {len(excluded)} excluded)")


def _good(r):
    return r is not None and r.status_code == 200 and "html" in r.headers.get("content-type", "") and len(r.content) > 5000


LATIN1 = {"iso-8859-1", "iso8859-1", "iso_8859-1", "iso8859_1", "latin-1", "latin1", "l1", "us-ascii", "ascii", "windows-1252", "cp1252"}


def html_text(content, header_charset=None):
    """Decode a page. requests' r.text assumes ISO-8859-1 when the Content-Type header names no charset, which turns
    UTF-8 curly quotes and accents into 'Ã©'/'â€™' junk. Valid UTF-8 is almost never accidental, so it wins over a
    missing or Latin-1 header; otherwise the header, then the page's <meta> charset, with Latin-1 read as Windows-1252
    (as browsers do). A page declaring UTF-8 with stray Latin-1 bytes keeps UTF-8 unless the stray bytes dominate."""
    cs = (header_charset or "").strip().strip("\"'").lower()
    if cs and cs not in LATIN1:
        try:
            return content.decode(cs, "replace")
        except LookupError:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    m = re.search(rb"""<meta[^>]+charset\s*=\s*["']?([\w.:-]+)""", content[:8192], re.I)
    enc = (m.group(1).decode("ascii", "ignore").lower() if m else "") or cs
    if enc.replace("_", "-") in ("utf-8", "utf8"):
        u = content.decode("utf-8", "replace")
        if sum(ch > "\x7f" and ch != "�" for ch in u) > u.count("�"):
            return u
        enc = ""
    if not enc or enc in LATIN1:
        enc = "cp1252"
    try:
        return content.decode(enc, "replace")
    except LookupError:
        return content.decode("cp1252", "replace")


def _text(r):
    return html_text(r.content, (re.search(r"charset=([^;\s]+)", r.headers.get("content-type", ""), re.I) or [None, None])[1])


def _try(url, gap):
    try:
        return http_get(url, gap=gap, tries=2, timeout=30)
    except Exception:
        return None


RETRY_WAITS = (20, 60)  # v2: 402/403/timeouts are often transient bot blocks; retry twice, spaced out


def _try_live(url):
    r = _try(url, 2.0)
    for wait in RETRY_WAITS:
        if r is not None and r.status_code not in (402, 403):
            break
        time.sleep(wait)
        r = _try(url, 2.0)
    return r


def _wayback_url(url, archive_url, access_date):
    if archive_url:
        a = re.sub(r"\s.*", "", archive_url.strip())
    else:
        params = {"url": url}
        try:
            params["timestamp"] = dateparser.parse(access_date).strftime("%Y%m%d")
        except Exception:
            pass
        r = _try("https://archive.org/wayback/available?" + "&".join(f"{k}={requests_quote(v)}" for k, v in params.items()), 1.5)
        try:
            a = r.json()["archived_snapshots"]["closest"]["url"] if r is not None and r.ok else ""
        except Exception:
            a = ""
    return re.sub(r"(web\.archive\.org/web/\d+)/", r"\1id_/", a, count=1) if "web.archive.org/web/" in a else ""


def requests_quote(v):
    from urllib.parse import quote
    return quote(v, safe="")


def _save(text):
    data = text.encode("utf-8", "replace")
    sha = hashlib.sha256(data).hexdigest()
    path = HTML_DIR / f"{sha}.html.gz"
    if not path.exists():
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as f:
            f.write(data)
    return sha


def fetch_one(c):
    url = re.sub(r"\s.*", "", c["url"])
    attempts, cands = [], []
    live = _try_live(url)
    attempts.append(["live", live.status_code if live is not None else -1, len(live.content) if live is not None else 0])
    if _good(live):
        text = _text(live)
        head = page_headline(text)
        cands.append(("live", title_sim(c["title"], head), live.url, text, head))
    if not cands or (cands[0][1] is not None and cands[0][1] < 0.5):
        wb = _wayback_url(url, c["archive_url"], c["access_date"])
        if wb:
            r = _try(wb, 1.5)
            attempts.append(["wayback", r.status_code if r is not None else -1, len(r.content) if r is not None else 0, wb])
            if _good(r):
                text = _text(r)
                head = page_headline(text)
                cands.append(("wayback", title_sim(c["title"], head), wb, text, head))
        else:
            attempts.append(["wayback", "no snapshot"])
    if not cands:
        return dict(via="fail", status="unreachable", snapshot_url="", html_sha="", headline="", attempts=attempts)
    # Prefer a candidate whose headline matches the cited title; live first. Else the best match.
    ok = [x for x in cands if x[1] is None or x[1] >= 0.5]
    via, _, snap, text, head = ok[0] if ok else max(cands, key=lambda x: x[1] or 0)
    return dict(via=via, status="ok" if ok else "title_mismatch", snapshot_url=snap, html_sha=_save(text),
                headline=head, attempts=attempts)


def fetch(sample, run, workers=8):
    con = connect()
    rows = [dict(r) for r in con.execute("""
        SELECT c.* FROM citations c JOIN samples s ON s.citation_id = c.id
        WHERE s.name = ? AND c.url_norm NOT IN (SELECT url_norm FROM sources WHERE run = ?)""", (sample, run))]
    print(f"fetch: {len(rows)} URLs for run {run}")

    def work(c):
        try:
            return c, fetch_one(c)
        except Exception as e:
            return c, dict(via="fail", status=f"error {type(e).__name__}", snapshot_url="", html_sha="", headline="", attempts=[])

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for c, res in ex.map(work, rows):
            con.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (c["url_norm"], run, c["url"], res["via"], res["status"], res["snapshot_url"], res["html_sha"],
                         res["headline"], dumps(res["attempts"]), datetime.now(timezone.utc).isoformat(timespec="seconds")))
            done += 1
            if done % 50 == 0:
                con.commit()
                print(f"  {done}/{len(rows)}")
    con.commit()
    for r in con.execute("SELECT via, status, count(*) n FROM sources WHERE run=? GROUP BY 1,2", (run,)):
        print(f"  {r['via']:8} {r['status']:15} {r['n']}")


def fetch_alternates(sample, run, workers=8):
    """v2: for HIGH proposals, fetch the other version (archive if the primary was live, live if it was Wayback)
    into sources(run + ':alt'), so decide can require the two to agree."""
    con = connect()
    rows = [dict(r) for r in con.execute("""
        SELECT c.*, s.via AS primary_via FROM proposals p JOIN citations c ON c.id = p.citation_id
        JOIN samples sm ON sm.citation_id = c.id AND sm.name = ? JOIN sources s ON s.url_norm = c.url_norm AND s.run = ?
        WHERE p.run = ? AND p.tier = 'HIGH' AND c.url_norm NOT IN (SELECT url_norm FROM sources WHERE run = ?)""",
        (sample, run, run, run + ":alt"))]
    print(f"fetch alternates: {len(rows)} HIGH cases for run {run}")

    def work(c):
        url = re.sub(r"\s.*", "", c["url"])
        try:
            if c["primary_via"] == "live":
                wb = _wayback_url(url, c["archive_url"], c["access_date"])
                r = _try(wb, 1.5) if wb else None
                via, snap, att = "wayback", wb, ["wayback", r.status_code if r is not None else -1 if wb else "no snapshot"]
            else:
                r = _try_live(url)
                via, snap, att = "live", (r.url if r is not None else url), ["live", r.status_code if r is not None else -1]
        except Exception as e:
            r, via, snap, att = None, "fail", "", ["error", type(e).__name__]
        if not _good(r):
            return c, dict(via="fail", status="unavailable", snapshot_url=snap or "", html_sha="", headline="", attempts=[att])
        text = _text(r)
        return c, dict(via=via, status="ok", snapshot_url=snap, html_sha=_save(text), headline=page_headline(text), attempts=[att])

    with ThreadPoolExecutor(workers) as ex:
        for c, res in ex.map(work, rows):
            con.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (c["url_norm"], run + ":alt", c["url"], res["via"], res["status"], res["snapshot_url"], res["html_sha"],
                         res["headline"], dumps(res["attempts"]), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    for r in con.execute("SELECT via, count(*) n FROM sources WHERE run=? GROUP BY 1", (run + ":alt",)):
        print(f"  {r['via']:8} {r['n']}")
