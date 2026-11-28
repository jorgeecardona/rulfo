# Errors

Find the exception you got in the first table, and do what its row says. An outcome such as
`busy` or `expired` is not an exception: `request` returns it. See [Peer](python.md#send-a-request).

Every exception of rulfo is a subclass of `rulfo.RulfoError`.

## Exceptions that rulfo raises

| Exception | Raised by | When | What to do |
|---|---|---|---|
| `rulfo.PeerClosed` | `request`, `request_copy`, `wait_connected`, `serve` | `aclose` was called. A request that was open at that time fails with it too. | Stop using this `Peer` or `Server`. End the reconnect loop. |
| `rulfo.ProtocolError` | `serve` | The other side sent bytes that are not rulfo, or the handshake took longer than `Config.handshake`. | Connect again after a wait. `error.status` names the cause. If it repeats, the other side is not rulfo, or the two sides differ in `max_length`. |
| `rulfo.Overloaded` | `serve` | The other side reads too little of what this side sends. | Connect again after a wait. |
| `OSError` and others | `serve` | The transport raised it: a connection that broke, for example. | Connect again after a wait. |
| `rulfo.NotCompleted` | `Result.one()` | The outcome is not `completed`. | Read `error.result.outcome`. |
| `rulfo.RemoteError` | `Result.one()` | The reply is an error reply: the handler on the other side raised an exception. | The message is the type and the text of that exception. |
| `ValueError` | `Result.one()` | The request completed with no reply, or with more than one. | Read `result.replies`. |
| `rulfo.CallError` | `request_copy` | The call is not valid now. | Read `error.status`. `RF_ERR_STATE`: this `Peer` already waits for this id. |
| `ValueError` | `request`, `request_copy` | An empty key, a timeout that is not above 0, a key plus body above `max_length`, an id that is not 16 bytes. | Fix the call. |
| `TypeError` | `request`, `request_copy`, `Request.reply` | The body is a `str`. | Pass bytes: `text.encode()`. |
| `ValueError` | `Request.reply` | A body above `max_length`. | Send smaller replies. |
| `RuntimeError` | `Request.reply` | The handler of this request has finished. | Call `reply` only while the handler runs. |

Three status names of `ProtocolError`, from real runs:

- `RF_ERR_MAGIC`: the first bytes are not rulfo. A text protocol on the same port, for example.
- `RF_ERR_HANDSHAKE`: the other side sent nothing in time.
- `RF_ERR_LENGTH`: a key plus body above the `max_length` of the side that receives it.

The reconnect loop in [Peer](python.md#connections) catches the three rows that say "connect
again".

## Exceptions that your handler raises

| The handler raises | The requester gets | rulfo logs it |
|---|---|---|
| `rulfo.Busy` before its first reply | the outcome `busy` | No |
| `rulfo.Busy` after a reply | an error reply, then `completed` | Yes |
| any other `Exception` | an error reply, then `completed` | Yes |
| nothing: it still runs at the deadline, and is cancelled | the outcome `expired` | No |

- An error reply has `error=True`, and its body is `TypeName: message`. The text of your
  exception goes to the other side: catch exceptions whose text must stay private.
- The log is on the logger named `rulfo`, at level `ERROR`, with the traceback.
- `completed` does not mean that the handler succeeded. Check `reply.error` for each reply, or
  call `result.one()`, which raises `RemoteError` for an error reply.

`examples/outcomes.py` in [Peer](python.md#answer-requests) runs each row but the second, and
shows the output.
