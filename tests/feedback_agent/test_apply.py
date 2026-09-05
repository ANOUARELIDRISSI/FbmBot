from __future__ import annotations

from becarscout.feedback_agent import apply as apply_module
from becarscout.feedback_agent.apply import apply_latest_suggestions

CHAT_ID = 111


class _FakeSession:
    def close(self) -> None:
        pass


def test_returns_none_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(apply_module, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(apply_module.repo, "get_pending_suggestions", lambda session, chat_id: None)

    assert apply_latest_suggestions(CHAT_ID) is None


def test_returns_empty_list_when_last_review_had_no_suggestions(monkeypatch):
    monkeypatch.setattr(apply_module, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(apply_module.repo, "get_pending_suggestions", lambda session, chat_id: [])
    cleared = []
    monkeypatch.setattr(apply_module.repo, "clear_pending_suggestions", lambda session, chat_id: cleared.append(chat_id))

    result = apply_latest_suggestions(CHAT_ID)

    assert result == []
    assert cleared == [CHAT_ID]  # cleared either way


def test_applies_suggestions_and_clears_them(monkeypatch):
    # Never touch the real DB in this test -- a fake session object plus
    # mocked repo functions are enough to verify the argument-passing
    # logic in isolation.
    suggestions = [
        {"weight": "for_export", "current_value": -15, "new_value": -25, "reason": "test"},
        {"weight": "timing_belt_replaced", "current_value": 12, "new_value": 18, "reason": "test"},
    ]
    monkeypatch.setattr(apply_module, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(apply_module.repo, "get_pending_suggestions", lambda session, chat_id: suggestions)
    cleared = []
    monkeypatch.setattr(apply_module.repo, "clear_pending_suggestions", lambda session, chat_id: cleared.append(chat_id))

    applied_changes = {}

    def fake_update_scoring_weights(session, chat_id, **changes):
        applied_changes.update(changes)

    monkeypatch.setattr(apply_module.repo, "update_scoring_weights", fake_update_scoring_weights)

    result = apply_latest_suggestions(CHAT_ID)

    assert result == suggestions
    assert applied_changes == {"for_export": -25, "timing_belt_replaced": 18}
    assert cleared == [CHAT_ID]
