"""Online backup of a shadow runner's SQLite state (read-only on the source; safe while the runner is running).

Each run copies every `*.sqlite` file in the state directory with SQLite's backup API through a read-only
connection (`mode=ro`), so the copy is a consistent snapshot even while the runner writes (WAL mode) and the source
is never opened for writing. Each copy is checked with `PRAGMA integrity_check`, and a manifest records sizes,
SHA-256 digests and table row counts. Only the newest `--keep` backup sets are retained (older sets created by this
tool are deleted; nothing else is touched). Text files (pre-registration hash, STOP/KILL markers) are not copied:
the pre-registration lives in git, and STOP/KILL are operator controls.

Usage: python -m research.state_backup --state-dir <state> --dest <backup root> [--keep 14]
       python -m research.state_backup --verify <backup set dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "backup_manifest.json"
SET_PREFIX = "backup_"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_counts(con: sqlite3.Connection) -> dict:
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def backup_state(state_dir: Path, dest_root: Path, *, keep: int = 14, now: datetime | None = None) -> dict:
    state_dir, dest_root = Path(state_dir), Path(dest_root)
    if keep < 1:
        raise ValueError("keep must be >= 1")
    if not state_dir.is_dir():
        raise FileNotFoundError(f"state directory not found: {state_dir}")
    if dest_root.resolve() == state_dir.resolve() or state_dir.resolve() in dest_root.resolve().parents:
        raise ValueError("backup destination must be outside the state directory")
    now = now or datetime.now(timezone.utc)
    target = dest_root / f"{SET_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}"
    target.mkdir(parents=True, exist_ok=False)
    files = {}
    for src in sorted(state_dir.glob("*.sqlite")):
        out = target / src.name
        source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            copy = sqlite3.connect(str(out))
            try:
                source.backup(copy)
                integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
                counts = _row_counts(copy)
            finally:
                copy.close()
        finally:
            source.close()
        files[src.name] = {"bytes": out.stat().st_size, "sha256": _sha256(out), "integrity": integrity,
                           "rows": counts}
    manifest = {"created_at": now.isoformat(), "source": str(state_dir.resolve()), "files": files,
                "ok": all(f["integrity"] == "ok" for f in files.values())}
    (target / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    sets = sorted(p for p in dest_root.iterdir() if p.is_dir() and p.name.startswith(SET_PREFIX)
                  and (p / MANIFEST).exists())
    removed = []
    for old in sets[:-keep]:
        shutil.rmtree(old)
        removed.append(old.name)
    return {"backup": str(target), "ok": manifest["ok"], "files": {k: {"bytes": v["bytes"], "integrity":
            v["integrity"]} for k, v in files.items()}, "retained": min(len(sets), keep), "removed": removed}


def verify_backup(target: Path) -> dict:
    target = Path(target)
    manifest = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
    problems = []
    for name, meta in manifest["files"].items():
        path = target / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif _sha256(path) != meta["sha256"]:
            problems.append(f"{name}: digest mismatch")
    return {"backup": str(target), "ok": not problems and manifest["ok"], "problems": problems}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--verify", type=Path, default=None)
    args = parser.parse_args()
    if args.verify:
        result = verify_backup(args.verify)
    else:
        if not args.state_dir or not args.dest:
            parser.error("--state-dir and --dest are required")
        result = backup_state(args.state_dir, args.dest, keep=args.keep)
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
