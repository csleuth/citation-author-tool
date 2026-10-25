"""Wikipedia login and guarded sending.

Dry run is the default everywhere: an edit is only POSTed when the caller passes dry_run=False,
which the review app does only when started with --live. Every edit:
  1. re-reads the live page and requires the revision it was approved against (base_revid);
  2. rebuilds the new text from that revision and requires it to equal the approved text;
  3. checks {{nobots}}, protection (full and template-editor protection are skipped) and the account's edit right;
  4. writes an 'attempt' row to the edit log BEFORE the POST;
  5. POSTs with baserevid, nocreate, assert=user, assertuser and maxlag, marked as a minor edit;
  6. on a lost response, re-reads the page and counts the edit as saved only if the change is there
     (if the page can't be re-read either, the log row stays an 'attempt': it may or may not be saved);
  7. reads the saved revision back and marks it verified only if every new citation is present.
"""
import json, time
from datetime import datetime, timezone
import requests
from .config import API, RULES_VERSION, USER_AGENT, ApiError, code_version, read_secret, redact, wiki_api
from .db import connect, dumps
from .edit import BLOCKING, Skip, apply_to_page, edit_summary
from .scan import NOBOTS


class Refused(Exception):
    """Nothing was sent."""


class Session:
    def __init__(self):
        user, password = read_secret("WIKI_BOT_USER"), read_secret("WIKI_BOT_PASSWORD")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        t = wiki_api(dict(action="query", meta="tokens", type="login", format="json", formatversion=2), session=self.s, gap=0)
        out = wiki_api(dict(action="login", lgname=user, lgpassword=password, lgtoken=t["query"]["tokens"]["logintoken"],
                            format="json", formatversion=2), post=True, session=self.s, gap=0)
        if out.get("login", {}).get("result") != "Success":
            raise Refused(redact(f"Wikipedia login failed: {out.get('login', {}).get('result')} {out.get('login', {}).get('reason', '')}"))
        self.user = out["login"]["lgusername"]
        ui = wiki_api(dict(action="query", meta="userinfo", uiprop="rights|groups", format="json", formatversion=2),
                      session=self.s, gap=0)["query"]["userinfo"]
        expected = user.split("@")[0]
        if ui.get("name") != expected:
            raise Refused(f"Logged in as '{ui.get('name')}' but WIKI_BOT_USER names '{expected}'; refusing to write.")
        self.rights, self.groups = set(ui.get("rights", [])), set(ui.get("groups", []))
        self.csrf = self._csrf()

    def _csrf(self):
        return self._query(meta="tokens", type="csrf")["query"]["tokens"]["csrftoken"]

    def post(self, body, tries=4):
        for i in range(tries):
            b = {"format": "json", "formatversion": 2, "maxlag": 5, "assert": "user", "assertuser": self.user, "token": self.csrf, **body}
            out = self.s.post(API, data=b, timeout=120).json()
            code = out.get("error", {}).get("code")
            if code is None:
                return out
            if code == "maxlag" and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            if code == "badtoken" and i < tries - 1:
                self.csrf = self._csrf()
                continue
            raise RuntimeError(f"Wikipedia API error: {code} - {out['error'].get('info', '')}")

    def _query(self, **params):
        """A read as this account. assert=user: an expired login fails as such instead of reading as an anonymous user."""
        try:
            return wiki_api({"action": "query", "format": "json", "formatversion": 2, "assert": "user", **params}, session=self.s, gap=0)
        except ApiError as e:
            if e.code == "assertuserfailed":
                raise Refused("your Wikipedia login has expired; nothing was sent") from e
            raise

    def logged_in(self):
        """Whether the login still holds (checked before each batch of sends)."""
        try:
            self._query(meta="userinfo")
            return True
        except Refused:
            return False

    def can_edit(self, pageid):
        out = self._query(prop="info", pageids=pageid, intestactions="edit")
        return bool(out["query"]["pages"][0].get("actions", {}).get("edit"))


def page_live(pageid):
    """Read the current revision. Busy, lagged or garbled replies are retried twice; then WikiUnavailable."""
    r = wiki_api(dict(action="query", prop="revisions|info", rvprop="content|ids", rvslots="main", inprop="protection",
                      pageids=pageid, format="json", formatversion=2, maxlag=5), gap=0)  # gap=0: reads you trigger aren't queued
    pg = r["query"]["pages"][0]
    if pg.get("missing") or "revisions" not in pg:
        raise Refused("page is missing")
    return dict(pageid=pg["pageid"], title=pg["title"], ns=pg["ns"], redirect=pg.get("redirect", False),
                revid=pg["revisions"][0]["revid"], text=pg["revisions"][0]["slots"]["main"]["content"],
                protection=[p for p in pg.get("protection", []) if p["type"] == "edit"])


def build_plan(pageid, items):
    """items: [{citation_id, raw, names}] -> plan for ONE article edit, built from the current live revision."""
    pg = page_live(pageid)
    if pg["ns"] != 0 or pg["redirect"]:
        raise Refused("not an article (moved or redirect)")
    if NOBOTS.search(pg["text"]):
        raise Refused("page has {{nobots}}")
    if {p["level"] for p in pg["protection"]} & BLOCKING:
        raise Refused("page is fully or template-editor protected")
    new, res = apply_to_page(pg["text"], items)
    ok = [r for r in res if r["status"] == "ok"]
    return dict(pageid=pageid, title=pg["title"], base_revid=pg["revid"], new_text=new, results=res,
                citation_ids=[r["citation_id"] for r in ok], new_templates=[r["new"] for r in ok],
                summary=edit_summary(ok) if ok else "", rules_version=RULES_VERSION)


