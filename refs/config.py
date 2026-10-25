"""Paths, versions, HTTP session and on-demand secrets."""
import os, re, subprocess, threading, time
from pathlib import Path
from urllib.parse import urlparse
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.environ.get("REFS_DB", DATA / "refs.sqlite"))  # REFS_DB: test copy
HTML_DIR = DATA / "html"
LLM_CACHE = DATA / "llmcache"

# Versions recorded with every proposal and decision. v2 was applied without a new held-out sample
# (17 Oct 2026): it only makes HIGH stricter or fixes casing deterministically.
RULES_VERSION = "v3"  # v3 (17 Oct 2026): word-like given names go to REVIEW. v2: casing, handles, live/archive, retries
PROMPT_VERSION = "v1"
READER_MODEL = "gpt-6-luna"

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "ReferencesWikipedia/0.1 (https://en.wikipedia.org/wiki/User:CipherSleuth; citation author checker) python-requests"
SECRETS_PATH = Path(os.environ.get("REFS_SECRETS_PATH", "~/.config/citation-author-tool/secrets")).expanduser()


_code_version = None


def code_version():
    """Git commit of the running code, fixed the first time it is asked for (the app asks at startup).
    '<hash>-dirty' if tracked files differed from that commit; 'unknown' if git could not say."""
    global _code_version
    if _code_version is None:
        def git(*args):
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        try:
            _code_version = git("rev-parse", "HEAD") + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")
        except Exception:
            _code_version = "unknown"
    return _code_version


def read_secret(name):
    """Read one variable from the secrets file at the moment of use. Never log the value."""
    for line in SECRETS_PATH.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise KeyError(f"{name} not found in {SECRETS_PATH}")


def redact(text):
    for name in ("OPENAI_API_KEY", "WIKI_BOT_PASSWORD"):
        try:
            v = read_secret(name)
        except Exception:
            continue
        if v:
            text = text.replace(v, "<redacted>")
    return re.sub(r"(Bearer\s+)\S+", r"\1<redacted>", text)


_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_next_ok = {}
_lock = threading.Lock()


def throttle(host, gap):
    """Space requests to one host at least `gap` seconds apart (thread-safe)."""
    while True:
        with _lock:
            now = time.monotonic()
            t = _next_ok.get(host, 0)
            if now >= t:
                _next_ok[host] = now + gap
                return
            wait = t - now
        time.sleep(wait)


def http_get(url, gap=1.0, tries=4, **kw):
    host = urlparse(url).netloc
    timeout = kw.pop("timeout", 30)
    for i in range(tries):
        throttle(host, gap)
        try:
            r = _session.get(url, timeout=timeout, **kw)
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
            continue
        if r.status_code in (429, 502, 503, 504) and i < tries - 1:
            ra = str(r.headers.get("retry-after", ""))
            time.sleep(min(float(ra), 60) if ra.isdigit() else 2 ** (i + 1))  # a site asking for an hour gets a minute
            continue
        return r


def http_post(url, gap=1.0, **kw):
    throttle(urlparse(url).netloc, gap)
    return _session.post(url, timeout=kw.pop("timeout", 60), **kw)


class WikiUnavailable(requests.RequestException):
    """Wikipedia answered with something other than data (busy, lagged, rate-limited, an error page) after retries."""


class ApiError(RuntimeError):
    """The API answered with an error that retrying won't fix (code: e.g. 'assertuserfailed')."""
    def __init__(self, code, info=""):
        super().__init__(f"Wikipedia API error: {code} - {info}")
        self.code = code


API_WAITS = (2, 5)  # seconds before the 2nd and 3rd try of an API call


def wiki_api(params, post=False, url=API, session=None, gap=1.0, tries=3, timeout=60):
    """One MediaWiki API call (Wikipedia by default, or Wikidata), parsed. A network error, an HTTP error, a reply that
    isn't JSON, or a maxlag/ratelimited error is retried after 2 s and 5 s; then WikiUnavailable. Other API errors
    raise ApiError at once. session: a logged-in requests.Session (default: the shared one). gap=0: no throttle."""
    s, why = session or _session, ""
    for i in range(tries):
        if i:
            time.sleep(API_WAITS[min(i, len(API_WAITS)) - 1])
        if gap:
            throttle(urlparse(url).netloc, gap)
        try:
            r = s.post(url, data=params, timeout=timeout) if post else s.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            why = type(e).__name__
            continue
        if r.status_code != 200:
            why = f"HTTP {r.status_code}"
            continue
        try:
            out = r.json()
        except ValueError:
            why = "a reply that wasn't data"
            continue
        err = out.get("error") or {}
        if err.get("code") in ("maxlag", "ratelimited"):  # the servers are lagging, or asked us to slow down
            why = err["code"]
            continue
        if err:
            raise ApiError(err.get("code"), err.get("info", ""))
        return out
    raise WikiUnavailable(f"Wikipedia didn't answer properly ({why}, {tries} {'try' if tries == 1 else 'tries'}); try again in a minute")
