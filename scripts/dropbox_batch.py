"""Previewable server-side bulk moves/copies with durable asynchronous job state."""
import json
import os
import tempfile
import unicodedata
from pathlib import Path

from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, RelocationPath


def key(value):
    return unicodedata.normalize("NFC", value).casefold().rstrip("/")


def path(value):
    if not isinstance(value, str) or not value.startswith("/") or value == "/":
        raise ValueError("Use an absolute Dropbox path below root")
    if "\\" in value or any(part in {"", ".", ".."} for part in value[1:].split("/")):
        raise ValueError("Dropbox paths must not contain empty, dot or parent components")
    return unicodedata.normalize("NFC", value)


def validate_plan(plan):
    if not isinstance(plan, list) or not 1 <= len(plan) <= 1000:
        raise ValueError("Plan must contain 1 to 1000 {from_path,to_path} entries")
    entries = [{"from_path": path(row["from_path"]), "to_path": path(row["to_path"])} for row in plan]
    sources, targets = [key(row["from_path"]) for row in entries], [key(row["to_path"]) for row in entries]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise ValueError("Duplicate source or destination in bulk plan")
    for i, source in enumerate(sources):
        if any(i != j and source.startswith(other + "/") for j, other in enumerate(sources)):
            raise ValueError("Source roots overlap")
        for target in targets:
            if target == source or target.startswith(source + "/") or source.startswith(target + "/"):
                raise ValueError("Sources and destinations overlap; use separate plans")
    for i, target in enumerate(targets):
        if any(i != j and target.startswith(other + "/") for j, other in enumerate(targets)):
            raise ValueError("Destination roots overlap")
    return entries


def metadata(dbx, target):
    try:
        return dbx.files_get_metadata(target)
    except ApiError as error:
        if error.error.is_path() and error.error.get_path().is_not_found():
            return None
        raise


def describe(meta):
    if meta is None:
        raise ValueError("Source does not exist")
    data = {"id": meta.id, "path": meta.path_display, "kind": "file" if isinstance(meta, FileMetadata) else "folder"}
    if isinstance(meta, FileMetadata):
        data.update(size=meta.size, content_hash=meta.content_hash, rev=meta.rev)
    return data


def write_state(target, state):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name, suffix=".partial", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def finish(dbx, state, result, state_file):
    if len(result.entries) != len(state["entries"]):
        raise RuntimeError("Dropbox returned an incomplete batch result; retain state and investigate")
    for entry, outcome in zip(state["entries"], result.entries):
        if outcome.is_failure():
            entry.update(status="failed", error=str(outcome.get_failure()))
            continue
        remote = metadata(dbx, entry["to_path"])
        if remote is None or key(remote.path_display) != key(entry["to_path"]):
            entry["status"] = "verification_failed"
            continue
        original, actual = entry["source"], describe(remote)
        same = actual["kind"] == original["kind"]
        if original["kind"] == "file":
            same &= (actual["size"], actual["content_hash"]) == (original["size"], original["content_hash"])
        if state["operation"] == "move":
            same &= actual["id"] == original["id"] and metadata(dbx, entry["from_path"]) is None
        entry.update(status="verified" if same else "verification_failed", destination=actual)
    state["status"] = "complete" if all(e["status"] == "verified" for e in state["entries"]) else "needs_review"
    write_state(state_file, state)
    return state


def run(dbx, args):
    operation = "move" if args.command == "move-many" else "copy"
    plan = validate_plan(json.loads(Path(args.plan_file).read_text(encoding="utf-8-sig")))
    state = {"operation": operation, "status": "preview", "entries": []}
    for entry in plan:
        original = describe(metadata(dbx, entry["from_path"]))
        if metadata(dbx, entry["to_path"]) is not None:
            raise ValueError("Destination already exists: " + entry["to_path"])
        state["entries"].append({**entry, "source": original})
    if not args.execute:
        return state
    if not args.state_file:
        raise ValueError("--state-file is required when executing a batch")
    # Reserve before submission. An unknown outcome can never be blindly resubmitted.
    target = Path(args.state_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    state["status"] = "submitting"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    entries = [RelocationPath(from_path=e["from_path"], to_path=e["to_path"]) for e in plan]
    method = dbx.files_move_batch_v2 if operation == "move" else dbx.files_copy_batch_v2
    result = method(entries, autorename=False)
    if result.is_async_job_id():
        state.update(status="in_progress", job_id=result.get_async_job_id())
        write_state(target, state)
        return state
    return finish(dbx, state, result.get_complete(), target)


def status(dbx, args):
    state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
    if state["status"] != "in_progress":
        return state
    method = dbx.files_move_batch_check_v2 if state["operation"] == "move" else dbx.files_copy_batch_check_v2
    result = method(state["job_id"])
    return finish(dbx, state, result.get_complete(), args.state_file) if result.is_complete() else state


def command(dbx, args):
    result = status(dbx, args) if args.command == "transfer-status" else run(dbx, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] in {"submitting", "needs_review"}:
        raise SystemExit(1)
