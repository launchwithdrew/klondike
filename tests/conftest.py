import pytest


@pytest.fixture(autouse=True)
def _isolated_judging_data(tmp_path_factory):
    """No test writes the install's real judging records (data/judged.jsonl, censor_caught.jsonl, labels/).

    Regression (Nov 21): taste/skip tests appended fixture verdicts to the real data/judged.jsonl on every run —
    including on the operator's machine, where `update` runs the suite — and `eval-judge` then counted them.
    (Plain save/restore, not the shared `monkeypatch` object: that would change other fixtures' teardown order.)"""
    from clipengine import judge
    before = judge.DATA
    judge.DATA = str(tmp_path_factory.mktemp("judging-data"))
    yield
    judge.DATA = before


@pytest.fixture(autouse=True)
def _isolated_facecam_checks(tmp_path_factory):
    """Clips cut in tests never add to the install's weekly webcam-box re-check (runs/facecam_checks.jsonl)."""
    from clipengine import facecam
    before = facecam.CHECKS
    facecam.CHECKS = str(tmp_path_factory.mktemp("facecam") / "checks.jsonl")
    yield
    facecam.CHECKS = before
