"""The 60-second window is the safety property this project rests on."""

from __future__ import annotations

import time

import pytest

from signal_calendar_bot.confirm import Action, ConfirmationManager, Reply, classify_reply


def _propose(manager: ConfirmationManager, thread="+1555", action=Action.CREATE):
    return manager.propose(
        thread,
        correlation_id="cid1",
        action=action,
        payload={"title": "Lunch", "start": "2026-09-10T16:00:00+00:00"},
        preview_text="Lunch — Thursday 12pm-1pm",
    )


def test_yes_inside_window_confirms(store, confirm_cfg):
    manager = ConfirmationManager(store, confirm_cfg)
    _propose(manager)
    resolution = manager.resolve("+1555", "yes")
    assert resolution.confirmed
    assert resolution.pending.payload["title"] == "Lunch"


def test_yes_after_window_does_not_confirm(store, confirm_cfg):
    """A late yes must never write. This is the whole point of the design."""
    confirm_cfg.timeout_seconds = 5
    manager = ConfirmationManager(store, confirm_cfg)
    pending = _propose(manager)

    # Reach into the store rather than sleeping: expire the row directly.
    store.put_pending(
        "+1555",
        correlation_id=pending.correlation_id,
        action=pending.action,
        payload=pending.payload,
        preview_text=pending.preview_text,
        ttl_seconds=-1,
    )

    resolution = manager.resolve("+1555", "yes")
    assert resolution.outcome == "expired"
    assert not resolution.confirmed


def test_proposal_is_consumed_by_a_single_reply(store, confirm_cfg):
    """One proposal, one chance. A second yes has nothing to land on."""
    manager = ConfirmationManager(store, confirm_cfg)
    _propose(manager)

    assert manager.resolve("+1555", "yes").confirmed
    assert manager.resolve("+1555", "yes").outcome == "none"


def test_sweep_destroys_lapsed_proposals(store, confirm_cfg):
    manager = ConfirmationManager(store, confirm_cfg)
    store.put_pending(
        "+1555",
        correlation_id="cid",
        action="create",
        payload={},
        preview_text="Lunch",
        ttl_seconds=-1,
    )
    swept = manager.sweep()
    assert len(swept) == 1
    assert store.peek_pending("+1555") is None


def test_sweep_leaves_live_proposals_alone(store, confirm_cfg):
    manager = ConfirmationManager(store, confirm_cfg)
    _propose(manager)
    assert manager.sweep() == []
    assert store.peek_pending("+1555") is not None


def test_new_proposal_replaces_the_old_one(store, confirm_cfg):
    manager = ConfirmationManager(store, confirm_cfg)
    _propose(manager, action=Action.CREATE)
    _propose(manager, action=Action.DELETE)
    live = store.peek_pending("+1555")
    assert live.action == "delete"


def test_unrelated_reply_drops_the_proposal(store, confirm_cfg):
    """Moving on must not leave a proposal armed for a later 'yes'."""
    manager = ConfirmationManager(store, confirm_cfg)
    _propose(manager)
    resolution = manager.resolve("+1555", "actually make it friday")
    assert resolution.outcome == "cancelled"
    assert store.peek_pending("+1555") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", Reply.AFFIRMATIVE),
        ("YES", Reply.AFFIRMATIVE),
        ("ok.", Reply.AFFIRMATIVE),
        ("y", Reply.AFFIRMATIVE),
        ("no", Reply.NEGATIVE),
        ("cancel", Reply.NEGATIVE),
        # Must NOT be read as yes just because it contains "yes"/"y".
        ("yes but move it to friday", Reply.UNRELATED),
        ("no, not thursday", Reply.UNRELATED),
        ("maybe", Reply.UNRELATED),
        ("", Reply.UNRELATED),
    ],
)
def test_reply_classification_is_exact(text, expected, confirm_cfg):
    assert classify_reply(text, confirm_cfg) is expected


def test_window_is_actually_sixty_seconds_by_default(store):
    from signal_calendar_bot.config import ConfirmationConfig

    manager = ConfirmationManager(store, ConfirmationConfig())
    pending = _propose(manager)
    assert manager.ttl == 60
    assert 59 <= pending.expires_at - time.time() <= 61
