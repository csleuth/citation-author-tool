"""Shared fakes: a stand-in for the shared HTTP session, so API calls run offline with canned replies."""
import pytest
from refs import config


class Reply:
    """A canned HTTP reply; body None means the body isn't JSON (an HTML error page)."""
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.body, self.headers, self.text = status, body, headers or {}, "<html>busy</html>"

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self.body is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self.body


class FakeHTTP:
    """Hands out the replies in order (an exception is raised instead) and records every call."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def _next(self, method, url, kw):
        self.calls.append((method, url, kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._next("get", url, kw)

    def post(self, url, **kw):
        return self._next("post", url, kw)


@pytest.fixture
def http(monkeypatch):
    """http(*replies) installs a FakeHTTP as the shared session (no waiting between retries or requests) and returns it."""
    monkeypatch.setattr(config.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "throttle", lambda host, gap: None)

    def install(*replies):
        fake = FakeHTTP(replies)
        monkeypatch.setattr(config, "_session", fake)
        return fake
    return install