def _log(con, plan, status, detail, dry_run, edit_id=None):
    """Every entry records the article title and the git commit of the code that built and sent the edit."""
    detail = {"title": plan.get("title", ""), **detail, "commit": code_version()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if edit_id is None:
        cur = con.execute("INSERT INTO edits(pageid, base_revid, citation_ids, summary, status, detail, dry_run, rules_version, created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (plan["pageid"], plan["base_revid"], dumps(plan["citation_ids"]), plan["summary"],
                                                        status, dumps(detail), int(dry_run), plan.get("rules_version", ""), now))
        con.commit()
        return cur.lastrowid
    con.execute("UPDATE edits SET status=?, detail=? WHERE id=?", (status, dumps(detail), edit_id))
    con.commit()
    return edit_id


def live_sent_count(con):
    return con.execute("SELECT count(*) FROM edits WHERE dry_run=0 AND status NOT IN ('refused')").fetchone()[0]


def _why(e):
    """A refusal's reason: our own messages as they are, anything unexpected with its type."""
    return redact(str(e) if isinstance(e, (Refused, Skip, requests.RequestException)) else f"{type(e).__name__}: {e}")


def send(plan, items, sess=None, dry_run=True, max_live=None):
    """Send one approved article edit. Returns (status, detail). Never raises for an ordinary refusal.
    max_live: optional limit on live article edits in total (None: no limit)."""
    con = connect(timeout=120)
    if not plan["citation_ids"]:
        return "refused", "nothing to send"
    if not dry_run and max_live is not None and live_sent_count(con) >= max_live:
        return "refused", f"live batch cap reached ({max_live} article edits); restart with a higher --max-live to continue"
    try:
        now = build_plan(plan["pageid"], [i for i in items if i["citation_id"] in plan["citation_ids"]])
    except Exception as e:  # nothing has been sent yet, so any failure here is a refusal
        _log(con, plan, "refused", {"reason": _why(e)}, dry_run)
        return "refused", _why(e)
    if now["base_revid"] != plan["base_revid"]:
        _log(con, plan, "refused", {"reason": f"page changed since approval (now {now['base_revid']})"}, dry_run)
        return "refused", f"page changed since approval (revision {now['base_revid']}, approved {plan['base_revid']}); review it again"
    if now["new_text"] != plan["new_text"]:
        _log(con, plan, "refused", {"reason": "rebuilt edit differs from the approved one"}, dry_run)
        return "refused", "the edit built now is not the one approved; review it again"
    if dry_run:
        _log(con, plan, "dry-run ok", {"title": plan["title"]}, True)
        return "dry-run ok", f"would edit {plan['title']} @ {plan['base_revid']}: {plan['summary']}"
    if sess is None:
        return "refused", "no Wikipedia session"
    try:
        allowed = sess.can_edit(plan["pageid"])
    except Exception as e:  # an expired login or a busy reply: still nothing sent
        _log(con, plan, "refused", {"reason": f"edit-rights check: {_why(e)}"}, False)
        return "refused", _why(e) if isinstance(e, Refused) else f"couldn't check the account's edit rights ({_why(e)}); try again"
    if not allowed:
        _log(con, plan, "refused", {"reason": "account may not edit this page"}, False)
        return "refused", "your account may not edit this page (protection or block)"
    eid = _log(con, plan, "attempt", {"title": plan["title"]}, False)  # written before the POST
    try:
        out = sess.post(dict(action="edit", pageid=plan["pageid"], text=plan["new_text"], summary=plan["summary"],
                             baserevid=plan["base_revid"], nocreate=1, minor=1, watchlist="nochange"))
    except Exception as e:
        try:
            after = page_live(plan["pageid"])
            done = after["revid"] != plan["base_revid"] and all(t in after["text"] for t in plan["new_templates"])
        except Exception:
            after, done = None, False
        if done:
            _log(con, plan, "success-reconciled", {"newrevid": after["revid"], "error": redact(str(e))}, False, eid)
            return "success-reconciled", f"response lost but the change is on the page (revision {after['revid']})"
        if after is None:  # no reply and no re-read: the edit may or may not be saved, so the row stays an 'attempt'
            _log(con, plan, "attempt", {"error": redact(str(e)), "reread": "failed"}, False, eid)
            return "attempt", f"no reply, and the page couldn't be re-read: the edit may be saved, so check the article ({redact(str(e))})"
        _log(con, plan, "error", {"error": redact(str(e))}, False, eid)
        return "error", redact(str(e))
    ed = out.get("edit", {})
    if ed.get("result") != "Success":
        _log(con, plan, "failed", {"response": ed}, False, eid)
        return "failed", json.dumps(ed)[:300]
    if ed.get("nochange"):
        _log(con, plan, "nochange", {"response": ed}, False, eid)
        return "nochange", "Wikipedia saved no change"
    newrev = ed.get("newrevid")
    try:
        saved = page_live(plan["pageid"])
        verified = saved["revid"] >= newrev and all(t in saved["text"] for t in plan["new_templates"])
    except Exception:
        verified = None
    status = "success-verified" if verified else ("success-unverified" if verified is None else "success-NOT-VERIFIED")
    _log(con, plan, status, {"newrevid": newrev, "title": plan["title"]}, False, eid)
    return status, f"saved revision {newrev}"


def whoami():
    """Log in (no edit) and show the account's relevant groups."""
    s = Session()
    print(f"logged in as {s.user}; groups: {', '.join(sorted(s.groups))}")
    print("extendedconfirmed:", "extendedconfirmed" in s.groups, "| autoconfirmed:", "autoconfirmed" in s.groups,
          "| bot flag:", "bot" in s.groups)
