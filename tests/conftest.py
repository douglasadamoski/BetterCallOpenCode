"""Shared test guards.

The one rule this suite cannot afford to break is: no test spends an OpenRouter
request. The maintainer's free tier is 20/minute and 50-1000/day, shared with real use,
and CI runs the suite on three Python versions on every push. A single test that
accidentally reached the network would drain that quota silently — the run would still
be green, just slower and poorer.

So `urllib.request.urlopen` is replaced, for every test, with something that raises.
`scripts/or_client.py` and `scripts/list_free_models.py` are the only modules that call
out, and both reach the network exactly through it.

If a test ever legitimately needs the real thing, opt that single test out:

    @pytest.mark.allow_network
    def test_something_that_really_must_dial_out():
        ...

Do not weaken the fixture instead. Note the guard is per-process: it protects code
imported into the test run, not scripts started via subprocess. Those are kept offline
by giving them no resolvable credential (see the `env` fixtures), which makes the
wrappers refuse with AUTH before any socket is opened.
"""
import urllib.request

import pytest


class NetworkAccessDuringTests(BaseException):
    """Raised instead of making a real HTTP request.

    Deliberately NOT an `Exception`. `or_client.http_json` ends with
    `except Exception as e: raise ConnectionError(str(e))`, so an ordinary error raised
    here would be relabelled as a routine connection failure — which is exactly what a
    test asserting offline behaviour expects to see, so the tripwire would be swallowed
    into a green run. Inheriting from BaseException makes it pass straight through.
    """


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: let this test perform real network I/O (must be justified in "
        "the test's docstring; every such test spends the maintainer's free quota)",
    )


@pytest.fixture(autouse=True)
def block_network(request, monkeypatch):
    if "allow_network" in request.keywords:
        return

    def _blocked(*args, **kwargs):
        target = args[0] if args else ""
        url = getattr(target, "full_url", target)
        raise NetworkAccessDuringTests(
            "a test tried to open %r. The suite must be fully offline: a real request "
            "spends one of the maintainer's free daily OpenRouter calls. Stub the "
            "client, or mark the test @pytest.mark.allow_network if it truly must "
            "dial out." % (url,)
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    # Anything that builds its own opener instead of calling urlopen goes through here.
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _blocked)
