from app.api.dashboard import _dashboard_html


def test_dashboard_distinguishes_simulated_account_and_historical_replay() -> None:
    html = _dashboard_html()
    assert "s.demo_backend==='OFFLINE_SIM'" in html
    assert "simulated?'SIMULATED OBSERVATION':'REAL BROKER OBSERVED'" in html
    assert "HISTORICAL REPLAY — NOT LIVE" in html
    assert "Replay complete" in html
    assert "Qualification sessions" in html


def test_dashboard_marks_prior_state_stale_when_refresh_fails() -> None:
    html = _dashboard_html()
    assert "AbortSignal.timeout(4000)" in html
    assert "STALE / CONNECTION FAILED" in html
    assert "Displayed data may be stale" in html
