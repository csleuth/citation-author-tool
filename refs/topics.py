"""Article topics, for the review app's "hide sports" filter.

It errs towards showing articles. An article counts as sports when a sports WikiProject tags it (the talk-page banners),
no strong non-sports topic does, and, for a person, Wikidata gives them a sport (P641) and no major non-sports
occupation (actor, singer, politician, presenter, businessperson...). So Michael Jordan (Basketball, Biography, Chicago)
is sports; Dwight D. Eisenhower (College football, Politics), Ozzy Osbourne (Professional wrestling, Metal), Ashley
Judd and Dwayne Johnson (actors) are not, and some athletes with acting credits get through. Events, clubs and leagues
need no Wikidata sport (many have none). Biography, places, universities and team projects are neutral.
"""
import json
import re
from datetime import datetime, timezone
from .config import wiki_api

WIKIDATA = "https://www.wikidata.org/w/api.php"
SPORT = re.compile(
    r"\b(football|soccer|basketball|baseball|cricket|tennis|golf|hockey|rugby|olympics?|sports?|athletics|wrestling|"
    r"boxing|motorsport|formula one|nascar|cycling|swimming|martial arts|volleyball|skiing|skating|horse racing|snooker|"
    r"darts|esports|gymnastics|badminton|handball|lacrosse|softball|chess|referees?|gaelic games|bodybuilding|running|"
    r"sailing)\b", re.I)
TOPIC = re.compile(
    r"politic|president|congress|parliament|government|conservatism|liberalism|socialism|international relations|nato|"
    r"military|cold war|television|film|studios|screenwriter|comedy|theatre|music|hip-hop|metal\b|punk|dance|poetry|"
    r"visual arts|literature|novels|books|business|finance|equity|economics|capitalism|media|law\b|crime|royalty|"
    r"civil rights|black lives|computing|internet|youtube|tiktok|big brother|doctor who|james bond|disney|fashion|"
    r"medicine|science|psychology|aviation|christianity|judaism|islam|religion|barack obama|bush family|donald trump|"
    r"kennedy|cities|geography", re.I)
# Wikidata occupations (P106) that keep a person visible: actors, musicians, comedians, film-makers, presenters,
# politicians, business people, writers, models.
OTHER_JOBS = {"Q33999", "Q10800557", "Q10798782", "Q2259451", "Q2405480", "Q177220", "Q639669", "Q2252262", "Q753110",
              "Q488205", "Q36834", "Q245068", "Q2526255", "Q3282637", "Q28389", "Q947873", "Q1930187", "Q82955",
              "Q43845", "Q131524", "Q36180", "Q4610556", "Q3405603"}


def is_sports(t):
    """t: an article_topics row (or dict) with projects (JSON list), human, wd_sport and wd_other_job."""
    projects = json.loads(t["projects"])
    return (any(SPORT.search(p) for p in projects) and not any(TOPIC.search(p) for p in projects)
            and (not t["human"] or bool(t["wd_sport"]) and not t["wd_other_job"]))


def topics(con, pageids):
    """{pageid: article_topics row}; articles missing from the table are looked up and stored first."""
    pageids = list(dict.fromkeys(pageids))
    have = set()
    for i in range(0, len(pageids), 500):
        chunk = pageids[i:i + 500]
        have |= {r[0] for r in con.execute(f"SELECT pageid FROM article_topics WHERE pageid IN ({','.join('?' * len(chunk))})", chunk)}
    need = [p for p in pageids if p not in have]
    for i in range(0, len(need), 50):
        _lookup(con, need[i:i + 50])
    out = {}
    for i in range(0, len(pageids), 500):
        chunk = pageids[i:i + 500]
        out.update((r["pageid"], r) for r in con.execute(
            f"SELECT * FROM article_topics WHERE pageid IN ({','.join('?' * len(chunk))})", chunk))
    return out


def _lookup(con, pageids):
    """WikiProjects and Wikidata item for up to 50 articles, then whether each item is a human, has a sport, and has
    a major non-sports occupation."""
    proj, qid, cont = {p: set() for p in pageids}, {}, {}
    while True:
        r = wiki_api(dict(action="query", format="json", formatversion="2", prop="pageassessments|pageprops",
                          ppprop="wikibase_item", palimit="max", pageids="|".join(map(str, pageids)), **cont), gap=0.5)
        for pg in r["query"]["pages"]:
            proj.setdefault(pg["pageid"], set()).update(pg.get("pageassessments") or {})
            qid[pg["pageid"]] = qid.get(pg["pageid"]) or pg.get("pageprops", {}).get("wikibase_item")
        if "continue" not in r:
            break
        cont = r["continue"]
    claims = {}
    if any(qid.values()):
        r = wiki_api(dict(action="wbgetentities", format="json", props="claims", ids="|".join(q for q in qid.values() if q)),
                     url=WIKIDATA, gap=0.5)
        claims = {q: e.get("claims", {}) for q, e in r.get("entities", {}).items()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for p in proj:
        c = claims.get(qid.get(p) or "", {})
        human = any(x["mainsnak"].get("datavalue", {}).get("value", {}).get("id") == "Q5" for x in c.get("P31", []))
        jobs = {x["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for x in c.get("P106", [])}
        rows.append((p, json.dumps(sorted(proj[p])), qid.get(p), int(human), int("P641" in c), int(bool(jobs & OTHER_JOBS)), now))
    con.executemany("INSERT OR REPLACE INTO article_topics VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
