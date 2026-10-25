"""Gate 1 tooling: export checker cases, compare readings, build the review page, import labels, report."""
import csv, json, random, re
from collections import Counter
from datetime import datetime, timezone
from .config import DATA, RULES_VERSION
from .db import connect
from .read import fold

CHECK = DATA / "check"
LEAVE_TIERS = ("LEAVE", "WRONG_PAGE")


def clean_title(t):
    t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", t or "")
    return re.sub(r"''+|<.*?>|\{\{!\}\}", "", t).strip()


def export_cases(sample, run, n_review=50, n_leave=50, seed=7):
    """Gold set: every HIGH case plus random REVIEW and leave-unchanged cases. Writes cases_<sample>.jsonl for the checker
    (heldout1's is the older cases.jsonl); the checker appends every sample's labels to checker_labels.jsonl."""
    con = connect()
    gold = f"{sample}-gold"
    if con.execute("SELECT 1 FROM samples WHERE name=?", (gold,)).fetchone():
        raise SystemExit(f"{gold} already exists")
    p = {t: [r["citation_id"] for r in con.execute(
        "SELECT p.citation_id FROM proposals p JOIN samples s ON s.citation_id=p.citation_id AND s.name=? "
        "WHERE p.run=? AND p.tier IN (%s) ORDER BY p.citation_id" % ",".join("?" * len(ts)), (sample, run, *ts))]
         for t, ts in (("HIGH", ("HIGH",)), ("REVIEW", ("REVIEW",)), ("LEAVE", LEAVE_TIERS))}
    rng = random.Random(seed)
    ids = p["HIGH"] + rng.sample(p["REVIEW"], min(n_review, len(p["REVIEW"]))) + rng.sample(p["LEAVE"], min(n_leave, len(p["LEAVE"])))
    con.executemany("INSERT INTO samples VALUES(?,?)", [(gold, i) for i in ids])
    con.commit()
    CHECK.mkdir(parents=True, exist_ok=True)
    out = CHECK / f"cases_{sample}.jsonl"
    with open(out, "w") as f:
        for i in ids:
            r = con.execute("""SELECT c.id, c.title, c.url, s.html_sha, s.snapshot_url FROM citations c
                               JOIN sources s ON s.url_norm=c.url_norm AND s.run=? WHERE c.id=?""", (run, i)).fetchone()
            f.write(json.dumps({"case_id": f"{sample}-{i}", "cited_title": clean_title(r["title"]), "url": r["url"],
                                "snapshot_path": f"data/html/{r['html_sha']}.html.gz", "snapshot_url": r["snapshot_url"]},
                               ensure_ascii=False) + "\n")
    print(f"gold set {gold}: {len(p['HIGH'])} HIGH, {min(n_review, len(p['REVIEW']))} REVIEW, "
          f"{min(n_leave, len(p['LEAVE']))} leave-unchanged -> {out}")


def _surnames(names):
    return sorted(fold(re.sub(r"[^\w\s'’-]", " ", n)).split()[-1] for n in names if n.strip())


def compare(sample, run, n_agree=35, seed=11):
    con = connect()
    labels = {}
    for line in open(CHECK / "checker_labels.jsonl"):
        x = json.loads(line)
        labels[int(x["case_id"].rsplit("-", 1)[1])] = x
    rows = []
    for r in con.execute("""SELECT c.id, c.title, c.url, c.archive_url, p.tier, p.reason, p.names, p.params, p.evidence
                            FROM proposals p JOIN citations c ON c.id=p.citation_id
                            JOIN samples s ON s.citation_id=c.id AND s.name=? WHERE p.run=?""", (f"{sample}-gold", run)):
        k = labels.get(r["id"])
        if k is None:
            continue
        names = json.loads(r["names"])
        ck_names = [a["name"] for a in k["authors"]] if k["is_cited_article"] == "yes" else []
        if names:  # pipeline proposes an author
            agree = k["is_cited_article"] == "yes" and _surnames(names) == _surnames(ck_names)
        else:      # pipeline leaves the citation unchanged
            agree = not ck_names
        rows.append(dict(id=r["id"], tier=r["tier"], agree=agree, row=dict(r), checker=k))
    rng = random.Random(seed)
    dis = [x for x in rows if not x["agree"]]
    high_agree = [x for x in rows if x["agree"] and x["tier"] == "HIGH"]
    show = dis + rng.sample(high_agree, min(n_agree, len(high_agree)))
    by = Counter((x["tier"] if x["tier"] in ("HIGH", "REVIEW") else "LEAVE", x["agree"]) for x in rows)
    print(f"compared {len(rows)} cases: " + ", ".join(f"{t} {'agree' if a else 'DISAGREE'} {n}" for (t, a), n in sorted(by.items())))
    _write_page(show, CHECK / "review.html")
    print(f"review page: {len(show)} rows ({len(dis)} disagreements + {len(show) - len(dis)} random HIGH agreements) -> {CHECK / 'review.html'}")


