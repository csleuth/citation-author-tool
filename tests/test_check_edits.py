"""Post-send check, offline: pure outcome logic plus the database read, with no network."""
import json
import refs.db as db
from refs import check_edits as ce

URL = "https://news.example.com/story"
CIT = dict(citation_id=1, url=URL, url_norm="news.example.com/story", names=["Jane Smith"])
EDIT = dict(edit_id=10, pageid=7, title="Alpha", newrevid=101, citations=[CIT])


def page(cite):
    return f"Alpha.<ref>{cite}</ref>"


def outcome(text, reverted=(), missing=()):
    texts = {} if text is None else {7: text}
    return ce.check([EDIT], set(reverted), set(missing), texts)[0]["outcome"]


def test_kept():
    assert outcome(page(f"{{{{cite news |last=Smith |first=Jane |url={URL} |title=T}}}}")) == "kept"


def test_kept_with_author_param_and_www_url():
    assert outcome(page("{{cite news |author=Jane Smith |url=https://www.news.example.com/story/ |title=T}}")) == "kept"


def test_changed_when_names_removed_or_replaced():
    assert outcome(page(f"{{{{cite news |url={URL} |title=T}}}}")) == "changed"
    assert outcome(page(f"{{{{cite news |last=Jones |first=Jane |url={URL} |title=T}}}}")) == "changed"


def test_gone_when_url_not_on_page():
    assert outcome(page("{{cite news |url=https://other.example.com/x |title=T}}")) == "gone"


def test_page_missing():
    assert outcome(None) == "page missing"


def test_reverted_tag_or_deleted_revision_wins():
    kept = page(f"{{{{cite news |last=Smith |first=Jane |url={URL} |title=T}}}}")
    assert outcome(kept, reverted={101}) == "reverted"
    assert outcome(kept, missing={101}) == "reverted"


def test_sent_edits_reads_names_and_skips_unsent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    con.execute("INSERT INTO citations(id, pageid, url, url_norm) VALUES(1, 7, ?, 'news.example.com/story')", (URL,))
    con.execute("INSERT INTO proposals(citation_id, run, names) VALUES(1, 'r1', ?)", (json.dumps(["Jane Smith"]),))
    con.execute("INSERT INTO decisions(citation_id, run, kind, label) VALUES(1, 'r1', 'live', 'approve')")
    for status, dry in (("success-verified", 0), ("refused", 0), ("dry-run built", 1)):
        con.execute("INSERT INTO edits(pageid, citation_ids, status, detail, dry_run) VALUES(7, '[1]', ?, ?, ?)",
                    (status, json.dumps({"newrevid": 101, "title": "Alpha"}), dry))
    con.commit()
    got = ce.sent_edits(con)
    assert len(got) == 1 and got[0]["newrevid"] == 101 and got[0]["citations"][0]["names"] == ["Jane Smith"]
