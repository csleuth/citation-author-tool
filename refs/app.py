"""Review app: one decision per citation, a send tray with one edit per article, guarded sending.
DRY RUN unless started with --live.

    .venv/bin/python -m refs app            # dry run (default): builds and checks edits, never posts
    .venv/bin/python -m refs app --live     # live: posts approved edits (no limit unless --max-live N is given)

Decisions are logged as kind='live' in live mode and kind='dryrun' in a dry run, so practising in a dry
run never removes citations from the live queue.
"""
import difflib, functools, itertools, json, random, re, threading, time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import mwparserfromhell as mw
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from .config import DATA, RULES_VERSION, code_version, wiki_api
from .db import connect
from .edit import BLOCKING, Skip
from .label import clean_title
from .read import fold, load_html, norm, title_sim
from . import topics, wiki

# The batch under review: its run and its checked gold set (<sample>-gold, from export-cases). Set by run() from the command line.
QUEUE_RUN = "r1"
GOLD = "sample-gold"
REASONS = {"1": "not the author / wrong byline", "2": "wire, staff or organisation", "3": "wrong page or article",
           "4": "name formatting", "5": "other", "6": "couldn't verify (paywall or blocked page)"}
RETURNABLE = {"queued", "refused", "failed"}  # tray statuses whose citations can go back to the queue (nothing was saved)
ROW_TTL = 600  # seconds a built preview row is reused before the live page is read again
META_KEY = re.compile(r"^(jsonld\.[\w.\[\]]+|meta\[[\w:.-]+\]):\s*")  # 'jsonld.author[Person]: Jane Smith' -> 'Jane Smith'


def checker_names():
    """The blind checker's authors by citation id ([] when it found no individual author or a different page)."""
    out = {}
    path = DATA / "check" / "checker_labels.jsonl"
    if not path.exists():  # no checker run yet: nothing can be sent live
        return out
    for line in open(path):
        x = json.loads(line)
        out[int(x["case_id"].rsplit("-", 1)[1])] = [a["name"] for a in x["authors"]] if x["is_cited_article"] == "yes" else []
    return out


def full_names(names):
    """Names for comparison in full. Case, accents, punctuation and spacing are ignored ('Jane A. Smith' == 'jane a smith'),
    but every word counts, so 'Jane Smith' and 'John Smith' (or 'J. Smith') differ."""
    return sorted(" ".join(fold(re.sub(r"[^\w\s'’-]", " ", n)).split()) for n in names if n.strip())


def checker_agrees(names, checked):
    """Live edits need the checker to credit exactly the same people, full names included. checked: None if unchecked."""
    return bool(names) and checked is not None and full_names(names) == full_names(checked)


def _articles(rows):
    """Proposal rows -> {pageid: article with its citations as review items}, keeping the rows' order."""
    arts = {}
    for r in rows:
        arts.setdefault(r["pageid"], dict(pageid=r["pageid"], title=r["article"], views=r["views"], items=[]))["items"].append(
            dict(citation_id=r["citation_id"], raw=r["raw"], names=json.loads(r["names"]), url=r["url"], cited=clean_title(r["title"]),
                 evidence=json.loads(r["evidence"]), html_sha=r["html_sha"]))
    return arts


def _proposals(con):
    return con.execute("""SELECT p.citation_id, p.names, p.evidence, c.pageid, c.raw, c.url, c.title, a.title AS article, a.views,
        src.html_sha
        FROM proposals p JOIN citations c ON c.id=p.citation_id JOIN articles a ON a.pageid=c.pageid
        JOIN samples s ON s.citation_id=c.id AND s.name=?
        LEFT JOIN sources src ON src.url_norm=c.url_norm AND src.run=p.run
        WHERE p.run=? AND p.tier='HIGH' AND p.rules_version=? ORDER BY a.views DESC, c.id""", (GOLD, QUEUE_RUN, RULES_VERSION)).fetchall()


def eligible_ids(con=None):
    """Citations that qualify for live edits whether or not they have been decided (for overall progress)."""
    ck = checker_names()
    return {r["citation_id"] for r in _proposals(con or connect()) if checker_agrees(json.loads(r["names"]), ck.get(r["citation_id"]))}


def sports_pageids(con=None):
    """Articles in the batch under review that count as sports (refs/topics.py); missing topics are looked up first."""
    con = con or connect()
    t = topics.topics(con, [r["pageid"] for r in _proposals(con)])
    return {p for p, row in t.items() if topics.is_sports(row)}


def load_queue():
    """Eligible for live edits: HIGH (current rules) in QUEUE_RUN, checker agrees on the full names, not yet decided or sent. Grouped by article."""
    con = connect()
    ck = checker_names()
    done = {r[0] for r in con.execute("SELECT citation_id FROM decisions WHERE kind='live'")}
    arts = _articles(r for r in _proposals(con) if r["citation_id"] not in done
                     and checker_agrees(json.loads(r["names"]), ck.get(r["citation_id"])))
    blocked = _blocked_pages(list(arts))
    return [a for a in arts.values() if a["pageid"] not in blocked]


SAVED = ("success-verified", "success-reconciled", "success-unverified", "success-NOT-VERIFIED", "nochange")


def unsent_approvals(con=None):
    """Live approvals that no live edit has saved, for rebuilding the send tray after a restart or crash.
    Returns (articles, {citation_id: decision_id}, held): held lists (citation_id, why) for approvals not put back in the tray."""
    con = con or connect()
    status = {}
    for r in con.execute("SELECT citation_ids, status FROM edits WHERE dry_run=0 ORDER BY id"):
        for cid in json.loads(r["citation_ids"] or "[]"):
            status.setdefault(cid, set()).add(r["status"])
    approved, splits = {}, {}
    for r in con.execute("SELECT * FROM decisions WHERE kind='live' AND label='approve' ORDER BY id"):
        approved[r["citation_id"]] = r["id"]
        if r["params"]:
            splits[r["citation_id"]] = json.loads(r["params"])
    held, want = [], {}
    for cid, did in approved.items():
        st = status.get(cid, set())
        if st & set(SAVED):
            continue
        if "attempt" in st:  # the app stopped mid-send: the edit may or may not have been saved
            held.append((cid, "a send was interrupted, so it may already be on the page; check the article"))
        else:
            want[cid] = did
    ck = checker_names()
    rows = []
    for r in _proposals(con):
        if r["citation_id"] in want:
            if checker_agrees(json.loads(r["names"]), ck.get(r["citation_id"])):
                rows.append(r)
            else:
                held.append((r["citation_id"], "the checker's names no longer match in full"))
    kept = {r["citation_id"] for r in rows}
    held += [(cid, "no longer a current high-tier proposal") for cid in want if cid not in kept and cid not in dict(held)]
    arts = list(_articles(rows).values())
    for a in arts:
        for it in a["items"]:
            if it["citation_id"] in splits:
                it["split"] = splits[it["citation_id"]]  # the name split chosen when it was approved
    return arts, {cid: did for cid, did in want.items() if cid in kept}, held


def _blocked_pages(pageids):
    """Pages with full (admin-only) or template-editor protection (skipped). Re-checked again at send time."""
    out = set()
    for i in range(0, len(pageids), 50):
        r = wiki_api(dict(action="query", prop="info", inprop="protection", pageids="|".join(map(str, pageids[i:i + 50])),
                          format="json", formatversion=2), post=True)
        for pg in r["query"]["pages"]:
            if {p["level"] for p in pg.get("protection", []) if p["type"] == "edit"} & BLOCKING:
                out.add(pg["pageid"])
    return out


def param_diff(old, new):
    """[(kind, text)], kind '' (unchanged), 'del' or 'ins', compared parameter by parameter: shows removed
    empty slots ('|last3= ') as well as the added names."""
    a, b = re.split(r"(?=\|)", old), re.split(r"(?=\|)", new)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(("", "".join(a[i1:i2])))
            continue
        if i2 > i1:
            out.append(("del", "".join(a[i1:i2])))
        if j2 > j1:
            out.append(("ins", "".join(b[j1:j2])))
    return out


def removed_params(old, new):
    """Parameter names in the old citation that the new one no longer has (only ever empty name slots; edit.py checks)."""
    def keys(raw):
        return [str(p.name).strip() for p in mw.parse(raw).filter_templates(recursive=False)[0].params]
    kept = set(keys(new))
    return [k for k in keys(old) if k not in kept]


# Romanised family names that often come first in Korean, Chinese, Singaporean, Malaysian and Vietnamese bylines.
FAMILY_FIRST = set("""kim lee yi park choi jung jeong kang cho jo yoon yun jang lim im han oh seo shin kwon hwang ahn song jeon hong yoo
ko moon yang son bae baek heo nam noh ha kwak sung cha joo woo min ryu yeo wang li zhang liu chen yang huang zhao wu zhou xu sun ma
zhu hu guo he lin gao luo zheng liang xie tang feng deng cao peng zeng xiao tian dong pan yuan cai jiang du ye cheng wei su lu ding
ren shen yao jin fan fang shi tan liao zou xiong qiu qin jia hou meng long wan duan lei qian yin hao kong bai cui mao chan cheung
wong leung ng lam lau tsang yeung teo ong goh chua koh yeo tay chong seah quek tong foo chia low loh nguyen tran le pham hoang huynh
phan vu vo dang bui ngo duong ly""".split())


def family_first_risk(names, params):
    """True when last/first parameters are proposed and a name may be written family name first ('Choi Won-suk')."""
    if not any(k.startswith("last") for k, _ in params):
        return False
    lasts = [v for k, v in params if k.startswith("last")]
    return any(n.split()[0].lower().strip(".,") in FAMILY_FIRST for n in names if len(n.split()) >= 2) or \
        any(re.fullmatch(r"[A-Z][a-z]+-[a-z]+", v) for v in lasts)  # 'Won-suk' as a surname: a Korean given name


CITE_WORK = ("work", "website", "newspaper", "magazine", "periodical", "publisher")


def cite_preview(new):
    """The new citation roughly as the reference list shows it; 'authors' is the part being added."""
    t = mw.parse(new).filter_templates(recursive=False)[0]
    v = {str(p.name).strip().lower(): " ".join(p.value.strip_code().split()) for p in t.params}
    v = {k: x for k, x in v.items() if x}
    people = []
    for i in ["", *map(str, range(1, 10))]:
        if v.get(f"last{i}"):
            people.append(v[f"last{i}"] + (", " + v[f"first{i}"] if v.get(f"first{i}") else ""))
        elif v.get(f"author{i}"):
            people.append(v[f"author{i}"])
    return dict(authors="; ".join(people), date=v.get("date", ""), title=v.get("title", ""), url=v.get("url", ""),
                work=next((v[k] for k in CITE_WORK if k in v), ""), retrieved=v.get("access-date") or v.get("accessdate", ""))


