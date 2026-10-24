#!/usr/bin/env python3
"""
Dropbox CLI — browse, search, share, upload, download via the Dropbox Python SDK.

Credentials are NOT stored here. Discovered at runtime in this order:
  1. DROPBOX_TOKEN env var (raw access token string)
  2. --token CLI flag (raw access token)
  3. --creds-file CLI flag (path to JSON credentials file), else DROPBOX_CREDS_FILE env var
  4. ./dropbox_credentials.json  (current working directory)
  5. ~/codetest/dropbox_credentials.json
  6. ~/.config/dropbox-cli/dropbox_credentials.json (where auth finish saves)
  7. Next to this script: dropbox_credentials.json

Credentials JSON format (minimal — access token only):
  { "access_token": "sl.u...." }

Full format (enables token refresh and auth login flow):
  {
    "app_key": "...",
    "app_secret": "...",
    "access_token": "sl.u....",
    "refresh_token": "..."
  }

Usage: python dropbox_cli.py <command> [args]
"""

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from email.headerregistry import Address
from pathlib import Path, PurePosixPath
from datetime import datetime

import dropbox
from dropbox import DropboxOAuth2FlowNoRedirect
from dropbox.common import PathRoot
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import (
    CommitInfo,
    FileMetadata,
    FolderMetadata,
    SearchOptions,
    SearchOrderBy,
    UploadSessionCursor,
)
from dropbox.sharing import (
    AccessLevel,
    AddMember,
    FolderAction,
    MemberSelector,
    RequestedVisibility,
    SharedLinkSettings,
)


SIMPLE_UPLOAD_LIMIT = 140 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
KNOWN_FOLDERS = set()
INVITE_PREVIEW_FILE = Path.home() / ".config" / "dropbox-cli" / "invite_preview.json"
INVITE_PREVIEW_TTL_SECONDS = 30 * 60


# ── Credential discovery ───────────────────────────────────────────────────────

CREDS_FILENAME = "dropbox_credentials.json"

def _read_json(path):
    try:
        p = Path(path)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _creds_candidates(explicit=None):
    if explicit:
        return [explicit]
    cands = []
    env = os.environ.get("DROPBOX_CREDS_FILE")
    if env:
        cands.append(env)
    cands.append(os.path.join(os.getcwd(), CREDS_FILENAME))
    home = Path.home()
    cands.append(str(home / "codetest" / CREDS_FILENAME))
    cands.append(str(home / ".config" / "dropbox-cli" / CREDS_FILENAME))
    script_dir = Path(__file__).parent
    cands.append(str(script_dir / CREDS_FILENAME))
    return cands

def load_credentials(explicit_file=None, explicit_token=None):
    if explicit_token:
        return {"access_token": explicit_token}
    env_token = os.environ.get("DROPBOX_TOKEN")
    if env_token:
        return {"access_token": env_token.strip()}
    for path in _creds_candidates(explicit_file):
        data = _read_json(path)
        if data and ("access_token" in data or "refresh_token" in data):
            return data
    return None

def save_credentials(creds, explicit_file=None):
    if explicit_file:
        target = Path(explicit_file)
    else:
        target = Path.home() / ".config" / "dropbox-cli" / CREDS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    return str(target)

def build_client(creds):
    if "refresh_token" in creds and "app_key" in creds:
        dbx = dropbox.Dropbox(
            app_key=creds["app_key"],
            app_secret=creds.get("app_secret", ""),
            oauth2_refresh_token=creds["refresh_token"],
        )
    elif "access_token" in creds:
        dbx = dropbox.Dropbox(oauth2_access_token=creds["access_token"])
    else:
        die("No valid credentials found. Run: dropbox_cli.py auth token <your_token>")

    # For Business/Teams accounts, use the team root namespace so all paths
    # resolve relative to the shared team space, not just the personal folder.
    try:
        acct = dbx.users_get_current_account()
        if hasattr(acct, "root_info") and acct.root_info:
            root_ns = acct.root_info.root_namespace_id
            home_ns = getattr(acct.root_info, "home_namespace_id", None)
            if home_ns and root_ns != home_ns:
                dbx = dbx.with_path_root(PathRoot.root(root_ns))
    except Exception:
        pass

    return dbx


# ── Helpers ────────────────────────────────────────────────────────────────────

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)

