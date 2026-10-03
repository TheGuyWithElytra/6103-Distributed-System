"""Application-level simulation: drop before dispatch or before reply send."""

import random


class Faults:
    def __init__(self, drop_requests=(), drop_replies=(), request_rate=0.0,
                 reply_rate=0.0, seed=1):
        if not 0 <= request_rate <= 1 or not 0 <= reply_rate <= 1:
            raise ValueError("loss rates must be between 0 and 1")
        self.indices = {"request": set(drop_requests), "reply": set(drop_replies)}
        self.rates = {"request": request_rate, "reply": reply_rate}
        self.counts = {"request": 0, "reply": 0}
        self.dropped = {"request": 0, "reply": 0}
        self.rng = random.Random(seed)

    def drop(self, direction):
        self.counts[direction] += 1
        drop = (self.counts[direction] in self.indices[direction]
                or self.rng.random() < self.rates[direction])
        if drop:
            self.dropped[direction] += 1
        return drop
