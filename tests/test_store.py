"""Dedupe. Without it, one redelivered message becomes two calendar events."""

from __future__ import annotations

from signal_calendar_bot.signal_client import IncomingMessage


def test_first_claim_wins_and_the_redelivery_loses(store):
    assert store.claim_message("+1555:1700000000000") is True
    assert store.claim_message("+1555:1700000000000") is False


def test_different_messages_both_pass(store):
    assert store.claim_message("+1555:1700000000000") is True
    assert store.claim_message("+1555:1700000000001") is True


def test_dedupe_survives_a_restart(store):
    """signal-cli redelivers on reconnect, which often follows a bot restart."""
    from signal_calendar_bot.store import Store

    store.claim_message("+1555:1700000000000")
    path = store.path
    store.close()

    reopened = Store(path)
    try:
        assert reopened.claim_message("+1555:1700000000000") is False
    finally:
        reopened.close()


def test_message_key_is_source_plus_timestamp():
    msg = IncomingMessage(
        source="+1555", source_uuid=None, destination="+1555",
        timestamp=1700000000000, body="hi",
    )
    assert msg.message_key == "+1555:1700000000000"


def test_note_to_self_detection():
    account = "+1555"
    own = IncomingMessage(
        source=account, source_uuid=None, destination=account, timestamp=1, body="hi"
    )
    other = IncomingMessage(
        source="+1999", source_uuid=None, destination=account, timestamp=2, body="hi"
    )
    group = IncomingMessage(
        source=account, source_uuid=None, destination=account, timestamp=3, body="hi",
        group_id="abc",
    )
    assert own.is_note_to_self(account) is True
    assert other.is_note_to_self(account) is False
    assert group.is_note_to_self(account) is False


def test_prune_leaves_recent_entries(store):
    store.claim_message("+1555:1")
    assert store.prune_processed(older_than_days=30) == 0
    assert store.claim_message("+1555:1") is False


def test_audit_log_records_writes(store):
    store.record_audit(
        correlation_id="cid1", action="create", event_id="ev1", detail={"title": "Lunch"}
    )
    rows = store._conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "ev1"


def test_heartbeat_ack_is_single_use(store):
    store.record_heartbeat_sent("nonce1")
    assert store.unacked_heartbeats(0) == ["nonce1"]
    assert store.ack_heartbeat("nonce1") is True
    assert store.ack_heartbeat("nonce1") is False
    assert store.unacked_heartbeats(0) == []
