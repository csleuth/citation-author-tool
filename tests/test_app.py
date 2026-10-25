"""Review app endpoints, with a fake Wikipedia, a temporary database and a hand-made queue (no network)."""
import copy
import pytest
import refs.db as db
from refs import app as review, wiki

CITES = {1: "{{cite news |url=http://a |title=A}}", 2: "{{cite news |url=http://b |title=B}}",
         3: "{{cite news |url=http://c |title=C}}"}
EV = dict(via="live", snapshot_url="http://a", headline="A", title_sim=1.0, agency="", notes="",
          authors=[dict(name="Jane Smith", role="author", visible_quote="By Jane Smith",
                        metadata_quote="jsonld.author[Person]: Jane Smith", vis_ok=True, meta_ok=True)], meta_authors=[])


def item(cid, name="Jane Smith"):
    return dict(citation_id=cid, raw=CITES[cid], names=[name], url=f"http://{cid}", cited="T", evidence=copy.deepcopy(EV))


def fake_build_plan(pageid, items):
    res = [dict(citation_id=i["citation_id"], status="ok", old=i["raw"], new=i["raw"].replace("|title", "|last=Smith |first=Jane |title"),
                params=[["last", "Smith"], ["first", "Jane"]]) for i in items]
    return dict(pageid=pageid, title="P", base_revid=1, new_text="x", results=res, citation_ids=[i["citation_id"] for i in items],
                new_templates=[r["new"] for r in res], summary="s", rules_version="v2")


