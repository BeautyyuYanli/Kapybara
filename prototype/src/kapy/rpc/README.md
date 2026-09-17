# Shared JSON-RPC

Gateway and the machine daemon adapt their text transports to `RpcPeer`:

```python
async with RpcPeer(
    send_text=send_text,
    receive_text=receive_text,
    close_transport=close_transport,
    handler=handler,
) as peer:
    await peer.wait_closed()
```

Callbacks are async. `send_text(str)` sends one complete message;
`receive_text()` returns one complete string or `None` for EOF;
`close_transport()` closes the owned connection once. `handler(method, params)`
returns a JSON value or raises `RpcError(code, message, data)`. Capture the
authenticated caller in the handler closure; the RPC package does not infer
identity from request parameters.

The peer uses the asyncio backend and supports simultaneous requests in both
directions, including a handler calling back into its own peer. Enter it once
before calling `call(method, params, *, timeout=60.0)` or `notify(method, params)`.
It serializes writes, dispatches handlers concurrently, and closes all connection
tasks on exit. `RpcTimeout` and `RpcDisconnected` leave remote side effects
uncertain. Calls are never replayed. A daemon must own persistent process/transfer
work separately from connection handlers so connection cancellation does not
cancel that work.

HTTP endpoints use `await dispatch_json(payload, handler)` after authenticating
and strictly decoding their bounded UTF-8 body. Return the resulting string as
JSON, or HTTP 204 when it is `None`. Invalid UTF-8 can use an empty payload to
obtain the shared parse-error response. HTTP connection/global concurrency limits
remain the endpoint's responsibility.

Both paths share JSON-RPC request/error/batch validation. Missing params become
`{}`; positional arrays remain arrays. Null IDs receive responses, while omitted
IDs are notifications. Business `RpcError` details are public; other exception
details are removed. Invalid response envelopes close a duplex connection.

Limits are 1 MiB of UTF-8 per message, 64 nesting levels, 16 entries per batch,
64 pending outbound calls, 64 active handlers and 64 queued messages. Handler
overflow rejects calls with `-32020`, counts dropped notifications and leaves
the reader available to receive responses. A full send queue closes the peer.
Oversized handler results or combined batches become resource errors. If an ID
itself leaves insufficient room for an error envelope, the bounded error uses
null ID. Callers should use short IDs and paginate large business results.

`MachineCaller` is a protocol implemented by Gateway:
`call(machine_id, method, params, *, timeout=60.0)`. Its named params must include
the target `session_id`; caller/session authentication is a separate concern.
