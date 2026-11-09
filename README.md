# Spot On

Manage your Spotify playlists from GitHub Actions or a local Python CLI. Fork this repository, connect your own Spotify app, and run the same commands in either environment. Spotify OAuth and Web API requests are implemented directly with HTTPX; Spotipy is not used.

| Command | Behavior |
| --- | --- |
| `duplicates` | Report songs with the same title and artists, including their playlist positions. Does not remove entries. |
| `backup` | Save the current playlist and its order to a JSON file. |
| `sort` | Back up the playlist, then sort by release date, title, artist, album, duration, or date added. |
| `compare` | Report shared tracks and tracks unique to either playlist, without changing either playlist. |
| `merge` | Back up the destination, then append tracks missing from it from one or more source playlists. |
| `restore` | Back up the current order, then restore the order from a saved backup. |
| `batch` | Run configured operations in order and save a report for each job. |
| `batch-due` | Run only configured jobs scheduled for the current UTC weekday and hour. |

Sorting retains duplicate occurrences and each entry's original added date. Equal sort keys retain their relative order. The default is release date, newest first; date-added sorting also defaults to descending, while title, artist, album, and duration default to ascending. Spotify release dates with only a year use January 1; dates with a year and month use the first day of that month. The date is the release date of the album edition attached to the track, which can differ from the song's original release.

Sorting, comparison, and merging require catalog music tracks and fail if a playlist contains local files, unavailable entries, or episodes. Playlist reading validates required catalog metadata, including album release dates, and reports missing or invalid fields as errors. Backups preserve unsupported entries. Restore requires catalog music tracks and only reorders an existing playlist: the current playlist must contain the same entries with the same occurrence counts as the backup. It does not recreate deleted songs or remove newly added ones.

Comparison and merging identify tracks by Spotify URI, so different recordings of the same song remain distinct. Merging preserves the destination's existing entries, including duplicates, and appends each missing track once, in source playlist order. Sources are unchanged. Running the same merge again adds no tracks unless a source has new tracks. A merge backup records the destination before additions, but `restore` cannot undo those additions; remove added tracks separately before restoring its previous order.

## Connect your Spotify account

Each fork uses its own Spotify developer app and credentials. Under Spotify's current [Development Mode rules](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide), the app owner needs an active Premium subscription; new apps are limited to five authorized users. Playlist contents are accessible for playlists you own or collaborate on. This project uses the current `/playlists/{id}/items` endpoints.

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) with Web API access. Add your Spotify account to the app's authorized users when needed.
2. Register `http://127.0.0.1:8888/callback` as the redirect URI. Spotify accepts explicit loopback IP addresses, and [does not accept `localhost`](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).
3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone your fork, and open a terminal in the repository.
4. Copy `.env.example` to `.env`. Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from the app settings. Set `SPOTIFY_PLAYLIST_ID` to the ID from your playlist URL, such as the ID after `/playlist/` in `https://open.spotify.com/playlist/ID`.
5. Install the locked dependencies and generate an authorization URL:

   ```sh
   uv sync --locked
   uv run --env-file .env spot-on auth-url
   ```

6. Open the returned `url` in your browser and approve Spotify access. Save the returned `state` from the command. After approval, copy the complete callback URL from the browser address bar. The browser may show a connection error because this CLI does not start a callback server; the URL still contains the authorization code.
7. Exchange it locally, supplying the saved state:

   ```sh
   uv run --env-file .env spot-on auth-token --callback-url 'http://127.0.0.1:8888/callback?code=RETURNED_CODE&state=RETURNED_STATE' --state 'SAVED_STATE'
   ```

8. Put the returned refresh token in `.env` as `SPOTIFY_REFRESH_TOKEN`. Keep the client secret, refresh token, and callback URL private. Authorization setup follows Spotify's [Authorization Code flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow); access tokens refresh automatically for later commands.

`.env` is ignored by Git. `SPOTIFY_REDIRECT_URI` is only needed during authorization and must match the URI registered with Spotify. If authorization is revoked, repeat the authorization steps and replace the refresh token.

## Run locally

Python 3.12 or newer is required. uv can install a compatible Python version during setup. Supply credentials through `.env` or your shell environment.

```sh
uv run --env-file .env spot-on duplicates
uv run --env-file .env spot-on backup
uv run --env-file .env spot-on sort
uv run --env-file .env spot-on sort --by artist
uv run --env-file .env spot-on sort --by added-at --order asc
uv run --env-file .env spot-on compare --playlist-id LEFT_ID --other-playlist-id RIGHT_ID
uv run --env-file .env spot-on merge --playlist-id DESTINATION_ID --source-playlist-id FIRST_SOURCE_ID --source-playlist-id SECOND_SOURCE_ID
uv run --env-file .env spot-on restore --backup-file backups/YOUR_BACKUP.json
```

Use `--playlist-id ID` to select a playlist; local commands also accept a Spotify playlist URI or URL. For `restore`, omitting `--playlist-id` uses the playlist ID saved in the backup, regardless of `SPOTIFY_PLAYLIST_ID`. `backup`, `sort`, `merge`, and `restore` accept `--backup-dir PATH`; the default is `backups`. Commands write JSON results to standard output and errors to standard error. `uv run spot-on --help` shows the available options.

