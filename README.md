# Dropbox skill

An AI agent skill and command line tool for Dropbox: browse, search, upload and download files and folders, create and inspect shared links, invite people to folders, and run bulk moves and copies. It uses the official Dropbox Python SDK. Output is plain JSON, which suits agents and screen readers.

## What you need

1. Python 3.10 or newer.
2. A Dropbox account, and a Dropbox app you create yourself (free).

## Setup

1. Clone this repository into your agent's skills folder, for example `%USERPROFILE%\.claude\skills\dropbox`.
2. Install the packages: `python -m pip install -r requirements.txt`.
3. Create a Dropbox app at https://www.dropbox.com/developers/apps. Choose "Scoped access" and "Full Dropbox". On its Permissions tab, enable the files and sharing read and write scopes, then note the app key and app secret.
4. Run `python scripts/dropbox_cli.py auth start --app-key YOUR_APP_KEY --app-secret YOUR_APP_SECRET`. It prints a Dropbox address. Open it, allow access, and copy the code Dropbox shows.
5. Run `python scripts/dropbox_cli.py auth finish THE_CODE`. This saves a refresh token, so access keeps working after the short-lived token expires.
6. The credentials are saved to `~/.config/dropbox-cli/dropbox_credentials.json`. Every place the tool looks is listed in `SKILL.md`; the `DROPBOX_TOKEN` environment variable also works for a quick test.
7. Run `python scripts/dropbox_cli.py ls /` to confirm.

## Using it

Ask your agent to find, share or move files. Sharing, invitations and deletions are shown to you before they happen. All commands are in `SKILL.md`.

## Tests

Run `python -m pytest scripts`. The tests are offline.

## Licence

MIT; see `LICENSE`.

## Privacy

Never commit the credentials file; git ignores it. Personal notes for your installation can go in a `LOCAL.md` beside `SKILL.md`, which git also ignores. The pre-commit hook in `.githooks` runs `tools/check_secrets.py` to block tokens and words from your private word list.