def page_card(pageid):
    """Lead-image thumbnail and short description for the article header ({} if Wikipedia doesn't answer)."""
    try:
        r = wiki_api(dict(action="query", prop="pageimages|description", pageids=pageid, piprop="thumbnail", pithumbsize=160,
                          format="json", formatversion=2), gap=0, tries=1, timeout=10)  # cosmetic: one quick try
        pg = r["query"]["pages"][0]
        return dict(thumb=pg.get("thumbnail", {}).get("source", ""), description=pg.get("description", ""))
    except Exception:
        return {}


def _unique(xs):
    return list(dict.fromkeys(x for x in xs if x))


@functools.lru_cache(maxsize=512)
def page_headlines(sha):
    """Headlines the stored page gives besides the JSON-LD one: visible <h1>s, og:/twitter:title, then <title>."""
    try:
        soup = BeautifulSoup(load_html(sha), "lxml")
    except Exception:
        return ()
    out = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    out += [m.get("content") or "" for m in soup.find_all("meta")
            if (m.get("property") or m.get("name") or "").lower() in ("og:title", "twitter:title")]
    out += [soup.title.get_text(" ", strip=True)] if soup.title else []
    return tuple(_unique(norm(x) for x in out))


def best_headline(cited, headline, sim, sha):
    """Display only (the tier rules keep the pipeline's headline): the page headline closest to the cited title,
    so a hidden SEO headline doesn't flag 'headline differs' when the visible one matches."""
    if not sha or (sim is not None and sim >= 0.9):
        return headline, sim
    for h in page_headlines(sha):
        s = title_sim(cited, h)
        if s is not None and (sim is None or s > sim):
            headline, sim = h, s
    return headline, sim


def evidence_row(it, res):
    """Everything the reviewer sees for one citation. res: this citation's result from build_plan ({} if not built)."""
    ev = it["evidence"]
    authors = [x for x in ev.get("authors", []) if x["role"] == "author"]
    alt = ev.get("alt")
    ok = res.get("status") == "ok"
    headline, sim = best_headline(it["cited"], ev.get("headline") or "", ev.get("title_sim"), it.get("html_sha"))
    return dict(
        citation_id=it["citation_id"], names=it["names"], url=it["url"], cited=it["cited"],
        domain=urlsplit(it["url"] or "").netloc.removeprefix("www."),
        headline=headline, title_sim=sim,
        snapshot=ev.get("snapshot_url") or it["url"], via=ev.get("via", ""),
        quotes=_unique(x["visible_quote"] for x in authors),
        meta=_unique([META_KEY.sub("", x["metadata_quote"] or "") for x in authors] or [n for _, n in ev.get("meta_authors", [])]),
        alt=dict(via=alt["via"], authors=alt["authors"]) if alt else None,
        others=[dict(name=x["name"], role=x["role"]) for x in ev.get("authors", []) if x["role"] != "author"],
        agency=ev.get("agency") or "", notes=ev.get("notes") or "",
        params=res.get("params") or [], built=bool(res), split=it.get("split"),
        family_first=family_first_risk(it["names"], res.get("params") or []),
        diff=param_diff(res["old"], res["new"]) if ok else None,
        removed=removed_params(res["old"], res["new"]) if ok else [],
        preview=cite_preview(res["new"]) if ok else None,
        raw=it["raw"], skip=res.get("reason", "") if res.get("status") == "skip" else "", already=res.get("already", False))


