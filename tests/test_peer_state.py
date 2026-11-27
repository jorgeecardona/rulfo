"""``PeerState``: the Python layer over the C core, with bytes moved by the test."""

from __future__ import annotations

import pytest

from links import HEADER, ID, Link, frames
from rulfo import (
    CallError,
    Closed,
    Config,
    Ended,
    Outcome,
    PeerState,
    Ready,
    ReplyReceived,
    RequestReceived,
)

REPLIES = [ReplyReceived(ID, 0, b"one", error=False), ReplyReceived(ID, 1, b"two", error=True)]


def exchange(link: Link, chunk: int = 1 << 20) -> None:
    link.requester.request(ID, "op", b"ask", 1000, link.now)
    link.pump(chunk)
    link.replier.reply(ID, b"one")
    link.replier.reply(ID, b"two", error=True)
    link.replier.end(ID)
    link.pump(chunk)


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 31, 32, 33, 1 << 20])
def test_a_request_with_replies_arrives_however_the_bytes_are_cut(chunk: int) -> None:
    link = Link()
    link.requester.connected(0)
    link.replier.connected(0)
    link.pump(chunk)
    exchange(link, chunk)
    assert link.ran == [Ready(), RequestReceived(ID, "op", b"ask", 1000, repeat=False)]
    assert link.got == [Ready(), *REPLIES, Ended(ID, Outcome.COMPLETED, 2)]
    assert link.requester.open_requests == 0


def test_a_large_body_goes_through_in_small_pieces() -> None:
    link = Link()
    link.connect()
    body = bytes(range(256)) * 400
    link.requester.request(ID, "op", body, 1000, 0)
    link.pump(chunk=100)
    link.replier.reply(ID, body[::-1])
    link.replier.end(ID)
    link.pump(chunk=100)
    assert link.ran[-1] == RequestReceived(ID, "op", body, 1000, repeat=False)
    assert link.got[1] == ReplyReceived(ID, 0, body[::-1], error=False)


@pytest.mark.parametrize("arrived", [0, 1, 2])
@pytest.mark.parametrize("answered_offline", [False, True])
def test_replies_lost_in_a_reconnect_are_sent_again(
    arrived: int, *, answered_offline: bool
) -> None:
    link = Link()
    link.connect()
    link.request()
    link.got.clear()
    if answered_offline:
        link.disconnect()
    link.replier.reply(ID, b"one")
    link.replier.reply(ID, b"two", error=True)
    link.replier.end(ID)
    sent = frames(link.replier.data_to_send())
    assert len(sent) == (0 if answered_offline else 3)
    for frame in sent[:arrived]:
        link.got += link.requester.receive(frame, 0)
    if not answered_offline:
        link.disconnect()
    link.connect()
    assert [e for e in link.got if e != Ready()] == [*REPLIES, Ended(ID, Outcome.COMPLETED, 2)]
    assert [e for e in link.ran if isinstance(e, RequestReceived)] == [
        RequestReceived(ID, "op", b"ask", 1000, repeat=False)
    ]


def test_a_repeat_while_the_handler_runs_gets_the_replies_so_far() -> None:
    link = Link()
    link.connect()
    link.request()
    link.replier.reply(ID, b"one")
    link.replier.data_to_send()  # lost
    link.disconnect()
    link.connect()
    link.replier.reply(ID, b"two", error=True)
    link.replier.end(ID)
    link.pump()
    assert [e for e in link.got if e != Ready()] == [*REPLIES, Ended(ID, Outcome.COMPLETED, 2)]


@pytest.mark.parametrize(
    "config",
    [Config(replay_bytes=0), Config(replay_bytes_per_request=HEADER + 2), Config(replay_bytes=40)],
    ids=["off", "one-request-too-large", "total-too-large"],
)
def test_replies_that_are_not_stored_are_not_sent_again(config: Config) -> None:
    link = Link.with_config(config)
    link.connect()
    link.request()
    link.replier.reply(ID, b"one")
    link.replier.reply(ID, b"two")
    link.replier.end(ID)
    link.replier.data_to_send()  # lost
    assert link.replier.stored_bytes == 0
    link.disconnect()
    link.connect()
    link.now = 1000
    link.got += link.requester.poll(link.now)
    assert link.got[-1] == Ended(ID, Outcome.EXPIRED, 0)
    assert not [e for e in link.got if isinstance(e, ReplyReceived)]


def test_stored_replies_are_freed_at_the_deadline_and_the_id_after_the_keep_time() -> None:
    link = Link()
    link.connect()
    assert link.replier.next_time() is None
    link.request()
    link.replier.reply(ID, b"one")
    assert link.replier.stored_bytes == HEADER + 3
    assert link.replier.next_time() == 1000
    link.replier.end(ID)
    link.pump()
    assert link.replier.poll(999) == []
    assert link.replier.stored_bytes == HEADER + 3
    assert link.replier.poll(1000) == []
    assert link.replier.stored_bytes == 0
    assert link.replier.next_time() == 61000
    assert not link.replier.idle
    link.replier.poll(61000)
    assert link.replier.idle
    assert link.replier.next_time() is None


