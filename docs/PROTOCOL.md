# Flight UDP protocol v1

This specification is the contract for the Python implementation and a future client/server in another language. One UDP datagram contains exactly one message. Maximum datagram length is 1200 bytes. All multi-byte integers use big-endian/network byte order. Signed integers use two's complement. There is no object serialization, text envelope, stream framing, or automatic RPC layer.

## Header: 28 bytes

| Offset | Bytes | Field | Representation |
|---:|---:|---|---|
| 0 | 2 | magic | ASCII `FL`, hexadecimal `46 4c` |
| 2 | 1 | version | unsigned, 1 |
| 3 | 1 | kind | 1=request, 2=reply, 3=callback |
| 4 | 1 | operation | 1..6, below |
| 5 | 1 | reserved | must be zero |
| 6 | 8 | session | unsigned 64-bit, random per client process |
| 14 | 8 | request | unsigned 64-bit, monotonically increasing per session |
| 22 | 2 | status | unsigned 16-bit; requests/callbacks must use zero |
| 24 | 4 | body length | unsigned 32-bit, must equal datagram length minus 28 |
| 28 | variable | body | operation-specific |

The same logical invocation retains exactly the same session, request number, operation and body on every retry. A new user operation gets a new number. Replies echo session, request number and operation. Callbacks use the original MONITOR request's session and request number.

## Primitive types

- `i32`: signed 32-bit integer.
- `u16`, `u32`, `u64`: unsigned integers of the indicated width.
- `i64`: signed 64-bit integer; departure time is UTC Unix seconds.
- `f64`: finite IEEE 754 binary64 in big-endian order. Negative fares are rejected by business validation. NaN and infinity are rejected by the codec.
- `text`: `u16 byte_count` followed by exactly that many UTF-8 bytes. Maximum 512 bytes per string. No NUL terminator. The count is bytes, not Unicode characters.

Every successful reply uses status 0. Every error reply, regardless of operation, has a single `text` body explaining the error. Extra bytes after any payload are invalid.

## Operations and payloads

| ID | Operation | Request | Successful reply |
|---:|---|---|---|
| 1 | SEARCH | `text source, text destination` | `u16 count, i32 flight_id[count]` |
| 2 | DETAILS | `i32 flight_id` | `i32 flight_id, text source, text destination, i64 departure, f64 fare, i32 seats, u64 version` |
| 3 | RESERVE | `i32 flight_id, i32 count` | `i32 remaining_seats, u64 version` |
| 4 | MONITOR | `i32 flight_id, u32 interval_ms` | `u32 interval_ms` |
| 5 | SET_FARE | `i32 flight_id, f64 new_fare` | `f64 resulting_fare` |
| 6 | DELAY | `i32 flight_id, i32 minutes` | `i64 resulting_departure` |

Callback payload: kind=3, operation=4, status=0, body=`i32 flight_id, i32 remaining_seats, u64 version`. Flight seat version starts at zero and increments only on successful reservation. Duplicate cached requests do not increment it or emit notifications.

SEARCH trims surrounding whitespace and compares case-insensitively. It returns all matches in server dataset order. Empty routes are invalid; no matches is NOT_FOUND. The server rejects more than 292 matches instead of returning a partial list. Current data contains four flights. A future larger dataset requires pagination or an explicitly designed fragmentation protocol.

RESERVE requires positive count no greater than available seats. MONITOR accepts 1..3,600,000 ms. SET_FARE accepts finite nonnegative values. DELAY accepts positive minutes and rejects results beyond 9999-12-31T23:59:59Z to keep console formatting valid. No cancellation or refund is implied by these operations.

## Status codes

| Code | Name | Meaning |
|---:|---|---|
| 0 | OK | Success |
| 1 | BAD_REQUEST | Malformed body or invalid business argument |
| 2 | NOT_FOUND | Flight or matching route absent |
| 3 | INSUFFICIENT_SEATS | Reservation exceeds availability |
| 4 | REQUEST_CONFLICT | Same session/request with different packet contents in at-most-once mode |
| 5 | TOO_LARGE | Result cannot fit in one datagram |

Malformed headers, unknown versions/opcodes, oversized datagrams and non-request datagrams sent to the server are discarded rather than answered. Valid request headers with malformed bodies receive BAD_REQUEST. This distinction avoids constructing responses from untrusted/unparseable identifiers.

## Independent byte example

DETAILS request for flight 101, session 1, request 2:

```text
46 4c             magic
01                version
01                kind = REQUEST
02                operation = DETAILS
00                reserved
00 00 00 00 00 00 00 01  session
00 00 00 00 00 00 00 02  request
00 00             status
00 00 00 04       payload length
00 00 00 65       flight_id = 101
```

The full message has 32 bytes. The UTF-8 string `新` encodes as `00 03 e6 96 b0`. Fare 500.0 encodes as `40 7f 40 00 00 00 00 00`. These byte fixtures are checked in the test suite so a cross-language implementation can validate itself independently.

## Delivery and state

- Client retries a byte-identical message after each deadline, with a finite attempt limit. Replies must match source IP/port, session, request and operation.
- At-least-once mode dispatches every delivered request. This mode can execute a non-idempotent operation repeatedly; finite retry exhaustion may still mean zero executions.
- At-most-once mode stores the original packet and encoded reply under `(session, request)`. Replays return the stored result. Different content under the same key returns REQUEST_CONFLICT. Errors are cached as well as successes.
- The server records the result before callbacks/reply transmission and before simulated reply loss. No crash-atomic or restart-persistent guarantee is claimed.
- Requests are serialized in the server event loop. Source addresses come from UDP receive, not a claimed address in the message body.
- Server monotonic time determines subscription expiry; periodic 100 ms receive timeouts clean up expired entries. The server also checks expiry immediately before sending callbacks.
- Registrations use `(session, request)` as the subscription key. Under at-least-once, re-execution replaces this registration and restarts its interval. Under at-most-once, cache replay does not modify it.
- Notifications are best effort. Client drops old versions and unrelated subscription IDs. Notifications received while waiting for a registration ACK are temporarily queued (maximum 1024). Server and client clock synchronization is not required.
- Client waits the returned interval after receiving the registration ACK. Delayed ACKs can produce a quiet tail after server expiry; no early cancellation message is sent.
- Invocation semantics are a server startup configuration, not a field negotiated on the wire.