@pytest.fixture(autouse=True)
def no_page_card(monkeypatch):
    monkeypatch.setattr(review, "page_card", lambda pageid: dict(thumb="", description="Test article"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(wiki, "build_plan", fake_build_plan)
    queue = [dict(pageid=10, title="Ten", views=9, items=[item(1), item(2)]), dict(pageid=20, title="Twenty", views=5, items=[item(3)])]
    return review.create_app(live=False, queue=queue).test_client()


def decisions():
    return [tuple(r) for r in db.connect().execute("SELECT citation_id, kind, label FROM decisions ORDER BY id")]


def test_article_view_shows_evidence(client):
    a = client.get("/api/article?pos=0").json
    assert a["title"] == "Ten" and [r["citation_id"] for r in a["rows"]] == [1, 2]
    r = a["rows"][0]
    assert r["quotes"] == ["By Jane Smith"] and r["meta"] == ["Jane Smith"] and r["params"] == [["last", "Smith"], ["first", "Jane"]]
    assert ["ins", "|last=Smith |first=Jane "] in r["diff"] and r["status"] == "pending"
    assert client.get("/api/article?pos=5").json["end"]


def test_approvals_merge_into_one_edit_per_article(client):
    assert client.post("/api/approve", json=dict(pageid=10, citation_id=1)).json["gone"] is False
    r = client.post("/api/approve", json=dict(pageid=10, citation_id=2)).json
    assert r["ok"] and r["gone"] is True and r["stats"]["approved"] == 2
    tray = client.get("/api/status").json["tray"]
    assert len(tray) == 1 and tray[0]["citations"] == ["Jane Smith", "Jane Smith"]
    assert decisions() == [(1, "dryrun", "approve"), (2, "dryrun", "approve")]  # a dry run never logs 'live' decisions


def test_reject_and_undo(client):
    client.post("/api/reject", json=dict(pageid=20, citation_id=3, reason="4"))
    assert decisions() == [(3, "dryrun", "reject")]
    assert client.get("/api/article?pos=1").json.get("end")  # article 20 left the queue
    r = client.post("/api/undo").json
    assert r["ok"] and r["pageid"] == 20 and decisions() == []
    assert client.get("/api/article?pageid=20").json["rows"][0]["status"] == "pending"
    assert not client.post("/api/undo").json["ok"]


def test_undo_approval_rebuilds_the_article_edit(client):
    client.post("/api/approve", json=dict(pageid=10, citation_id=1))
    client.post("/api/approve", json=dict(pageid=10, citation_id=2))
    client.post("/api/undo")
    tray = client.get("/api/status").json["tray"]
    assert len(tray) == 1 and len(tray[0]["citations"]) == 1
    a = client.get("/api/article?pageid=10").json
    assert [(r["citation_id"], r["status"]) for r in a["rows"]] == [(1, "approve"), (2, "pending")]


def test_return_to_queue_restores_citations_in_place(client):
    client.post("/api/approve", json=dict(pageid=10, citation_id=1))
    client.post("/api/approve", json=dict(pageid=10, citation_id=2))
    tid = client.get("/api/status").json["tray"][0]["id"]
    assert client.post("/api/return", json=dict(id=tid)).json["ok"]
    assert client.get("/api/status").json["tray"] == [] and decisions() == []
    a = client.get("/api/article?pos=0").json
    assert a["pageid"] == 10 and [r["status"] for r in a["rows"]] == ["pending", "pending"]


def test_cannot_undo_after_sending(client, monkeypatch):
    monkeypatch.setattr(wiki, "send", lambda plan, items, **kw: ("dry-run ok", "would edit"))
    client.post("/api/approve", json=dict(pageid=20, citation_id=3))
    assert client.post("/api/send").json["ok"]
    for _ in range(100):
        st = client.get("/api/status").json
        if not st["sending"]:
            break
    assert st["tray"][0]["status"] == "dry-run ok" and not st["tray"][0]["returnable"]
    r = client.post("/api/undo").json
    assert not r["ok"] and "already been sent" in r["message"]


def test_uneditable_citation_cannot_be_approved(client, monkeypatch):
    def plan(pageid, items):
        p = fake_build_plan(pageid, items)
        p["results"][-1] = dict(citation_id=items[-1]["citation_id"], status="skip", reason="template already has |author=", old="")
        p["citation_ids"] = p["citation_ids"][:-1]
        return p
    monkeypatch.setattr(wiki, "build_plan", plan)
    r = client.post("/api/approve", json=dict(pageid=20, citation_id=3)).json
    assert not r["ok"] and "already has |author=" in r["message"] and decisions() == []


def test_live_page_error_is_reported(client, monkeypatch):
    def boom(pageid, items):
        raise wiki.Refused("page has {{nobots}}")
    monkeypatch.setattr(wiki, "build_plan", boom)
    a = client.get("/api/article?pos=0").json
    assert a["error_kind"] == "blocked" and all(r["diff"] is None for r in a["rows"])


# ---------- checker agreement on full names ----------

@pytest.mark.parametrize("names, checked, ok", [
    (["Jane Smith"], ["Jane Smith"], True),
    (["Jane Smith"], ["JANE SMITH"], True),
    (["José Núñez"], ["Jose Nunez"], True),
    (["Jane A. Smith"], ["Jane A Smith"], True),
    (["Jane Smith", "Tom Lee"], ["Tom Lee", "Jane Smith"], True),
    (["Jane Smith"], ["John Smith"], False),          # same surname, different first name
    (["Jane Smith"], ["J. Smith"], False),
    (["Jane Smith"], ["Jane A. Smith"], False),
    (["Jane Smith"], [], False),                      # checker found no individual author
    (["Jane Smith"], None, False),                    # not checked
    (["Jane Smith", "Tom Lee"], ["Jane Smith"], False),
])
def test_checker_must_agree_on_full_names(names, checked, ok):
    assert review.checker_agrees(names, checked) is ok


# ---------- rebuilding the tray after a restart ----------

def seed_proposals(con, cids, pageid=10):
    con.execute("INSERT INTO articles(pageid, title, views) VALUES(?,?,?)", (pageid, "Ten", 9))
    for cid in cids:
        con.execute("INSERT INTO citations(id, pageid, raw, url, title) VALUES(?,?,?,?,?)", (cid, pageid, CITES[1].replace("http://a", f"http://{cid}"), f"http://{cid}", "T"))
        con.execute("INSERT INTO samples VALUES(?,?)", (review.GOLD, cid))
        con.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?)", (cid, review.QUEUE_RUN, review.RULES_VERSION, "HIGH", "", '["Jane Smith"]', "[]",
                                                                      review.json.dumps(EV)))


