"""Offline, bounded state restore for moving one stopped pipeline between hosts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


MAX_EXPORT_BYTES = 24 * 1024 * 1024


def policy_digest(pipeline):
    raw = json.dumps(pipeline.state_document(), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def restore_state(document, directory, pipeline):
    """Validate all databases before installing into a fresh private directory.

    This runs before the HTTP service starts. Existing state is never replaced.
    Source exports require processing to be paused; only one writer may resume.
    """
    if (not isinstance(document, dict) or document.get("schema") != "event-pipeline-state.v1"
            or document.get("source") != pipeline.spec["source"]["source"]
            or document.get("policy_digest") != policy_digest(pipeline)):
        raise ValueError("Imported state does not match the pipeline")
    rows = document.get("databases")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("Invalid database list")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(directory.glob("*.sqlite3")):
        raise FileExistsError("Refusing to replace existing pipeline state")
    total, names = 0, set()
    staged = []
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=directory) as temporary:
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"binding", "sha256", "sqlite_base64"}:
                raise ValueError("Invalid database entry")
            binding = row["binding"]
            if not isinstance(binding, str) or not 1 <= len(binding) <= 128:
                raise ValueError("Invalid binding identity")
            name = "relay-ingest.sqlite3" if binding == "__relay_ingest__" else hashlib.sha256(binding.encode()).hexdigest() + ".sqlite3"
            if name in names:
                raise ValueError("Duplicate binding")
            names.add(name)
            encoded = row["sqlite_base64"]
            if not isinstance(encoded, str) or len(encoded) > 16 * 1024 * 1024:
                raise ValueError("Database exceeds migration limit")
            raw = base64.b64decode(encoded, validate=True)
            total += len(raw)
            if total > 15 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("Invalid database size or checksum")
            path = Path(temporary) / name
            with path.open("xb") as output:
                path.chmod(0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            try:
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                try:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("Database integrity check failed")
                    metadata = dict(db.execute("SELECT key,value FROM meta"))
                    if (json.loads(metadata["binding_id"]) != binding
                            or json.loads(metadata["policy_digest"]) != document["policy_digest"]):
                        raise ValueError("Database metadata differs from export")
                finally:
                    db.close()
            except (sqlite3.DatabaseError, KeyError) as exc:
                raise ValueError("Invalid pipeline database") from exc
            staged.append((path, directory / name))
        # Hard links provide exclusive installation: no overwrite even after a race.
        installed = []
        try:
            for source, target in staged:
                os.link(source, target)
                installed.append(target)
        except OSError:
            for target in installed:
                target.unlink()
            raise
    return len(staged)
