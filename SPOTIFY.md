# Spotify export import

`spotify_import.py` reads extracted Extended Streaming History JSON files locally.
It uses Python's standard library, makes no network requests and needs no Spotify
API app, credentials or additional dependencies. Account/library exports and
technical logs are different packages; this importer does not ingest them.

## Commands

In a local Mac terminal, from the repository:

```sh
python3 spotify_import.py inspect "/path/to/Spotify Extended Streaming History"
python3 spotify_import.py import "/path/to/Spotify Extended Streaming History" --label delivery-1
python3 spotify_import.py status
```

The source is read in place; it is not copied into the repository. Only recognized
`Streaming_History_Audio_YEAR[_PART].json` and Video equivalents are read, at most
256 files and 64 MB per file. Unknown package files are ignored. Source symlinks
are rejected. Inspect validates data and reports counts/dates without writing a
database or displaying song names, IP addresses or raw rows.

The default database is private, Git-ignored `Data/spotify-history.sqlite3`,
separate from Last.fm and Apple history. `--database` and `--account` precede the
subcommand when used. Account defaults to the local alias `personal`; use a
different alias for a different person's/account's export. The files may omit a
username, so account ownership cannot be verified automatically.

## Privacy and evidence semantics

Persist only music identifiers/metadata, stream-end UTC, milliseconds played,
start/end reasons and nullable playback flags. Podcasts/audiobooks and video
retain separate media/content categories. Do not feed them into a music profile
by default. Unknown content remains unknown.

IP addresses, country, device/platform, username, user-agent, offline timestamps
and unrelated fields are not stored. Raw input/error bodies are not logged.
Database files are created mode 600, directories mode 700. SQLite uses a separate
application/schema ID, foreign keys, full synchronous transactions and a writer
lock. A supplied delivery commits atomically; invalid input/interruption rolls
back that delivery. The source export and previously completed imports remain
unchanged. Keep original archives private, including their unrelated sensitive
fields; do not commit them or send them to an AI provider.

Spotify's included English field guide (pages 3–5) and its official
[data description](https://support.spotify.com/us/article/understanding-your-data/)
identify `ts` as stream-end time. `ms_played` is accumulated playback evidence,
not track duration. Do not infer exact starts, completion percentages or full
listens. A skip is not a dislike. Null flags remain unknown. A filename's year
is not an authoritative event-year partition; derive coverage from timestamps.

The private review preview omits known context exclusions, private sessions and
zero-duration rows. Its 30-second artist-summary threshold is an explicit review
convention, not a definition of a listen, a positive label or a learned weight.
These are analysis views, not a deployed recommendation engine.

## Repeat deliveries and deduplication

Each file retains its digest, safe basename, media type and row count. Source row
ordinals link to imported events. Batches retain their file manifests. Identical
payload copies within one delivery are read as one source file.

Events are identified by a hash of allowed semantic fields plus an occurrence
ordinal within the supplied delivery, scoped to an account. This preserves
identical same-time occurrences within a delivery while preventing repeat or
overlapping exports from multiplying ordinary records. Stored multiplicity is
the maximum observed across deliveries. Changes only to omitted device/location
fields do not create new events.

There is no universal stream-event ID in these files. Exact duplicate rows split
across separate partial deliveries cannot always be distinguished from repeat
exports; combine all parts of a delivery in one input directory. Changes to music
metadata or playback-context fields can produce a different fingerprint and need
later reconciliation. Raw source provenance remains available for investigation.
Cross-source events are not merged: Last.fm start-time semantics and Spotify
end-time semantics must be considered before matching possible overlapping plays.

## First live delivery and verification

All 27 supplied JSON files imported: 151,335 records spanning July 2011–October
2026. Audio contains 150,941 music records (43,097 distinct track URIs) and 291
podcast records. Video contains 69 music records, 33 podcast records and one
unknown record. Coverage is dense in 2016–2025; 2026 has only 33 audio records,
31 of which are music. Do not infer continuous coverage from the outer date range.

The complete source folder was imported again as a live idempotency check:
zero new events, unchanged 151,335 records. Integrity check returned `ok`, with
zero foreign-key violations. All 86 Python tests and the browser test pass.
Synthetic tests cover overlapping exports, same-time multiplicity, account
separation, atomic rollback, nullable flags, timezone normalization, source
redaction, media/content separation, locking and read-only reopen.

Detailed private coverage and context-filtered summaries are in
`Data/spotify-import-review.json`; the draft `Data/taste-profile-review.json`
now includes a separate Spotify section. Current context rules identify 15,270
audio music records for exclusion. The combined preview excludes 16,555 records
after also considering private sessions and zero playback, leaving 134,386
nonzero records. Exclusion categories overlap; do not add them independently.
The recommendation algorithm and cross-source event reconciliation remain
unimplemented. No source history was uploaded, committed or deployed.