def norm_path(p):
    if not p or p == "/":
        return ""
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")

def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def fmt_date(dt):
    if dt is None:
        return ""
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)

def union_tag(value):
    return getattr(value, "_tag", None) if value is not None else None

def validate_recipient_email(value):
    text = value.strip()
    if not text or len(text) > 254 or "\r" in text or "\n" in text:
        die("A valid recipient email address is required")
    try:
        address = Address(addr_spec=text)
    except (TypeError, ValueError):
        die(f"Invalid recipient email address: {value}")
    if not address.username or not address.domain:
        die(f"Invalid recipient email address: {value}")
    return address.addr_spec

def load_custom_message(args):
    if args.message_file:
        try:
            message = Path(args.message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            die(f"Could not read UTF-8 custom message file: {e}")
    else:
        message = args.message

    if message is not None:
        if not message.strip():
            die("Custom invitation message cannot be empty")
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in message):
            die("Custom invitation message contains unsupported control characters")
    return message

def invite_fingerprint(invitation):
    canonical = json.dumps(
        invitation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

def save_invite_preview(invitation):
    INVITE_PREVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "created_at": time.time(),
        "fingerprint": invite_fingerprint(invitation),
        "invitation": invitation,
    }
    temp_path = INVITE_PREVIEW_FILE.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, INVITE_PREVIEW_FILE)

