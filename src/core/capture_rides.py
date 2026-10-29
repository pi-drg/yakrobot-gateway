"""Which captures have already started a ride (capture-flag design G4), on disk.

One capture, one ride. A capture is named by ``(robot, player, t)``, where ``t`` is its
on-chain time from ``lastCapture``; once a reserve succeeds for it, it is recorded here
and refused after. Kept in a small SQLite file, not in memory, so a restart (a friend
rebooting the Pi) doesn't let a capture still inside ``VALID_MINUTES`` ride again. Rows
older than the window can never match a recent capture, so they're pruned as they age.

Plain ``sqlite3`` from the standard library: a few rows, one writer (this process), and
each write committed before the ride is granted, so a power cut can't forget one.
"""

import os
import sqlite3


class RideLedger:
    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        # Autocommit: every statement is its own transaction, durable on return.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS rides ("
            " robot TEXT NOT NULL, player TEXT NOT NULL, t INTEGER NOT NULL,"
            " exp INTEGER NOT NULL, PRIMARY KEY (robot, player, t))"
        )

    def used(self, robot: str, player: str, t: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM rides WHERE robot = ? AND player = ? AND t = ?", (robot, player, t)
        ).fetchone()
        return row is not None

    def record(self, robot: str, player: str, t: int, exp: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO rides (robot, player, t, exp) VALUES (?, ?, ?, ?)",
            (robot, player, t, exp),
        )

    def prune(self, before_t: int) -> None:
        """Forget captures made before ``before_t``: too old to reserve with anyway."""
        self._db.execute("DELETE FROM rides WHERE t < ?", (before_t,))