def test_the_timer_follows_the_open_requests() -> None:
    link = Link()
    assert link.requester.next_time() is None
    link.requester.connected(10)
    assert link.requester.next_time() == 10 + 5000 + 1
    link.replier.connected(10)
    link.now = 10
    link.pump()
    assert link.requester.next_time() is None
    link.request(deadline=500)
    assert link.requester.next_time() == 500
    link.disconnect()
    link.requester.connected(20)
    link.replier.connected(20)
    to_replier = link.requester.data_to_send()
    to_requester = link.replier.data_to_send()
    link.replier.receive(to_replier, 20)
    link.requester.receive(to_requester, 20)
    link.replier.receive(link.requester.data_to_send(), 20)
    link.requester.receive(link.replier.data_to_send(), 20)
    # The copy sent again waits in the output.
    assert [frame[4:6] for frame in frames(link.requester.data_to_send())] == [b"\x01\x01"]
    assert link.requester.next_time() == 500
    assert link.requester.poll(500) == [Ended(ID, Outcome.EXPIRED, 0)]
    assert link.requester.next_time() is None


def test_a_late_handshake_closes_the_connection() -> None:
    state = PeerState()
    state.connected(0)
    assert state.poll(5001) == [Closed("RF_ERR_HANDSHAKE")]
    assert state.data_to_send() == b""


def test_bytes_that_break_the_wire_format_close_the_connection() -> None:
    state = PeerState()
    state.connected(0)
    assert state.receive(b"HTTP/1.1 200 OK\r\n", 0) == [Closed("RF_ERR_MAGIC")]
    assert state.receive(b"more", 0) == []
    assert not state.ready


@pytest.mark.parametrize("no_handler", [False, True])
def test_a_refusal(*, no_handler: bool) -> None:
    link = Link()
    link.connect()
    link.request()
    link.replier.refuse(ID, no_handler=no_handler)
    link.pump()
    outcome = Outcome.UNROUTABLE if no_handler else Outcome.BUSY
    assert link.got[-1] == Ended(ID, outcome, 0)
    assert link.replier.idle


def test_a_copy_sent_again_is_not_refused_on_the_wire() -> None:
    link = Link()
    link.connect()
    link.requester.request(ID, "op", b"", 1000, 0, sent_before=True)
    link.pump()
    assert link.ran[-1] == RequestReceived(ID, "op", b"", 1000, repeat=True)
    link.replier.refuse(ID)
    assert link.replier.data_to_send() == b""
    assert link.replier.idle


def test_giving_up_sends_nothing_and_the_requester_expires() -> None:
    link = Link()
    link.connect()
    link.request()
    link.replier.reply(ID, b"one")
    link.pump()
    link.replier.give_up(ID)
    assert link.replier.data_to_send() == b""
    link.disconnect()
    link.connect()
    assert link.requester.poll(1000) == [Ended(ID, Outcome.EXPIRED, 1)]


def test_an_abandoned_request_gives_no_event() -> None:
    link = Link()
    link.connect()
    link.request()
    link.requester.abandon(ID)
    link.requester.abandon(ID)
    link.replier.reply(ID, b"one")
    link.replier.end(ID)
    link.got.clear()
    link.pump()
    assert link.got == []
    assert link.requester.poll(1000) == []
    assert link.requester.idle


def test_requests_above_the_store_of_ids_are_refused_as_busy() -> None:
    link = Link(PeerState(), PeerState(Config(max_ids=1)))
    link.connect()
    for n in range(40):
        link.requester.request(bytes([n]) * 16, "op", b"", 1000, 0)
    link.pump()
    ended = [e for e in link.got if isinstance(e, Ended)]
    assert [e.outcome for e in ended] == [Outcome.BUSY] * 39


def test_calls_that_are_not_valid() -> None:
    link = Link()
    with pytest.raises(CallError, match="RF_ERR_NOT_READY"):
        link.requester.request(ID, "op", b"", 1000, 0)
    link.connect()
    with pytest.raises(ValueError, match="RF_ERR_KEY"):
        link.requester.request(ID, "", b"", 1000, 0)
    with pytest.raises(ValueError, match="16 bytes"):
        link.requester.request(b"short", "op", b"", 1000, 0)
    with pytest.raises(CallError, match="RF_ERR_EXPIRED"):
        link.requester.request(ID, "op", b"", 0, 0)
    assert link.requester.idle
    link.request()
    with pytest.raises(CallError, match="RF_ERR_STATE"):
        link.requester.request(ID, "op", b"", 1000, 0)
    with pytest.raises(CallError, match="RF_ERR_UNKNOWN_ID"):
        link.replier.reply(bytes(16), b"")
    link.replier.end(ID)
    for call in (lambda: link.replier.reply(ID, b""), lambda: link.replier.end(ID)):
        with pytest.raises(CallError, match="RF_ERR_STATE"):
            call()


def test_a_body_above_the_largest_length_is_refused() -> None:
    link = Link.with_config(Config(max_length=10))
    link.connect()
    with pytest.raises(ValueError, match="RF_ERR_LENGTH"):
        link.requester.request(ID, "op", bytes(9), 1000, 0)
    link.request(body=bytes(8))
    with pytest.raises(ValueError, match="RF_ERR_LENGTH"):
        link.replier.reply(ID, bytes(11))
    assert link.replier.stored_bytes == 0


@pytest.mark.parametrize(
    "config",
    [Config(keep=1.0), Config(handshake=0), Config(max_ids=-1), Config(replay_bytes=-1)],
)
def test_settings_out_of_range(config: Config) -> None:
    with pytest.raises(ValueError, match=r"range|negative"):
        PeerState(config)