def require_matching_invite_preview(invitation):
    try:
        state = json.loads(INVITE_PREVIEW_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        die("No valid invitation preview found. Run the same command without --confirm first.")

    age = time.time() - state.get("created_at", 0)
    if age < 0 or age > INVITE_PREVIEW_TTL_SECONDS:
        die("Invitation preview expired. Run the same command without --confirm again.")
    if state.get("fingerprint") != invite_fingerprint(invitation):
        die("Invitation does not match the latest preview. Preview these exact details first.")

def get_folder_invite_context(dbx, path, access):
    try:
        meta = dbx.files_get_metadata(path)
    except ApiError as e:
        die(f"Cannot get folder metadata for '{path}': {e}")

    if not isinstance(meta, FolderMetadata):
        die(f"Invitation target is not a folder: {meta.path_display}")

    sharing_info = getattr(meta, "sharing_info", None)
    shared_folder_id = getattr(sharing_info, "shared_folder_id", None)
    if not shared_folder_id:
        die(
            "The exact target is not a shared folder. Refusing to invite through a "
            "parent folder because that could grant broader access."
        )

    action_name = "invite_editor" if access == "editor" else "invite_viewer"
    action = FolderAction.invite_editor if access == "editor" else FolderAction.invite_viewer
    try:
        folder = dbx.sharing_get_folder_metadata(shared_folder_id, actions=[action])
    except ApiError as e:
        die(f"Cannot check invitation permission for '{meta.path_display}': {e}")

    permission = next(
        (
            item
            for item in (getattr(folder, "permissions", None) or [])
            if union_tag(getattr(item, "action", None)) == action_name
        ),
        None,
    )
    if permission is None or not getattr(permission, "allow", False):
        reason = getattr(permission, "reason", None) if permission is not None else None
        detail = f" Reason: {reason}" if reason else ""
        die(f"Dropbox does not allow {action_name} on this folder.{detail}")

    policy = getattr(folder, "policy", None)
    return {
        "path": meta.path_display,
        "shared_folder_id": shared_folder_id,
        "current_access": union_tag(getattr(folder, "access_type", None)),
        "acl_update_policy": union_tag(getattr(policy, "acl_update_policy", None)),
        "member_policy": union_tag(getattr(policy, "member_policy", None)),
        "permission": action_name,
    }

def find_recipient_membership(dbx, shared_folder_id, recipient):
    try:
        page = dbx.sharing_list_folder_members(shared_folder_id, limit=1000)
        while True:
            for item in getattr(page, "users", None) or []:
                user = getattr(item, "user", None)
                email = getattr(user, "email", None)
                if email and email.casefold() == recipient.casefold():
                    return {
                        "status": "existing_member",
                        "access": union_tag(getattr(item, "access_type", None)),
                    }

            for item in getattr(page, "invitees", None) or []:
                invitee = getattr(item, "invitee", None)
                if invitee and invitee.is_email():
                    email = invitee.get_email()
                    if email.casefold() == recipient.casefold():
                        return {
                            "status": "pending_invitation",
                            "access": union_tag(getattr(item, "access_type", None)),
                        }

            cursor = getattr(page, "cursor", None)
            if not cursor:
                break
            page = dbx.sharing_list_folder_members_continue(cursor)
    except ApiError as e:
        die(f"Cannot check existing folder membership: {e}")
    return None

def fmt_entry(entry, verbose=False):
    if isinstance(entry, FolderMetadata):
        tag = "[DIR]"
        size = ""
        date = ""
    else:
        tag = "[FILE]"
        size = fmt_size(entry.size) if hasattr(entry, "size") else ""
        date = fmt_date(getattr(entry, "client_modified", None))
    name = entry.name
    path = entry.path_display
    if verbose:
        return f"{tag} {path}  {size}  {date}"
    return f"{tag} {name}  {size}"

def get_or_create_shared_link(dbx, path):
    """Return a shared link URL for path, creating one if needed."""
    try:
        links = dbx.sharing_list_shared_links(path=path, direct_only=True)
        if links.links:
            return links.links[0].url
    except ApiError:
        pass
    try:
        settings = SharedLinkSettings(requested_visibility=RequestedVisibility.public)
        result = dbx.sharing_create_shared_link_with_settings(path, settings)
        return result.url
    except ApiError as e:
        # Link already exists — fetch it
        if "shared_link_already_exists" in str(e):
            links = dbx.sharing_list_shared_links(path=path)
            if links.links:
                return links.links[0].url
        raise


# ── Commands ───────────────────────────────────────────────────────────────────

def dropbox_join(base, *parts):
    base = norm_path(base)
    clean_parts = []
    for part in parts:
        text = str(part).replace("\\", "/").strip("/")
        if text:
            clean_parts.append(text)
    if not clean_parts:
        return base
    suffix = "/".join(clean_parts)
    return (base.rstrip("/") + "/" + suffix) if base else "/" + suffix


def dropbox_parent(path):
    parent = str(PurePosixPath(norm_path(path)).parent)
    return norm_path(parent)


def is_conflict_error(error):
    return "conflict" in str(error).lower()


def ensure_dropbox_folder(dbx, folder_path):
    folder_path = norm_path(folder_path)
    if not folder_path:
        return

    current = ""
    for part in folder_path.strip("/").split("/"):
        current = dropbox_join(current, part)
        if current in KNOWN_FOLDERS:
            continue
        try:
            dbx.files_create_folder_v2(current)
        except ApiError as e:
            if not is_conflict_error(e):
                raise
            meta = dbx.files_get_metadata(current)
            if not isinstance(meta, FolderMetadata):
                raise
        KNOWN_FOLDERS.add(current)


def upload_file(dbx, local_path, dropbox_path, mode, chunk_size=DEFAULT_CHUNK_SIZE):
    ensure_dropbox_folder(dbx, dropbox_parent(dropbox_path))
    size = local_path.stat().st_size

    if size <= SIMPLE_UPLOAD_LIMIT:
        with local_path.open("rb") as fh:
            return dbx.files_upload(fh.read(), dropbox_path, mode=mode, mute=True)

    with local_path.open("rb") as fh:
        session = dbx.files_upload_session_start(fh.read(chunk_size), close=False)
        cursor = UploadSessionCursor(session_id=session.session_id, offset=fh.tell())
        commit = CommitInfo(path=dropbox_path, mode=mode, mute=True)

        while fh.tell() < size:
            remaining = size - fh.tell()
            data = fh.read(min(chunk_size, remaining))
            if fh.tell() == size:
                return dbx.files_upload_session_finish(data, cursor, commit)
            dbx.files_upload_session_append_v2(data, cursor, close=False)
            cursor.offset = fh.tell()

    die(f"Upload did not complete: {local_path}")


def iter_files(root):
    return sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: str(p).lower())


def iter_dirs(root):
    return sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: str(p).lower())


def summarize_transfer(action, count, byte_count, failed):
    failure_text = f", failed {failed}" if failed else ""
    print(f"{action}: {count} file(s), {fmt_size(byte_count)}{failure_text}")