def live_decision(con, cid):
    return con.execute("INSERT INTO decisions(citation_id, run, kind, tier, rules_version, label) VALUES(?,?,?,?,?,?)",
                       (cid, review.QUEUE_RUN, "live", "HIGH", review.RULES_VERSION, "approve")).lastrowid


def test_unsent_approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(review, "checker_names", lambda: {1: ["Jane Smith"], 2: ["Jane Smith"], 3: ["Jane Smith"], 4: ["John Smith"]})
    con = db.connect()
    seed_proposals(con, [1, 2, 3, 4])
    ids = {cid: live_decision(con, cid) for cid in [1, 2, 3, 4, 5]}
    for cids, status in (("[1]", "refused"), ("[2]", "success-verified"), ("[3]", "attempt")):
        con.execute("INSERT INTO edits(pageid, citation_ids, status, dry_run) VALUES(10, ?, ?, 0)", (cids, status))
    con.execute("INSERT INTO edits(pageid, citation_ids, status, dry_run) VALUES(10, '[4]', 'success-verified', 1)")  # dry run: not sent
    con.commit()
    arts, dids, held = review.unsent_approvals(con)
    assert [[i["citation_id"] for i in a["items"]] for a in arts] == [[1]] and dids == {1: ids[1]}
    assert {cid for cid, _ in held} == {3, 4, 5}  # interrupted send, checker first name differs, no current proposal


def test_restart_restores_tray_and_return_to_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(wiki, "build_plan", fake_build_plan)
    con = db.connect()
    did = live_decision(con, 3)
    con.commit()
    art = dict(pageid=20, title="Twenty", views=5, items=[item(3)])
    queue = [dict(pageid=10, title="Ten", views=9, items=[item(1)])]
    c = review.create_app(live=True, queue=queue, restore=([art], {3: did}, [(7, "a send was interrupted")])).test_client()
    st = c.get("/api/status").json
    assert [(t["title"], t["status"]) for t in st["tray"]] == [("Twenty", "queued")]
    assert any("citation 7" in line for line in st["log"]) and st["version"]
    assert c.post("/api/return", json=dict(id=st["tray"][0]["id"])).json["ok"]
    assert decisions() == [] and [a["pageid"] for a in queue] == [10, 20]  # back in the queue, after the busier article


def test_restored_edit_that_no_longer_builds_is_held_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    def gone(pageid, items):
        raise wiki.Refused("page has {{nobots}}")
    monkeypatch.setattr(wiki, "build_plan", gone)
    art = dict(pageid=20, title="Twenty", views=5, items=[item(3)])
    c = review.create_app(live=True, queue=[], restore=([art], {3: 1}, [])).test_client()
    t = c.get("/api/status").json["tray"][0]
    assert t["status"] == "refused" and t["returnable"] and "nobots" in t["detail"]
    assert not c.post("/api/send").json["ok"]  # nothing queued to send


# ---------- history and overall progress ----------

def history_app(tmp_path, monkeypatch, live=True):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(wiki, "build_plan", fake_build_plan)
    monkeypatch.setattr(review, "checker_names", lambda: {1: ["Jane Smith"], 2: ["Jane Smith"]})
    con = db.connect()
    seed_proposals(con, [1, 2])
    rej = con.execute("INSERT INTO decisions(citation_id, run, kind, tier, rules_version, label, reason) VALUES(1,?,?,?,?,?,?)",
                      (review.QUEUE_RUN, "live", "HIGH", review.RULES_VERSION, "reject", "name formatting")).lastrowid
    app = live_decision(con, 2)
    con.execute("INSERT INTO edits(pageid, citation_ids, status, detail, dry_run) VALUES(10, '[2]', 'success-verified', '{\"newrevid\": 99}', 0)")
    con.commit()
    c = review.create_app(live=live, queue=[], restore=([], {}, []), eligible={1, 2}).test_client()
    return c, rej, app