`sort --by` accepts `release-date`, `title`, `artist`, `album`, `duration`, and `added-at`. Use `--order asc` or `--order desc` to override the default direction.

Text sorting ignores case. Artist sorting compares the full credited artist list in its existing order. Album sorting groups matching album names and preserves the existing order within each group. Date-added sorting compares timestamps in UTC. Duration and date-added sorting fail clearly when the required duration or timestamp is missing or invalid.

Sort and restore change the playlist's custom order, so choose the custom ordering in Spotify to see the result. Sort, merge, and restore save a uniquely named backup before their first write. Operations use Spotify playlist snapshots to detect concurrent changes; avoid editing a playlist from another client during an operation. If sorting or restoration fails after some changes, keep its backup and use `restore` to recover the saved order. A failed merge may have appended some tracks; keep its reports and backup to review those additions before rerunning.

## Batch operations and schedules

Copy `batch.example.json` to `batch.json`, replace every example playlist ID with your own, and edit the jobs. Credentials belong in `.env` or Actions secrets, never in the JSON configuration. Every job requires a unique `id` and an `operation` with explicit playlist IDs; batch jobs do not use `SPOTIFY_PLAYLIST_ID` as a default.

| Operation `command` | Required operation fields |
| --- | --- |
| `sort` | `playlist_id`, `sort_by`, `order` |
| `duplicates` or `backup` | `playlist_id` |
| `compare` | `playlist_id`, `other_playlist_id` |
| `merge` | Destination `playlist_id`, nonempty `source_playlist_ids` array |

`sort_by` uses the same values as `sort --by`, and `order` must be `asc` or `desc`. Each job can include a `schedule` with `weekdays` (one or more of `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`) and `hour_utc` (an integer from 0 through 23). Omit `schedule` or set it to `null` for a manual-only job.

```sh
uv run --env-file .env spot-on batch --config batch.json --backup-dir backups --report-dir reports
uv run --env-file .env spot-on batch-due --config batch.json --backup-dir backups --report-dir reports
```

`batch` runs every job regardless of schedule. `batch-due` selects jobs whose schedule matches the current UTC weekday and hour; invoke it hourly from your local scheduler to use the same configuration locally. There is no background scheduler in the CLI. Both commands validate the entire configuration before executing jobs, run selected jobs sequentially in file order, and stop on the first failure. Successful earlier jobs remain applied. Reports are saved as `jobs/job-JOB_ID.json` and `summary.json` inside the report directory. Selected job reports start with a pending status before playlist operations begin; the summary starts as running and is updated after each job, ending as completed or failed. If the report files cannot be prepared, the batch fails before changing any playlist. Use a dedicated report directory per local run if you want to retain previous reports.

## Run with GitHub Actions

1. Fork the repository and enable workflows in your fork's **Actions** tab.
2. In **Settings → Secrets and variables → Actions**, add these repository secrets using the credentials and refresh token generated locally:

   | Repository secret | Value |
   | --- | --- |
   | `SPOTIFY_CLIENT_ID` | Your Spotify app's client ID |
   | `SPOTIFY_CLIENT_SECRET` | Your Spotify app's client secret |
   | `SPOTIFY_REFRESH_TOKEN` | Your account's refresh token |

3. Optionally, add a repository variable named `SPOTIFY_PLAYLIST_ID` for your default playlist; without it, enter `playlist_id` on every run that asks for one. Use its 22-character ID containing letters and numbers, without a URI or URL.
4. Open **Actions**, pick a workflow, and choose **Run workflow**. Each operation has its own workflow showing only its inputs: **Find duplicates**, **Back up playlist**, **Sort playlist** (sort key and direction), **Compare playlists** (`other_playlist_id`), and **Merge playlists** (the selected playlist is the destination; enter source IDs separated by spaces or newlines in `source_playlist_ids`). Actions accepts IDs only. Sort and merge write to Spotify. All of them share the job in `.github/workflows/run.yml`.

The run uploads an artifact named `spot-on-RUN_ID-ATTEMPT`, containing `reports/` and any `backups/`. Artifacts are retained for 30 days; download backups you want to keep longer. Backups and reports contain playlist information, so use a private fork if that information should be private. Credentials are supplied as secrets and are not included in these files.

All workflows, including CI, run only when started manually from the Actions tab; none run on a schedule, push, or pull request.

For batches, commit your configured `batch.json` to the default branch, then open **Actions → Batch playlist tools → Run workflow**. This manual run executes all jobs. Use the `config_file` input or repository variable `SPOTIFY_BATCH_CONFIG` to select another repository path. The default is `batch.json`; the example file is never loaded automatically.

To restore a backup in Actions, open **Restore playlist order → Run workflow** and enter:

- The source run ID, found in its URL: `.../actions/runs/RUN_ID`.
- The source attempt number, normally `1`, also shown in its artifact name.
- The JSON filename inside that artifact's `backups/` directory, without a directory path.
- The playlist ID, or leave it empty to use `SPOTIFY_PLAYLIST_ID`.

The source artifact must still exist in this repository. If it has expired, use a backup you downloaded earlier and restore locally. Playlist operations, batches, and restore share one repository-wide concurrency group, so GitHub runs them sequentially even when a batch touches several playlists. Local runs and other forks are outside that group.

## Development

The CI workflow runs these checks without Spotify credentials:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```