def _write_page(items, path):
    data = []
    for x in items:
        r, k = x["row"], x["checker"]
        ev = json.loads(r["evidence"])
        live = r["url"]
        archive = ev.get("snapshot_url") if ev.get("via") == "wayback" else (r["archive_url"] or "https://web.archive.org/web/2/" + live)
        data.append(dict(
            case=r["id"], tier=r["tier"], agree=x["agree"], cited=clean_title(r["title"]), headline=ev.get("headline", ""),
            pipeline=json.loads(r["names"]), params=" ".join(f"|{a}={b}" for a, b in json.loads(r["params"])),
            pipe_quotes=[(a["name"], a["visible_quote"], a["metadata_quote"]) for a in ev.get("authors", []) if a["role"] == "author"],
            pipe_note=(r["reason"] or "") + (f" [{ev.get('byline_kind')}{'; agency ' + ev['agency'] if ev.get('agency') else ''}]" if ev else ""),
            checker_article=k["is_cited_article"], checker=[(a["name"], a.get("quote", "")) for a in k["authors"]],
            checker_reason=k.get("no_author_reason", ""), checker_note=k.get("notes", ""), live=live, archive=archive))
    page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    path.write_text(page)


def import_labels(csv_path, run):
    con = connect()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for row in csv.DictReader(open(csv_path)):
        if not row.get("pipeline"):
            continue
        cid = int(row["case"])
        tier = con.execute("SELECT tier FROM proposals WHERE citation_id=? AND run=?", (cid, run)).fetchone()["tier"]
        con.execute("DELETE FROM decisions WHERE citation_id=? AND run=? AND kind='gate1'", (cid, run))
        con.execute("INSERT INTO decisions(citation_id, run, kind, tier, rules_version, label, checker_label, note, reviewer, decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", (cid, run, "gate1", tier, RULES_VERSION, row["pipeline"], row.get("checker") or None,
                                                    row.get("note", ""), "user", now))
        n += 1
    con.commit()
    print(f"imported {n} gate-1 labels")


def stability(sample, run1, run2):
    """v2 criterion: measured only on cases reachable in both runs (>= 97% same tier, no HIGH name changes)."""
    con = connect()
    q = "SELECT citation_id, tier, names FROM proposals WHERE run=? AND citation_id IN (SELECT citation_id FROM samples WHERE name=?)"
    a = {r[0]: (r[1], json.loads(r[2])) for r in con.execute(q, (run1, sample))}
    b = {r[0]: (r[1], json.loads(r[2])) for r in con.execute(q, (run2, sample))}
    allc = a.keys() & b.keys()
    both = [i for i in allc if "UNREACHABLE" not in (a[i][0], b[i][0])]
    same = sum(a[i][0] == b[i][0] for i in both)
    name_changes = [i for i in both if a[i][0] == b[i][0] == "HIGH" and _surnames(a[i][1]) != _surnames(b[i][1])]
    print(f"stability {run1} vs {run2}: {len(allc)} cases, {len(both)} reachable in both; same tier {same} "
          f"({same / max(1, len(both)):.1%}); HIGH name changes {len(name_changes)}")
    print("  tier moves among reachable:", Counter((a[i][0], b[i][0]) for i in both if a[i][0] != b[i][0]).most_common())
    ok = same / max(1, len(both)) >= 0.97 and not name_changes
    print("  criterion (reachable in both: >=97% same tier, no HIGH name changes):", "PASS" if ok else "FAIL")
    return ok


def report(sample, run):
    con = connect()
    rows = con.execute("SELECT d.label, d.checker_label, d.tier FROM decisions d WHERE d.kind='gate1' AND d.run=? "
                       "AND d.citation_id IN (SELECT citation_id FROM samples WHERE name=?)", (run, f"{sample}-gold")).fetchall()
    c = Counter((r["tier"], r["label"]) for r in rows)
    print("pipeline verdicts by tier:", dict(c))
    ck = Counter(r["checker_label"] for r in rows if r["checker_label"])
    print("checker verdicts on disagreements:", dict(ck))
    high_err = c[("HIGH", "wrong")]
    print(f"gate 1 high-tier errors: {high_err} ({'PASS' if high_err <= 1 else 'FAIL'} on the <=1 bar; stability checked separately)")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gate 1 review</title><style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e2e1dc;--acc:#2b5fd9;--ok:#1d7a3e;--bad:#b3261e;--hi:#fff3b0}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#ececea;--mut:#a0a09a;--card:#222225;--line:#38383c;--acc:#7aa2ff;--ok:#5cc583;--bad:#ff8a80;--hi:#5a4d00}}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0;padding:16px}
