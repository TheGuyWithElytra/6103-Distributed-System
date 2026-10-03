# UDP experiment results

Every row starts with a fresh server and sample data.
The harness uses real UDP on one computer; this is not a cross-computer demonstration.

| Semantics | Loss | Operation | Outcome | Attempts | Executions | Seats | Fare | Delay (min) |
|---|---|---|---|---:|---:|---:|---:|---:|
| at-least-once | no_loss | RESERVE | ok | 1 | 1 | 8 | 500.0 | 0 |
| at-least-once | first_request_lost | RESERVE | ok | 2 | 1 | 8 | 500.0 | 0 |
| at-least-once | first_reply_lost | RESERVE | ok | 2 | 2 | 6 | 500.0 | 0 |
| at-least-once | all_replies_lost | RESERVE | unknown | 4 | 4 | 2 | 500.0 | 0 |
| at-least-once | all_requests_lost | RESERVE | unknown | 4 | 0 | 10 | 500.0 | 0 |
| at-least-once | random_30_percent | RESERVE | ok | 4 | 2 | 6 | 500.0 | 0 |
| at-least-once | first_reply_lost | SET_FARE | ok | 2 | 2 | 10 | 700.0 | 0 |
| at-least-once | first_reply_lost | DELAY | ok | 2 | 2 | 10 | 500.0 | 20 |
| at-most-once | no_loss | RESERVE | ok | 1 | 1 | 8 | 500.0 | 0 |
| at-most-once | first_request_lost | RESERVE | ok | 2 | 1 | 8 | 500.0 | 0 |
| at-most-once | first_reply_lost | RESERVE | ok | 2 | 1 | 8 | 500.0 | 0 |
| at-most-once | all_replies_lost | RESERVE | unknown | 4 | 1 | 8 | 500.0 | 0 |
| at-most-once | all_requests_lost | RESERVE | unknown | 4 | 0 | 10 | 500.0 | 0 |
| at-most-once | random_30_percent | RESERVE | ok | 4 | 1 | 8 | 500.0 | 0 |
| at-most-once | first_reply_lost | SET_FARE | ok | 2 | 1 | 10 | 700.0 | 0 |
| at-most-once | first_reply_lost | DELAY | ok | 2 | 1 | 10 | 500.0 | 10 |

Unknown means no reply arrived before the retry limit; it does not imply no execution.
Random trials use seed 42, request loss 0.3, reply loss 0.3; they are illustrative, not statistical estimates.
At-most-once guarantees here assume one live server process retaining its request history.
