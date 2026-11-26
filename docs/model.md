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
ends, one replier crash, one connection drop that loses every frame in flight, and one refusal.

After a drop, the requester can send the request again. After `busy`, it can retry once.

| Property | Holds with | Fails without it |
|---|---|---|
| Every request gets an outcome | the deadline | one hung handler, one crash or one lost frame leaves it open |
| A replier runs one id at most once | repeats sent only to a replier that drops repeated ids | a repeat runs the handler again |
| A replier runs one id at most once | the id is kept until the deadline | an id forgotten earlier runs again, also while the handler runs |
| A replier runs one id at most once | a retry keeps the id, and every replier of several drops repeated ids | a retry runs the handler again on a replier that already ran it |
| A replier that refused did not run it, in a `busy` outcome | one send per connection | a second copy runs while the busy end is delivered |
| `completed` means every reply arrived | the reply count in the end | a reply lost in a connection drop goes unseen |
| No reply reaches the caller after the outcome | late replies are dropped | a reply in flight at the deadline is delivered |
| `unroutable` if and only if no replier | the requester detects that there is no replier | the request waits and ends `expired` |

The tests also show that these states can happen:

- `completed` with zero replies, and with many replies
- `expired` with a crashed replier after one reply arrived
- `completed` after a retry of a `busy` request
- `expired` after a retry to two repliers, one of which already ran the request

## What it does not prove

- Two requests at once. The model has one id, so it does not check that a reply goes to the
  right request.
- A replier that restarts after a crash with an empty store of ids.
- Why a replier refuses. The model has one refusal that can happen at any receive, not a store
  with a size or a longest deadline.
- A replier that restarts and then refuses a request that it ran before the restart.
- Clocks. The requester and the replier see the deadline at the same instant in the model.
- More than 2 repliers, more than 2 replies, more than one retry, or more than one fault of
  each kind.
- The bytes. The model has frames, not the header in [Wire format](wire-format.md).
- The C code. It does not exist yet. A later test will replay model traces on it.
