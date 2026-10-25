"""SQLite schema and connection."""
import json, sqlite3
from .config import DB_PATH, DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  pageid INTEGER PRIMARY KEY, title TEXT, views INTEGER, lists TEXT,
  lastrevid INTEGER, scanned_revid INTEGER, scanned_at TEXT, nobots INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS citations(
  id INTEGER PRIMARY KEY, pageid INTEGER, revid INTEGER, tmpl TEXT, template_hash TEXT, raw TEXT,
  url TEXT, url_norm TEXT, domain TEXT, title TEXT, work TEXT, date TEXT, access_date TEXT, archive_url TEXT,
  has_author INTEGER, author_comment INTEGER, eligible INTEGER DEFAULT 0,
  UNIQUE(pageid, template_hash));
CREATE INDEX IF NOT EXISTS citations_url ON citations(url_norm);
CREATE TABLE IF NOT EXISTS samples(
  name TEXT, citation_id INTEGER, PRIMARY KEY(name, citation_id));
CREATE TABLE IF NOT EXISTS sources(
  url_norm TEXT, run TEXT, url TEXT, via TEXT, status TEXT, snapshot_url TEXT, html_sha TEXT,
  headline TEXT, attempts TEXT, fetched_at TEXT, PRIMARY KEY(url_norm, run));
CREATE TABLE IF NOT EXISTS readings(
  citation_id INTEGER, run TEXT, html_sha TEXT, model TEXT, prompt_version TEXT,
  answer TEXT, metadata TEXT, usage TEXT, read_at TEXT, PRIMARY KEY(citation_id, run));
CREATE TABLE IF NOT EXISTS proposals(
  citation_id INTEGER, run TEXT, rules_version TEXT, tier TEXT, reason TEXT,
  names TEXT, params TEXT, evidence TEXT, PRIMARY KEY(citation_id, run));
-- kind: 'gate1' (offline label), 'live' (a review-app approval/rejection) or 'dryrun' (one made in a dry-run app).
-- label: gate1 -> correct|wrong|unsure for the pipeline; live and dryrun -> approve|reject.
-- tier and rules_version are copied in so gate 2 can count high-tier live decisions per version.
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, citation_id INTEGER, run TEXT, kind TEXT, tier TEXT, rules_version TEXT,
  label TEXT, checker_label TEXT, reason TEXT, note TEXT, reviewer TEXT, decided_at TEXT);
-- WikiProject tags and Wikidata facts per article (refs/topics.py), for the review app's sports filter.
CREATE TABLE IF NOT EXISTS article_topics(
  pageid INTEGER PRIMARY KEY, projects TEXT, qid TEXT, human INTEGER, wd_sport INTEGER, wd_other_job INTEGER, fetched_at TEXT);
-- Edit log, one row per send (dry_run = 1 for dry runs). A live send writes its row as 'attempt' before the POST,
-- then updates it in place with the outcome (success-verified, error, ...).
CREATE TABLE IF NOT EXISTS edits(
  id INTEGER PRIMARY KEY, pageid INTEGER, base_revid INTEGER, citation_ids TEXT, summary TEXT,
  status TEXT, detail TEXT, dry_run INTEGER, rules_version TEXT, created_at TEXT);
"""


COLUMNS = {"decisions": {"params": "TEXT"}}  # added after the first release; created on demand
TABLES = {"articles", "citations", "samples", "sources", "readings", "proposals", "decisions", "edits", "article_topics"}


class Connection(sqlite3.Connection):
    """Closes itself once nothing refers to it any more (at once, in CPython). The app and the commands open many
    short-lived connections; this is the close each would otherwise need, and Python 3.13+ no longer warns about them."""
    def __del__(self):
        try:
            self.close()
        except sqlite3.Error:  # released in another thread: freeing it closes it all the same
            pass


def connect(timeout=60):
    DATA.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=timeout, factory=Connection)
    con.row_factory = sqlite3.Row
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not TABLES <= have:  # schema setup needs a write lock; skip it when nothing is missing
        con.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:  # decisions.params: a reviewer-chosen name split, as JSON [[last, first], ...]
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                con.commit()
    return con


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
