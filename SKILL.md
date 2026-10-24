---
name: dropbox
description: "Browse, search, share, invite collaborators by email, upload/download files and folders, inspect shared links, and manage bulk moves/copies through the bundled CLI."
---

# Dropbox Skill

Use the bundled CLI for all Dropbox access. A local Dropbox client folder is not live Dropbox state unless uploaded through this CLI.

If `LOCAL.md` exists beside this file, read it first: it holds this installation's paths, account notes and user preferences, and it wins where it differs from the examples below. It is never committed.

## Command Prefix

Use this prefix on Windows:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; $env:MSYS_NO_PATHCONV='1'; <venv>/Scripts/python.exe <skills>/dropbox/scripts/dropbox_cli.py <command> [args]
```

`MSYS_NO_PATHCONV=1` matters when a shell might rewrite Dropbox paths that start with `/`.

## Credentials

Credential discovery order:

1. `DROPBOX_TOKEN` environment variable
2. `--token TOKEN`
3. `--creds-file path\to\credentials.json`, else the `DROPBOX_CREDS_FILE` environment variable
4. `.\dropbox_credentials.json`
5. `~/codetest/dropbox_credentials.json`
6. `~/.config/dropbox-cli/dropbox_credentials.json` (where `auth finish` saves)
7. `scripts/dropbox_credentials.json` next to the CLI

Auth commands:

```powershell
python dropbox_cli.py auth token sl.u.YOUR_TOKEN_HERE
python dropbox_cli.py auth start --app-key YOUR_APP_KEY --app-secret YOUR_APP_SECRET
python dropbox_cli.py auth finish THE_CODE_FROM_DROPBOX
```

Refresh-token credentials are preferred because access tokens expire.

## Core Commands

- `whoami`: Show account and storage usage.
- `ls [path]`: List a folder. Flags: `--recursive`, `--dirs-only`, `--files-only`, `--verbose`.
- `info <path>`: Show metadata for a file or folder.
- `search <query> [--path /folder] [--limit N]`: Search filenames and content.
- `link <path>`: Get or create a shared link for a file or folder. This is idempotent.
- `invite <path> <email> [--access viewer|editor] [--message TEXT|--message-file FILE] [--confirm]`: Preview or send an email-based shared-folder invitation.
- `mkdir <path>`: Create a Dropbox folder.
- `move <from_path> <to_path>`: Move or rename.
- `delete <path>`: Dry run delete. Add `--confirm` only when certain.

## Shared-folder invitations

Invitation commands notify an external recipient through Dropbox. Always use this approval-gated workflow:

1. Run `invite` without `--confirm`. This validates the exact shared folder, checks the live invite permission and existing membership, and prints the complete caller-controlled invitation details without sending.
2. Show the user the exact folder, recipient, access level, notification setting, and custom message. Dropbox controls the surrounding email template and does not expose it for preview.
3. Receive explicit approval for those exact details.
4. Repeat the identical command with `--confirm` within 30 minutes. Any changed field or expired preview is blocked.

Editor example, preview first:

```powershell
python dropbox_cli.py invite "/Management/Press Kit & Riders" person@example.com --access editor
```

After explicit approval, send the exact preview:

```powershell
python dropbox_cli.py invite "/Management/Press Kit & Riders" person@example.com --access editor --confirm
```

Use `--message-file` for multiline or shell-sensitive custom messages, and keep that file unchanged from preview through confirmation. Invitations always set Dropbox notifications on. Never retry an uncertain failure without checking whether the recipient became a member or pending invitee, then creating a new preview and obtaining fresh approval.

## Uploads

Use one CLI command for folder or bulk uploads. Do not loop over hundreds of files from the agent.

Single file:

```powershell
python dropbox_cli.py upload C:\local\track.wav /Promos/track.wav --overwrite
```

Folder:

```powershell
python dropbox_cli.py upload C:\local\promo-folder /Promos/promo-folder --overwrite
```

If the Dropbox path is omitted, a file uploads to `/<filename>` and a folder uploads to `/<foldername>`.

Multiple local roots into one Dropbox folder:

```powershell
python dropbox_cli.py upload-many C:\local\a.wav C:\local\folder-b --dest /Promos/Batch --overwrite
```

Upload output is concise by default, one summary line for folders or batches. Use `--verbose` only when per-file output is needed. Use `--progress-every 50` for long transfers where periodic stderr progress is useful.

Large files are uploaded with Dropbox upload sessions automatically. The default chunk size is 8 MB. Override with `--chunk-size BYTES` only for a specific reason.

## Downloads

Single file:

```powershell
python dropbox_cli.py download /Promos/track.wav --out C:\Users\you\Downloads
```

Folder download saves one zip by default:

```powershell
python dropbox_cli.py download /Promos/Batch --out C:\Users\you\Downloads
```

Add `--extract` to extract the saved folder zip after download:

```powershell
python dropbox_cli.py download /Promos/Batch --out C:\Users\you\Downloads --extract
```

Multiple Dropbox paths can be downloaded in one command:

```powershell
python dropbox_cli.py download /Promos/a.wav /Promos/b.wav /Promos/Batch --out C:\Users\you\Downloads
```

Bulk download output is summarized by default. Use `--verbose` only when each downloaded file path is needed.

## Shared links and bulk routing

`shared-list URL` inventories a shared file or all nested folders, following every listing page.
`download-shared URL --out NEW_LOCAL_FILE [--path /relative/file.wav]` downloads one original file,
using the same SDK client and protected credentials as the rest of this skill. The Python helper
`scripts/dropbox_shared.py` accepts expected size and Dropbox content hash for verified pipeline use.
It writes through a partial file and refuses an existing local destination. No links are created or shared.

`move-many PLAN.json` and `copy-many PLAN.json` preview server-side operations without mutation.
The plan is a JSON array of objects with exact `from_path` and `to_path` Dropbox paths. Both files and
folders are supported. Duplicate destinations, overlapping roots, existing destinations and root paths
are rejected. No autorename, overwrite or ownership transfer is enabled.

Add `--execute --state-file NEW_JOB.json` only for an authorized concrete operation. State is saved
before submission, including an uncertain `submitting` result if the request outcome is unknown.
An existing job file blocks resubmission. `transfer-status JOB.json` checks one saved asynchronous job
and verifies each result's destination metadata; file results also compare byte size/content hash,
and moves compare identity and confirm the source path is gone. A folder check verifies root metadata,
not every descendant's content. Per-entry failures remain in the state file for review.

Implementation evidence, 2026-10-22: bulk and shared-link helpers have nine offline tests for nested
pagination, truncated/hash-mismatched downloads, conflicting plans, interrupted submission and async
job recovery. Live single-file and nested-folder discovery, plus a read-only bulk copy preview, have passed. Bulk execution is not live-proven,
and no real promo library folders have been moved. Monthly archive routing is not enabled.

## Typical workflow examples

Find and share a folder:

1. `search "track name" --limit 10`
2. `ls /path/to/parent --verbose`
3. `link /path/to/folder`

Upload a large promo folder and share it:

1. `upload C:\path\promo-folder /Promos/promo-folder --overwrite`
2. `link /Promos/promo-folder`

Download a folder for local work:

1. `download /Promos/promo-folder --out C:\Users\you\Downloads --extract`

## Notes

- Dropbox paths may be written with or without the leading `/`; the CLI normalizes them.
- On a team Dropbox, top-level folder creation can fail with `no_write_permission`. Prefer an existing writable folder unless the user gives a specific destination.
- `link` returns an existing direct shared link when one already exists.
- Shared links default to public visibility, anyone with the link can view.
- `delete` is dry by default.
- For huge transfers that may run longer than the shell timeout, launch it detached, without a console window, and write a log file.