def create_app(live=False, max_live=None, queue=None, restore=None, eligible=None, sports=None):
    """queue: a preloaded review queue, restore: unsent approvals in unsent_approvals() form, eligible: citation ids that
    qualify for live edits, sports: page ids the "hide sports" filter hides (all four for tests; by default they come
    from the database).
    By default the live queue is loaded and, in live mode, the tray is rebuilt from approvals no live edit has saved."""
    app = Flask(__name__)
    version = code_version()  # fixed at startup: the code this process is running
    q = load_queue() if queue is None else queue
    for rank, a in enumerate(q):
        a["rank"] = rank
    kind = "live" if live else "dryrun"
    lock = threading.RLock()
    # history: this session's decisions, newest last (undo pops it). rows: built preview rows by citation id.
    if restore is None:
        restore = unsent_approvals() if live else ([], {}, [])
    if eligible is None:
        eligible = eligible_ids() if queue is None else {it["citation_id"] for a in q + restore[0] for it in a["items"]}
    if sports is None:
        try:
            sports = sports_pageids() if queue is None else set()
        except Exception as e:  # the filter is a convenience; the app works without it
            print(f"sports filter unavailable: {wiki.redact(str(e))}")
            sports = set()
    cards = {}  # pageid -> page_card(): fetched once per article
    state = dict(queue=q, sports=sports, hide_sports=False, tray=[], sending=False, next_at=None, log=[], sess=None, history=[], rows={},
                 eligible=eligible, ids=itertools.count(1))

    def fail(message, **kw):
        return jsonify(dict(ok=False, message=message, **kw))

    def record(cid, label, reason="", note="", params=None):
        con = connect(timeout=120)
        cur = con.execute("INSERT INTO decisions(citation_id, run, kind, tier, rules_version, label, reason, note, reviewer, decided_at, params) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, QUEUE_RUN, kind, "HIGH", RULES_VERSION, label, reason, note, "user",
                                                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                            json.dumps(params) if params else None))
        con.commit()
        return cur.lastrowid

    def unrecord(decision_id):
        """Undo: the decision is withdrawn before anything was sent, so its row is removed."""
        con = connect(timeout=120)
        con.execute("DELETE FROM decisions WHERE id=?", (decision_id,))
        con.commit()

    def find(pageid):
        return next((a for a in state["queue"] if a["pageid"] == pageid), None)

    def shown():
        """The queue as navigated: without sports articles while the filter is on (they stay queued, just hidden)."""
        if not state["hide_sports"]:
            return state["queue"]
        return [a for a in state["queue"] if a["pageid"] not in state["sports"]]

    def meta(a):
        return {k: a[k] for k in ("pageid", "title", "views", "rank")}

    def take(a, it):
        """Remove one citation from the queue; the article goes when it has none left. Returns True if it went."""
        a["items"].remove(it)
        if not a["items"]:
            state["queue"].remove(a)
            return True
        return False

    def put_back(art, it):
        """Return a citation to the queue, in its article's original place."""
        a = find(art["pageid"])
        if a is None:
            a = dict(art, items=[])
            state["queue"].insert(next((i for i, x in enumerate(state["queue"]) if x["rank"] > art["rank"]), len(state["queue"])), a)
        it = {k: v for k, v in it.items() if k != "split"}  # back to the proposal as made; the split can be chosen again
        a["items"] = sorted(a["items"] + [it], key=lambda x: x["citation_id"])
        return a

    def queued_entry(pageid):
        return next((t for t in state["tray"] if t["pageid"] == pageid and t["status"] == "queued"), None)

    def stats():
        """Overall progress: every live decision so far (plus, in a dry run, this session's practice decisions)."""
        h = state["history"]
        labels = [r["label"] for r in connect().execute("SELECT citation_id, label FROM decisions WHERE kind='live'")
                  if r["citation_id"] in state["eligible"]]
        if not live:
            labels += [x["label"] for x in h]
        vis = shown()
        remaining = sum(len(a["items"]) for a in vis)
        return dict(total=len(labels) + remaining, decided=len(labels), approved=labels.count("approve"),
                    rejected=labels.count("reject"), session=len(h), articles=len(vis), remaining=remaining,
                    hide_sports=state["hide_sports"], sports_hidden=len(state["queue"]) - len(vis),
                    sports_queued=sum(a["pageid"] in state["sports"] for a in state["queue"]))

    def article_view(a, fresh=False):
        now = time.time()
        need = [it for it in a["items"] if fresh or now - state["rows"].get(it["citation_id"], (0, None))[0] > ROW_TTL]
        err, err_kind = "", ""
        if need:
            try:
                plan = wiki.build_plan(a["pageid"], need)
                res = {r["citation_id"]: r for r in plan["results"]}
                for it in need:
                    state["rows"][it["citation_id"]] = (now, evidence_row(it, res.get(it["citation_id"], {})))
            except (wiki.Refused, Skip) as e:
                err, err_kind = str(e), "blocked"
            except Exception as e:
                err, err_kind = wiki.redact(str(e)), "network"
        rows = []
        for it in a["items"]:
            cached = state["rows"].get(it["citation_id"])
            rows.append(dict(cached[1] if cached and not err else evidence_row(it, {}), status="pending"))
        for h in state["history"]:
            if h["art"]["pageid"] == a["pageid"]:
                rows.append(dict(h["row"], status=h["label"], reason=h["reason"]))
        rows.sort(key=lambda r: r["citation_id"])
        vis = shown()
        if a not in vis:  # decided meanwhile (e.g. by a request that overtook a prefetch), or hidden by the filter
            return dict(missing=True, articles=len(vis))
        if a["pageid"] not in cards:
            cards[a["pageid"]] = page_card(a["pageid"])
        return dict(pos=vis.index(a), articles=len(vis), pageid=a["pageid"], title=a["title"],
                    views=a["views"], rows=rows, error=err, error_kind=err_kind, **cards[a["pageid"]])

    def restore_tray(arts, decision_ids, held):
        """Put approved but unsent citations back in the tray, rebuilt against the live pages."""
        stamp = datetime.now().strftime("%H:%M:%S")
        for cid, why in held:
            state["log"].append(f"{stamp} not restored to the tray: citation {cid}: {why}")
        for a in arts:
            a["rank"] = sum(x["views"] > a["views"] for x in state["queue"]) - 0.5  # its place if returned to the queue
            t = dict(id=next(state["ids"]), pageid=a["pageid"], title=a["title"], art=meta(a), items=a["items"],
                     decisions={it["citation_id"]: decision_ids[it["citation_id"]] for it in a["items"]},
                     status="queued", detail="approved before the restart")
            try:
                t["plan"] = wiki.build_plan(a["pageid"], a["items"])
                bad = [r for r in t["plan"]["results"] if r["status"] != "ok"]
                if bad:
                    raise Skip("; ".join(r.get("reason", "") for r in bad))
            except Exception as e:
                t.update(plan=dict(summary="", citation_ids=[]), status="refused",
                         detail=f"approved before the restart, but the edit can't be rebuilt now: {wiki.redact(str(e))}")
            state["tray"].append(t)
        if arts:
            state["log"].append(f"{stamp} restored {sum(len(a['items']) for a in arts)} approved, unsent citation(s) to the tray")

    restore_tray(*restore)

    @app.get("/")
    def index():
        return PAGE.replace("__MODE__", "LIVE" if live else "DRY RUN").replace("__LIVE__", "true" if live else "false").replace(
            "__REASONS__", json.dumps(REASONS))

    @app.get("/api/article")
    def get_article():
        """?pos=N (position in the queue) or ?pageid=N. &fresh=1 re-reads the live page."""
        with lock:
            vis = shown()
            if not vis:
                return jsonify(dict(end=True, articles=0))
            if request.args.get("pageid"):
                a = next((x for x in vis if x["pageid"] == int(request.args["pageid"])), None)
                if a is None:
                    return jsonify(dict(missing=True, articles=len(vis)))
            else:
                pos = int(request.args.get("pos", 0))
                if pos >= len(vis):
                    return jsonify(dict(end=True, articles=len(vis)))
                a = vis[max(0, pos)]
        return jsonify(article_view(a, fresh=request.args.get("fresh") == "1"))

    @app.post("/api/filter")
    def set_filter():
        """{"hide_sports": bool}: hide or show sports articles while navigating (nothing is decided or removed)."""
        with lock:
            state["hide_sports"] = bool(request.json.get("hide_sports"))
            return jsonify(dict(ok=True, stats=stats()))

    def decided(a, it, label, reason, decision_id, row, sent_item=None):
        """sent_item: the item as approved (with any chosen name split); it is what sits in the tray."""
        state["history"].append(dict(id=decision_id, label=label, reason=reason, art=meta(a), item=it, row=row,
                                     tray_item=sent_item or it))
        gone = take(a, it)
        return jsonify(dict(ok=True, gone=gone, stats=stats()))

    def current(pageid, cid):
        a = find(pageid)
        it = next((x for x in a["items"] if x["citation_id"] == cid), None) if a else None
        return a, it

    @app.post("/api/approve")
    def approve():
        """Approve one citation. It joins its article's queued tray edit (rebuilt with it), or starts one."""
        d = request.json
        with lock:
            a, it = current(d["pageid"], d["citation_id"])
            if it is None:
                return fail("This citation is no longer in the queue.")
            t = queued_entry(a["pageid"])
            before = list(t["items"]) if t else []
        split = d.get("split")
        if split is not None and (len(split) != len(it["names"]) or not all(isinstance(x, list) and len(x) == 2 for x in split)):
            return fail("The name split doesn't match the proposed names.")
        sent = dict(it, split=[list(x) for x in split]) if split else it
        try:
            plan = wiki.build_plan(a["pageid"], before + [sent])
        except Exception as e:
            return fail(f"Could not build the edit: {wiki.redact(str(e))}")
        if sorted(plan["citation_ids"]) != sorted(x["citation_id"] for x in before + [sent]):
            why = "; ".join(r.get("reason", "") for r in plan["results"] if r["status"] != "ok")
            return fail(f"This citation can't be edited now: {why}")
        with lock:
            if current(d["pageid"], d["citation_id"])[1] is not it or queued_entry(a["pageid"]) is not t or (t and t["items"] != before):
                return fail("The tray changed while the edit was being built; try again.")
            did = record(it["citation_id"], "approve", params=sent.get("split"))
            row = state["rows"][it["citation_id"]][1] if it["citation_id"] in state["rows"] else evidence_row(it, {})
            if t:
                t.update(plan=plan, items=before + [sent])
                t["decisions"][it["citation_id"]] = did
            else:
                state["tray"].append(dict(id=next(state["ids"]), pageid=a["pageid"], title=a["title"], art=meta(a), plan=plan,
                                          items=[sent], decisions={it["citation_id"]: did}, status="queued", detail=""))
            return decided(a, it, "approve", "", did, row, sent)

    @app.post("/api/preview")
    def preview():
        """Rebuild one citation's edit with a reviewer-chosen name split (nothing is recorded)."""
        d = request.json
        with lock:
            a, it = current(d["pageid"], d["citation_id"])
        if it is None:
            return fail("This citation is no longer in the queue.")
        split = d.get("split")
        try:
            plan = wiki.build_plan(a["pageid"], [dict(it, split=split) if split else it])
        except Exception as e:
            return fail(f"Could not build the edit: {wiki.redact(str(e))}")
        r = plan["results"][0]
        if r["status"] != "ok":
            return fail(f"That split doesn't work: {r.get('reason', '')}")
        return jsonify(dict(ok=True, row=dict(evidence_row(dict(it, split=split), r), status="pending")))

    @app.post("/api/reject")
    def reject():
        d = request.json
        with lock:
            a, it = current(d["pageid"], d["citation_id"])
            if it is None:
                return fail("This citation is no longer in the queue.")
            reason = REASONS.get(d["reason"], d["reason"])
            did = record(it["citation_id"], "reject", reason, d.get("note", ""))
            row = state["rows"][it["citation_id"]][1] if it["citation_id"] in state["rows"] else evidence_row(it, {})
            return decided(a, it, "reject", reason, did, row)

    @app.post("/api/undo")
    def undo():
        """Withdraw the most recent decision of this session (an approval only while its edit is still unsent)."""
        with lock:
            if not state["history"]:
                return fail("Nothing to undo.")
            h = state["history"][-1]
            t, rest = None, []
            if h["label"] == "approve":
                t = next((x for x in state["tray"] if any(y is h["tray_item"] for y in x["items"])), None)
                if t is None or t["status"] != "queued":
                    return fail("That edit has already been sent, so it can't be undone here.")
                rest = [x for x in t["items"] if x is not h["tray_item"]]
        plan = None
        if rest:
            try:
                plan = wiki.build_plan(h["art"]["pageid"], rest)
            except Exception as e:
                return fail(f"Could not rebuild the article's remaining edit: {wiki.redact(str(e))}")
            if sorted(plan["citation_ids"]) != sorted(x["citation_id"] for x in rest):
                return fail("The article's remaining edit no longer builds; use “Return to queue” on it in the tray.")
        with lock:
            if not state["history"] or state["history"][-1] is not h or (t and (t["status"] != "queued" or
                                                                                all(y is not h["tray_item"] for y in t["items"]))):
                return fail("Something changed meanwhile; try again.")
            if t:
                t["decisions"].pop(h["item"]["citation_id"], None)
            if t and rest:
                t.update(plan=plan, items=rest)
            elif t:
                state["tray"].remove(t)
            state["history"].pop()
            unrecord(h["id"])
            a = put_back(h["art"], h["item"])
            return jsonify(dict(ok=True, pageid=a["pageid"], citation_id=h["item"]["citation_id"], label=h["label"],
                                names=h["item"]["names"], stats=stats()))

    @app.post("/api/return")
    def return_to_queue():
        """Take an unsent edit out of the tray and put its citations back in the queue for review."""
        with lock:
            t = next((x for x in state["tray"] if x["id"] == request.json["id"]), None)
            if t is None or t["status"] not in RETURNABLE:
                return fail("This edit can't be returned to the queue.")
            state["tray"].remove(t)
            for it in t["items"]:
                state["history"] = [h for h in state["history"] if h["tray_item"] is not it]
                unrecord(t["decisions"][it["citation_id"]])
                put_back(t["art"], it)
            return jsonify(dict(ok=True, pageid=t["pageid"], stats=stats()))

    def live_edits(con):
        """citation id -> its live edit log rows, oldest first."""
        out = {}
        for r in con.execute("SELECT citation_ids, status, detail, created_at FROM edits WHERE dry_run=0 ORDER BY id"):
            for cid in json.loads(r["citation_ids"] or "[]"):
                out.setdefault(cid, []).append(r)
        return out

    @app.get("/api/history")
    def history():
        """Every live decision, newest first, with what happened to it."""
        con = connect()
        edits = live_edits(con)
        rows = con.execute("""SELECT d.id, d.citation_id, d.label, d.reason, d.note, d.decided_at, c.pageid, c.url, a.title, p.names
            FROM decisions d JOIN citations c ON c.id=d.citation_id JOIN articles a ON a.pageid=c.pageid
            LEFT JOIN proposals p ON p.citation_id=d.citation_id AND p.run=d.run AND p.rules_version=d.rules_version
            WHERE d.kind='live' ORDER BY d.id DESC""").fetchall()
        with lock:
            tray = {it["citation_id"]: t for t in state["tray"] for it in t["items"]}
        out = []
        for r in rows:
            es, t = edits.get(r["citation_id"], []), tray.get(r["citation_id"])
            saved = next((e for e in reversed(es) if e["status"] in SAVED), None)
            last = es[-1] if es else None
            if r["label"] == "reject":
                where = "rejected"
            elif saved:
                where = "sent"
            elif t and t["status"] in ("queued", "sending"):
                where = "tray"
            elif any(e["status"] == "attempt" for e in es):
                where = "unclear"
            else:
                where = "unsent"
            detail = json.loads(saved["detail"] if saved else (last["detail"] if last else "{}") or "{}")
            out.append(dict(decision_id=r["id"], citation_id=r["citation_id"], pageid=r["pageid"], title=r["title"], url=r["url"],
                            names=json.loads(r["names"] or "[]"), label=r["label"], reason=r["reason"] or "", note=r["note"] or "",
                            decided_at=r["decided_at"], where=where, revid=detail.get("newrevid") if saved else None,
                            sent_at=saved["created_at"] if saved else None,
                            why=detail.get("reason") or detail.get("error") or "" if where == "unsent" else "",
                            tray_id=t["id"] if t and t["status"] in RETURNABLE else None,
                            requeue=live and not t and where in ("rejected", "unsent")))
        return jsonify(dict(rows=out, live=live))

    @app.post("/api/requeue")
    def requeue():
        """Put a rejected (or approved but never sent) citation back in the queue; its decision is withdrawn."""
        if not live:
            return fail("History actions change live decisions, so they only work in live mode.")
        con = connect()
        d = con.execute("SELECT * FROM decisions WHERE id=? AND kind='live'", (request.json["decision_id"],)).fetchone()
        if d is None:
            return fail("That decision no longer exists.")
        cid = d["citation_id"]
        with lock:
            if any(it["citation_id"] == cid for t in state["tray"] for it in t["items"]):
                return fail("This citation is in the tray; use “Return to queue” there.")
            if d["label"] == "approve" and {e["status"] for e in live_edits(con).get(cid, [])} & (set(SAVED) | {"attempt"}):
                return fail("This edit was (or may have been) saved on Wikipedia, so it can't go back to the queue.")
            row = next((r for r in _proposals(con) if r["citation_id"] == cid), None)
            if row is None or not checker_agrees(json.loads(row["names"]), checker_names().get(cid)):
                return fail("This citation no longer qualifies for the live queue.")
            art = next(iter(_articles([row]).values()))
            art["rank"] = sum(x["views"] > art["views"] for x in state["queue"]) - 0.5
            state["history"] = [h for h in state["history"] if h["id"] != d["id"]]
            unrecord(d["id"])
            a = put_back(meta(art), art["items"][0])
            return jsonify(dict(ok=True, pageid=a["pageid"], citation_id=cid, title=a["title"], stats=stats()))

    def revid(detail):
        m = re.search(r"revision (\d+)", detail or "")
        return int(m.group(1)) if m else None

    @app.get("/api/status")
    def status():
        con = connect()
        with lock:
            tray = [dict(id=t["id"], pageid=t["pageid"], title=t["title"], status=t["status"], detail=t["detail"],
                         citations=[", ".join(it["names"]) for it in t["items"]], summary=t["plan"]["summary"],
                         revid=revid(t["detail"]) if t["status"].startswith("success") else None,
                         returnable=t["status"] in RETURNABLE) for t in state["tray"]]
        nxt = state["next_at"]
        return jsonify(dict(tray=tray, sending=state["sending"], next_in=max(0, round(nxt - time.time())) if nxt else None,
                            live=live, max_live=max_live, live_sent=wiki.live_sent_count(con), log=state["log"][-30:],
                            stats=stats(), can_undo=bool(state["history"]), version=version))

    def sender(ids):
        try:
            if live and (state["sess"] is None or not state["sess"].logged_in()):  # a login can lapse while the app runs
                state["sess"] = wiki.Session()
            first = True
            for tid in ids:
                with lock:
                    t = next((x for x in state["tray"] if x["id"] == tid and x["status"] == "queued"), None)
                if t is None:
                    continue
                if not first:  # live edits are paced; a dry run only re-reads pages, so a short gap is enough
                    gap = random.uniform(10, 20) if live else 1
                    state["next_at"] = time.time() + gap
                    time.sleep(gap)
                    state["next_at"] = None
                with lock:
                    if t not in state["tray"] or t["status"] != "queued":
                        continue
                    t["status"] = "sending"
                first = False
                status, detail = wiki.send(t["plan"], t["items"], sess=state["sess"], dry_run=not live, max_live=max_live)
                t["status"], t["detail"] = status, detail
                state["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {t['title']}: {status} - {detail}")
                if "cap reached" in detail:
                    break
        except Exception as e:
            state["log"].append(f"send stopped: {wiki.redact(str(e))}")
        finally:
            state["next_at"] = None
            state["sending"] = False

    @app.post("/api/send")
    def send():
        with lock:
            if state["sending"]:
                return fail("Already sending.")
            ids = [t["id"] for t in state["tray"] if t["status"] == "queued"]
            if not ids:
                return fail("Nothing in the tray to send.")
            state["sending"] = True
        threading.Thread(target=sender, args=(ids,), daemon=True).start()
        return jsonify(dict(ok=True))

    return app


