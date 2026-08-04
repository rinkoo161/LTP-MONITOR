import pytest

from marketsense.net.budget import BudgetExceeded, CircuitOpen, RequestBudget


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(per_minute=6, threshold=2, cooldown=60.0):
    clk = FakeClock()
    b = RequestBudget(per_minute=per_minute, breaker_threshold=threshold,
                      breaker_cooldown=cooldown, clock=clk)
    return b, clk


def test_bucket_refuses_when_empty_and_refills():
    b, clk = make(per_minute=6)
    for _ in range(6):
        b.acquire()
    with pytest.raises(BudgetExceeded):
        b.acquire()
    clk.advance(10)  # 6/min → one token per 10s
    b.acquire()
    with pytest.raises(BudgetExceeded):
        b.acquire()


def test_bucket_never_exceeds_capacity():
    b, clk = make(per_minute=6)
    clk.advance(3600)
    granted = 0
    try:
        for _ in range(20):
            b.acquire()
            granted += 1
    except BudgetExceeded:
        pass
    assert granted == 6


def test_breaker_opens_on_consecutive_auth_failures():
    b, clk = make(threshold=2)
    b.record_auth_failure()
    b.acquire()  # one failure: still closed
    b.record_auth_failure()
    with pytest.raises(CircuitOpen):
        b.acquire()


def test_breaker_half_open_probe_and_close():
    b, clk = make(threshold=2, cooldown=60.0)
    b.record_auth_failure()
    b.record_auth_failure()
    with pytest.raises(CircuitOpen):
        b.acquire()
    clk.advance(61)
    b.acquire()  # the single half-open probe
    with pytest.raises(CircuitOpen):
        b.acquire()  # second concurrent probe refused
    b.record_success()
    b.acquire()  # closed again


def test_breaker_reopens_on_failed_probe():
    b, clk = make(threshold=2, cooldown=60.0)
    b.record_auth_failure()
    b.record_auth_failure()
    clk.advance(61)
    b.acquire()               # probe
    b.record_auth_failure()   # probe failed → reopen, cooldown restarts
    with pytest.raises(CircuitOpen):
        b.acquire()
    clk.advance(30)           # not yet
    with pytest.raises(CircuitOpen):
        b.acquire()
    clk.advance(31)
    b.acquire()               # next probe allowed


def test_transport_failures_do_not_open_breaker():
    b, clk = make(threshold=2)
    for _ in range(5):
        b.record_transport_failure()
    b.acquire()