def cmd_whoami(dbx, args):
    acct = dbx.users_get_current_account()
    usage = dbx.users_get_space_usage()
    used = usage.used
    alloc = usage.allocation.get_individual().allocated if usage.allocation.is_individual() else 0
    print(f"Name:   {acct.name.display_name}")
    print(f"Email:  {acct.email}")
    print(f"ID:     {acct.account_id}")
    print(f"Used:   {fmt_size(used)} / {fmt_size(alloc)}")

def cmd_ls(dbx, args):
    path = norm_path(args.path or "")
    try:
        result = dbx.files_list_folder(path, recursive=args.recursive)
    except ApiError as e:
        die(f"Cannot list '{args.path or '/'}': {e}")

    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    if args.dirs_only:
        entries = [e for e in entries if isinstance(e, FolderMetadata)]
    if args.files_only:
        entries = [e for e in entries if isinstance(e, FileMetadata)]

    entries.sort(key=lambda e: (isinstance(e, FileMetadata), e.name.lower()))

    if not entries:
        print("(empty)")
        return

    for e in entries:
        print(fmt_entry(e, verbose=args.verbose))

def cmd_search(dbx, args):
    query = args.query
    search_path = norm_path(args.path or "")
    options = SearchOptions(
        path=search_path if search_path else None,
        max_results=args.limit,
        file_status=dropbox.files.FileStatus.active,
        order_by=SearchOrderBy.relevance,
    )
    try:
        result = dbx.files_search_v2(query, options=options)
    except ApiError as e:
        die(f"Search failed: {e}")

    matches = result.matches
    while result.has_more:
        result = dbx.files_search_continue_v2(result.cursor)
        matches.extend(result.matches)

    if not matches:
        print("No results.")
        return

    print(f"Found {len(matches)} result(s):\n")
    for m in matches:
        meta = m.metadata.get_metadata()
        print(fmt_entry(meta, verbose=True))

def cmd_link(dbx, args):
    path = norm_path(args.path)
    if not path:
        die("Path required")
    try:
        url = get_or_create_shared_link(dbx, path)
        print(url)
    except ApiError as e:
        die(f"Could not get/create link for '{args.path}': {e}")