def test_progress_counts_earlier_sessions(tmp_path, monkeypatch):
    c, _, _ = history_app(tmp_path, monkeypatch)
    s = c.get("/api/status").json["stats"]
    assert (s["decided"], s["total"], s["approved"], s["rejected"], s["session"]) == (2, 2, 1, 1, 0)


def test_history_lists_outcomes(tmp_path, monkeypatch):
    c, rej, app = history_app(tmp_path, monkeypatch)
    rows = {r["decision_id"]: r for r in c.get("/api/history").json["rows"]}
    assert rows[rej]["where"] == "rejected" and rows[rej]["reason"] == "name formatting" and rows[rej]["requeue"]
    assert rows[app]["where"] == "sent" and rows[app]["revid"] == 99 and not rows[app]["requeue"]


def test_requeue_rejected_citation(tmp_path, monkeypatch):
    c, rej, app = history_app(tmp_path, monkeypatch)
    assert c.post("/api/requeue", json=dict(decision_id=rej)).json["ok"]
    assert decisions() == [(2, "live", "approve")]
    a = c.get("/api/article?pageid=10").json
    assert [(r["citation_id"], r["status"]) for r in a["rows"]] == [(1, "pending")]
    assert c.get("/api/status").json["stats"]["remaining"] == 1
    r = c.post("/api/requeue", json=dict(decision_id=app)).json
    assert not r["ok"] and "saved on Wikipedia" in r["message"]


def test_requeue_needs_live_mode(tmp_path, monkeypatch):
    c, rej, _ = history_app(tmp_path, monkeypatch, live=False)
    assert not c.post("/api/requeue", json=dict(decision_id=rej)).json["ok"]
    assert not c.get("/api/history").json["rows"][0]["requeue"]


# ---------- family-name-first names ----------

@pytest.mark.parametrize("names, params, risky", [
    (["Choi Won-suk"], [["last", "Won-suk"], ["first", "Choi"]], True),
    (["Lee Nian Tjoe"], [["last", "Tjoe"], ["first", "Lee Nian"]], True),
    (["Jane Smith"], [["last", "Smith"], ["first", "Jane"]], False),
    (["Choi Won-suk"], [["author", "Choi Won-suk"]], False),  # |author= keeps the name whole
])
def test_family_first_risk(names, params, risky):
    assert review.family_first_risk(names, params) is risky


def split_plan(pageid, items):
    p = fake_build_plan(pageid, items)
    for r, i in zip(p["results"], items):
        last, first = i.get("split", [["Smith", "Jane"]])[0]
        r["params"] = [["last", last], ["first", first]]
    return p


def test_approve_with_chosen_split_is_kept_and_restored(client, monkeypatch):
    monkeypatch.setattr(wiki, "build_plan", split_plan)
    r = client.post("/api/preview", json=dict(pageid=20, citation_id=3, split=[["Smith", "Jane"]])).json
    assert r["ok"] and r["row"]["split"] == [["Smith", "Jane"]] and decisions() == []
    assert client.post("/api/approve", json=dict(pageid=20, citation_id=3, split=[["Smith", "Jane"]])).json["ok"]
    con = db.connect()
    assert review.json.loads(con.execute("SELECT params FROM decisions").fetchone()[0]) == [["Smith", "Jane"]]
    # after a restart the tray edit is rebuilt with the same split
    con.execute("UPDATE decisions SET kind='live'")
    con.commit()
    seed_proposals(con, [3], pageid=20)
    monkeypatch.setattr(review, "checker_names", lambda: {3: ["Jane Smith"]})
    arts, _, _ = review.unsent_approvals(con)
    assert arts[0]["items"][0]["split"] == [["Smith", "Jane"]]


