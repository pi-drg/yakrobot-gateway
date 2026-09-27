"""``yakrobot-py dataset publish`` — turn an exported v3 dataset into a listing.

Publishes to one of two stores (execution plan §0.8, design §0.5):

- ``--store r2``: upload the LeRobot tree to the operator's R2 bucket under
  ``datasets/{id}/{rev}/…``, preceded by a content-addressed ``manifest.json`` whose
  sha256 **is** the ``rev`` (so a new upload is a new prefix, and nothing is ever
  overwritten). The manifest is PUT **last**, so a half-finished upload is never valid.
- ``--store hf``: create a public repo, upload the tree, set gating to ``manual``, and
  record the commit sha as ``rev``.

Both stores then append a ``[[dataset]]`` entry to ``DATASETS_FILE`` (atomically). The
publish CLI reads the **write** credentials from the shell — ``HF_TOKEN`` for hf,
``R2_WRITE_ACCESS_KEY_ID``/``R2_WRITE_SECRET_ACCESS_KEY`` for r2 — never from the
gateway's ``.env``.
"""

import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def build_manifest(dataset_id: str, files) -> tuple[str, dict]:
    """Build the §0.8 manifest: sorted files, sorted keys, no whitespace.

    ``files`` is an iterable of ``(relative_path, bytes)`` (the LeRobot tree, excluding
    ``manifest.json`` and ``preview/*``). Returns ``(rev, manifest_dict)`` where ``rev``
    is the sha256 of the exact manifest bytes.
    """
    entries = sorted(
        (
            {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in files
        ),
        key=lambda e: e["path"],
    )
    manifest = {"format": 1, "dataset": dataset_id, "files": entries}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest(), manifest


def _manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def _walk_files(root: Path) -> list[tuple[str, bytes]]:
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if rel == "manifest.json" or rel.startswith("preview/"):
                continue
            files.append((rel, full.read_bytes()))
    return files


def _thumbnails(root: Path, max_n: int = 4) -> list[bytes]:
    """First frame of the first few episodes, decoded with PyAV; [] when it's absent."""
    try:
        import av
    except ImportError:
        return []
    thumbs: list[bytes] = []
    for mp4 in sorted((root / "videos").rglob("*.mp4")):
        if len(thumbs) >= max_n:
            break
        try:
            with av.open(str(mp4)) as container:
                for frame in container.decode(video=0):
                    buf = io.BytesIO()
                    frame.to_image().save(buf, "JPEG")
                    thumbs.append(buf.getvalue())
                    break
        except Exception:
            continue
    return thumbs


def _info(root: Path) -> dict:
    return json.loads((root / "meta" / "info.json").read_text())


def _listing_entry(*, dataset_id, robot, store, hub_repo, rev, episodes, frames,
                   price_usdc, price_cents, currency, license_, source) -> dict:
    entry = {
        "id": dataset_id,
        "robot": robot,
        "store": store,
        "rev": rev,
        "episodes": episodes,
        "frames": frames,
        "license": license_,
        "source": source,
    }
    if store == "hf":
        entry["hub_repo"] = hub_repo
    if price_usdc is not None:
        entry["price_usdc"] = price_usdc
    if price_cents is not None:
        entry["price_cents"] = price_cents
    if currency:
        entry["currency"] = currency
    return entry


def _append_listing(datasets_file: str | None, entry: dict) -> None:
    if not datasets_file:
        raise SystemExit("no DATASETS_FILE: pass --datasets-file or set DATASETS_FILE")
    path = Path(datasets_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[[dataset]]"]
    for key, value in entry.items():
        if value is None:
            continue
        lines.append(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {value}")
    block = "\n".join(lines) + "\n"
    existing = path.read_text() if path.exists() else ""
    new_content = (existing.rstrip() + "\n\n" + block) if existing.strip() else block
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_content)
    os.replace(tmp, path)


def _listed_repo_id(datasets_file: str | None, store: str, robot: str, dataset_id: str) -> bool:
    """True when this dataset id is already listed for this robot (any store)."""
    if not datasets_file:
        return False
    from core.datasets_listing import DatasetListings

    listings = DatasetListings(datasets_file, {robot})
    return listings.get(robot, dataset_id) is not None


def _publish_r2(root, *, dataset_id, robot, r2, license_, source, price_usdc,
                price_cents, currency, episodes, frames) -> str:
    import httpx

    from core.r2_presign import presign

    write_key = os.getenv("R2_WRITE_ACCESS_KEY_ID", "").strip()
    write_secret = os.getenv("R2_WRITE_SECRET_ACCESS_KEY", "").strip()
    if not write_key or not write_secret:
        raise SystemExit("r2 publish needs R2_WRITE_ACCESS_KEY_ID and R2_WRITE_SECRET_ACCESS_KEY")

    tree = _walk_files(root)
    rev, manifest = build_manifest(dataset_id, tree)

    def _put(key: str, data: bytes) -> None:
        url = presign(
            "PUT", r2.endpoint, r2.bucket, key,
            access_key=write_key, secret_key=write_secret,
            expires=3600, now=datetime.now(timezone.utc),
        )
        resp = httpx.put(url, content=data)
        if resp.status_code != 200:
            raise SystemExit(f"upload of {key} failed: HTTP {resp.status_code}")

    # Refuse a re-publish of the same rev: a GET of the existing manifest returns 200.
    check = presign(
        "GET", r2.endpoint, r2.bucket, f"datasets/{dataset_id}/{rev}/manifest.json",
        access_key=write_key, secret_key=write_secret,
        expires=300, now=datetime.now(timezone.utc),
    )
    if httpx.get(check).status_code == 200:
        raise SystemExit(f"datasets/{dataset_id}/{rev} already exists — nothing was overwritten")

    for rel, data in tree:
        _put(f"datasets/{dataset_id}/{rev}/{rel}", data)

    card = (root / "README.md").read_bytes()
    _put(f"datasets/{dataset_id}/{rev}/preview/README.md", card)
    for n, thumb in enumerate(_thumbnails(root)):
        _put(f"datasets/{dataset_id}/{rev}/preview/thumb_{n}.jpg", thumb)

    _put(f"datasets/{dataset_id}/{rev}/manifest.json", _manifest_bytes(manifest))
    return rev


def _publish_hf(root, *, dataset_id, robot, hub_repo, license_, source, price_usdc,
                price_cents, currency) -> str:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise SystemExit(
            "huggingface_hub is not installed — run `uv sync --extra datasets-hf`"
        ) from None

    token = os.getenv("HF_TOKEN", "").strip() or None
    api = HfApi(token=token)
    api.create_repo(hub_repo, repo_type="dataset", private=False, exist_ok=True)
    api.upload_folder(folder_path=str(root), repo_id=hub_repo, repo_type="dataset")
    api.update_repo_settings(hub_repo, gated="manual", repo_type="dataset")
    return api.dataset_info(hub_repo).sha


def publish(path, store, dataset_id, *, price_usdc=None, price_cents=None, currency="usd",
            license_=None, source="gateway", repo_id=None, datasets_file=None, robot=None):
    """Validate a v3 dataset, upload it to ``store``, and append its listing."""
    from .dataset_export import validate

    validate(path)  # raises SystemExit on any structural problem

    root = Path(path)
    info = _info(root)
    robot = robot or info.get("robot_type")
    if not robot:
        raise SystemExit("cannot determine the robot — re-export, or pass --robot")

    license_ = license_ or "cc-by-nc-4.0"

    # A listing may never re-use a repo: one listing = one frozen revision (design §4.1).
    if _listed_repo_id(datasets_file, store, robot, dataset_id):
        raise SystemExit(f"{dataset_id} is already listed — publish a new id/revision")

    if store == "hf":
        if not repo_id:
            raise SystemExit("--store hf needs --repo-id owner/name")
        rev = _publish_hf(
            root, dataset_id=dataset_id, robot=robot, hub_repo=repo_id, license_=license_,
            source=source, price_usdc=price_usdc, price_cents=price_cents, currency=currency,
        )
        hub_repo = repo_id
    elif store == "r2":
        from core.datasets_config import load_r2_config

        r2 = load_r2_config()
        if r2 is None:
            raise SystemExit("r2 publish needs the R2_* variables set")
        rev = _publish_r2(
            root, dataset_id=dataset_id, robot=robot, r2=r2, license_=license_, source=source,
            price_usdc=price_usdc, price_cents=price_cents, currency=currency,
            episodes=info.get("total_episodes", 1), frames=info.get("total_frames", 1),
        )
        hub_repo = None
    else:
        raise SystemExit(f"unknown store {store!r} — use hf or r2")

    _append_listing(
        datasets_file or os.getenv("DATASETS_FILE"),
        _listing_entry(
            dataset_id=dataset_id, robot=robot, store=store, hub_repo=hub_repo, rev=rev,
            episodes=info.get("total_episodes", 1), frames=info.get("total_frames", 1),
            price_usdc=price_usdc, price_cents=price_cents, currency=currency,
            license_=license_, source=source,
        ),
    )
    print(f"Published {dataset_id} to {store} (rev {rev})")
