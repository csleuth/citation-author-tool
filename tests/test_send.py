"""Send guards, with a fake Wikipedia and a temporary database (no network, no credentials)."""
import json
import pytest
import refs.db as db
from conftest import FakeHTTP, Reply
from refs import config, wiki

CITE = "{{cite news |url=http://x |title=T |work=W}}"
TEXT = f"Alpha.<ref>{CITE}</ref>"
ITEMS = [dict(citation_id=1, raw=CITE, names=["Jane Smith"])]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    page = dict(pageid=7, title="Alpha", ns=0, redirect=False, revid=100, text=TEXT, protection=[])
    monkeypatch.setattr(wiki, "page_live", lambda pid: dict(page))
    return page


class FakeSession:
    def __init__(self, page, fail=False, apply=True):
        self.page, self.fail, self.apply, self.posts = page, fail, apply, []

    def can_edit(self, pid):
        return True

    def post(self, body):
        self.posts.append(body)
        if self.apply:
            self.page.update(revid=101, text=body["text"])
        if self.fail:
            raise RuntimeError("connection reset")
        return {"edit": {"result": "Success", "newrevid": 101}}


def test_dry_run_never_posts(env):
    plan = wiki.build_plan(7, ITEMS)
    s = FakeSession(env)
    status, _ = wiki.send(plan, ITEMS, sess=s, dry_run=True)
    assert status == "dry-run ok" and s.posts == []


def test_refuses_when_page_changed(env, monkeypatch):
    plan = wiki.build_plan(7, ITEMS)
    monkeypatch.setattr(wiki, "page_live", lambda pid: dict(env, revid=105))
    status, detail = wiki.send(plan, ITEMS, sess=FakeSession(env), dry_run=False)
    assert status == "refused" and "changed since approval" in detail


def test_refuses_when_rebuilt_text_differs(env):
    plan = wiki.build_plan(7, ITEMS)
    plan["new_text"] += "x"
    status, _ = wiki.send(plan, ITEMS, sess=FakeSession(env), dry_run=False)
    assert status == "refused"


@pytest.mark.parametrize("level", ["sysop", "templateeditor"])
def test_refuses_full_and_template_protection(env, monkeypatch, level):
    monkeypatch.setattr(wiki, "page_live", lambda pid: dict(env, protection=[{"type": "edit", "level": level}]))
    with pytest.raises(wiki.Refused):
        wiki.build_plan(7, ITEMS)


@pytest.mark.parametrize("level", ["autoconfirmed", "extendedconfirmed"])
def test_semi_and_extended_confirmed_protection_are_fine(env, monkeypatch, level):
    monkeypatch.setattr(wiki, "page_live", lambda pid: dict(env, protection=[{"type": "edit", "level": level}]))
    assert wiki.build_plan(7, ITEMS)["citation_ids"] == [1]


def test_live_success_verified_and_logged(env):
    plan = wiki.build_plan(7, ITEMS)
    s = FakeSession(env)
    status, _ = wiki.send(plan, ITEMS, sess=s, dry_run=False)
    assert status == "success-verified" and len(s.posts) == 1
    p = s.posts[0]
    assert p["baserevid"] == 100 and p["nocreate"] == 1 and p["minor"] == 1 and "notminor" not in p and "|last=Smith |first=Jane" in p["text"]
    con = db.connect()
    assert [r["status"] for r in con.execute("SELECT status FROM edits")] == ["success-verified"]


def test_lost_response_reconciled(env):
    plan = wiki.build_plan(7, ITEMS)
    status, _ = wiki.send(plan, ITEMS, sess=FakeSession(env, fail=True), dry_run=False)
    assert status == "success-reconciled"


def test_lost_response_not_saved_is_error(env):
    plan = wiki.build_plan(7, ITEMS)
    status, _ = wiki.send(plan, ITEMS, sess=FakeSession(env, fail=True, apply=False), dry_run=False)
    assert status == "error"


def test_live_cap(env):
    plan = wiki.build_plan(7, ITEMS)
    con = db.connect()
    for _ in range(2):
        con.execute("INSERT INTO edits(pageid, status, dry_run) VALUES(1, 'success-verified', 0)")
    con.commit()
    s = FakeSession(env)
    status, detail = wiki.send(plan, ITEMS, sess=s, dry_run=False, max_live=2)
    assert status == "refused" and "cap" in detail and s.posts == []


