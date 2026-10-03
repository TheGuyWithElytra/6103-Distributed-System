"""Reproducible experiments over real loopback UDP, with machine-readable evidence."""

import argparse
import json
import time
from pathlib import Path

from .client import FlightClient, OutcomeUnknown
from .faults import Faults
from .harness import running_server
from .protocol import Op


def experiment(mode, scenario, op, args):
    logs = []
    fault_args = {
        "no_loss": {},
        "first_request_lost": {"drop_requests": {1}},
        "first_reply_lost": {"drop_replies": {1}},
        "all_replies_lost": {"reply_rate": 1.0},
        "all_requests_lost": {"request_rate": 1.0},
        "random_30_percent": {"request_rate": 0.3, "reply_rate": 0.3, "seed": 42},
    }[scenario]
    with running_server(mode, Faults(**fault_args), logs.append) as server:
        client = FlightClient(*server.address, timeout=0.08, attempts=4, logger=logs.append)
        initial_departure = server.flights[101].departure
        start = time.monotonic()
        outcome, reply = "ok", None
        try:
            reply = client.invoke(op, *args)
        except OutcomeUnknown:
            outcome = "unknown"
        finally:
            elapsed = (time.monotonic() - start) * 1000
            client.close()
    # Inspect state only after the harness stops, not with a new lossy request.
    flight = server.flights[101]
    row = {
        "semantics": mode, "scenario": scenario, "operation": op.name,
        "outcome": outcome, "attempts": client.last_attempts,
        "successful_executions": server.executions[op.name],
        "seats": flight.seats, "fare": flight.fare,
        "delay_minutes": (flight.departure - initial_departure) // 60,
        "elapsed_ms": round(elapsed, 2), "reply": reply,
        "dropped": server.faults.dropped,
    }
    if scenario == "first_reply_lost":
        executions = 2 if mode == "at-least-once" else 1
        assert row["successful_executions"] == executions, row
        if op == Op.RESERVE:
            assert row["seats"] == 10 - 2 * executions, row
        elif op == Op.SET_FARE:
            assert row["fare"] == 700, row
        elif op == Op.DELAY:
            assert row["delay_minutes"] == 10 * executions, row
    elif scenario in ("no_loss", "first_request_lost"):
        assert row["successful_executions"] == 1 and row["seats"] == 8, row
    elif scenario == "all_requests_lost":
        assert outcome == "unknown" and row["seats"] == 10, row
    elif scenario == "all_replies_lost":
        assert outcome == "unknown", row
        assert row["seats"] == (2 if mode == "at-least-once" else 8), row
    return row, logs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", default="output/experiments")
    args = p.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    cases = [(scenario, Op.RESERVE, (101, 2)) for scenario in (
        "no_loss", "first_request_lost", "first_reply_lost", "all_replies_lost",
        "all_requests_lost", "random_30_percent")]
    cases += [("first_reply_lost", Op.SET_FARE, (101, 700.0)),
              ("first_reply_lost", Op.DELAY, (101, 10))]
    for mode in ("at-least-once", "at-most-once"):
        for scenario, op, op_args in cases:
            row, logs = experiment(mode, scenario, op, op_args)
            rows.append(row)
            (output / f"{mode}_{scenario}_{op.name.lower()}.log").write_text("\n".join(logs) + "\n", encoding="utf-8")
            print(f"{mode:14} {scenario:19} {op.name:8} outcome={row['outcome']:7} "
                  f"executions={row['successful_executions']} seats={row['seats']} delay={row['delay_minutes']}")
    (output / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    lines = ["# UDP experiment results", "", "Every row starts with a fresh server and sample data.",
             "The harness uses real UDP on one computer; this is not a cross-computer demonstration.", "",
             "| Semantics | Loss | Operation | Outcome | Attempts | Executions | Seats | Fare | Delay (min) |",
             "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['semantics']} | {r['scenario']} | {r['operation']} | {r['outcome']} | "
                     f"{r['attempts']} | {r['successful_executions']} | {r['seats']} | {r['fare']} | {r['delay_minutes']} |")
    lines += ["", "Unknown means no reply arrived before the retry limit; it does not imply no execution.",
              "Random trials use seed 42, request loss 0.3, reply loss 0.3; they are illustrative, not statistical estimates.",
              "At-most-once guarantees here assume one live server process retaining its request history.", ""]
    (output / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {len(rows)} cases to {output.resolve()}")


if __name__ == "__main__":
    main()
