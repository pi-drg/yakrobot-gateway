"""Load and hot-reload the ``DATASETS_FILE`` catalogue (execution plan §0.6).

A TOML file of ``[[dataset]]`` entries, one per dataset the operator lists for sale.
Parsed with stdlib ``tomllib`` and re-read on mtime change, exactly the way
``core.server.FileTokenVerifier`` hot-reloads token files — a missing file means "no
listings", never an error. An entry with any validation error is skipped and logged
(warning, once per mtime) rather than poisoning the whole file; the whole file is only
rejected (treated as empty) when it does not parse as TOML.

Credentials are required lazily: an ``hf`` listing is skipped when ``HF_TOKEN`` is
unset, an ``r2`` listing when the ``R2_*`` variables are unset, because a gateway that
lists nothing must not demand credentials at startup.
"""

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeGuard

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_HF_REV_RE = re.compile(r"[0-9a-f]{40}")
_R2_REV_RE = re.compile(r"[0-9a-f]{64}")
_PRICE_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")
_CURRENCY_RE = re.compile(r"[a-z]{3}")

STORES = ("hf", "r2")
SOURCES = ("gateway", "lelab")


@dataclass(frozen=True)
class Listing:
    id: str
    robot: str
    store: str
    hub_repo: str | None
    rev: str
    episodes: int
    frames: int
    price_usdc: str | None
    price_cents: int | None
    currency: str
    license: str
    source: str

    def index_entry(self) -> dict:
        """The per-robot ``datasets`` element reported on ``GET /`` (§0.7)."""
        preview = (
            f"https://huggingface.co/datasets/{self.hub_repo}"
            if self.store == "hf"
            else f"/{self.robot}/datasets/{self.id}/preview"
        )
        return {
            "id": self.id,
            "store": self.store,
            "rev": self.rev,
            "episodes": self.episodes,
            "frames": self.frames,
            "price_usdc": self.price_usdc,
            "price_cents": self.price_cents,
            "currency": self.currency,
            "license": self.license,
            "source": self.source,
            "hub_repo": self.hub_repo,
            "preview": preview,
        }


def _is_int(value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


class DatasetListings:
    def __init__(self, path: str | None, plugin_names: set[str]) -> None:
        self._path = (path or "").strip()
        self._plugins = plugin_names
        self._mtime: float | None = None
        self._listings: list[Listing] = []
        self._refresh()

    def _refresh(self) -> None:
        if not self._path:
            self._listings = []
            self._mtime = None
            return
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            self._listings = []  # missing file = no listings
            self._mtime = None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime

        try:
            with open(self._path, "rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            logger.warning(
                "DATASETS_FILE %s does not parse as TOML — treating it as empty", self._path
            )
            self._listings = []
            return

        parsed: list[Listing] = []
        seen: set[str] = set()
        for entry in data.get("dataset", []):
            listing = self._parse(entry)
            if listing is None:
                continue
            if listing.id in seen:
                logger.warning("dataset listing %r skipped: duplicate id", listing.id)
                continue
            seen.add(listing.id)
            parsed.append(listing)
        self._listings = parsed

    def _warn(self, dataset_id, reason: str) -> None:
        # Once per mtime, not once per access — _refresh re-parses only on mtime change.
        logger.warning("dataset listing %r skipped: %s", dataset_id, reason)

    def _parse(self, entry) -> Listing | None:
        if not isinstance(entry, dict):
            return None

        dataset_id = entry.get("id")
        if not isinstance(dataset_id, str) or not _ID_RE.fullmatch(dataset_id):
            self._warn(dataset_id, "invalid id")
            return None

        robot = entry.get("robot")
        if robot not in self._plugins:
            self._warn(dataset_id, f"unknown robot {robot!r}")
            return None

        store = entry.get("store")
        if store not in STORES:
            self._warn(dataset_id, "store must be hf or r2")
            return None

        hub_repo = entry.get("hub_repo")
        if store == "hf":
            if not isinstance(hub_repo, str) or not _REPO_RE.fullmatch(hub_repo):
                self._warn(dataset_id, "hub_repo must look like owner/name")
                return None
        else:
            hub_repo = None

        rev = entry.get("rev")
        rev_re = _HF_REV_RE if store == "hf" else _R2_REV_RE
        if not isinstance(rev, str) or not rev_re.fullmatch(rev):
            self._warn(dataset_id, "invalid rev")
            return None

        episodes = entry.get("episodes")
        if not _is_int(episodes) or episodes < 1:
            self._warn(dataset_id, "episodes must be an int >= 1")
            return None

        frames = entry.get("frames")
        if not _is_int(frames) or frames < 1:
            self._warn(dataset_id, "frames must be an int >= 1")
            return None

        price_usdc = entry.get("price_usdc")
        if price_usdc is not None:
            if not isinstance(price_usdc, str) or not _PRICE_RE.fullmatch(price_usdc) or Decimal(price_usdc) <= 0:
                self._warn(dataset_id, "invalid price_usdc")
                return None

        price_cents = entry.get("price_cents")
        if price_cents is not None:
            if not _is_int(price_cents) or price_cents < 50:
                self._warn(dataset_id, "price_cents must be an int >= 50")
                return None

        currency = entry.get("currency", "usd")
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            self._warn(dataset_id, "currency must be 3 lowercase letters")
            return None

        license_ = entry.get("license")
        if not isinstance(license_, str) or not license_.strip():
            self._warn(dataset_id, "license is required")
            return None

        source = entry.get("source")
        if source not in SOURCES:
            self._warn(dataset_id, "source must be gateway or lelab")
            return None

        # Credentials are required lazily, per store (§0.6).
        from core.datasets_config import hf_token, load_r2_config

        if store == "hf" and hf_token() is None:
            self._warn(dataset_id, "HF_TOKEN not set")
            return None
        if store == "r2" and load_r2_config() is None:
            self._warn(dataset_id, "R2 not set")
            return None

        return Listing(
            id=dataset_id, robot=robot, store=store, hub_repo=hub_repo, rev=rev,
            episodes=episodes, frames=frames, price_usdc=price_usdc,
            price_cents=price_cents, currency=currency, license=license_, source=source,
        )

    def get(self, robot: str, dataset_id: str) -> Listing | None:
        self._refresh()
        for listing in self._listings:
            if listing.robot == robot and listing.id == dataset_id:
                return listing
        return None

    def for_robot(self, robot: str) -> list[Listing]:
        self._refresh()
        return [l for l in self._listings if l.robot == robot]
