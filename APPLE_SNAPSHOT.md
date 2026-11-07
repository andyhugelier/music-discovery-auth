# Apple Music preference snapshot

`apple_snapshot.py` is a one-off read-only export using existing authorization.
It does not change songs, favorites, ratings or playlists. It runs in a separate
temporary container using the existing image; no image rebuild is required.

## What it captures

- Library songs, albums, artists, playlists and ordered playlist tracks.
- Returned favorite flags, plus a separate `extend=inFavorites` song probe.
  The probe may be unsupported or omit the field; success alone does not prove
  favorite coverage. A favorited playlist does not make every member song a favorite.
- +/-1 ratings for known library-song IDs in batches of 25. This does not enumerate
  all historical likes/dislikes, particularly unknown songs outside the library.
- Recent tracks, recently added items and heavy rotation as observations/aggregates.
- Replay's latest eligible-year summary and views, when the account is eligible.
- Personal recommendations and their contents, kept separate from user preferences.
- Storefront metadata for future catalog matching.

Library/catalog IDs, genres, version metadata and source structures are retained.
No audio is downloaded. Artwork/previews, URLs, credential-shaped fields and
echoed active credentials are stripped before persistence. Raw error bodies and
request headers are never printed or saved.

## Run the approved extraction

In a **local Mac terminal**:

```sh
scp -O -P 54 \
  "/Users/andyhugelier/Project Repos/music-discovery-auth/apple_snapshot.py" \
  andyhugelier@192.168.6.239:~/apple_snapshot.py
```

In the **SSH terminal**, run each command separately:

```sh
cd /volume1/docker/Music-discovery
```

```sh
sudo docker compose -f compose.collector.yaml run --rm -T --no-deps \
  collector python - < "$HOME/apple_snapshot.py"
```

Wait for the prompt to return and review the final JSON. It shows section labels,
counts and safe errors, without playlist/track names or credentials. This helper
gets its own 128 MB limit and process; it does not share the live collector's
memory budget. Existing shared modules and private configuration are in the image.
No Apple credential is copied to the Mac or pasted into a command.

### Background continuation for a large library

After checking the initial report, the export can resume independently of the
SSH terminal using a read-only bind mount of the already transferred script:

```sh
sudo docker compose -f compose.collector.yaml run --rm -d --no-deps \
  --name music-discovery-apple-export \
  -v "$HOME/apple_snapshot.py:/app/apple_snapshot.py:ro" \
  collector python /app/apple_snapshot.py --max-pages 10000
```

This prints a container ID and returns immediately. Do not launch another export
while it is running; the database lock rejects concurrent writers. It remains a
bounded one-off job, not a recurring service. `--rm` overrides the inherited
restart policy and removes the temporary container when it exits; saved pages
and the summary remain in the database. A large library and per-song rating
lookups can take substantially longer than a few minutes. Rate-limit/network
failures may still pause with partial progress rather than complete everything.

To inspect saved progress at any point, including after the job exits:

```sh
sudo docker compose -f compose.collector.yaml run --rm -T --no-deps \
  -v "$HOME/apple_snapshot.py:/app/apple_snapshot.py:ro" \
  collector python /app/apple_snapshot.py status
```

The status command does not request Apple data or start another export. While the
job runs, `sudo docker logs --tail=10 music-discovery-apple-export` also shows safe
progress. After automatic container removal, use the persisted status report.

## Persistence, resume and interpretation

The export writes `/app/data/apple-music-snapshots.sqlite3`, separate from the
collector database. Its schema has `runs`, `sections`, `pages` and a rating-work
marker, with a dedicated application/version ID and process lock. Each page and
its discovered child sections commit atomically. New files are mode `600`; keep
directory permissions and backups restricted too. Database/sidecar files are
ignored by Git and excluded from Docker build contexts.

Rerun the same command to resume `partial` work. A finished run leads to a fresh
dated snapshot on the next invocation; `--fresh` explicitly starts a new run.
`--max-pages N` bounds section attempts per invocation (default 1000). Requests
start at most once a second with three attempts for transient errors. Long
rate-limit delays pause instead of retrying early; developer JWTs refresh during
long jobs. API errors cannot silently become empty successful collections.

