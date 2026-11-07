# Private taste review

This is the first local taste-group implementation, built from the imported
Apple, Last.fm and Spotify databases. It replaces the need to edit analysis JSON
for routine review. It is an overlapping genre/tag baseline, not learned clusters,
audio analysis, a mood classifier or a recommendation/station engine.

## Open the interface

In a local Mac terminal, from this repository:

```sh
.venv/bin/python taste_profile.py build
.venv/bin/python profile_server.py
```

Open **http://127.0.0.1:8765**. Keep that terminal/process running while using the
app; Ctrl+C stops it. It is not installed as an unattended service. The original
MusicKit authorization page remains separate and is not needed for local review.
No credentials are required by the review server. The UI has no third-party
scripts, fonts, artwork requests or analytics.

You can inspect counts without opening the server:

```sh
.venv/bin/python taste_profile.py status
```

The standalone scripts use standard-library Python and existing repository
helpers. No new package dependencies were added. The project venv is recommended
for consistent test/CLI execution.

## What you can review

- Browse twelve broad, overlapping genre/tag groups with representative artists.
- Open groups, search source tracks and inspect explicit preferences versus
  historical playback, weak library membership and grouping evidence.
- Rename groups and assign a source track to several groups or none.
- Exclude an individual source track or an artist from personal discovery.
- Review separate contexts and include a previously excluded artist.
- Undo saved changes. Actions survive a complete source rebuild.

An artist inclusion does not erase an explicit negative song rating or make
private-session/zero-playback-only evidence eligible. Group membership changes
affect the displayed source track; cross-service recording identities are not yet
reconciled. There are no automatic platform writes or recommendations in this UI.

## Persistence and regeneration

The private derived store is `Data/taste-profile.sqlite3` (mode 600), with its own
application/schema ID. It contains source-track summaries, seeded context rules,
genre/tag memberships, metadata cache, source coverage and persistent review
actions. `build` replaces only derived track summaries and automatic memberships
inside one transaction; rules, cached features and review actions are retained.
Manual group assignments take precedence over regenerated metadata groups.

The first build seeds approved rules from private
`Data/listening-context-review.json`. Subsequent builds do not silently reimport
that JSON over saved UI decisions. The earlier `taste-profile-review.json` is a
legacy analysis snapshot, not the live UI database. Use the UI for new corrections.

Sources are read-only, with shared locks cooperating with the importers. An
immutable connection is used only for a closed snapshot with no nonempty WAL;
otherwise the normal read-only connection includes WAL contents. Do not replace
or copy a live main SQLite file alone. Source application/schema IDs are checked.
This baseline rejects multi-account Last.fm/Spotify inputs rather than silently
mixing multiple people's histories. Missing sources are reported; invalid input
rolls back without replacing a previously built profile.

Private history, rules, actions, cache and generated screenshots must stay outside
Git. The Docker build allowlist excludes this tooling and all private stores.

## How the baseline works

Track-level Apple genre metadata is preferred. Missing genres can use cached
Last.fm artist tags, or a broad estimate from other eligible library tracks by
that artist. The basis is visible per track. Artist metadata does not establish
a track's mood, and overlapping memberships are not an independently learned
model of the user's musical interests. Unclassified tracks remain available.

Representative examples use an explicit initial heuristic: deduplicated Apple
favorite/positive-rating evidence and Last.fm loved tracks are strong; repeated
scrobbles and Spotify streams have saturated influence; library-only membership
is weak. These weights are not learned or probabilities of liking. No aggressive
age decay is used. Suspicious Last.fm dates are marked undated, and Spotify's
30-second threshold is only a transparent review convention. Skips are never
automatic dislikes. Group counts are source tracks, not unique recordings or
independent listens, and source event counts are not added together as plays.

## Bounded metadata enrichment

Optionally, using the existing privately saved Last.fm API key:

```sh
.venv/bin/python taste_profile.py enrich --max-artists 50
```

This sends individual eligible artist-name queries to `artist.getTopTags`, with
the existing client's one-request-per-second pacing and bounded retry behavior.
It does not upload history, write scrobbles or fetch audio. Initial seed selection
prioritizes eligible explicit Apple preferences, then other supported artists.
Cached artists are skipped on subsequent invocations; empty responses remain
explicit. Only tag names/weights are cached, not upstream URLs or raw bodies.
Successful cache writes survive interruptions; rerun enrichment or build to
refresh memberships after an interrupted batch. Show Last.fm attribution in UI.

## Verified locally

- 169,970 source-track summaries; 1,909 excluded records and 168,061 eligible.
- Twelve overlapping groups; 129,547 eligible source tracks have a group after
  the bounded pilot, while 38,514 remain unclassified.
- Fifty artist queries: 49 returned usable tags, one returned no tags. This is
  pilot yield, not whole-library coverage or a recommendation-quality score.
- Rebuild preserved counts, cached tags and review behavior. Source main-file
  hashes were unchanged; integrity/foreign-key checks passed. The named rain
  recording stays excluded, and the approved artist exception remains eligible.
- A local Mac rebuild plus hash/integrity verification took about 11 seconds;
  that process peaked around 474 MB RSS. This includes verification overhead and
  is not a production memory budget or a NAS benchmark. No deployment was made.
- Desktop and 390px mobile browser checks: group browsing, search, renaming,
  membership edits, exclusions and undo worked without horizontal overflow.
  Browser test edits were undone. Automated tests cover regeneration, context
  specificity, source semantics, active WAL reads, locking and HTTP protections.
- Final verification: all 96 Python tests, the existing browser authorization
  test, JavaScript syntax checks and `git diff --check` pass.

The server binds only loopback, validates Host, restricts assets to an explicit
allowlist, disables caching and denies cross-origin writes using Origin plus a
per-process CSRF token. Source strings are rendered as text. No Data directory or
arbitrary file endpoint is served. Revisit access control before any shared or
remote deployment rather than changing the bind address.

Next: compare richer grouping approaches against this inspectable baseline,
resolve recording identities and candidate sources, then add controllable station
previews and feedback for actual recommendation batches. Audio/mood features
remain a separate, unimplemented experiment.