def test_log_records_code_commit(env):
    plan = wiki.build_plan(7, ITEMS)
    wiki.send(plan, ITEMS, sess=FakeSession(env), dry_run=False)
    con = db.connect()
    details = [json.loads(r["detail"]) for r in con.execute("SELECT detail FROM edits")]
    assert details and all(d["commit"] == wiki.code_version() for d in details)


def test_no_cap_by_default(env):
    plan = wiki.build_plan(7, ITEMS)
    con = db.connect()
    for _ in range(30):
        con.execute("INSERT INTO edits(pageid, status, dry_run) VALUES(1, 'success-verified', 0)")
    con.commit()
    status, _ = wiki.send(plan, ITEMS, sess=FakeSession(env), dry_run=False)
    assert status == "success-verified"


PAGE = {"query": {"pages": [{"pageid": 7, "title": "Alpha", "ns": 0, "revisions": [{"revid": 100, "slots": {"main": {"content": "x"}}}]}]}}


def test_page_read_retries_a_bad_reply(http):
    fake = http(Reply(), Reply(429, PAGE), Reply(200, PAGE))
    assert wiki.page_live(7)["revid"] == 100 and len(fake.calls) == 3


def test_page_read_gives_a_clear_error_after_three_tries(http):
    http(Reply(), Reply(), Reply(200, {"error": {"code": "maxlag"}}))
    with pytest.raises(config.WikiUnavailable, match="didn't answer properly \\(maxlag, 3 tries\\)"):
        wiki.page_live(7)


def test_send_refuses_cleanly_when_wikipedia_does_not_answer(env, monkeypatch):
    plan = wiki.build_plan(7, ITEMS)
    def down(pid):
        raise config.WikiUnavailable("Wikipedia didn't answer properly (HTTP 503, 3 tries); try again in a minute")
    monkeypatch.setattr(wiki, "page_live", down)
    s = FakeSession(env)
    status, detail = wiki.send(plan, ITEMS, sess=s, dry_run=False)
    assert status == "refused" and "didn't answer properly" in detail and s.posts == []


def rows():
    return [(r["status"], json.loads(r["detail"])) for r in db.connect().execute("SELECT status, detail FROM edits ORDER BY id")]


class RightsCheckFails(FakeSession):
    def __init__(self, page, error):
        super().__init__(page)
        self.error = error

    def can_edit(self, pid):
        raise self.error


@pytest.mark.parametrize("error, says", [
    (config.WikiUnavailable("Wikipedia didn't answer properly (HTTP 503, 3 tries)"), "couldn't check the account's edit rights"),
    (wiki.Refused("your Wikipedia login has expired; nothing was sent"), "login has expired")])
def test_failed_rights_check_is_a_refusal_not_a_crash(env, error, says):
    # it used to escape send(), leaving the tray entry at "Sending..." until a restart
    plan = wiki.build_plan(7, ITEMS)
    s = RightsCheckFails(env, error)
    status, detail = wiki.send(plan, ITEMS, sess=s, dry_run=False)
    assert status == "refused" and says in detail and s.posts == []
    assert rows()[-1][0] == "refused" and rows()[-1][1]["title"] == "Alpha"


def test_expired_login_is_reported_as_such(http):
    s = object.__new__(wiki.Session)
    s.s = FakeHTTP([Reply(200, {"error": {"code": "assertuserfailed", "info": "You are no longer logged in."}})] * 2)
    with pytest.raises(wiki.Refused, match="login has expired"):
        s.can_edit(7)
    assert s.logged_in() is False
    assert s.s.calls[0][2]["params"]["assert"] == "user"


def test_lost_reply_and_failed_reread_stays_an_attempt(env, monkeypatch):
    plan = wiki.build_plan(7, ITEMS)
    reads = iter([dict(env)])  # the pre-send check reads the page; the re-read after the lost reply fails
    def page(pid):
        try:
            return next(reads)
        except StopIteration:
            raise config.WikiUnavailable("Wikipedia didn't answer properly (HTTP 503, 3 tries)")
    monkeypatch.setattr(wiki, "page_live", page)
    status, detail = wiki.send(plan, ITEMS, sess=FakeSession(env, fail=True, apply=False), dry_run=False)
    assert status == "attempt" and "may be saved" in detail
    st, d = rows()[-1]
    assert st == "attempt" and d["reread"] == "failed" and d["title"] == "Alpha"