def test_undo_split_approval_returns_plain_proposal(client):
    client.post("/api/approve", json=dict(pageid=20, citation_id=3, split=[["Smith", "Jane"]]))
    assert client.get("/api/status").json["tray"]
    assert client.post("/api/undo").json["ok"]
    assert client.get("/api/status").json["tray"] == []
    assert client.get("/api/article?pageid=20").json["rows"][0]["split"] is None


def test_split_shape_is_checked(client):
    r = client.post("/api/approve", json=dict(pageid=20, citation_id=3, split=[["Smith", "Jane"], ["X", "Y"]])).json
    assert not r["ok"] and decisions() == []


def test_hide_sports_skips_sports_articles_without_removing_them(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(wiki, "build_plan", fake_build_plan)
    queue = [dict(pageid=10, title="Ten", views=9, items=[item(1), item(2)]), dict(pageid=20, title="Twenty", views=5, items=[item(3)])]
    c = review.create_app(live=False, queue=queue, sports={10}).test_client()
    assert client_titles(c) == ["Ten", "Twenty"]
    s = c.post("/api/filter", json=dict(hide_sports=True)).json["stats"]
    assert (s["articles"], s["remaining"], s["sports_hidden"], s["sports_queued"]) == (1, 1, 1, 1)
    assert client_titles(c) == ["Twenty"] and c.get("/api/article?pageid=10").json["missing"]
    c.post("/api/approve", json=dict(pageid=20, citation_id=3))
    assert c.get("/api/article?pos=0").json["end"]
    s = c.post("/api/filter", json=dict(hide_sports=False)).json["stats"]
    assert client_titles(c) == ["Ten"] and s["remaining"] == 2 and s["sports_hidden"] == 0


def client_titles(c):
    out, pos = [], 0
    while not (a := c.get(f"/api/article?pos={pos}").json).get("end"):
        out.append(a["title"])
        pos += 1
    return out


def test_article_has_description_and_citation_preview(client):
    a = client.get("/api/article?pos=0").get_json()
    assert a["description"] == "Test article"
    assert a["rows"][0]["preview"] == dict(authors="Smith, Jane", date="", title="A", url="http://a", work="", retrieved="")


def test_cite_preview_formats_authors_and_work():
    p = review.cite_preview("{{cite news |last1=Smith |first1=Jane |last2=Doe |first2=John |author3=Ana Lima "
                            "|title=T |work=[[The Guardian]] |date=1 May 2020 |access-date=2 May 2020}}")
    assert p["authors"] == "Smith, Jane; Doe, John; Ana Lima" and p["work"] == "The Guardian" and p["retrieved"] == "2 May 2020"


def test_best_headline_prefers_the_page_headline_matching_the_cited_title(monkeypatch):
    monkeypatch.setattr(review, "page_headlines", lambda sha: ("The Theranos deception", "Other story"))
    assert review.best_headline("The Theranos deception", "The Theranos — Elizabeth Holmes Deception", 0.7, "sha") == \
        ("The Theranos deception", 1.0)
    # no stored page, or already a close match: the pipeline's headline stays
    assert review.best_headline("A", "B", 0.3, None) == ("B", 0.3)
    assert review.best_headline("Same", "Same", 1.0, "sha") == ("Same", 1.0)


def test_diff_and_removed_slots_show_what_the_edit_takes_out():
    old = "{{Cite web |last3= |first3= |date=D |title=T}}"
    new = "{{Cite web |last=Smith |first=Jane |date=D |title=T}}"
    assert review.removed_params(old, new) == ["last3", "first3"]
    assert review.param_diff(old, new) == [("", "{{Cite web "), ("del", "|last3= |first3= "), ("ins", "|last=Smith |first=Jane "),
                                          ("", "|date=D |title=T}}")]
    # filling a slot in place removes nothing
    assert review.removed_params("{{cite news |last= |first= |url=x}}", "{{cite news |last=Smith |first=Jane |url=x}}") == []
