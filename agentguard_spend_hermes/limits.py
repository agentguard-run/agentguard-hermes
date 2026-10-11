"""Launch reservations on a rolling clock that excludes inactive gaps."""

from __future__ import annotations

import threading
import time
from collections import deque

DEFAULTS = {
    "short_launches": 15,
    "short_minutes": 15,
    "long_launches": 40,
    "long_minutes": 120,
    "idle_seconds": 60,
}


class DelegationLimit:
    def __init__(self, settings=None, clock=time.monotonic):
        self.settings = {**DEFAULTS, **(settings or {})}
        if any(
            type(value) is not int or value <= 0 for value in self.settings.values()
        ):
            raise ValueError("Delegation limits must be positive integers")
        if set(self.settings) != set(DEFAULTS):
            raise ValueError("Unknown delegation limit setting")
        # `lock` serializes launch decisions; a tool gate holds it through a
        # person's approval. `_state` guards the clock and counters only and
        # is never held while waiting, so model calls never queue behind an
        # approval prompt.
        self.clock, self.lock = clock, threading.RLock()
        self._state = threading.Lock()
        self.last = None
        self.active = 0.0
        self.launches = deque()
        self.in_flight = 0

    def touch(self):
        with self._state:
            self._touch()

    def _touch(self):
        now = self.clock()
        if self.last is not None:
            elapsed = max(0, now - self.last)
            self.active += (
                elapsed
                if self.in_flight
                else min(elapsed, self.settings["idle_seconds"])
            )
        self.last = now
        retention = (
            max(self.settings["short_minutes"], self.settings["long_minutes"]) * 60
        )
        while self.launches and self.launches[0][0] <= self.active - retention:
            self.launches.popleft()

    def exceeded(self, count):
        with self._state:
            self._touch()
            return self._exceeded(count)

    def snapshot(self):
        with self._state:
            # A status poll is not agent activity. Project the same active
            # clock without moving its anchor, or a Desktop timer would
            # gradually clear launch history while the agent is idle.
            elapsed = max(0, self.clock() - self.last) if self.last is not None else 0
            active = self.active + (elapsed if self.in_flight else min(elapsed, self.settings["idle_seconds"]))
            return {
                name: {"used": sum(n for at, n in self.launches
                                   if at > active - self.settings[f"{name}_minutes"] * 60),
                       "limit": self.settings[f"{name}_launches"],
                       "minutes": self.settings[f"{name}_minutes"]}
                for name in ("short", "long")
            }

    def _exceeded(self, count):
        return any(
            sum(
                n
                for at, n in self.launches
                if at > self.active - self.settings[f"{name}_minutes"] * 60
            )
            + count
            > self.settings[f"{name}_launches"]
            for name in ("short", "long")
        )

    def reserve(self, count):
        with self._state:
            self.launches.append((self.active, count))

    def begin(self):
        with self._state:
            self._touch()
            self.in_flight += 1

    def end(self):
        with self._state:
            self._touch()
            self.in_flight -= 1


def launch_count(args):
    action = args.get("action", "spawn")
    if action in ("list", "steer", "stop"):
        return 0
    if action != "spawn":
        raise ValueError("Invalid delegation action")
    if "tasks" in args:
        tasks = args["tasks"]
        if (
            not isinstance(tasks, list)
            or not tasks
            or any(
                not isinstance(task, dict)
                or not isinstance(task.get("goal"), str)
                or not task["goal"].strip()
                for task in tasks
            )
        ):
            raise ValueError("Invalid delegation tasks")
        return len(tasks)
    if isinstance(args.get("goal"), str) and args["goal"].strip():
        return 1
    raise ValueError("Missing delegation goal")