.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:12px 0;max-width:980px}
.card.sel{outline:2px solid var(--acc)} .mut{color:var(--mut)} .tag{font-size:12px;padding:1px 6px;border-radius:4px;border:1px solid var(--line);margin-right:6px}
.dis{color:var(--bad);border-color:var(--bad)} .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
@media (max-width:700px){.grid{grid-template-columns:1fr}} q{background:var(--hi)} .lab b{color:var(--ok)} .lab b.w{color:var(--bad)}
input[type=text]{width:100%;box-sizing:border-box;padding:4px;background:var(--bg);color:var(--fg);border:1px solid var(--line)} button{padding:4px 10px}
a{color:var(--acc)}</style></head><body>
<div class="bar"><b>Gate 1 review</b><span id="prog" class="mut"></span>
<span class="mut">Keys: j/k move · pipeline: c correct, w wrong, u unsure · checker (disagreements): 1 correct, 2 wrong, 3 unsure · n note</span>
<button onclick="dl()">Download labels.csv</button></div><div id="list"></div>
<script>
const D=__DATA__; let sel=0; const KEY='gate1-labels';
let L={}; try{L=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function quotes(a){return a.map(x=>`<div><b>${esc(x[0])}</b> — visible: <q>${esc(x[1])||'—'}</q>${x.length>2?` · metadata: <q>${esc(x[2])||'—'}</q>`:''}</div>`).join('')||'<div class="mut">no individual author</div>'}
function render(){document.getElementById('list').innerHTML=D.map((d,i)=>{const l=L[d.case]||{};return `<div class="card ${i==sel?'sel':''}" id="c${i}" onclick="sel=${i};render()">
<div><span class="tag">${d.tier}</span>${d.agree?'<span class="tag">agree</span>':'<span class="tag dis">DISAGREE</span>'}<span class="mut">case ${d.case}</span></div>
<div><span class="mut">Cited:</span> ${esc(d.cited)}</div><div><span class="mut">Page headline:</span> ${esc(d.headline)}</div>
<div><a href="${esc(d.live)}" target="_blank">live page</a> · <a href="${esc(d.archive)}" target="_blank">archived page</a></div>
<div class="grid"><div><b>Pipeline</b> ${d.params?'<code>'+esc(d.params)+'</code>':''}${quotes(d.pipe_quotes)}<div class="mut">${esc(d.pipe_note)}</div></div>
<div><b>Checker</b> <span class="mut">(article: ${esc(d.checker_article)}${d.checker_reason?'; '+esc(d.checker_reason):''})</span>${quotes(d.checker)}<div class="mut">${esc(d.checker_note)}</div></div></div>
<div class="lab">Pipeline: <b class="${l.pipeline=='wrong'?'w':''}">${l.pipeline||'—'}</b>${d.agree?'':` · Checker: <b class="${l.checker=='wrong'?'w':''}">${l.checker||'—'}</b>`}</div>
<input type="text" placeholder="note (n)" value="${esc(l.note||'')}" onchange="set(${i},'note',this.value)"></div>`}).join('');
document.getElementById('prog').textContent=`${D.filter(d=>(L[d.case]||{}).pipeline&&(d.agree||(L[d.case]||{}).checker)).length}/${D.length} done`;
const el=document.getElementById('c'+sel); if(el) el.scrollIntoView({block:'nearest'})}
function set(i,k,v){const c=D[i].case;L[c]=Object.assign(L[c]||{},{[k]:v});try{localStorage.setItem(KEY,JSON.stringify(L))}catch(e){};render()}
document.addEventListener('keydown',e=>{if(e.target.tagName=='INPUT'){if(e.key=='Escape')e.target.blur();return}
const m={c:'correct',w:'wrong',u:'unsure'},n={'1':'correct','2':'wrong','3':'unsure'};
if(e.key=='j'){sel=Math.min(D.length-1,sel+1);render()}else if(e.key=='k'){sel=Math.max(0,sel-1);render()}
else if(m[e.key])set(sel,'pipeline',m[e.key]);else if(n[e.key]&&!D[sel].agree)set(sel,'checker',n[e.key]);
else if(e.key=='n'){e.preventDefault();document.querySelector('#c'+sel+' input').focus()}});
function dl(){const rows=[['case','tier','agree','pipeline','checker','note']].concat(D.map(d=>{const l=L[d.case]||{};return [d.case,d.tier,d.agree,l.pipeline||'',l.checker||'',l.note||'']}));
const csv=rows.map(r=>r.map(x=>'"'+String(x).replace(/"/g,'""')+'"').join(',')).join('\n');
const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='labels.csv';a.click()}
render();</script></body></html>"""
