# Model

This page says what the model of `request` proves and what it does not. The model is
`models/request_reply.py`, written with [musil](https://pypi.org/project/musil/). The rules it
checks are in [Operations](operations.md).

## Run it

```bash
uv run python models/request_reply.py
```

```bash
uv run pytest -q
```

## What it checks

The model has one request, 0 to 2 repliers and a deadline. Its faults are a handler that never
ends, one replier crash, one connection drop (up to 2 in the tests) and refusals (1 by default).

A drop loses every frame toward the requester. The replier can still read the request copy of the
closed connection, at any later time. After a `busy` outcome, the requester can retry once.

On the replier's side the deadline passes for each connection on its own, at any time: before or
after the requester's deadline, and before or after the one of another connection.

| Property | Holds with | Fails without it |
|---|---|---|
| Every request gets an outcome, also after a retry | the deadline | one hung handler, one crash, one lost frame or a partly refused retry leaves it open |
| A replier runs one id at most once | repeats sent only to a replier that drops repeated ids | a repeat runs the handler again |
| A replier runs one id at most once | the id is kept until every deadline passed | an id forgotten while the handler runs, or when it is done, runs again |
| A replier runs one id at most once | the id is kept until every deadline passed | kept only until the deadline of the copy that ran, a late copy of another connection runs again |
| A replier runs one id at most once | a retry only when every replier refused | a retry to a replier that ran it, and does not drop repeats, runs it again |
| In a `busy` outcome, a replier that refused did not run it | one copy without an end per connection | a second copy runs while the busy end is delivered |
| In a `busy` outcome, a replier that refused did not run it | a copy sent again is never refused | the old copy is read late and runs |
| In a `busy` outcome, a replier that refused did not run it | the requester stops reading the old connection | an old busy end arrives while the new copy runs |
| In a `busy` outcome, a replier that refused did not run it | a refusal is sent only on the connection that brought the copy | the busy end answers a copy sent again, which then runs |
| `completed` means every reply arrived | the reply count in the end | a reply lost in a connection drop goes unseen |
| No reply reaches the caller after the outcome | late replies are dropped | a reply in flight at the deadline is delivered |
| `unroutable` if and only if no replier is connected | the requester detects that there is no replier | the request waits and ends `expired` |

The tests also show that these states can happen:

- `completed` with zero replies, and with many replies
- `expired` with a crashed replier after one reply arrived
- `completed` after a retry of a `busy` request, with one and with two repliers
- a second `busy` after a retry
- a replier that runs a request after the requester got `expired`
- `completed` after a replier refused a copy whose connection had closed

They show that one state cannot happen: `completed` after a retry when one of two repliers already
ran the request.

## What it does not prove

- Two requests at once. The model has one id, so it does not check that a reply goes to the
  right request.
- A replier that restarts after a crash with an empty store of ids.
- Why a replier refuses. The model has refusals that can happen to any copy, not a store with a
  size or a longest deadline.
- A replier that restarts and then refuses a request that it ran before the restart.
- The arithmetic of deadlines. The model assumes that the keep time covers the difference between
  the deadlines of two copies. It does not see a keep time that is too short, or senders whose
  clocks differ too much (rule 14).
- A replier that is connected but has no handler for the key.
- Repliers that differ: in one run, all of them drop repeated ids or none does.
- More than 2 repliers, more than 2 replies, more than one retry, or more than one fault of
  each kind.
- The bytes. The model has frames, not the header in [Wire format](wire-format.md).
- The C code in `core/`. It has its own tests. A later test will replay model traces on it.
