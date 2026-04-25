from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def default_codex_home() -> Path:
    return Path.home() / ".codex"


@dataclass
class Paths:
    codex_home: Path
    config_path: Path
    db_path: Path
    backup_dir: Path


def resolve_paths(codex_home: str | None) -> Paths:
    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    return Paths(
        codex_home=home,
        config_path=home / "config.toml",
        db_path=home / "state_5.sqlite",
        backup_dir=home / "history_sync_backups",
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_current_provider(config_text: str) -> str | None:
    match = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"', config_text)
    return match.group(1) if match else None


def parse_current_model(config_text: str) -> str | None:
    match = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', config_text)
    return match.group(1) if match else None


def connect_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def infer_current_provider(conn: sqlite3.Connection, current_model: str | None) -> tuple[str, str]:
    order_by_recent = "ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC"
    if current_model:
        row = conn.execute(
            f"""
            SELECT model_provider
            FROM threads
            WHERE model = ?
              AND model_provider <> ''
            {order_by_recent}
            LIMIT 1
            """,
            (current_model,),
        ).fetchone()
        if row:
            return str(row[0]), f"latest thread using model {current_model}"

    row = conn.execute(
        f"""
        SELECT model_provider
        FROM threads
        WHERE model_provider <> ''
        {order_by_recent}
        LIMIT 1
        """
    ).fetchone()
    if row:
        return str(row[0]), "latest thread"

    raise RuntimeError(
        "Could not find model_provider in config.toml and could not infer one from state_5.sqlite."
    )


def ensure_environment(paths: Paths) -> None:
    if not paths.config_path.exists():
        raise RuntimeError(f"Missing config file: {paths.config_path}")
    if not paths.db_path.exists():
        raise RuntimeError(f"Missing database file: {paths.db_path}")


def query_provider_counts(conn: sqlite3.Connection) -> OrderedDict[str, int]:
    counts = OrderedDict()
    for provider, count in conn.execute(
        """
        SELECT model_provider, COUNT(*)
        FROM threads
        GROUP BY model_provider
        ORDER BY COUNT(*) DESC, model_provider ASC
        """
    ):
        counts[provider or "(empty)"] = count
    return counts


def query_thread_rows(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = []
    for thread_id, rollout_path, model_provider in conn.execute(
        """
        SELECT id, rollout_path, model_provider
        FROM threads
        ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC
        """
    ):
        rows.append(
            {
                "id": str(thread_id),
                "rollout_path": str(rollout_path),
                "model_provider": str(model_provider),
            }
        )
    return rows


def parse_backup_name(backup_path: Path) -> tuple[str, str] | None:
    match = re.match(r"^state_5\.sqlite\.(.+)\.(\d{8}-\d{6})\.bak$", backup_path.name)
    if not match:
        return None
    return match.group(1), match.group(2)


def session_meta_backup_path(paths: Paths, label: str, timestamp: str) -> Path:
    return paths.backup_dir / f"session_meta.{label}.{timestamp}.jsonl"


def associated_session_meta_backup(paths: Paths, backup_path: Path) -> Path | None:
    parts = parse_backup_name(backup_path)
    if not parts:
        return None
    label, timestamp = parts
    path = session_meta_backup_path(paths, label, timestamp)
    return path if path.exists() else None


def list_backups(paths: Paths, limit: int = 20) -> list[dict[str, str]]:
    if not paths.backup_dir.exists():
        return []
    files = sorted(
        paths.backup_dir.glob("state_5.sqlite.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    output = []
    for item in files[:limit]:
        metadata_backup = associated_session_meta_backup(paths, item)
        output.append(
            {
                "name": item.name,
                "path": str(item),
                "modified_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds"),
                "session_meta_backup": str(metadata_backup) if metadata_backup else None,
            }
        )
    return output


def get_status(paths: Paths) -> dict[str, object]:
    ensure_environment(paths)
    config_text = read_text(paths.config_path)
    configured_provider = parse_current_provider(config_text)
    current_model = parse_current_model(config_text)

    with connect_db(paths.db_path, readonly=True) as conn:
        if configured_provider:
            current_provider = configured_provider
            current_provider_source = "config.toml"
        else:
            current_provider, current_provider_source = infer_current_provider(conn, current_model)
        thread_rows = query_thread_rows(conn)
        counts = query_provider_counts(conn)
        total_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        moved_if_sync = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE model_provider <> ?",
            (current_provider,),
        ).fetchone()[0]
        rollout_metadata = query_rollout_metadata_status(str(current_provider), thread_rows)

    return {
        "codex_home": str(paths.codex_home),
        "config_path": str(paths.config_path),
        "db_path": str(paths.db_path),
        "backup_dir": str(paths.backup_dir),
        "current_provider": current_provider,
        "current_provider_source": current_provider_source,
        "current_model": current_model,
        "total_threads": total_threads,
        "movable_threads": moved_if_sync,
        "rollout_metadata_mismatches": rollout_metadata["mismatched_files"],
        "rollout_metadata": rollout_metadata,
        "provider_counts": [{"provider": key, "count": value} for key, value in counts.items()],
        "backups": list_backups(paths),
    }


def make_backup(paths: Paths, label: str, timestamp: str | None = None) -> Path:
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = paths.backup_dir / f"state_5.sqlite.{label}.{timestamp}.bak"
    with connect_db(paths.db_path, readonly=True) as source, connect_db(backup_path, readonly=False) as target:
        source.backup(target)
    return backup_path


def split_line_ending(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body) :]


def backup_session_meta(paths: Paths, label: str, timestamp: str, rows: list[dict[str, str]]) -> dict[str, object]:
    manifest_path = session_meta_backup_path(paths, label, timestamp)
    entries = []
    skipped = []
    seen = set()

    for row in rows:
        rollout_path = row["rollout_path"]
        if rollout_path in seen:
            continue
        seen.add(rollout_path)
        path = Path(rollout_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
        except OSError as exc:
            skipped.append({"path": rollout_path, "reason": str(exc)})
            continue
        if not first_line:
            skipped.append({"path": rollout_path, "reason": "empty rollout file"})
            continue
        entries.append(
            {
                "id": row["id"],
                "rollout_path": rollout_path,
                "original_first_line": first_line,
            }
        )

    if entries:
        manifest_path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )

    return {
        "path": str(manifest_path) if entries else None,
        "entries": len(entries),
        "skipped": skipped,
    }


def read_rollout_provider(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError as exc:
        return None, str(exc)
    if not first_line:
        return None, "empty rollout file"

    first_line_body, _line_ending = split_line_ending(first_line)
    try:
        event = json.loads(first_line_body)
    except json.JSONDecodeError as exc:
        return None, f"invalid first-line JSON: {exc}"

    payload = event.get("payload")
    if event.get("type") != "session_meta" or not isinstance(payload, dict):
        return None, "first line is not a session_meta event"

    provider = payload.get("model_provider")
    if not isinstance(provider, str):
        return None, "session_meta is missing model_provider"
    return provider, None


def query_rollout_metadata_status(current_provider: str, rows: list[dict[str, str]]) -> dict[str, object]:
    total_files = 0
    matched_files = 0
    mismatched_files = 0
    skipped = []
    seen = set()

    for row in rows:
        rollout_path = row["rollout_path"]
        if rollout_path in seen:
            continue
        seen.add(rollout_path)
        total_files += 1
        provider, error = read_rollout_provider(Path(rollout_path))
        if error:
            skipped.append({"path": rollout_path, "reason": error})
            continue
        if provider == current_provider:
            matched_files += 1
        else:
            mismatched_files += 1

    return {
        "total_files": total_files,
        "matched_files": matched_files,
        "mismatched_files": mismatched_files,
        "skipped": skipped,
    }


def replace_first_line(path: Path, new_first_line: str) -> None:
    with path.open("r", encoding="utf-8") as handle:
        handle.readline()
        rest = handle.read()
    tmp_path = path.with_name(f".{path.name}.history-sync.tmp")
    tmp_path.write_text(new_first_line + rest, encoding="utf-8")
    tmp_path.replace(path)


def sync_rollout_metadata(current_provider: str, rows: list[dict[str, str]]) -> dict[str, object]:
    updated_files = 0
    already_current = 0
    skipped = []
    seen = set()

    for row in rows:
        rollout_path = row["rollout_path"]
        if rollout_path in seen:
            continue
        seen.add(rollout_path)
        path = Path(rollout_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
        except OSError as exc:
            skipped.append({"path": rollout_path, "reason": str(exc)})
            continue
        if not first_line:
            skipped.append({"path": rollout_path, "reason": "empty rollout file"})
            continue

        first_line_body, line_ending = split_line_ending(first_line)
        try:
            event = json.loads(first_line_body)
        except json.JSONDecodeError as exc:
            skipped.append({"path": rollout_path, "reason": f"invalid first-line JSON: {exc}"})
            continue

        payload = event.get("payload")
        if event.get("type") != "session_meta" or not isinstance(payload, dict):
            skipped.append({"path": rollout_path, "reason": "first line is not a session_meta event"})
            continue

        if payload.get("model_provider") == current_provider:
            already_current += 1
            continue

        payload["model_provider"] = current_provider
        new_first_line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + line_ending
        try:
            replace_first_line(path, new_first_line)
        except OSError as exc:
            skipped.append({"path": rollout_path, "reason": str(exc)})
            continue
        updated_files += 1

    return {
        "updated_files": updated_files,
        "already_current": already_current,
        "skipped": skipped,
    }


def restore_session_meta_backup(paths: Paths, db_backup_path: Path) -> dict[str, object]:
    manifest_path = associated_session_meta_backup(paths, db_backup_path)
    if not manifest_path:
        return {"path": None, "restored_files": 0, "skipped": []}

    restored_files = 0
    skipped = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        entry = json.loads(line)
        path = Path(entry["rollout_path"])
        if not path.exists():
            skipped.append({"path": str(path), "reason": "rollout file does not exist"})
            continue
        try:
            replace_first_line(path, entry["original_first_line"])
        except OSError as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
            continue
        restored_files += 1

    return {
        "path": str(manifest_path),
        "restored_files": restored_files,
        "skipped": skipped,
    }


def checkpoint(conn: sqlite3.Connection) -> tuple[int, int, int]:
    row = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def sync_to_current_provider(paths: Paths) -> dict[str, object]:
    status_before = get_status(paths)
    current_provider = status_before["current_provider"]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    with connect_db(paths.db_path, readonly=True) as conn:
        thread_rows = query_thread_rows(conn)

    backup_path = make_backup(paths, "pre-sync", timestamp)
    session_meta_backup = backup_session_meta(paths, "pre-sync", timestamp, thread_rows)

    with connect_db(paths.db_path, readonly=False) as conn:
        before_counts = query_provider_counts(conn)
        updated_rows = conn.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider <> ?",
            (current_provider, current_provider),
        ).rowcount
        conn.commit()
        checkpoint_result = checkpoint(conn)
        after_counts = query_provider_counts(conn)

    rollout_metadata = sync_rollout_metadata(str(current_provider), thread_rows)
    status_after = get_status(paths)
    rollout_metadata_remaining = int(status_after["rollout_metadata_mismatches"])
    return {
        "action": "sync",
        "current_provider": current_provider,
        "updated_rows": updated_rows,
        "backup_path": str(backup_path),
        "session_meta_backup": session_meta_backup,
        "rollout_metadata": rollout_metadata,
        "before_counts": [{"provider": key, "count": value} for key, value in before_counts.items()],
        "after_counts": [{"provider": key, "count": value} for key, value in after_counts.items()],
        "remaining_threads": status_after["movable_threads"],
        "remaining_rollout_metadata": rollout_metadata_remaining,
        "verified": int(status_after["movable_threads"]) == 0 and rollout_metadata_remaining == 0,
        "checkpoint": {
            "busy": checkpoint_result[0],
            "log_frames": checkpoint_result[1],
            "checkpointed_frames": checkpoint_result[2],
        },
    }


def resolve_backup(paths: Paths, requested_path: str | None) -> Path:
    if requested_path:
        backup = Path(requested_path).expanduser()
    else:
        backups = list_backups(paths, limit=1)
        if not backups:
            raise RuntimeError("No backup files were found.")
        backup = Path(backups[0]["path"])
    if not backup.exists():
        raise RuntimeError(f"Backup file does not exist: {backup}")
    return backup


def restore_backup(paths: Paths, backup_path: str | None) -> dict[str, object]:
    ensure_environment(paths)
    chosen_backup = resolve_backup(paths, backup_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with connect_db(paths.db_path, readonly=True) as conn:
        thread_rows = query_thread_rows(conn)
    restore_snapshot = make_backup(paths, "pre-restore", timestamp)
    restore_session_meta_snapshot = backup_session_meta(paths, "pre-restore", timestamp, thread_rows)

    with connect_db(chosen_backup, readonly=True) as source, connect_db(paths.db_path, readonly=False) as target:
        source.backup(target)
        checkpoint_result = checkpoint(target)

    restored_session_meta = restore_session_meta_backup(paths, chosen_backup)
    status_after = get_status(paths)
    return {
        "action": "restore",
        "restored_from": str(chosen_backup),
        "safety_backup": str(restore_snapshot),
        "safety_session_meta_backup": restore_session_meta_snapshot,
        "restored_session_meta": restored_session_meta,
        "checkpoint": {
            "busy": checkpoint_result[0],
            "log_frames": checkpoint_result[1],
            "checkpointed_frames": checkpoint_result[2],
        },
        "status": status_after,
    }


def to_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex history sync helper")
    parser.add_argument("--codex-home", help="Override Codex home directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show current provider/thread status")
    subparsers.add_parser("sync", help="Move all thread providers to the current provider")
    restore_parser = subparsers.add_parser("restore", help="Restore from a backup")
    restore_parser.add_argument("--backup", help="Backup file path; newest backup is used when omitted")
    subparsers.add_parser("backup", help="Create a manual backup")

    args = parser.parse_args()
    paths = resolve_paths(args.codex_home)

    try:
        if args.command == "status":
            payload = get_status(paths)
        elif args.command == "sync":
            payload = sync_to_current_provider(paths)
        elif args.command == "restore":
            payload = restore_backup(paths, args.backup)
        elif args.command == "backup":
            ensure_environment(paths)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            with connect_db(paths.db_path, readonly=True) as conn:
                thread_rows = query_thread_rows(conn)
            payload = {
                "action": "backup",
                "backup_path": str(make_backup(paths, "manual", timestamp)),
                "session_meta_backup": backup_session_meta(paths, "manual", timestamp, thread_rows),
            }
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        error_payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(to_json(error_payload))
        else:
            print(error_payload["error"])
        return 1

    if isinstance(payload, dict):
        payload["ok"] = True

    if args.json:
        print(to_json(payload))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