def run(sample, run_name, live=False, max_live=None, port=4580):
    global QUEUE_RUN, GOLD
    QUEUE_RUN, GOLD = run_name, f"{sample}-gold"
    app = create_app(live=live, max_live=max_live)
    print(f"Review app ({'LIVE' if live else 'DRY RUN'}; {GOLD}, run {QUEUE_RUN}; live cap {max_live or 'none'}; code {code_version()}) at http://127.0.0.1:{port}")
    if live and not re.fullmatch(r"[0-9a-f]{40}", code_version()):
        print("WARNING: the running code is not exactly a git commit; live edits will be logged with this version string.")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__MODE__ · Citation authors</title><style>
:root{--bg:#f6f6f3;--fg:#1d1d1b;--mut:#6b6b66;--faint:#9a9a93;--card:#fff;--line:#e3e2dc;--soft:#f0efea;--acc:#2b5fd9;
--ok:#1d7a3e;--okbg:#e5f4ea;--bad:#b3261e;--badbg:#fbe9e7;--warn:#8a5a00;--warnbg:#fdf3dc;--hi:#fff1a8;--ins:#cdeed6;--shadow:0 1px 2px rgba(0,0,0,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececea;--mut:#a3a39d;--faint:#77776f;--card:#212124;--line:#36363a;--soft:#2a2a2e;
--acc:#8fb0ff;--ok:#6cd093;--okbg:#1d3326;--bad:#ff8a80;--badbg:#3d2222;--warn:#f0c060;--warnbg:#3a3020;--hi:#5a4d00;--ins:#1f4d2b;--shadow:none}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif;margin:0}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
button{font:inherit;color:inherit;cursor:pointer} button:disabled{cursor:default;opacity:.45}
kbd{font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:2px 5px;
background:var(--card);color:var(--mut);margin-left:6px;vertical-align:1px}
code,pre{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
mark{background:var(--hi);color:inherit;border-radius:2px} ins{background:var(--ins);text-decoration:none;border-radius:2px}
del{background:var(--badbg);color:var(--bad);border-radius:2px}
.mut{color:var(--mut)} .grow{flex:1}

.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:10px 20px;flex-wrap:wrap}
.brand{font-weight:700}
.mode{font:700 12px/1 system-ui;letter-spacing:.04em;padding:5px 8px;border-radius:5px;color:var(--ok);background:var(--okbg)}
.mode.live{color:#fff;background:#c0352b}
.prog{display:flex;align-items:center;gap:10px;flex:1;min-width:160px;font-size:13px;color:var(--mut)}
#progtxt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
@media (max-width:1360px){#progtxt .more{display:none}}  /* the counts stay in the tooltip */
@media (max-width:1200px){.bar{flex-basis:80px}.top{gap:10px}.brand{display:none}}
.bar{flex:0 0 120px;height:6px;border-radius:3px;background:var(--line);overflow:hidden} .bar i{display:block;height:100%;background:var(--ok);width:0;transition:width .3s}
.tbtn{background:none;border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:13px;color:var(--mut)}
.tbtn:hover:not(:disabled){color:var(--fg);border-color:var(--mut)}

@media (max-width:640px){.top{gap:8px 10px;padding:8px 14px}.prog{order:5;flex-basis:100%}.bar{flex-basis:90px}}
main{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:24px;max-width:1320px;margin:0 auto;padding:18px 20px 80px}
@media (max-width:960px){main{grid-template-columns:1fr}}

.ahead{margin-bottom:12px}
.aline{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut)}
.nav button{background:none;border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:13px;color:var(--mut)}
.nav button:hover:not(:disabled){color:var(--fg)}
h1{font-size:24px;line-height:1.25;margin:4px 0 2px} h1 a{color:var(--fg)}
.sub{font-size:13px;color:var(--mut)}
.banner{border-radius:8px;padding:10px 14px;margin:10px 0;font-size:14px;display:flex;gap:10px;align-items:center}
.banner.bad{background:var(--badbg);color:var(--bad)} .banner.warn{background:var(--warnbg);color:var(--warn)}
.banner button{background:var(--card);border:1px solid currentColor;border-radius:6px;padding:3px 10px;color:inherit}

.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;box-shadow:var(--shadow);position:relative}
.card.focus{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.card .cols{display:grid;grid-template-columns:minmax(200px,5fr) 8fr;gap:0}
@media (max-width:760px){.card .cols{grid-template-columns:1fr}}
.prop{padding:16px 18px;border-right:1px solid var(--line)}
@media (max-width:760px){.prop{border-right:0;border-bottom:1px solid var(--line)}}
.lbl{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--faint)}
.author{margin:6px 0 10px}
.names{font-size:20px;font-weight:650;line-height:1.3;margin-bottom:3px}
.params{display:flex;flex-wrap:wrap;gap:4px}
.hint2{text-transform:none;letter-spacing:0;font-weight:400}
.params span{background:var(--ins);border-radius:4px;padding:1px 6px}
.ev{padding:12px 18px;display:grid;grid-template-columns:92px minmax(0,1fr);column-gap:12px;row-gap:7px;align-content:start;font-size:14px}
.ev .k{font-size:12px;color:var(--faint);padding-top:1px}
.ev q{quotes:"“" "”"} .ev .v{overflow-wrap:anywhere}
.tag{display:inline-block;font-size:11px;font-weight:600;border-radius:4px;padding:1px 6px;margin-left:6px;background:var(--soft);color:var(--mut);vertical-align:1px}
.tag.warn{background:var(--warnbg);color:var(--warn)}
.okt{color:var(--ok)} .warnt{color:var(--warn)}
.skipmsg{margin-top:10px;font-size:13px;color:var(--bad)} .skipmsg.already{color:var(--warn)}
.details{border-top:1px solid var(--line);padding:12px 18px;font-size:13px;display:grid;gap:8px}
.details pre{margin:0;white-space:pre-wrap;word-break:break-all;background:var(--soft);border-radius:6px;padding:8px 10px}
.actions{display:flex;align-items:center;gap:8px;padding:10px 18px;border-top:1px solid var(--line);flex-wrap:wrap}
.btn{border:1px solid var(--line);background:var(--card);border-radius:7px;padding:6px 12px;font-size:14px;display:inline-flex;align-items:center}
.btn:hover:not(:disabled){border-color:var(--mut);text-decoration:none}
.btn.approve{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:600}
.btn.approve kbd{background:transparent;color:#fff;border-color:rgba(255,255,255,.5)}
@media (prefers-color-scheme:dark){.btn.approve{color:#0d2216}.btn.approve kbd{color:#0d2216;border-color:rgba(0,0,0,.35)}}
.btn.reject{color:var(--bad)} .btn.quiet{border-color:transparent;background:none;color:var(--mut)} .btn.quiet:hover{color:var(--fg)}
.card:not(.focus) kbd{display:none}
.reasons{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:10px 18px;border-top:1px solid var(--line);background:var(--badbg)}
.reasons .lbl{color:var(--bad);margin-right:4px}
.chip{border:1px solid var(--line);background:var(--card);border-radius:16px;padding:4px 10px;font-size:13px}
.chip kbd{margin:0 6px 0 0}
.chip:hover{border-color:var(--bad)}
.reasons input{flex:1;min-width:160px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit;font-size:13px;background:var(--card);color:var(--fg)}

.ffbox{margin-top:10px;padding:8px 10px;border-radius:7px;background:var(--warnbg);color:var(--warn);font-size:13px;line-height:1.4}
.ffbox.ok{background:var(--okbg);color:var(--ok)}
.ffbox .row{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.ffbox button{border:1px solid currentColor;background:var(--card);color:inherit;border-radius:6px;padding:2px 8px;font-size:12px}
.ffbox button kbd{color:inherit}
.splitedit{display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center;font-size:12px;margin:6px 0}
.splitedit input{border:1px solid var(--line);border-radius:5px;padding:3px 6px;font:13px ui-monospace,Menlo,monospace;background:var(--card);color:var(--fg);min-width:0}
.card.done{opacity:.8}
.card.done .cols,.card.done .actions{display:none}
.stamp{display:flex;align-items:center;gap:10px;padding:10px 18px;font-size:14px}
.stamp b{font-weight:650}
.stamp .st{font-size:12px;font-weight:700;border-radius:4px;padding:2px 7px}
.st.approve{background:var(--okbg);color:var(--ok)} .st.reject{background:var(--badbg);color:var(--bad)} .st.skipped{background:var(--soft);color:var(--mut)}
.card.skipped .stamp{padding-bottom:0}

.hrow{display:flex;gap:14px;align-items:center;margin-top:4px}
.thumb{width:60px;height:60px;border-radius:10px;object-fit:cover;flex:none;border:1px solid var(--line);background:var(--soft)}
.thumb.none{display:grid;place-items:center;font:600 24px/1 Georgia,serif;color:var(--faint)}
.desc{font-size:14px;color:var(--mut)}
.refprev{border-top:1px solid var(--line);padding:10px 18px 11px;display:flex;gap:10px;align-items:baseline}
.refprev .lbl{flex:none}
.refprev .ref{font:15px/1.55 Georgia,'Linux Libertine','Times New Roman',serif;overflow-wrap:anywhere}
.refprev .ref i{font-style:italic} .refprev .ref ins{padding:0 2px}
@media (max-width:760px){.refprev{flex-direction:column;gap:2px}}
@keyframes flash{from{background:var(--okbg)}to{background:var(--card)}}
@keyframes pop{0%{transform:scale(.5);opacity:0}60%{transform:scale(1.2)}100%{transform:scale(1);opacity:1}}
@keyframes rise{from{transform:translateY(8px);opacity:.5}to{transform:none;opacity:1}}
.card.done.just{animation:flash .9s ease-out}
.card.done.just .st{display:inline-block;animation:pop .35s ease-out}
.card.focus.enter{animation:rise .22s ease-out}
@media (prefers-reduced-motion:reduce){.card.just,.card.just .st,.card.enter{animation:none}}

.endcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:28px;text-align:center;margin-top:24px}
.endcard h2{margin:0 0 6px}
.loading{color:var(--mut);padding:30px 0}

aside{position:sticky;top:64px;align-self:start;max-height:calc(100vh - 80px);overflow:auto}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow)}
.panel h2{font-size:15px;margin:0;display:flex;align-items:center;gap:8px}
.count{font-size:12px;background:var(--soft);color:var(--mut);border-radius:10px;padding:1px 8px}
.panel .hint{font-size:12px;color:var(--mut);margin:4px 0 10px}
.tl{list-style:none;margin:0;padding:0}
.tl li{padding:8px 0;border-top:1px solid var(--line);font-size:13px}
.tl .t{font-weight:600;font-size:14px;display:flex;gap:6px;align-items:baseline}
.tl .t span:first-child{flex:1}
.tl .c{color:var(--mut)}
.tl .d{color:var(--mut);font-size:12px;overflow-wrap:anywhere}
.tl .s{font-size:12px;margin-top:3px;overflow-wrap:anywhere} .tl .s .lbl{margin-right:4px}
.linkbtn{background:none;border:0;padding:0;color:var(--acc);font-size:12px} .linkbtn:hover{text-decoration:underline}
.sendbtn{width:100%;justify-content:center;margin-top:10px;font-weight:600;background:var(--acc);border-color:var(--acc);color:#fff}
.sendbtn.live{background:var(--bad);border-color:var(--bad)} .sendbtn.confirm{outline:3px solid var(--warnbg)}
@media (prefers-color-scheme:dark){.sendbtn{color:#111}}
.capline{font-size:12px;color:var(--mut);margin-top:6px;text-align:center}
.badge{font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px;white-space:nowrap;background:var(--soft);color:var(--mut)}
.badge.ok{background:var(--okbg);color:var(--ok)} .badge.bad{background:var(--badbg);color:var(--bad)} .badge.warn{background:var(--warnbg);color:var(--warn)}
.panel h3{font-size:13px;margin:16px 0 4px;color:var(--mut)}
details.log summary{font-size:12px;color:var(--mut);cursor:pointer;margin-top:12px}
details.log div{font-size:12px;color:var(--mut);overflow-wrap:anywhere}

.tabs{display:flex;gap:2px;background:var(--soft);border-radius:7px;padding:2px}
.tabs button{border:0;background:none;border-radius:5px;padding:3px 10px;font-size:13px;color:var(--mut)}
.tabs button.on{background:var(--card);color:var(--fg);box-shadow:var(--shadow);font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:12px 0}
.filters .chip.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.filters input{margin-left:auto;min-width:250px;border:1px solid var(--line);border-radius:6px;padding:5px 9px;font:inherit;font-size:13px;background:var(--card);color:var(--fg)}
.hist{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
.hist th{text-align:left;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);padding:9px 12px;border-bottom:1px solid var(--line)}
.hist td{padding:9px 12px;border-top:1px solid var(--line);vertical-align:top}
.hist tr:first-child td{border-top:0}
.hist .when{white-space:nowrap;color:var(--mut);font-size:13px}
.hist .why{font-size:12px;color:var(--mut);margin-top:2px}
.hist .act{white-space:nowrap;text-align:right}
.hist .act > *{margin-left:10px}
@media (max-width:760px){.hist thead{display:none}.hist tr{display:grid;grid-template-columns:1fr auto;padding:6px 0}.hist td{border:0;padding:3px 12px}.hist tr+tr{border-top:1px solid var(--line)}}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(20px);opacity:0;pointer-events:none;transition:.2s;
background:var(--fg);color:var(--bg);border-radius:8px;padding:9px 14px;font-size:14px;display:flex;gap:14px;align-items:center;max-width:min(680px,92vw);z-index:20}
#toast.show{opacity:1;transform:translateX(-50%);pointer-events:auto} #toast.err{background:var(--bad);color:#fff}
#toast button{background:none;border:0;color:inherit;font-weight:700;text-decoration:underline;padding:0}
#help{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:30}
#help.show{display:flex}
#help .box{background:var(--card);border-radius:12px;padding:20px 24px;max-width:480px;width:92vw}
#help h2{margin:0 0 10px;font-size:17px} #help dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0;font-size:14px}
#help dt{text-align:right} #help dt kbd{margin:0 0 0 3px} #help p{font-size:13px;color:var(--mut)}
</style></head><body>
<header class="top">
 <span class="brand">Citation authors</span>
 <span class="mode" id="mode" title="">__MODE__</span>
 <div class="tabs"><button id="tabReview" class="on" onclick="setView('review')">Review</button><button id="tabHistory" onclick="setView('history')">History<kbd>H</kbd></button></div>
 <div class="prog"><div class="bar"><i id="bar"></i></div><span id="progtxt"></span></div>
 <button class="tbtn" id="sportsBtn" onclick="toggleSports()" hidden title="Sports articles: a sports WikiProject tags them, no other main topic does (politics, film, music, business…), and people have a sport on Wikidata. Hidden articles stay in the queue.">Hide sports</button>
 <button class="tbtn" id="undoBtn" onclick="undo()" disabled>Undo<kbd>U</kbd></button>
 <button class="tbtn" onclick="toggleHelp()">Shortcuts<kbd>?</kbd></button>
</header>
<main><section id="art"><div class="loading">Loading…</div></section>
<aside><div class="panel" id="tray"></div></aside></main>
<div id="toast"><span id="toastmsg"></span><button id="toastundo" onclick="undo()">Undo</button></div>
<div id="help" onclick="if(event.target===this)toggleHelp()"><div class="box"><h2>Keyboard shortcuts</h2><dl>
<dt><kbd>A</kbd></dt><dd>Approve the selected citation (adds it to its article's edit in the tray)</dd>
<dt><kbd>R</kbd></dt><dd>Reject, then pick a reason with <kbd>1</kbd>–<kbd>6</kbd></dd>
<dt><kbd>S</kbd></dt><dd>Skip for now (it stays in the queue)</dd>
<dt><kbd>U</kbd></dt><dd>Undo the last decision (until its edit is sent)</dd>
<dt><kbd>J</kbd><kbd>K</kbd></dt><dd>Next / previous citation in this article (also <kbd>↓</kbd><kbd>↑</kbd>)</dd>
<dt><kbd>N</kbd><kbd>P</kbd></dt><dd>Next / previous article (also <kbd>→</kbd><kbd>←</kbd>)</dd>
<dt><kbd>F</kbd></dt><dd>Use family name first for the selected citation (Choi Won-suk → last=Choi, first=Won-suk); press again to undo</dd>
<dt><kbd>O</kbd></dt><dd>Open the copy that was read (the archived copy, or the page the cited link leads to)</dd>
<dt><kbd>D</kbd></dt><dd>Show or hide wikitext and reader notes</dd>
<dt><kbd>H</kbd></dt><dd>Switch between Review and History (every live decision, with what happened to it)</dd>
<dt><kbd>Esc</kbd></dt><dd>Cancel / close</dd></dl>
<p>After each decision the next citation is selected automatically. Nothing is posted until you press the post button at the bottom of the tray.</p></div></div>
<script>
const LIVE=__LIVE__, REASONS=__REASONS__;
let splitEdit=null, justDone=null, entering=false, advTimer=0, rejectAt=0;
let A=null, pos=0, sel=0, busy=false, loading=false, rejecting=false, gone=false, ST=null, confirmAt=0, loadSeq=0, toastTimer=0;
const skipped=new Set();
let details=false; try{details=localStorage.getItem('details')=='1'}catch(e){}
let hideSports=false; try{hideSports=localStorage.getItem('hideSports')=='1'}catch(e){}
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);
const plural=(n,w)=>`${n} ${w}${n==1?'':'s'}`;
const compact=n=>n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1e3?Math.round(n/1e3)+'k':String(n||0);
function hl(text,names){let h=esc(text);for(const n of names){if(!n)continue;const re=new RegExp('('+esc(n).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi');h=h.replace(re,'<mark>$1</mark>')}return h}
async function post(u,b){try{const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return await r.json()}
 catch(e){return {ok:false,message:'Could not reach the review app: '+e.message}}}
function toast(msg,kind,withUndo){const t=$('toast');$('toastmsg').textContent=msg;t.className='show'+(kind=='err'?' err':'');
 $('toastundo').style.display=withUndo?'':'none';clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='',kind=='err'?9000:5000)}
const status=r=>r.status!='pending'?r.status:(skipped.has(r.citation_id)?'skipped':'pending');
const open_=r=>r.status=='pending';

/* ---------- loading and navigation ---------- */
async function load(q,opt={}){
 clearTimeout(advTimer);const seq=++loadSeq;loading=true;rejecting=false;renderArticle();
 let r;try{r=await (await fetch('/api/article?'+new URLSearchParams(q))).json()}catch(e){r={error:'Could not reach the review app: '+e.message,error_kind:'app'}}
 if(seq!=loadSeq)return;
 if(r.missing){return load({pos:Math.min(pos,Math.max(0,r.articles-1))})}
 if(!A||A.pageid!=r.pageid)window.scrollTo(0,0);
 loading=false;A=r;gone=false;
 if(r.end){pos=r.articles}else if(r.pos!=null){pos=r.pos;
  const want=opt.cid!=null?r.rows.findIndex(x=>x.citation_id==opt.cid):-1;
  sel=want>=0?want:Math.max(0,r.rows.findIndex(x=>status(x)=='pending'),0);
  if(want<0&&r.rows.every(x=>status(x)!='pending'))sel=Math.max(0,r.rows.findIndex(open_));
  fetch('/api/article?pos='+(pos+1)).catch(()=>{});   /* warm the next article */
 }
 renderArticle();
}
function nextArticle(){if(!A||A.end)return;load({pos:gone?pos:pos+1})}
function prevArticle(){if(pos>0)load({pos:pos-1})}
function moveSel(d){if(!A||!A.rows)return;sel=Math.min(A.rows.length-1,Math.max(0,sel+d));rejecting=false;splitEdit=null;renderArticle(true)}
function advance(){const n=A.rows.length;
 for(let d=1;d<=n;d++){const j=(sel+d)%n;if(status(A.rows[j])=='pending'){sel=j;renderArticle(true);return}}
 if(justDone!=null){advTimer=setTimeout(nextArticle,450);return}  /* let the approval animation show */
 nextArticle()}

/* ---------- decisions ---------- */
function cur(){return A&&A.rows?A.rows[sel]:null}
async function approve(){const r=cur();if(!r||!open_(r)||busy||loading)return;
 if(!r.diff){toast(r.skip?"Can't approve: "+r.skip:"The edit preview didn't load; retry the page first.",'err');return}
 busy=true;renderArticle();const res=await post('/api/approve',{pageid:A.pageid,citation_id:r.citation_id,split:r.split||null});busy=false;
 if(!res.ok){toast(res.message,'err');renderArticle();return}
 done(r,'approve','',res);toast(`Approved ${r.names.join(', ')} — added to the tray`,'ok',true)}
function startReject(){const r=cur();if(!r||!open_(r)||busy||loading)return;rejecting=true;rejectAt=Date.now();renderArticle()}
function cancelReject(){rejecting=false;renderArticle()}
async function reject(key){const r=cur();if(!r||!open_(r)||busy)return;const note=($('note')||{}).value||'';
 busy=true;renderArticle();const res=await post('/api/reject',{pageid:A.pageid,citation_id:r.citation_id,reason:key,note});busy=false;
 if(!res.ok){toast(res.message,'err');renderArticle();return}
 done(r,'reject',REASONS[key],res);toast(`Rejected ${r.names.join(', ')}: ${REASONS[key]}`,'ok',true)}
function done(r,label,reason,res){justDone=label=='approve'?r.citation_id:null;entering=true;
 setTimeout(()=>{if(justDone==r.citation_id)justDone=null},900);r.status=label;r.reason=reason;skipped.delete(r.citation_id);rejecting=false;if(res.gone)gone=true;
 if(ST)ST.stats=res.stats;renderProgress();refreshStatus();advance()}
function skip(){const r=cur();if(!r||!open_(r)||busy||loading)return;skipped.add(r.citation_id);rejecting=false;
 toast(`Skipped ${r.names.join(', ')} — it stays in the queue`);advance()}
async function undo(){if(busy)return;busy=true;const r=await post('/api/undo');busy=false;
 if(!r.ok){toast(r.message,'err');return}
 skipped.delete(r.citation_id);toast(`Undid the ${r.label=='approve'?'approval':'rejection'} of ${r.names.join(', ')}`);
 refreshStatus();load({pageid:r.pageid},{cid:r.citation_id})}
async function returnEntry(id,stay){const r=await post('/api/return',{id});if(!r.ok){toast(r.message,'err');return}
 toast('Returned to the queue for review');refreshStatus();
 if(stay&&view=='history'){loadHistory();return}if(view!='review')setView('review');load({pageid:r.pageid})}
function openSource(){const r=cur();if(r)window.open(viewUrl(r),'_blank','noopener')}
async function toggleSports(){hideSports=!hideSports;try{localStorage.setItem('hideSports',hideSports?'1':'0')}catch(e){}
 const r=await post('/api/filter',{hide_sports:hideSports});if(!r.ok){toast(r.message,'err');return}
 if(ST){ST.stats=r.stats;renderProgress()}load(A&&A.pageid?{pageid:A.pageid}:{pos:0})}
function toggleDetails(){details=!details;try{localStorage.setItem('details',details?'1':'0')}catch(e){}renderArticle()}
function retry(){if(A&&A.pageid)load({pageid:A.pageid,fresh:1})}

/* ---------- article rendering ---------- */
function evRow(k,v){return v?`<div class="k">${k}</div><div class="v">${v}</div>`:''}
function card(r,i){
 const st=status(r),focus=i==sel,names=r.names;
 if(st=='approve'||st=='reject'){
  return `<article class="card done ${r.citation_id==justDone?'just':''}" data-i="${i}"><div class="stamp"><span class="st ${st}">${st=='approve'?'✓ Approved':'Rejected'}</span>
  <b>${esc(names.join(', '))}</b><span class="mut">${st=='approve'?'in the tray':esc(r.reason)}</span></div></article>`}
 const title=r.headline||r.cited||r.domain||'source';
 const differs=r.headline&&r.cited&&r.title_sim!=null&&r.title_sim<0.9;
 const src=`<a href="${esc(r.url||viewUrl(r))}" target="_blank" rel="noopener" title="Open the cited link (the URL in the article)">${esc(title)} ↗</a> <span class="mut">${esc(r.domain)} · cited link</span><br>${readLine(r)}`;
 const alt=r.alt?`<span class="okt">✓</span> ${r.alt.via=='wayback'?'Archived copy':'Live page'} also credits ${esc(r.alt.authors.join(', ')||'nobody')}`:'';
 const roles={};r.others.forEach(o=>(roles[o.role]=roles[o.role]||[]).push(o.name));
 const others=Object.entries(roles).map(([role,ns])=>`<span title="${esc(ns.join(', '))}">${esc(ns.slice(0,3).join(', '))}${ns.length>3?` +${ns.length-3} more`:''}</span> <span class="mut">(${esc(role)}${ns.length>1?'s':''})</span>`).join('<br>');
 const meta=r.meta.length?r.meta.map(q=>`<q>${hl(q,names)}</q>`).join(' · '):'<span class="mut">none</span>';
 let h=`<article class="card ${focus?'focus':''} ${focus&&entering?'enter':''} ${st}" data-i="${i}">`;
 if(st=='skipped')h+=`<div class="stamp"><span class="st skipped">Skipped</span><span class="mut">still in the queue — you can decide it now</span></div>`;
 const groups=names.map(()=>[]);r.params.forEach(([k,v])=>{const m=k.match(/(\d+)$/);(groups[m?+m[1]-1:0]||groups[0]).push(`|${k}=${v}`)});
 h+=`<div class="cols"><div class="prop"><div class="lbl">Proposed author${names.length>1?'s':''}</div>
  ${names.map((n,j)=>`<div class="author"><div class="names">${esc(n)}</div>${groups[j].length?`<code class="params">${groups[j].map(p=>`<span>${esc(p)}</span>`).join('')}</code>`:''}</div>`).join('')}
  ${r.params.length?'<div class="lbl hint2">Highlighted text is added to the citation</div>':''}
  ${removedSlots(r)}
  ${splitBox(r,i)}
  ${r.skip?r.already?`<div class="skipmsg already">Already done on Wikipedia: ${esc(r.skip)}. Reject or skip it.</div>`
   :`<div class="skipmsg">Can't be added automatically: ${esc(r.skip)}</div>`:''}
  ${!r.built&&!r.skip&&!A.error?'<div class="skipmsg mut">Edit preview not loaded.</div>':''}</div>
  <div class="ev">${evRow('Byline',r.quotes.map(q=>`<q>${hl(q,names)}</q>`).join(' · ')||'<span class="mut">none</span>')}
  ${evRow('Metadata',meta)}${evRow('Source',src)}
  ${differs?evRow('Cited as',`${esc(r.cited)} <span class="tag warn">headline differs</span>`):''}
  ${evRow('Cross-check',alt)}${evRow('Not added',others)}${evRow('Agency',r.agency?esc(r.agency)+' <span class="mut">(also credited; not added)</span>':'')}</div></div>`;
 if(r.preview)h+=`<div class="refprev"><span class="lbl">Will read</span>${refPreview(r.preview)}</div>`;
 if(details){const d=r.diff,dels=d&&d.some(([k])=>k=='del');
  h+=`<div class="details"><div class="lbl">Citation wikitext${d?` (added text highlighted${dels?', removed text struck through':''})`:''}</div>
  <pre>${d?d.map(([k,t])=>k?`<${k}>${esc(t)}</${k}>`:esc(t)).join(''):esc(r.raw)}</pre>
  ${r.notes?`<div><span class="lbl">Reader note</span><div>${esc(r.notes)}</div></div>`:''}</div>`}
 if(focus&&rejecting){h+=`<div class="reasons"><span class="lbl">Reject because</span>${Object.entries(REASONS).map(([k,v])=>
  `<button class="chip" onclick="chipClick(event,'${k}')"><kbd>${k}</kbd>${esc(cap(v))}</button>`).join('')}
  <input id="note" placeholder="Note (optional)" title="Type a note, then pick a reason; Enter rejects as “other”" autocomplete="off"><button class="btn quiet" onclick="cancelReject()">Cancel<kbd>Esc</kbd></button></div>`}
 else{const dis=busy||loading;h+=`<div class="actions">
  <button class="btn approve" onclick="pick(${i});approve()" ${dis||!r.diff?'disabled':''}>${busy&&focus?'Approving…':'Approve'}<kbd>A</kbd></button>
  <button class="btn reject" onclick="pick(${i});startReject()" ${dis?'disabled':''}>Reject<kbd>R</kbd></button>
  <button class="btn" onclick="pick(${i});skip()" ${dis?'disabled':''}>Skip<kbd>S</kbd></button><span class="grow"></span>
  <button class="btn quiet" onclick="pick(${i});openSource()">Open source<kbd>O</kbd></button>
  <button class="btn quiet" onclick="toggleDetails()">${details?'Hide':'Show'} wikitext<kbd>D</kbd></button></div>`}
 return h+'</article>'}
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const bare=u=>String(u||'').replace(/^https?:\/\/(www\.)?/,'').replace(/\/$/,'');
/* the copy that was read, as a person would view it (Wayback's raw 'id_' form shows the page unstyled) */
const viewUrl=r=>String(r.snapshot||r.url||'').replace(/(\/web\/\d+)id_\//,'$1/');
function readLine(r){
 let read;
 if(r.via=='wayback'){const m=(r.snapshot||'').match(/\/web\/(\d{4})(\d{2})(\d{2})/);
  read=`<a href="${esc(viewUrl(r))}" target="_blank" rel="noopener">Archived copy ↗</a> <span class="mut">what was read: Wayback Machine${m?`, ${+m[3]} ${MONTHS[+m[2]-1]} ${m[1]}`:''}</span>`}
 else if(!r.snapshot||bare(r.snapshot)==bare(r.url))read=`<span class="mut">The live page at the cited link was read</span>`;
 else read=`<a href="${esc(viewUrl(r))}" target="_blank" rel="noopener">Page read ↗</a> <span class="mut">the cited link redirected here</span>`;
 return read}
function refPreview(p){const end=s=>/[.!?]$/.test(s)?s:s+'.';
 return `<span class="ref"><ins>${esc(p.authors)}</ins>${p.date?` (${esc(p.date)})`:''}. `+
  (p.title?`"${esc(p.title)}". `:'')+
  (p.work?`<i>${esc(end(p.work))}</i> `:'')+(p.retrieved?`Retrieved ${esc(p.retrieved)}.`:'')+`</span>`}
function removedSlots(r){return r.removed&&r.removed.length?`<div class="lbl hint2 warnt" style="margin-top:4px">Also removes empty author slots: <code>${esc(r.removed.map(k=>'|'+k+'=').join(' '))}</code></div>`:''}
function pick(i){if(sel!=i){sel=i;rejecting=false;splitEdit=null}}
const lastFirst=r=>r.params.some(([k])=>k.startsWith('last'));
function curSplit(r){const g=r.names.map(()=>['','']);r.params.forEach(([k,v])=>{const m=k.match(/^(last|first)(\d*)$/);if(m){const j=m[2]?+m[2]-1:0;if(g[j])g[j][m[1]=='last'?0:1]=v}});return g}
function splitBox(r,i){if(!lastFirst(r)||r.status!='pending')return'';
 if(splitEdit==r.citation_id){const g=curSplit(r);return `<div class="ffbox ok">Set which words are the surname (last) and given names (first):
  ${r.names.map((n,j)=>`<div class="splitedit"><b style="grid-column:1/-1">${esc(n)}</b><span>last</span><input class="sp" data-j="${j}" data-k="0" value="${esc(g[j][0])}"><span>first</span><input class="sp" data-j="${j}" data-k="1" value="${esc(g[j][1])}"></div>`).join('')}
  <div class="row"><button onclick="saveSplit()">Use this split<kbd>↵</kbd></button><button onclick="splitEdit=null;renderArticle()">Cancel<kbd>Esc</kbd></button></div></div>`}
 const edit=`<button onclick="pick(${i});splitEdit=${r.citation_id};renderArticle();setTimeout(()=>document.querySelector('.sp')?.focus(),0)">Edit last/first…</button>`;
 if(r.split)return `<div class="ffbox ok">You changed the last/first split for this citation.<div class="row"><button onclick="pick(${i});setSplit(null)">Back to the proposed split<kbd>F</kbd></button>${edit}</div></div>`;
 if(r.family_first)return `<div class="ffbox">This name may be written family name first, as in many Korean, Chinese and Singaporean bylines. Check which word is the surname.
  <div class="row"><button onclick="pick(${i});famFirst()">Family name first<kbd>F</kbd></button>${edit}</div></div>`;
 return `<div class="row" style="margin-top:6px">${edit.replace('<button','<button class="linkbtn"')}</div>`}
function famFirst(){const r=cur();if(!r||!open_(r)||!lastFirst(r))return;
 if(r.split){setSplit(null);return}
 setSplit(r.names.map(n=>{const w=n.trim().split(/\s+/);return [w[0],w.slice(1).join(' ')]}))}
function saveSplit(){const r=cur();if(!r)return;const g=r.names.map(()=>['','']);
 document.querySelectorAll('.sp').forEach(x=>g[+x.dataset.j][+x.dataset.k]=x.value.trim());setSplit(g)}
async function setSplit(split){const r=cur();if(!r||busy)return;busy=true;
 const res=await post('/api/preview',{pageid:A.pageid,citation_id:r.citation_id,split});busy=false;
 if(!res.ok){toast(res.message,'err');renderArticle();return}
 A.rows[sel]=res.row;splitEdit=null;renderArticle();
 toast(split?'Name split changed: '+res.row.params.map(([k,v])=>`|${k}=${v}`).join(' '):'Back to the proposed split')}
/* the chips appear where the Reject button was, so the second click of a double-click lands on one: ignore it */
function chipClick(e,k){if(e.detail>1||Date.now()-rejectAt<400)return;reasonClick(k)}
function reasonClick(k){if(k=='5'&&!($('note').value||'').trim()){$('note').focus();return}reject(k)}
function renderArticle(scroll){const el=$('art');if(view!='review')return;
 if(!A||(loading&&!A.rows)){el.innerHTML='<div class="loading">Loading the live Wikipedia page…</div>';return}
 if(A.end&&!loading){const s=ST&&ST.stats,left=s?s.remaining:0,q=ST?ST.tray.filter(t=>t.status=='queued').length:0;
  el.innerHTML=`<div class="endcard"><h2>${left?'End of the queue':'All done'}</h2>
  <p class="mut">${s?`${s.decided} of ${s.total} reviewed so far: ${s.approved} approved, ${s.rejected} rejected (${s.session} this session).`:''}</p>
  ${left?`<p>${plural(left,'citation')} ${left==1?'is':'are'} still undecided (skipped or passed over).</p><button class="btn" onclick="skipped.clear();load({pos:0})">Go to the first undecided citation</button>`:''}
  ${q?`<p>${plural(q,'edit')} ${q==1?'is':'are'} ready to send in the tray.</p>`:''}
  ${pos>0?`<p><button class="btn quiet" onclick="prevArticle()">← Back to the last article</button></p>`:''}</div>`;return}
 const pend=A.rows.filter(r=>r.status=='pending').length;
 let h=`<div class="ahead"><div class="aline"><span>Article ${A.pos+1} of ${A.articles}</span><span class="grow"></span><span class="nav">
  <button onclick="prevArticle()" ${pos<=0?'disabled':''}>← Prev<kbd>P</kbd></button> <button onclick="nextArticle()">Next<kbd>N</kbd> →</button></span></div>
  <div class="hrow">${A.thumb?`<img class="thumb" src="${esc(A.thumb)}" alt="">`:`<span class="thumb none">${esc(A.title.charAt(0))}</span>`}<div>
  <h1><a href="https://en.wikipedia.org/?curid=${A.pageid}" target="_blank" rel="noopener" title="Open the Wikipedia article">${esc(A.title)}</a></h1>
  ${A.description?`<div class="desc">${esc(cap(A.description))}</div>`:''}</div></div>
  <div class="sub">${pend?plural(pend,'citation')+' to review':'Nothing left to review here'}${A.rows.length>1?' · one edit for the whole article':''} · ${compact(A.views)} views/month${loading?' · <i>loading…</i>':''}</div>`;
 if(A.error){h+=A.error_kind=='blocked'?`<div class="banner warn">This article can't be edited by the tool: ${esc(A.error)}. Its citations stay in the queue. <span class="grow"></span><button onclick="nextArticle()">Next article</button></div>`
  :`<div class="banner bad"><span>Couldn't load the live Wikipedia page, so the edit can't be previewed yet.<br><small>${esc(A.error.slice(0,160))}</small></span><span class="grow"></span><button onclick="retry()">Retry</button></div>`}
 el.innerHTML=h+'</div>'+A.rows.map(card).join('');entering=false;
 if(scroll){const c=el.querySelector('.card.focus');if(c)c.scrollIntoView({block:'nearest',behavior:'smooth'})}
 if(rejecting)el.querySelector('.reasons')?.scrollIntoView({block:'nearest'})}
document.addEventListener('click',e=>{const c=e.target.closest('.card[data-i]');if(!c||e.target.closest('button,a,input,pre'))return;
 const i=+c.dataset.i;if(i!=sel){sel=i;rejecting=false;renderArticle()}});

/* ---------- tray and progress ---------- */
const BADGE={'queued':['Ready',''],'sending':['Sending…','warn'],'dry-run ok':['Dry run OK','ok'],'success-verified':['Saved ✓','ok'],
 'success-reconciled':['Saved','ok'],'success-unverified':['Saved (unverified)','warn'],'success-NOT-VERIFIED':['Saved — check it','warn'],
 'nochange':['No change','warn'],'refused':['Not sent','bad'],'failed':['Failed','bad'],'error':['Error','bad'],
 'attempt':['May be saved: check it','warn']};
function badge(s){const [t,k]=BADGE[s]||[s,''];return `<span class="badge ${k}">${esc(t)}</span>`}
function renderProgress(){if(!ST)return;const s=ST.stats,pct=s.total?100*s.decided/s.total:0;$('bar').style.width=pct+'%';
 const pt=$('progtxt');pt.innerHTML=`${s.decided} of ${s.total} reviewed · ${s.remaining} left<span class="more"> · ${s.approved} approved · ${s.rejected} rejected${s.session?` · ${s.session} this session`:''}</span>`;
 pt.title=pt.textContent+'\n'+(LIVE?'All live decisions so far, across sessions':'All live decisions so far, plus this dry run\'s practice decisions');
 $('undoBtn').disabled=!ST.can_undo;
 const sb=$('sportsBtn');sb.hidden=!s.sports_queued;sb.textContent=s.hide_sports?`Show sports (${s.sports_hidden} hidden)`:'Hide sports'}
function renderTray(){const r=ST,q=r.tray.filter(t=>t.status=='queued'),other=r.tray.filter(t=>t.status!='queued');
 const nq=q.reduce((n,t)=>n+t.citations.length,0),confirming=LIVE&&Date.now()-confirmAt<4000;
 let h=`<h2>Ready to send <span class="count">${plural(q.length,'edit')}</span></h2>
  <div class="hint">${LIVE?'Approved citations wait here, one edit per article, until you post them.':'Dry run: sending re-checks each edit against the live page and posts nothing.'}</div>`;
 h+=q.length?`<ul class="tl">${q.map(t=>`<li><div class="t"><span>${esc(t.title)}</span><button class="linkbtn" onclick="returnEntry(${t.id})" title="Withdraw the approval and review again">Return to queue</button></div>
  <div class="c">${t.citations.map(esc).join('<br>')}</div>${t.summary?`<div class="s"><span class="lbl">Edit summary</span>${esc(t.summary)}</div>`:''}${t.detail?`<div class="d">${esc(t.detail)}</div>`:''}</li>`).join('')}</ul>`:'<div class="hint">Nothing yet. Approved citations appear here.</div>';
 const label=!q.length?'Nothing to send':confirming?`Click again to post ${plural(q.length,'live edit')}`:LIVE?`Post ${plural(q.length,'edit')} to Wikipedia`:`Dry-run ${plural(q.length,'edit')}`;
 h+=`<button class="btn sendbtn ${LIVE?'live':''} ${confirming?'confirm':''}" onclick="sendClick()" ${!q.length||r.sending?'disabled':''}>${r.sending?'Sending…':label}</button>`;
 h+=`<div class="capline">${r.sending&&r.next_in!=null?`Next edit in ${r.next_in}s · `:''}${LIVE?`Live edits sent: ${r.live_sent}${r.max_live!=null?` of ${r.max_live} allowed`:''} · code ${esc(r.version.slice(0,7))}${r.version.endsWith('-dirty')?' (modified)':''}`:`${plural(nq,'citation')} in the tray · nothing is posted`}</div>`;
 if(other.length)h+=`<h3>${LIVE?'Results this session':'Dry-run results'}</h3><ul class="tl">${other.slice().reverse().map(t=>`<li><div class="t"><span>${esc(t.title)}</span>${badge(t.status)}</div>
  <div class="c">${t.citations.map(esc).join('<br>')}</div>${t.detail?`<div class="d">${esc(t.detail)}</div>`:''}
  ${t.revid?`<a class="linkbtn" href="https://en.wikipedia.org/w/index.php?diff=${t.revid}" target="_blank" rel="noopener">View diff</a>`:''}
  ${t.returnable?`<button class="linkbtn" onclick="returnEntry(${t.id})" title="Review these citations again">Return to queue</button>`:''}</li>`).join('')}</ul>`;
 h+=`<p><button class="linkbtn" onclick="setView('history')">Full history of sent and rejected citations →</button></p>`;
 if(r.log.length)h+=`<details class="log"><summary>Send log</summary><div>${r.log.map(esc).join('<br>')}</div></details>`;
 $('tray').innerHTML=h}
let statusTimer=0;
async function refreshStatus(){const was=ST&&ST.sending;try{ST=await (await fetch('/api/status')).json()}catch(e){return}renderProgress();renderTray();
 if(view=='history'&&(ST.sending||was))loadHistory();
 if(A&&A.end)renderArticle();clearTimeout(statusTimer);if(ST.sending)statusTimer=setTimeout(refreshStatus,1000)}
async function sendClick(){if(LIVE&&Date.now()-confirmAt>=4000){confirmAt=Date.now();renderTray();setTimeout(renderTray,4100);return}
 confirmAt=0;const r=await post('/api/send');if(!r.ok)toast(r.message,'err');refreshStatus()}

/* ---------- history ---------- */
let view='review',H=null,hFilter='all',hQuery='';
const WHERE={sent:['Sent','ok'],tray:['In the tray',''],unsent:['Approved, not sent','warn'],unclear:['Send interrupted','warn'],rejected:['Rejected','bad']};
function setView(v){view=v;$('tabReview').classList.toggle('on',v=='review');$('tabHistory').classList.toggle('on',v=='history');
 window.scrollTo(0,0);if(v=='history'){H=null;renderHistory();loadHistory()}else renderArticle()}
async function loadHistory(){try{H=await (await fetch('/api/history')).json()}catch(e){H={rows:[],error:e.message}}if(view=='history')renderHistory()}
function when(iso){if(!iso)return'';const d=new Date(iso);return d.toLocaleDateString(undefined,{day:'numeric',month:'short'})+' '+d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}
function renderHistory(){const el=$('art');
 if(!H){el.innerHTML='<div class="loading">Loading history…</div>';return}
 const n=k=>H.rows.filter(r=>k=='all'||r.where==k).length;
 const q=hQuery.trim().toLowerCase();
 const rows=H.rows.filter(r=>(hFilter=='all'||r.where==hFilter)&&(!q||(r.title+' '+r.names.join(' ')+' '+r.reason+' '+r.note).toLowerCase().includes(q)));
 let h=`<div class="ahead"><h1>History</h1><div class="sub">Every live decision, newest first. ${H.live?'Rejected citations, and approvals that were never sent, can go back to the queue.':'Read-only in a dry run: these are the live decisions.'}</div></div>
 <div class="filters">${[['all','All'],['sent','Sent'],['tray','In the tray'],['unsent','Not sent'],['unclear','Send interrupted'],['rejected','Rejected']].filter(([k])=>k!='unclear'||n(k)).map(([k,t])=>
  `<button class="chip ${hFilter==k?'on':''}" onclick="hFilter='${k}';renderHistory()">${t} ${n(k)}</button>`).join('')}
  <input id="hsearch" placeholder="Search article, author or reason" value="${esc(hQuery)}" oninput="hQuery=this.value;renderHistory();const i=$('hsearch');i.focus();i.setSelectionRange(i.value.length,i.value.length)"></div>`;
 if(!rows.length){el.innerHTML=h+`<div class="endcard"><p class="mut">${H.rows.length?'Nothing matches.':'No live decisions yet.'}</p></div>`;return}
 h+=`<table class="hist"><thead><tr><th>Decided</th><th>Article</th><th>Author</th><th>Outcome</th><th></th></tr></thead><tbody>`+rows.map(r=>{
  const [t,k]=WHERE[r.where]||[r.where,''];
  const why=r.where=='rejected'?cap(r.reason||'')+(r.note?` — ${r.note}`:''):r.where=='sent'?'':r.why;
  return `<tr><td class="when">${when(r.decided_at)}</td>
  <td><a href="https://en.wikipedia.org/?curid=${r.pageid}" target="_blank" rel="noopener">${esc(r.title)}</a></td>
  <td>${esc(r.names.join(', '))}</td>
  <td><span class="badge ${k}" ${r.sent_at?`title="Saved ${when(r.sent_at)}"`:''}>${t}</span>${why?`<div class="why">${esc(why)}</div>`:''}</td>
  <td class="act">${r.revid?`<a class="linkbtn" href="https://en.wikipedia.org/w/index.php?diff=${r.revid}" target="_blank" rel="noopener">View diff</a>`:''}
  ${r.tray_id?`<button class="linkbtn" onclick="returnEntry(${r.tray_id},true)">Return to queue</button>`:''}
  ${r.requeue?`<button class="linkbtn" onclick="requeue(${r.decision_id})">Return to queue</button>`:''}
  <a class="linkbtn" href="${esc(r.url)}" target="_blank" rel="noopener">Source</a></td></tr>`}).join('')+'</tbody></table>';
 el.innerHTML=h}
async function requeue(id){const r=await post('/api/requeue',{decision_id:id});if(!r.ok){toast(r.message,'err');return}
 toast(`Back in the queue: ${r.title}`);refreshStatus();loadHistory()}

/* ---------- keyboard ---------- */
function toggleHelp(){$('help').classList.toggle('show')}
document.addEventListener('keydown',e=>{
 if(e.metaKey||e.ctrlKey||e.altKey)return;const k=e.key;
 if(e.target.matches('input,textarea')){if(k=='Escape'){e.target.blur();cancelReject()}
  else if(k=='Enter'&&e.target.id=='note'){e.preventDefault();reasonClick('5')}
  else if(k=='Enter'&&e.target.classList.contains('sp')){e.preventDefault();saveSplit()}
  if(k=='Escape'&&splitEdit){splitEdit=null;renderArticle()}return}
 const isHelp=k=='?'||(k=='/'&&e.shiftKey);
 if($('help').classList.contains('show')){if(k=='Escape'||isHelp)toggleHelp();return}
 if(isHelp){toggleHelp();return}
 if(k=='h'||k=='H'){e.preventDefault();setView(view=='review'?'history':'review');return}
 if(view!='review'){if(k=='Escape')setView('review');return}
 if(rejecting){if(REASONS[k]){e.preventDefault();reasonClick(k)}else if(k=='Escape'||k=='r'||k=='x')cancelReject();return}
 const act={f:famFirst,a:approve,r:startReject,x:startReject,s:skip,u:undo,o:openSource,d:toggleDetails,
  j:()=>moveSel(1),ArrowDown:()=>moveSel(1),k:()=>moveSel(-1),ArrowUp:()=>moveSel(-1),
  n:nextArticle,ArrowRight:nextArticle,p:prevArticle,ArrowLeft:prevArticle}[k.length==1?k.toLowerCase():k];
 if(act){e.preventDefault();act()}});
const m=$('mode');if(LIVE){m.classList.add('live');m.title='Sent edits are posted to Wikipedia'}else m.title='Nothing is posted to Wikipedia';
post('/api/filter',{hide_sports:hideSports}).then(()=>{load({pos:0});refreshStatus()});</script></body></html>"""
