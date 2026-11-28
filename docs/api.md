# API reference

Every public name of `rulfo`, from the docstrings. Start with `Peer` for one remote party and
`Server` for many. The guide pages explain how to use them: [Peer](python.md),
[Server](server.md), [Transports](transports.md), [Errors](errors.md), [Testing](testing.md).

## Peer and Server

::: rulfo.Peer

::: rulfo.Server

::: rulfo.Request

::: rulfo.Result

::: rulfo.Reply

::: rulfo.Outcome

::: rulfo.Config

## Transports

::: rulfo.Transport

::: rulfo.StreamTransport

::: rulfo.testing

## Exceptions

::: rulfo.RulfoError

::: rulfo.PeerClosed

::: rulfo.ProtocolError

::: rulfo.Overloaded

::: rulfo.NotCompleted

::: rulfo.RemoteError

::: rulfo.CallError

::: rulfo.Busy

## Without asyncio

`PeerState` is the logic of `Peer` without I/O and without a clock. `receive` and `poll`
return the events below.

::: rulfo.PeerState

::: rulfo.Ready

::: rulfo.Closed

::: rulfo.ReplyReceived

::: rulfo.Ended

::: rulfo.RequestReceived