def cmd_invite(dbx, args):
    path = norm_path(args.path)
    if not path:
        die("Path required")

    recipient = validate_recipient_email(args.email)
    message = load_custom_message(args)
    context = get_folder_invite_context(dbx, path, args.access)
    existing = find_recipient_membership(
        dbx,
        context["shared_folder_id"],
        recipient,
    )
    if existing:
        die(
            f"Recipient has status {existing['status']} on this folder "
            f"with access {existing['access']}. No invitation was sent."
        )
    invitation = {
        "folder": context["path"],
        "shared_folder_id": context["shared_folder_id"],
        "recipient": recipient,
        "access": args.access,
        "notify_by_email_and_device": True,
        "custom_message": message,
    }

    if not args.confirm:
        save_invite_preview(invitation)
        preview = {
            "status": "preview",
            "invitation_sent": False,
            **invitation,
            "permission_check": {
                "allowed": True,
                "current_access": context["current_access"],
                "acl_update_policy": context["acl_update_policy"],
                "member_policy": context["member_policy"],
                "action": context["permission"],
            },
            "recipient_check": "not_a_member_or_pending_invitee",
            "approval_required": True,
            "preview_expires_in_minutes": INVITE_PREVIEW_TTL_SECONDS // 60,
            "next_step": (
                "After explicit approval, repeat these exact arguments with --confirm."
            ),
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    require_matching_invite_preview(invitation)
    access_level = AccessLevel.editor if args.access == "editor" else AccessLevel.viewer
    member = AddMember(
        member=MemberSelector.email(recipient),
        access_level=access_level,
    )

    # Consume the preview before the API call so an uncertain failure cannot be
    # blindly retried. A new preview and approval are required for every attempt.
    INVITE_PREVIEW_FILE.unlink(missing_ok=True)
    try:
        dbx.sharing_add_folder_member(
            context["shared_folder_id"],
            [member],
            quiet=False,
            custom_message=message,
        )
    except ApiError as e:
        die(
            "Dropbox rejected the invitation. Preview again before any retry. "
            f"Details: {e}"
        )
    except Exception as e:
        die(
            "Invitation delivery is uncertain because the request failed outside "
            "Dropbox's structured API response. Do not retry without checking the "
            f"folder membership first. Details: {e}"
        )

    result = {
        "status": "invitation_sent",
        "folder": context["path"],
        "recipient": recipient,
        "access": args.access,
        "notify_by_email_and_device": True,
        "custom_message": message,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_download(dbx, args):
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    byte_count = 0
    failed = 0

    for raw_path in args.paths:
        path = norm_path(raw_path)
        if not path:
            failed += 1
            print("ERROR: Dropbox path required", file=sys.stderr)
            continue

        try:
            meta = dbx.files_get_metadata(path)
            if isinstance(meta, FolderMetadata):
                out_path = out_dir / f"{meta.name}.zip"
                dbx.files_download_zip_to_file(str(out_path), path)
                size = out_path.stat().st_size
                if args.extract:
                    with zipfile.ZipFile(out_path) as zf:
                        zf.extractall(out_dir)
                if args.verbose or len(args.paths) == 1:
                    extra = " and extracted" if args.extract else ""
                    print(f"Downloaded folder zip{extra}: {out_path}  ({fmt_size(size)})")
            else:
                out_path = out_dir / meta.name
                dbx.files_download_to_file(str(out_path), path)
                size = out_path.stat().st_size
                if args.verbose or len(args.paths) == 1:
                    print(f"Downloaded: {out_path}  ({fmt_size(size)})")
            count += 1
            byte_count += size
        except ApiError as e:
            failed += 1
            print(f"ERROR: Download failed for {raw_path}: {e}", file=sys.stderr)

    if len(args.paths) > 1 and not args.verbose:
        summarize_transfer("Downloaded", count, byte_count, failed)

    if failed:
        sys.exit(1)

def upload_path(dbx, local, dropbox_path, mode, args, include_root=False):
    if local.is_file():
        if include_root:
            target = dropbox_join(dropbox_path, local.name)
        elif dropbox_path and str(dropbox_path).endswith(("/", "\\")):
            target = dropbox_join(dropbox_path, local.name)
        else:
            target = norm_path(dropbox_path) if dropbox_path else dropbox_join("", local.name)
        meta = upload_file(dbx, local, target, mode, args.chunk_size)
        return 1, local.stat().st_size, meta.path_display

    if not local.is_dir():
        die(f"Local path not found: {local}")

    root_target = norm_path(dropbox_path) if dropbox_path else dropbox_join("", local.name)
    if include_root:
        root_target = dropbox_join(root_target, local.name)

    ensure_dropbox_folder(dbx, root_target)
    for local_dir in iter_dirs(local):
        rel = local_dir.relative_to(local).as_posix()
        ensure_dropbox_folder(dbx, dropbox_join(root_target, rel))

    count = 0
    byte_count = 0
    last_path = root_target
    for local_file in iter_files(local):
        rel = local_file.relative_to(local).as_posix()
        target = dropbox_join(root_target, rel)
        meta = upload_file(dbx, local_file, target, mode, args.chunk_size)
        size = local_file.stat().st_size
        count += 1
        byte_count += size
        last_path = meta.path_display
        if args.verbose:
            print(f"Uploaded: {meta.path_display}  ({fmt_size(size)})")
        elif args.progress_every and count % args.progress_every == 0:
            print(f"Uploaded {count} file(s)...", file=sys.stderr)

    return count, byte_count, last_path


def cmd_upload(dbx, args):
    local = Path(args.local)
    mode = dropbox.files.WriteMode.overwrite if args.overwrite else dropbox.files.WriteMode.add
    try:
        count, byte_count, last_path = upload_path(dbx, local, args.dropbox_path, mode, args)
    except ApiError as e:
        die(f"Upload failed: {e}")

    if local.is_file():
        print(f"Uploaded: {last_path}  ({fmt_size(byte_count)})")
    else:
        summarize_transfer(f"Uploaded folder to {norm_path(args.dropbox_path) or dropbox_join('', local.name)}", count, byte_count, 0)


def cmd_upload_many(dbx, args):
    mode = dropbox.files.WriteMode.overwrite if args.overwrite else dropbox.files.WriteMode.add
    dest = norm_path(args.dest)
    if not dest:
        die("--dest Dropbox folder is required")

    total_count = 0
    total_bytes = 0
    failed = 0
    ensure_dropbox_folder(dbx, dest)

    for raw_local in args.local_paths:
        local = Path(raw_local)
        try:
            count, byte_count, _ = upload_path(dbx, local, dest, mode, args, include_root=True)
            total_count += count
            total_bytes += byte_count
        except (ApiError, OSError) as e:
            failed += 1
            print(f"ERROR: Upload failed for {raw_local}: {e}", file=sys.stderr)

    summarize_transfer(f"Uploaded to {dest}", total_count, total_bytes, failed)
    if failed:
        sys.exit(1)

def cmd_mkdir(dbx, args):
    path = norm_path(args.path)
    if not path:
        die("Path required")
    try:
        result = dbx.files_create_folder_v2(path)
        print(f"Created: {result.metadata.path_display}")
    except ApiError as e:
        die(f"mkdir failed: {e}")

def cmd_move(dbx, args):
    from_path = norm_path(args.from_path)
    to_path = norm_path(args.to_path)
    try:
        result = dbx.files_move_v2(from_path, to_path)
        print(f"Moved to: {result.metadata.path_display}")
    except ApiError as e:
        die(f"Move failed: {e}")

def cmd_bulk_transfer(dbx, args):
    from dropbox_batch import command
    command(dbx, args)


def cmd_shared(dbx, args):
    from dropbox_shared import shared_inventory, download_shared_file
    if args.command == "shared-list":
        print(json.dumps(shared_inventory(dbx, args.url), ensure_ascii=False, indent=2))
    else:
        size, digest = download_shared_file(dbx, args.url, args.out, relative=args.path)
        print(json.dumps({"saved": args.out, "bytes": size, "sha256": digest}))


def cmd_delete(dbx, args):
    path = norm_path(args.path)
    if not path:
        die("Path required")
    if not args.confirm:
        print(f"About to delete: {path}")
        print("Add --confirm to actually delete.")
        return
    try:
        result = dbx.files_delete_v2(path)
        print(f"Deleted: {result.metadata.path_display}")
    except ApiError as e:
        die(f"Delete failed: {e}")

def cmd_info(dbx, args):
    path = norm_path(args.path)
    if not path:
        die("Path required")
    try:
        meta = dbx.files_get_metadata(path, include_media_info=True)
    except ApiError as e:
        die(f"Cannot get info for '{args.path}': {e}")

    print(f"Name:     {meta.name}")
    print(f"Path:     {meta.path_display}")
    if isinstance(meta, FileMetadata):
        print(f"Size:     {fmt_size(meta.size)}")
        print(f"Modified: {fmt_date(meta.client_modified)}")
        print(f"ID:       {meta.id}")
    else:
        print(f"Type:     folder")
        print(f"ID:       {meta.id}")


# ── Auth commands ──────────────────────────────────────────────────────────────

def cmd_auth_token(args):
    """Store a raw access token in credentials file."""
    token = args.token.strip()
    existing = _read_json(args.creds_file) if args.creds_file else {}
    creds = existing or {}
    creds["access_token"] = token
    path = save_credentials(creds, args.creds_file)
    print(f"Access token saved to: {path}")
    # Quick test
    try:
        dbx = dropbox.Dropbox(oauth2_access_token=token)
        acct = dbx.users_get_current_account()
        print(f"Verified: logged in as {acct.name.display_name} ({acct.email})")
    except Exception as e:
        print(f"Warning: could not verify token: {e}")

AUTH_STATE_FILE = Path.home() / ".config" / "dropbox-cli" / "auth_state.json"

def cmd_auth_start(args):
    """Step 1: generate authorization URL and save PKCE state for later."""
    app_key = args.app_key
    app_secret = args.app_secret or ""

    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        consumer_secret=app_secret or None,
        token_access_type="offline",
        use_pkce=not bool(app_secret),
    )
    url = auth_flow.start()

    # Save state so cmd_auth_finish can resume
    state = {
        "app_key": app_key,
        "app_secret": app_secret,
        "csrf_token": auth_flow._csrf_token if hasattr(auth_flow, "_csrf_token") else None,
        "pkce_verifier": auth_flow._pkce_verifier if hasattr(auth_flow, "_pkce_verifier") else None,
    }
    AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    print(url)

def cmd_auth_finish(args):
    """Step 2: exchange authorization code for tokens and save credentials."""
    if not AUTH_STATE_FILE.is_file():
        die("No pending auth. Run 'auth start' first.")

    state = json.loads(AUTH_STATE_FILE.read_text(encoding="utf-8"))
    app_key = state["app_key"]
    app_secret = state.get("app_secret", "")

    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        consumer_secret=app_secret or None,
        token_access_type="offline",
        use_pkce=not bool(app_secret),
    )
    # Restore PKCE/CSRF state
    if state.get("csrf_token"):
        auth_flow._csrf_token = state["csrf_token"]
    if state.get("pkce_verifier"):
        auth_flow._pkce_verifier = state["pkce_verifier"]

    try:
        result = auth_flow.finish(args.code.strip())
    except Exception as e:
        die(f"Auth failed: {e}")

    AUTH_STATE_FILE.unlink(missing_ok=True)

    creds = {"app_key": app_key}
    if app_secret:
        creds["app_secret"] = app_secret
    creds["access_token"] = result.access_token
    if result.refresh_token:
        creds["refresh_token"] = result.refresh_token

    path = save_credentials(creds, args.creds_file)

    dbx = build_client(creds)
    acct = dbx.users_get_current_account()
    print(f"Success! Logged in as {acct.name.display_name} ({acct.email})")
    print(f"Credentials saved to: {path}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="dropbox_cli", description="Dropbox CLI")
    parser.add_argument("--token", help="Raw Dropbox access token (overrides file)")
    parser.add_argument("--creds-file", help="Path to credentials JSON file")
    parser.add_argument("--verbose", "-v", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # whoami
    sub.add_parser("whoami", help="Show account info and storage usage")

    # ls
    p_ls = sub.add_parser("ls", help="List folder contents")
    p_ls.add_argument("path", nargs="?", default="", help="Dropbox path (default: root)")
    p_ls.add_argument("--recursive", "-r", action="store_true")
    p_ls.add_argument("--dirs-only", action="store_true")
    p_ls.add_argument("--files-only", action="store_true")
    p_ls.add_argument("--verbose", "-v", action="store_true")

    # search
    p_search = sub.add_parser("search", help="Search files and folders")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--path", default="", help="Limit search to this folder")
    p_search.add_argument("--limit", type=int, default=20, help="Max results (default 20)")

    # link
    p_link = sub.add_parser("link", help="Get or create a shared link for a file/folder")
    p_link.add_argument("path", help="Dropbox path")

    # invite
    p_invite = sub.add_parser(
        "invite",
        help="Preview or send a shared-folder invitation",
    )
    p_invite.add_argument("path", help="Exact Dropbox shared-folder path")
    p_invite.add_argument("email", help="Bare recipient email address")
    p_invite.add_argument(
        "--access",
        choices=("viewer", "editor"),
        default="viewer",
        help="Access to grant (default: viewer)",
    )
    message_group = p_invite.add_mutually_exclusive_group()
    message_group.add_argument("--message", help="Optional custom invitation message")
    message_group.add_argument(
        "--message-file",
        help="Read the optional custom invitation message from a UTF-8 file",
    )
    p_invite.add_argument(
        "--confirm",
        action="store_true",
        help="Send an exact, unexpired invitation preview",
    )

    # download
    p_dl = sub.add_parser("download", help="Download file(s) or folder zip(s) from Dropbox")
    p_dl.add_argument("paths", nargs="+", help="Dropbox path(s)")
    p_dl.add_argument("--out", "-o", help="Output directory (default: current dir)")
    p_dl.add_argument("--extract", action="store_true", help="Extract folder zip downloads after saving")
    p_dl.add_argument("--verbose", "-v", action="store_true")

    # upload
    p_ul = sub.add_parser("upload", help="Upload a local file or folder to Dropbox")
    p_ul.add_argument("local", help="Local file path")
    p_ul.add_argument("dropbox_path", nargs="?", default="", help="Destination Dropbox path")
    p_ul.add_argument("--overwrite", action="store_true")
    p_ul.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Large-file upload chunk size in bytes")
    p_ul.add_argument("--progress-every", type=int, default=0, help="Print progress to stderr every N files for folders")
    p_ul.add_argument("--verbose", "-v", action="store_true")

    # upload-many
    p_um = sub.add_parser("upload-many", help="Upload multiple local files/folders into one Dropbox folder")
    p_um.add_argument("local_paths", nargs="+", help="Local file or folder paths")
    p_um.add_argument("--dest", required=True, help="Destination Dropbox folder")
    p_um.add_argument("--overwrite", action="store_true")
    p_um.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Large-file upload chunk size in bytes")
    p_um.add_argument("--progress-every", type=int, default=0, help="Print progress to stderr every N files")
    p_um.add_argument("--verbose", "-v", action="store_true")

    # mkdir
    p_mkdir = sub.add_parser("mkdir", help="Create a folder in Dropbox")
    p_mkdir.add_argument("path", help="Dropbox path")

    # move
    p_move = sub.add_parser("move", help="Move or rename a file/folder")
    p_move.add_argument("from_path", help="Source path")
    p_move.add_argument("to_path", help="Destination path")

    for name in ("move-many", "copy-many"):
        bulk = sub.add_parser(name, help="Preview a server-side batch of files/folders, or execute with durable job state")
        bulk.add_argument("plan_file", help="JSON array of {from_path,to_path} entries")
        bulk.add_argument("--execute", action="store_true", help="Submit this batch, default is preview")
        bulk.add_argument("--state-file", help="New local job state file, required for execution")
    transfer_status = sub.add_parser("transfer-status", help="Check one saved asynchronous move/copy job")
    transfer_status.add_argument("state_file")

    shared_list = sub.add_parser("shared-list", help="List a shared file or folder, including nested folders")
    shared_list.add_argument("url")
    shared_download = sub.add_parser("download-shared", help="Download one shared file or a file within a shared folder")
    shared_download.add_argument("url")
    shared_download.add_argument("--path", help="Relative path within a folder link, starting with /")
    shared_download.add_argument("--out", required=True, help="New local file path")

    # delete
    p_del = sub.add_parser("delete", help="Delete a file or folder")
    p_del.add_argument("path", help="Dropbox path")
    p_del.add_argument("--confirm", action="store_true", help="Actually delete (dry by default)")

    # info
    p_info = sub.add_parser("info", help="Get metadata for a file or folder")
    p_info.add_argument("path", help="Dropbox path")

    # auth subcommands
    p_auth = sub.add_parser("auth", help="Manage authentication")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)

    p_auth_token = auth_sub.add_parser("token", help="Save a raw access token")
    p_auth_token.add_argument("token", help="Access token (sl.u.....)")

    p_auth_start = auth_sub.add_parser("start", help="Step 1: generate OAuth2 URL (prints URL, no browser)")
    p_auth_start.add_argument("--app-key", required=True, help="Dropbox app key")
    p_auth_start.add_argument("--app-secret", default="", help="Dropbox app secret (optional for PKCE)")

    p_auth_finish = auth_sub.add_parser("finish", help="Step 2: exchange authorization code for tokens")
    p_auth_finish.add_argument("code", help="Authorization code from Dropbox")

    args = parser.parse_args()

    # Auth commands don't need an existing client
    if args.command == "auth":
        if args.auth_command == "token":
            cmd_auth_token(args)
        elif args.auth_command == "start":
            cmd_auth_start(args)
        elif args.auth_command == "finish":
            cmd_auth_finish(args)
        return

    # All other commands need credentials
    creds = load_credentials(
        explicit_file=args.creds_file,
        explicit_token=args.token,
    )
    if not creds:
        die(
            "No credentials found. Either:\n"
            "  1. Run: python dropbox_cli.py auth token <your_token>\n"
            "  2. Set DROPBOX_TOKEN environment variable\n"
            "  3. Place dropbox_credentials.json in your working directory"
        )

    try:
        dbx = build_client(creds)
    except AuthError as e:
        die(f"Authentication failed: {e}")

    dispatch = {
        "whoami": cmd_whoami,
        "ls":     cmd_ls,
        "search": cmd_search,
        "link":   cmd_link,
        "invite": cmd_invite,
        "download": cmd_download,
        "upload": cmd_upload,
        "upload-many": cmd_upload_many,
        "mkdir":  cmd_mkdir,
        "move":   cmd_move,
        "move-many": cmd_bulk_transfer,
        "copy-many": cmd_bulk_transfer,
        "transfer-status": cmd_bulk_transfer,
        "shared-list": cmd_shared,
        "download-shared": cmd_shared,
        "delete": cmd_delete,
        "info":   cmd_info,
    }

    fn = dispatch.get(args.command)
    if fn:
        args.verbose = getattr(args, "verbose", False)
        fn(dbx, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
