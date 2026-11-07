# Last.fm historical import

This importer runs locally on the Mac. The running Apple Music NAS collector is
unchanged. It uses only Last.fm read methods; it does not write scrobbles, change
loved tracks or transfer playlists. No shared secret or signed-in API session is
needed. The user's key setup and full initial import have now succeeded locally.

## First setup

In a **local Mac terminal**, from this repository:

```sh
python3 lastfm_import.py setup
```

The default account is `andyhugelier`. Paste the **API key** at the hidden prompt
and press Enter. Do not paste the application shared secret. Input is not echoed.
The importer verifies the API key/account through `user.getInfo` before saving it
as `Config/private/lastfm-api-key`. The folder is mode `700`, the file is `600`,
and both are ignored by Git. Clear clipboard/history copies afterward. Existing
saved keys survive a failed replacement attempt.

The CLI uses standard Python libraries and the repository's shared helpers; it
does not need additional packages for Last.fm access. Use Python 3.9 or later on
macOS/Linux. Run it as your normal Mac user, **without sudo**. Don't paste keys
into commands, environment variables, chat or screenshots.

To confirm access again:

```sh
python3 lastfm_import.py check
```

This prints only the account name and Last.fm's reported scrobble count. It does
not download history or print the API key, real name, email or location.

## Small validation before the full import

Download one page, up to 200 scrobbles:

```sh
python3 lastfm_import.py import-scrobbles --max-pages 1
python3 lastfm_import.py status
```

Run commands individually, waiting for the terminal prompt between them. Review
the reported count, dates and `latest_runs` state. A `partial` run is expected
when the account has more than one page. A successful command means those pages
were saved, not that the full history is complete.

Once validated, continue in bounded batches (100 pages is up to 20,000 records):

```sh
python3 lastfm_import.py import-scrobbles --max-pages 100
```

Repeat until the latest scrobble run reports `state: complete`. Running the same
command after a completed run starts a fresh history snapshot with deduplication.
There is no daemon or background task; the download ends when the command ends.
One request is started at most every second, with up to three attempts for
transient failures. A 100-page batch takes at least roughly 100 seconds, plus
network and retry time. Ctrl+C stops safely between atomic page writes.

Import loved tracks separately:

```sh
python3 lastfm_import.py import-loved --max-pages 100
python3 lastfm_import.py status
```

The source API may limit page sizes further. Always rely on returned counts and
progress, not an assumed fixed record count per page.

## Data, provenance and completeness

- Data lives in `Data/listening-imports.sqlite3`, **separate** from the NAS
  collector database. This is a local historical evidence store for the future
  taste-profile milestone. It is not yet unified with Spotify or Apple data.
- Each source user and evidence kind stays separate. Scrobbles retain Last.fm's
  source timestamp; it is not converted into an invented playback duration or
  Spotify-style stream-end time. Loved-track timestamps describe source love
  metadata, not listening events, and remain null if missing.
- Names, album and track MBIDs are retained where provided. They are source
  identities, not yet matched Apple catalog IDs. No fuzzy merging occurs.
- A content key plus occurrence number preserves identical same-second source
  records within a run, including across pages. Repeated imports retain the
  maximum observed multiplicity rather than multiplying records. Last.fm does
  not supply a unique event ID here, so source corrections can produce additional
  records; cross-service deduplication and recording-version matching are future work.
- Every page and its progress commit in one transaction. Resume reuses the saved
  cutoff and next page. The database is application/version marked, and refuses
  an existing collector or unrelated database. One importer holds a process lock.
- Scrobble requests use a fixed `to` timestamp so new listens do not shift pages.
  Loved-track pagination has no equivalent snapshot boundary. Remote edits or
  deletions can still affect either source. Changed totals, repeated pages,
  account mismatches or final count mismatches stop the run as incomplete.
- A complete run means retrieved records matched the API's advertised count for
  that run. It does not prove all lifetime listening was scrobbled, or that the
  remote data was immutable during pagination. Never silently report partial
  imports as complete historical coverage.
- Now-playing placeholders are excluded. A historical scrobble without a usable
  timestamp stops the page for investigation; it is not assigned an invented time.
- Coverage flags timestamps within the first Unix day as suspicious and excludes
  them from reported date bounds. Raw source values remain unchanged for provenance
  and idempotency. Future recency/timeline analysis must treat those dates as
  unknown; presence of a later timestamp alone does not independently verify it.
  The live import contained 2,786 such records, which must not be presented as
  genuine 1970 listening history.
- Loved records are evidence observed during an import. An item subsequently
  unloved is not deleted from the historical store. To determine current love
  membership later, use the latest complete loved run's `run_events` membership,
  rather than treating every historical loved record as currently loved.
- `status` needs no API key or network access. It reports counts and source date
  ranges, not track names or other profile details. Counts include saved pages
  of incomplete runs; use `latest_runs` to judge coverage.

Tables: `runs` (source, cutoff and progress), `pages` (page provenance/checksums),
`evidence` (source records), and `run_events` (per-run multiplicity/membership).
No raw API response, URL, key, device/IP data or unrelated account fields are
stored in the import database. Database files and WAL/SHM/lock sidecars are ignored
by Git. Source API attribution links to the user's Last.fm profile are included
in coverage output.

## Failures and resuming

Network/service failures retry up to three times with bounded waits. A requested
retry delay over one minute stops rather than retrying too soon; resume later.
Invalid/revoked API keys and inaccessible accounts stop with safe messages.
Request URLs contain the API key, so raw exception text, response bodies and
request URLs are never printed or logged. HTTPS and redirect rejection prevent
forwarding the key to a redirect destination.

If a run stops because the remote library changed, inspect the safe error and
restart pagination with a new run:

```sh
python3 lastfm_import.py import-scrobbles --fresh --max-pages 10
```

This preserves prior records/provenance, creates a new cutoff, and deduplicates
records already seen. It does not delete or change Last.fm data. Do not run two
imports against the same database concurrently. The read-only status command may
run while an import is active.

Global options go **before** the command:

```sh
python3 lastfm_import.py --user another-user --private-dir /absolute/private/path \
  --database /absolute/path/history.sqlite3 status
```

Use separate private directories if accounts need different API keys. Custom
paths should stay outside source control and have equivalent access restrictions.
Back up this private database using SQLite's online backup API, or after all
connections have closed; do not copy only a live WAL-mode main database file.
Do not publish listening histories or send complete archives to an AI provider.

## Validation

Automated tests use synthetic keys and mocked API responses. They cover key
handling, retries/redaction, pagination, same-second duplicates, repeat imports,
resume, source mutations, empty history, now-playing exclusion, loved-track
separation, rollback, epoch-date quality reporting and cross-process database
reopen. All 57 Python tests and the browser test pass.

Live validation for `andyhugelier`: setup verified the account/key, followed by
382 scrobble pages totaling 76,220 records and one loved-track page totaling 142
records. Both runs completed with advertised and downloaded counts matching.
The database has 5,946 distinct scrobble artist names, and 73,434 scrobbles have
non-epoch timestamps spanning November 2009–December 2015. The remaining 2,786
scrobble dates are flagged as suspicious; their raw source values are retained.
SQLite integrity and foreign-key checks passed. The live data and key remain in
Git-ignored local storage; the NAS collector is unchanged.

References: [Last.fm recent tracks](https://www.last.fm/api/show/user.getRecentTracks),
[loved tracks](https://www.last.fm/api/show/user.getLovedTracks),
[user profile](https://www.last.fm/api/show/user.getInfo),
[request guidelines](https://www.last.fm/api/intro).
