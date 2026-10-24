"""Shared-link file/folder inventory and verified downloads through the existing Dropbox SDK client."""
import hashlib
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from dropbox.files import FileMetadata, FolderMetadata, SharedLink
from dropbox.sharing import FileLinkMetadata, FolderLinkMetadata


def validate_link(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or not (
            host == "dropbox.com" or host.endswith(".dropbox.com") or host == "db.tt"):
        raise ValueError("Expected an HTTPS Dropbox shared link")


def shared_inventory(dbx, url, max_entries=10000):
    validate_link(url)
    root = dbx.sharing_get_shared_link_metadata(url)
    if isinstance(root, FileLinkMetadata):
        return {"kind": "file", "name": root.name, "files": [{"name": root.name, "path": None,
                "size": root.size, "id": root.id, "rev": getattr(root, "rev", None),
                "content_hash": getattr(root, "content_hash", None),
                "is_downloadable": getattr(getattr(root, "link_permissions", None), "allow_download", True)
                    is not False}], "requests": 1}
    if not isinstance(root, FolderLinkMetadata):
        raise RuntimeError("Unexpected shared-link metadata")
    pending, files, calls, seen = [""], [], 1, set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            raise RuntimeError("Folder appeared twice; inventory cannot be proven complete")
        seen.add(current)
        page = dbx.files_list_folder(current, shared_link=SharedLink(url=url), recursive=False)
        calls += 1
        while True:
            for item in page.entries:
                if item.name in {".", ".."} or "/" in item.name or "\\" in item.name:
                    raise RuntimeError("Unsafe shared entry name")
                relative = current.rstrip("/") + "/" + item.name
                if isinstance(item, FolderMetadata):
                    pending.append(relative)
                elif isinstance(item, FileMetadata):
                    files.append({"name": item.name, "path": relative, "size": item.size, "id": item.id,
                                  "rev": getattr(item, "rev", None), "content_hash": item.content_hash,
                                  "is_downloadable": item.is_downloadable})
                if len(files) + len(pending) + len(seen) > max_entries:
                    raise RuntimeError("Shared folder exceeds inventory bound; increase explicitly")
            if not page.has_more:
                break
            page = dbx.files_list_folder_continue(page.cursor)
            calls += 1
    return {"kind": "folder", "name": root.name, "files": files, "requests": calls}


def content_hash(target):
    outer = hashlib.sha256()
    with Path(target).open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            outer.update(hashlib.sha256(chunk).digest())
    return outer.hexdigest()


def download_shared_file(dbx, url, destination, relative=None, expected_size=None, expected_hash=None):
    validate_link(url)
    if relative and (not relative.startswith("/") or ".." in PurePosixPath(relative).parts):
        raise ValueError("Expected a path relative to the shared folder root")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Local destination already exists")
    partial = destination.with_name(destination.name + ".partial")
    meta, response = dbx.sharing_get_shared_link_file(url, path=relative)
    size, digest = 0, hashlib.sha256()
    try:
        with partial.open("wb") as handle:
            for chunk in response.iter_content(1 << 20):
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
                    digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        response.close()
    advertised = expected_size if expected_size is not None else getattr(meta, "size", None)
    if not size or (advertised is not None and size != advertised):
        os.remove(partial)
        raise RuntimeError("Shared download size differs from the inventory")
    if expected_hash and content_hash(partial) != expected_hash:
        os.remove(partial)
        raise RuntimeError("Shared download content hash differs from the inventory")
    os.replace(partial, destination)
    return size, digest.hexdigest()
