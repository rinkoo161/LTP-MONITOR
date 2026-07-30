"""store.py — the one place that decides WHERE persistent state lives.

2026-07-31. Every module resolved `~/.ltp-monitor` for itself, so tests
that construct a real Bus, deploy a paper future or persist a candle
wrote into the OPERATOR'S live store. That is not hypothetical: a suite
run on 2026-07-31 injected 11 fake futures signals into
shadow_signals.jsonl and a run of S10 observations into activity.log,
and they were later read back as real trading activity. Test data that
is indistinguishable from production data is worse than no test data.

Set LTP_MONITOR_HOME to point the whole application at a different
directory — that is what run_tests.py does, giving every test a private
store without any test needing to know about it.

Deliberately dependency-free and import-cheap: config, history, agents,
broker_adapter and the news modules all import this at module scope, so
it must not import any of them back.
"""
import os

ENV_VAR = "LTP_MONITOR_HOME"
DEFAULT = "~/.ltp-monitor"


def home():
    """Absolute path to the state directory, creating it if needed."""
    path = os.path.expanduser(os.environ.get(ENV_VAR) or DEFAULT)
    os.makedirs(path, exist_ok=True)
    return path


def path(*parts):
    """A file inside the state directory: store.path('history.db')."""
    return os.path.join(home(), *parts)


def is_isolated():
    """True when redirected away from the operator's real store — used by
    tests that want to assert they are not about to write to it."""
    return bool(os.environ.get(ENV_VAR))
