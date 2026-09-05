from __future__ import annotations

import json

from becarscout.feedback_agent import apply as apply_module
from becarscout.feedback_agent.apply import apply_latest_suggestions


def test_returns_none_when_nothing_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_module, "LATEST_SUGGESTIONS_PATH", tmp_path / "latest_suggestions.json")
    assert apply_latest_suggestions() is None


def test_returns_empty_list_when_last_review_had_no_suggestions(tmp_path, monkeypatch):
    path = tmp_path / "latest_suggestions.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(apply_module, "LATEST_SUGGESTIONS_PATH", path)

    result = apply_latest_suggestions()

    assert result == []
    assert not path.exists()  # cleared either way


def test_applies_suggestions_and_clears_the_file(tmp_path, monkeypatch):
    path = tmp_path / "latest_suggestions.json"
    suggestions = [
        {"weight": "for_export", "current_value": -15, "new_value": -25, "reason": "test"},
        {"weight": "timing_belt_replaced", "current_value": 12, "new_value": 18, "reason": "test"},
    ]
    path.write_text(json.dumps(suggestions), encoding="utf-8")
    monkeypatch.setattr(apply_module, "LATEST_SUGGESTIONS_PATH", path)

    # Never touch the real DB in this test -- a fake session object plus
    # a mocked update function is enough to verify the file-handling and
    # argument-passing logic in isolation.
    class _FakeSession:
        def close(self) -> None:
            pass

    monkeypatch.setattr(apply_module, "get_session", lambda: _FakeSession())

    applied_changes = {}

    def fake_update_scoring_weights(session, **changes):
        applied_changes.update(changes)

    monkeypatch.setattr(apply_module.repo, "update_scoring_weights", fake_update_scoring_weights)

    result = apply_latest_suggestions()

    assert result == suggestions
    assert applied_changes == {"for_export": -25, "timing_belt_replaced": 18}
    assert not path.exists()