Pagination carries the accepted page size and representation/filter options when
Apple's next link omits them; offsets always come from the response. Existing
checkpoints recover options from the first successful request, so unsupported
initial limits are not restored. Pending sections rotate by pages fetched rather
than letting one large collection monopolize the request budget. Progress includes
page ordinal and cumulative section items.

The states are `complete` (scheduled pagination exhausted),
`complete_with_unavailable` (explicitly reported unavailable sections), or
`partial` (pending/error work). HTTP 400/403/404/405/501 retain their status and
unavailable label; they are not proof of no data. A rejected limit gets one retry
using the endpoint's default page size. Global authorization, network and rate
limits pause the job. Looping/unsafe pagination and unexpected response shapes
remain errors for investigation.

Nested relationship/view pagination preserves its source category. Embedded
resources may also appear in child sections: page/section counts are observations,
not unique songs, independent endorsements or play counts. Missing favorite flags
are unknown, not false. Source library/playlist membership must not automatically
be weighted as a like or labeled user-created; transferred and curated collections
may be present. Apple library changes during pagination can affect completeness;
the run and page timestamps describe an extraction window, not an atomic account
snapshot or lifetime listening history.

For a smaller batch, change `python -` to `python - --max-pages 25` in the command.
For a local-only report with no Apple requests or credential loading, use
`python - status` instead. Do not run two exports against the same database.

After the temporary container exits and all writers close, the database can be
transferred privately into the Mac's ignored `Data` directory. Never copy only a
live WAL-mode main database while a writer is active; wait for closure or use a
SQLite online backup. No history or playlist metadata belongs in Git/public assets.

## Verification

All 75 Python tests and the browser-flow test pass. Snapshot tests cover pagination,
resume, child discovery, preference separation, redaction, GET-only requests,
transaction rollback, optional denials, default-limit fallback and database safety.
An offline Linux/amd64 image smoke test passed with synthetic data, networking
disabled, a read-only root filesystem and a 128 MB limit.

The first live run stopped normally at its 1000-attempt cap: storefront completed,
and library songs saved 25,050 entries in 999 pages. Other sections remained
pending, with no reported errors. The original implementation lost its requested
page size after page one, falling back to 25 items, and scheduled the same section
until exhausted. Both behaviors have been corrected with regression tests. Resume
the saved run with the updated script (no `--fresh`); a 100-attempt batch provides
a shorter validation of the fix and brings other sections into the report.
Preference counts from the first run are not evidence of zero preferences because
those sections had not started. Full coverage remains unverified.

The subsequent 100-attempt validation preserved run 1 and the 25,050 saved song
entries while fetching other sections. Replay returned one summary; heavy
rotation returned an empty collection. The first 100 library playlists were
listed, with 5,250 playlist-track entries saved across 100 discovered track
sections (58 finished, 42 pending). Library albums/artists and favorite flags
each saved an initial 100-item page. Recommendations and nested aggregate
pagination were discovered. No section errors were reported. The 100 returned
favorite flags were false; this is only sampled coverage. Ratings have not yet
been queued because the base song listing is still pending. Full extraction is
still partial at that checkpoint.

The subsequent background continuation finished run 1 with
`complete_with_unavailable`, confirmed by the user's final status output:
87,873 library songs and favorite flags, 10,923 albums, 5,657 artists, 134 playlists
and 26,136 playlist-track entries. Preference counters report 48 favorites,
48 positive ratings and 5 negative ratings. Favorites and positive ratings must
be matched by ID before weighting to avoid counting the same preference twice.
Recent tracks returned 146 observations, recently added returned 150 items,
Replay returned one summary, and 18 recommendation containers yielded 106
available content items. One ratings batch returned 403 and eight recommendation
content sections returned 404; these remain explicit coverage gaps.

This run is finished. Do not restart extraction to clear unavailable sections;
another normal invocation would start a new full snapshot. The next step is a
private local database transfer and integrity check before taste-profile analysis.
