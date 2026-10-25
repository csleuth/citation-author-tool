"""Seed articles from Pageviews top lists and record their current revision."""
import time
from .config import http_get, wiki_api
from .db import connect, dumps

TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{}"
SKIP = ("Special:", "Wikipedia:", "File:", "Portal:", "Help:", "Template:", "Category:", "User:", "Talk:")


def top_list(period):
    """period: 'YYYY/MM' for a month or 'YYYY/MM/DD' for a day."""
    path = period + "/all-days" if period.count("/") == 1 else period
    r = http_get(TOP.format(path))
    r.raise_for_status()
    return [a for a in r.json()["items"][0]["articles"]
            if a["article"] != "Main_Page" and not a["article"].startswith(SKIP)]


def discover(periods):
    seen = {}
    for p in periods:
        for a in top_list(p):
            seen.setdefault(a["article"].replace("_", " "), {})[p] = a["views"]
    titles = list(seen)
    con = connect()
    n = 0
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        r = wiki_api(dict(action="query", prop="info", titles="|".join(batch), redirects=1,
                          format="json", formatversion=2, maxlag=5), post=True)
        back = {x["to"]: x["from"] for x in r["query"].get("normalized", []) + r["query"].get("redirects", [])}
        for pg in r["query"]["pages"]:
            if pg.get("missing") or pg.get("ns") != 0:
                continue
            src = pg["title"]
            while src in back and src not in seen:
                src = back[src]
            lists = seen.get(src, seen.get(pg["title"], {}))
            views = max([v for k, v in lists.items() if k.count("/") == 1] or [0])
            con.execute("""INSERT INTO articles(pageid, title, views, lists, lastrevid) VALUES(?,?,?,?,?)
                           ON CONFLICT(pageid) DO UPDATE SET title=excluded.title, views=max(views, excluded.views),
                           lists=excluded.lists, lastrevid=excluded.lastrevid""",
                        (pg["pageid"], pg["title"], views, dumps(lists), pg["lastrevid"]))
            n += 1
        con.commit()
        time.sleep(0.3)
    print(f"discover: {len(titles)} titles from {len(periods)} lists -> {n} articles")
