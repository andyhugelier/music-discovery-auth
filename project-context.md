# Music Discovery — Project Context

## Project goal

Build a personal, always-on music discovery service that integrates with Apple Music, collects listening history, identifies musical preferences, generates personalized recommendations using AI, and eventually creates Apple Music playlists automatically.

The system should improve recommendations over time using listening behavior and explicit feedback.

### Product motivation and broader scope

The user has tried most major streaming platforms. In their experience, Spotify
and Pandora offer useful discovery but have not met their sound-quality needs,
while Apple Music and Tidal offer the listening quality they prefer but make
discovery harder. These are the user's experiences and priorities, not a verified
comparison of current platform specifications.

The purpose is to combine the user's preferred Apple Music listening experience
with stronger, personally controlled discovery. The preference profile should
include their musical history across services rather than starting from only the
new Apple collector. Listening history is scattered across multiple accounts.
They use TuneMyMusic to transfer libraries/playlists; do not assume those
transfers contain complete listening history or transfer the source's internal
recommendation model. The user identified Spotify as probably the largest source,
with substantial Last.fm use before streaming. Pandora and Tidal have smaller
pools; Shazam is used occasionally, and iTunes predates these services. Exact
years and export availability remain to be established. The user has submitted
the Spotify data request and supplied `andyhugelier` as their expected Last.fm
username. The first Spotify Extended Streaming History delivery has now been
received, inspected and imported locally; other requested packages remain pending.

### Confirmed historical source inventory

| Priority | Source | User context and proposed role |
| --- | --- | --- |
| 1 | Spotify | First extended-history delivery imported privately; inspect later packages separately. |
| 2 | Last.fm | Expected username `andyhugelier`; used before streaming. Recover older scrobbles and loved tracks if accessible. |
| 3 | iTunes | Earlier library; inspect any surviving XML/library backups for usable metadata, ratings or aggregate counts without assuming they contain per-play history. |
| 4 | Shazam | Occasional identification history; treat as evidence of curiosity, not automatically a like or completed listen. |
| Later | Pandora and Tidal | Smaller pools; add thumbs, favorites or history where actually available. |
| Ongoing | Apple Music | Continue the live collector and add current explicit preferences/library signals when implemented. |

Do not hold the first recommendation milestone until all sources are imported.
Start with Spotify and Last.fm, with the existing Apple collector continuing.

The user confirmed they can see their Last.fm history for `andyhugelier` in
their own browser. Automated web retrieval encountered a JavaScript challenge
and "Temporarily Unavailable"; those failures do not indicate missing history.
The user completed hidden API-key setup, which verified the account/key. Full
initial scrobble and loved-track imports have since completed locally (coverage
below); the key remains in private, Git-ignored storage. Recent/loved-track reads do not require
the user's password, a signed-in API session or the application shared secret.

### Recommendation roadmap

The user expanded the product direction on November 6, 2026: overlapping musical
interests should support genre-oriented stations, mixtures between interests and
cross-genre mood stations. Controls should distinguish familiar favorites,
rediscovery, nearby new tracks and broader exploration. Track fatigue needs
temporary cooldowns, separate from dislike and context exclusion. Preserve a
platform-independent profile, station recipes and feedback with Apple as the
first output adapter. A polished, simple visual review/feedback interface is part
of the early milestone, not an optional final layer. User corrections should not
require editing JSON.

Research and the proposed next implementation sequence are documented in
`RECOMMENDATION_DESIGN.md`. Recommendation: a hybrid pipeline using reusable
track/artist relationships and tags first, overlapping interests and transparent
ranking/playlist constraints, with optional pretrained audio features later.
Compare Last.fm and Troi/ListenBrainz candidate sources, NMF versus graph/tag
groups and ranking baselines before committing to a model. A vector index is
storage, not a prepopulated music knowledge source. The user confirmed local audio
files on the storage drive, mostly from the Last.fm era, and regards them as a
possible technical baseline rather than current-taste evidence. The user supplied
`volume1/music`, normalized to `/volume1/music`. The user ran the bounded inventory
in a temporary network-disabled container with a read-only mount as UID 1027:100:
root readable, 5,000 directory entries checked, zero directory errors, 1,244 MP3,
2,367 M4A and 41 M4P file extensions found (3,652 audio-file candidates). The cap
was reached; these are sample counts, not whole-library totals or a random sample.
Audio decoding, codecs, tag quality and duration remain unverified. Prefer MP3/M4A
for the initial probe; M4A can contain different codecs, and M4P may be protected.
Do not attempt DRM circumvention. No audio files have been copied or analyzed.
Plan a bounded read-only sample for feature/similarity evaluation,
without treating ownership as a like or training a current preference model from
this collection. Coverage for current releases and mood confidence remain
unresolved feature-source questions.
Research is documentation-only; no enrichment pilot, benchmark or engine/UI is
implemented yet. Do not upload history to ListenBrainz or assume protected streams
can be analyzed. Keep the first implementation bounded and measure real quality.

The user agreed with this direction; the weekly mix size remains a proposed default:
1. Build a reviewable, editable taste profile from current observations, historical
   imports, explicit preferences and catalog enrichment. Show data coverage and
   uncertainty so the user can correct misleading signals.
2. Produce an initial batch of real catalog-matched recommendations with reasons.
   Separate candidate generation from ranking, include exploration/diversity and
   record each batch's source and ranking decisions. A weekly mix of about 20
   songs is a proposal, not a fixed requirement.
3. Add feedback (love it, not for me, already know it, more like this), evaluate
   recommendation quality, and deliver approved batches as Apple Music playlists.

### Apple Music preference snapshot (completed; verified locally)

The user asked what additional Apple data should be extracted for the taste
profile. The live NAS collector currently captures recent-track observations
only; it has not fetched the broader library, playlists, ratings or Replay.
Authorization persists on the NAS and is not tied to the chat/browser session,
but can still require reauthorization later. There is no local Apple signing-key
copy; do not copy NAS credentials to the Mac to simplify extraction.

Recommended read-only extraction priorities:
1. Explicit favorites/ratings/dislikes where actually exposed for this account.
   Legacy +/-1 ratings and current favorite flags are different concepts; validate
   availability rather than treating them as interchangeable or assuming all
   favorites can be enumerated by a single endpoint.
2. All library playlists and their paginated ordered tracks, including source/
   editability/system-vs-user clues where returned. Treat transferred/curated
   playlist membership separately from a new personal endorsement.
3. Saved library songs/albums/artists and catalog identifiers/genre/version
   metadata where returned. Membership indicates collection/familiarity, not a
   confirmed recent listen or strong like; retain unmatched/local-library items.
4. Heavy rotation and Replay summaries where available. Heavy rotation contains
   resources, not a per-play log. Replay's documented endpoint returns the latest
   eligible year (`filter[year]=latest`), not arbitrary complete annual histories.
5. Apple recommendations as a separate candidate source/evaluation baseline,
   never treated as evidence the user liked or listened to the recommendation.

Store dated, source-labeled snapshots privately with extraction coverage/errors,
preserving library IDs separately from catalog IDs. Follow pagination, avoid
changing the live collector database schema, and make no library writes. Exact
play timestamps, skips and lifetime counts must not be inferred. This extraction
is implemented as `apple_snapshot.py`; the completed snapshot has been copied
locally and verified as described below. Consider a separate Apple account-data export for
historical coverage after inspecting available music-specific categories/fields.

The user explicitly requested this read-only extraction. The tool follows root
and embedded pagination, queues playlist tracks and recommendation contents,
queries ratings for known library-song IDs, probes song favorite flags, and
reports complete/partial/unavailable coverage per section. It stores sanitized,
dated pages in a separate `/app/data/apple-music-snapshots.sqlite3` database with
atomic progress, source categories and credential-safe output. No service rebuild
or interruption is needed: transfer the single script and run it via stdin in
a separate temporary container using the existing image. `APPLE_SNAPSHOT.md`
contains exact instructions. All 75 Python tests pass, plus the earlier offline
image smoke test under read-only filesystem and 128 MB restrictions. No new
changes are committed/pushed.

The user supplied the first live run report: run 1 is `partial`, storefront
completed, and `library_songs` saved 25,050 entries in 999 pages. Other sections
are pending and `incomplete_sections` is empty: this was the 1000-attempt budget,
not a reported API failure. Preference counters at zero are unmeasured, not proof
of no likes/favorites. The exporter initially dropped page size after page one
and prioritized the same section until exhausted. It now preserves accepted
page options and rotates pending sections by pages fetched. Existing run IDs,
pages and offsets are retained; no migration or `--fresh` is needed. The updated
script was transferred and the 100-attempt validation has now completed:
- Run 1 and the 25,050 saved song entries were preserved. Library songs remain
  pending while previously unstarted sections catch up under fair scheduling.
- Replay returned one summary; heavy rotation completed with zero returned items
  (do not infer absence of listening from this result).
- The first 100 library playlists were listed. Their discovered track sections
  saved 5,250 playlist-track entries: 58 sections completed and 42 are pending.
- Library albums/artists and the favorite-song probe each saved an initial
  100-item page. The report has 100 favorite flags, all false so far; this is
  sampled coverage, not evidence of zero account favorites. Ratings are not yet
  queued because the base library-song listing is incomplete.
- Recommendations and aggregate nested pagination are queued, and there are no
  reported section errors. Full extraction remains partial.

The detached continuation subsequently finished run 1 with state
`complete_with_unavailable`, verified from the user's final status report:
- 87,873 library songs and favorite flags; 10,923 albums; 5,657 artists.
- 134 playlists with 26,136 ordered track entries (not unique songs or plays).
- 48 favorited resources, 48 positive song ratings and 5 negative ratings.
  Compare IDs before combining favorites and positive ratings; they may overlap.
- 146 recent-track observations, 150 recently added items, one Replay summary,
  18 recommendation containers and 106 returned recommendation-content items.
- One known-library ratings batch was unavailable (403), along with eight
  recommendation-content sections (404). These are explicit coverage gaps;
  other sections completed. Heavy rotation returned zero items.

Do not rerun the extraction merely to clear `complete_with_unavailable`: this is
a finished run, and another invocation starts a new full snapshot. Next transfer
the closed private snapshot database locally, verify integrity, and combine its
signals with Last.fm for the first reviewable taste profile. Library membership
is a weaker signal than explicit preferences, especially for transferred lists.
The snapshot is not a historical playback ledger.

The user copied `Data/apple-music-snapshots.sqlite3` locally. Both that database
and the Last.fm database pass full SQLite integrity and foreign-key checks.
The Apple copy has no WAL sidecar; the Last.fm WAL is empty. Analysis used
read-only immutable connections to these closed snapshots without reading any
credentials or making API calls. The local Apple run and counts match the final
user report. All 48 favorites overlap the 48 positive ratings by library-song ID;
there are 48 distinct positive IDs, five negative IDs and no positive/negative
conflicts. 87,092 library IDs carry catalog IDs, covering 86,923 distinct catalog
IDs; preserve the 781 library records without that mapping.

An initial private profile review is saved in ignored `Data/taste-profile-review.json`
(mode 600). It includes source-specific artist summaries, all explicit positive
and negative song preferences, 2026 Replay's 20 artists and 15 albums, and
conservative cross-source comparisons. Replay's 100 top-song relationships
return catalog IDs without names; 40 match library catalog IDs directly. The
other 60 require catalog metadata resolution, not assumptions of missing songs.
Conservative normalized artist/title matching finds 7,403 of Last.fm's 36,483
distinct source pairs in the Apple library and 51 of its 142 loved-track pairs.
These are provisional name matches, not canonical recording identity matches.
The 2,786 suspicious Last.fm timestamps remain excluded from temporal summaries.

The profile is a draft awaiting user review, not a trained recommender. The user
confirmed that family, sleep/background and other listening contexts should be
kept separate from discovery. The user supplied named family-listening examples
and asked to separate rain sounds, with questionable tracks/artists presented for
confirmation. Detailed rules and assignments are private in
`Data/listening-context-review.json`. The initial review labels 203 Apple library
song IDs (129 rain-sound/background and 74 named family examples); one of the
48 positive song preferences falls in a confirmed background context. These are
analysis annotations, not deployed filtering or global artist bans. The user then
confirmed all examples in the review list should be filtered except one explicit
artist exception; exact artist rules, album-scoped soundtrack rules and the keep
exception are saved in the private context report. The updated draft excludes
976 Apple library IDs and 1,124 Last.fm scrobble records from personal discovery
evidence, retains 47 Apple positive preferences and all 142 Last.fm loved records,
and preserves original source data. The filtered preview is in
`Data/taste-profile-review.json`; production scoring/filtering is not implemented.
Validation checked the keep exception, count reconciliation and soundtrack-only
matching (unrelated albums and the performers' other work stay eligible).
Future ambiguous items still need review. Do not match arbitrary occurrences of "rain", "kids"
or "frozen" as evidence of sleep/children's use. Leave ambiguous evidence
unclassified. Context is separate from like/dislike:
retain source records and do not turn a context exclusion into negative taste
feedback. Do not automatically remove or downweight artists based on an inferred
household role. Keep detailed listening metadata
in private Data artifacts, not Git-tracked documentation or public assets.

Official API references checked:
- [Library songs](https://developer.apple.com/documentation/applemusicapi/get-all-library-songs)
  and [playlists](https://developer.apple.com/documentation/applemusicapi/playlists-api).
- [Ratings](https://developer.apple.com/documentation/applemusicapi/ratings-api)
  and [favorite album flags](https://developer.apple.com/documentation/applemusicapi/libraryalbums/attributes-data.dictionary).
- [Heavy rotation](https://developer.apple.com/documentation/applemusicapi/get-heavy-rotation-content)
  and [Replay](https://developer.apple.com/documentation/applemusicapi/get-the-user%27s-replay-data).
- [Recommendations](https://developer.apple.com/documentation/applemusicapi/get-a-relationship-on-the-recommendation).

### Historical import design and implementation status

`lastfm_import.py` is now implemented and tested locally. It provides a hidden
API-key setup prompt (profile verification before saving), safe account check,
resumable paginated scrobble/loved-track imports, and coverage output. Default
private storage is `Config/private/lastfm-api-key`; default historical database
is `Data/listening-imports.sqlite3`, separate from the NAS collector. No shared
secret is used. Existing NAS code/configuration is unchanged.

API keys, raw requests and response bodies are never printed or stored in history.
Page transactions preserve progress; fixed scrobble cutoffs, count checks and
repeated-page detection prevent silent completion of incomplete downloads.
Same-second duplicate occurrences are retained; repeating imports does not
multiply the same source records. Loved tracks remain a separate evidence kind.
Counts include saved partial pages, and per-run state makes incomplete coverage
explicit. Source matching/deduplication across services is not implemented.
Spotify ingestion has since been added as a separate offline
importer, described below. `LASTFM.md` documents Last.fm workflow and limitations.

Local verification: 57 Python tests plus the browser-flow test pass with synthetic
keys and mocked services. Live API ingestion has also completed for `andyhugelier`:
- Scrobbles: 76,220 source records across 382 pages, matching the reported total;
  5,946 distinct artist names and 36,483 distinct artist/title pairs (not yet
  canonical artist/recording identities).
- Non-epoch timestamps: 73,434 records spanning November 25, 2009–December 19,
  2015 in UTC. The other 2,786 scrobbles have suspicious first-Unix-day timestamps
  (small positive source integers). Preserve those raw values for provenance,
  but treat them as undated when building timelines or recency-weighted profiles.
- Loved tracks: 142 source records across one page, stored separately from
  scrobbles. Their source dates span November 2009–August 2012.
- Both import runs report complete with received/expected counts matching.
  SQLite integrity check returned `ok`; foreign-key check found no violations.
- No key or raw listening data has been committed, pushed or copied to the NAS.
  Importer code/documentation remain uncommitted; the running NAS collector is
  unchanged. Spotify delivery was pending at that checkpoint and is now imported.

### Spotify export received and imported (November 6, 2026)

The user supplied `/Users/andyhugelier/Downloads/Spotify Extended Streaming History`.
The 27 audio/video JSON files were inspected and imported in place without copying
the raw archive into the repository. The included field guide's English pages
3–5 describe lifetime streaming history and distinguish separately delivered
account data/technical logs. All supplied JSON files imported successfully;
account-lifetime completeness is not independently verified.

`spotify_import.py` is standard-library-only and offline. It stores allowed
music/playback-context fields, separate media/content categories, delivery/file/
row provenance and semantic occurrence fingerprints in private
`Data/spotify-history.sqlite3` (dedicated application/schema ID). No API credentials
or requests, raw JSON copies, IP/country/device/user-agent storage, or live-service
changes. Each delivery commits atomically; repeat/overlapping imports preserve
source provenance and same-time occurrence multiplicity. Maximum multiplicity
across supplied deliveries is a documented heuristic where no global event ID
exists. Use the same account alias for later exports of this account and combine
all parts of one delivery before importing. See `SPOTIFY.md` for limitations.

Verified live results:
- 151,335 total source records spanning July 14, 2011–October 16, 2026 UTC.
- Audio: 150,941 music records, 43,097 track URIs, 291 podcast records.
- Video: 69 music, 33 podcast and one unknown record, kept separate.
- Most music activity is 2016–2025; only 31 music records (33 total audio rows)
  occur in 2026. Filenames sometimes overlap years; use source timestamps.
- Audio music includes 1,134 zero-ms records, 5,054 skipped flags and 247 private
  session flags. None is automatically a full listen, like or dislike.
- Reimporting all files added zero events. SQLite integrity `ok`, foreign keys
  clean. All 86 Python tests and the browser-flow test pass in the project venv.

Private `Data/spotify-import-review.json` records coverage and filtered summaries;
the draft taste profile now includes Spotify as a separate source. Existing
context rules match 15,270 audio music records. Excluding those contexts, private
sessions and zero playback leaves 134,386 records (overlapping exclusions).
127,557 meet the explicitly labeled 30-second review threshold, not a definition
of a full listen or a positive preference. Original source evidence is retained.
3,968 normalized artist/title pairs in this filtered set overlap Last.fm; these
are provisional name matches, not deduplicated plays or canonical recordings.
Do not add source stream counts together as independently observed listens.
No cross-source event reconciliation, recommendation training or UI is implemented.

- Start with an inventory of services, years and available exports. Spotify
  Extended Streaming History is now the preferred first import based on the
  user's inventory. Last.fm is next for earlier history. Inspect actual exports
  and API results before committing to source-specific schemas.
- Retain source/account identity, import batch/file identity, original track IDs,
  supported event timestamps, field semantics and matching confidence. Imports
  must be idempotent and report coverage/unmatched records.
- Keep play events, saved/library membership, playlist membership, thumbs/ratings,
  explicit feedback and Apple polling observations as different evidence types.
  Unknown fields remain unknown. Imported timestamps must retain their original
  meaning (e.g. Spotify's stream-end time is not an exact playback-start time).
- Match recordings across catalogs using IDs/ISRC where available and corroborating
  artist/title/duration/version metadata. Do not silently merge live recordings,
  remasters, edits or ambiguous matches; preserve original service records.
- Deduplicate repeated imports without collapsing genuinely repeated listens.
  Transferred playlists or favorites must not automatically become independent
  endorsements across platforms; provenance may need to be marked unknown.
- Normalize only music-relevant fields. Keep raw archives private, outside Git
  and build contexts; do not import unrelated IP/device/payment data into the
  recommendation model or send complete archives to an AI provider.
- Weight explicit preference feedback separately from exposure. Explore recency
  weighting without assuming old favorites are obsolete or every skip is dislike.

Official sources checked for import planning:
- Spotify API alternative researched November 6, 2026: recent plays supports
  `played_at`, before/after cursors and up to 50 records per response, but is not
  a guaranteed lifetime-history backfill. Top artists/tracks provide affinity
  summaries over short/medium/long windows, not individual historical events.
  OAuth refresh tokens support unattended access. New development-mode access
  requires the app owner to maintain Premium; account access is not verified.
  Crucially, the current Developer Policy restricts profiling and AI/ML ingestion
  of Spotify Platform/Content data. Resolve intended permitted use before building
  a Spotify API pipeline for the recommendation engine. Do not assume a personal
  export grants unrestricted reuse or that all API-specific terms apply identically
  to exports. Keep provenance separate. The requested extended export remains
  the preferred historical source; an offline personal-export importer is now
  implemented, while a Spotify API integration is not.
  References: [recent plays](https://developer.spotify.com/documentation/web-api/reference/get-recently-played),
  [top items](https://developer.spotify.com/documentation/web-api/reference/get-users-top-artists-and-tracks),
  [quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes),
  [policy](https://developer.spotify.com/policy).
- [Spotify data contents](https://support.spotify.com/us/article/understanding-your-data/):
  Extended Streaming History documents account-lifetime streams, UTC stream-end
  timestamps, milliseconds listened, track URIs and skip/context fields. Standard
  account-data history covers the past year; request the extended package when
  available. Actual received files still need inspection.
- [TuneMyMusic FAQ](https://www.tunemymusic.com/help): documents library/playlist
  transfers; this does not establish full behavioral-history transfer.
- [Last.fm recent tracks](https://www.last.fm/api/show/user.getRecentTracks):
  supports paginated scrobbles and timestamp ranges; requires an API key but not
  a user-authenticated session for this method. Account API access and the full
  initial download are verified. Exclude the current "now playing" placeholder from
  completed-scrobble imports. [Loved tracks](https://www.last.fm/api/show/user.getLovedTracks)
  provide a separate explicit-preference source; 142 records have been recovered
  for `andyhugelier`. Source date coverage and uncertainty are recorded above.
- [Music library XML export](https://support.apple.com/guide/music/mus27cd5060f/mac):
  export metadata/playlists rather than audio files. Inspect surviving iTunes
  library exports for historical fields; no assumption of complete play events.
- [Shazam saved songs](https://support.apple.com/en-ie/guide/shazam/dev64d8cd51e/web):
  identified songs may sync into a My Shazam Tracks playlist. A privacy export
  route is also documented for supported account setups; determine the user's
  setup before choosing an import method.
- [Pandora privacy FAQ](https://help.pandora.com/s/article/Information-about-Privacy-on-Pandora-1519949298664):
  points eligible users to privacy-request options. Export contents and historical
  coverage, including thumbs and listening events, are not yet verified.
- [Tidal privacy notice](https://tidal.com/privacy): provides routes to request
  personal data through support; event-level export contents are not verified.
  [History Mixes](https://support.tidal.com/hc/en-us/articles/360009257397-Your-History)
  are a potential fallback preference signal, not a full event log.
- [Apple data tools](https://support.apple.com/en-us/102399): offer a copy of
  account-associated data. Music-specific fields/coverage must be confirmed from
  an actual export rather than assumed equivalent to the live API.

## User preferences

- Apple Music is the primary music service.
- The service should run continuously with minimal manual intervention.
- Prefer straightforward, maintainable solutions over unnecessary complexity.
- Keep operating costs low.
- Use VS Code and GitHub for development.
- Avoid using Synology's browser-based terminal for routine work.
- Explain architectural tradeoffs and implement changes incrementally.

## Existing infrastructure

**NAS:** Synology DS220+, DSM 7.4.1, 2 GB RAM.

**Container platform:** Synology Container Manager / Docker Compose.

**Project:** `music-discovery-test`

**NAS project directory:** `/volume1/docker/Music-discovery`

Existing Compose configuration:

```yaml
services:
  music-test:
    image: python:3.12-alpine
    container_name: music-discovery-test
    working_dir: /app
    command: ["sh", "-c", "pip install --no-cache-dir PyJWT cryptography requests && sleep infinity"]
    volumes:
      - ./Secrets:/run/secrets:ro
      - ./Config:/app/config
      - ./Data:/app/data
    mem_limit: 128m
    restart: unless-stopped
```

The container was successfully recreated and started on November 5, 2026.

Verified:
- `/app/data` is mounted and accessible.
- Python packages `PyJWT`, `cryptography`, and `requests` import successfully.
- NAS folder capitalization matters.
- SSH access and Docker inspection were verified by the user. Docker commands require `sudo` for the current SSH account.
- Bind mounts were verified: `Secrets` is read-only; `Config` and `Data` are persistent, writable mounts.
- The container defaults to root; authorization commands run explicitly as UID/GID `1027:100`, matching the private key owner.
- The private key was restricted to mode `600` and remains readable by that runtime user. `Config/private` was created with owner `1027:100` and mode `700`.
- Installed authorization dependencies match the local pins: PyJWT `2.15.1` and cryptography `50.0.2`.

**Important:** This container is currently a test environment, not a functioning listening-history collector.

## Apple Music integration

An Apple Developer Media Services key has been created and its `.p8` private key is stored on the NAS, mounted read-only under `/run/secrets`.

The key belongs to an employer-managed Apple Developer team. The user confirmed on November 5, 2026 that the employer permits the current personal use. A future migration to a personal team is intended; do not block the authorization proof on that migration. Do not copy or expose the private key. Long-term authorization reliability remains to be validated, and migration should be expected to require a new key and user authorization.

A Python script has successfully generated a developer token.

A GitHub Pages site has successfully:
- Authorized the user's personal Apple Music account using MusicKit.
- Retrieved the user's recent plays through the Apple Music API.

Existing GitHub repository:

`https://github.com/andyhugelier/music-discovery-auth`

Existing GitHub Pages site:

`https://andyhugelier.github.io/music-discovery-auth/`

The repository contains an `index.html` authorization/test interface.

### Authorization milestone progress (November 5, 2026)

Verified from the user's terminal output/screenshots:
- Updated local browser authorization succeeded. Setup and sign-in now use separate clicks; the page checks bootstrap-token expiration and shows safe error categories.
- `music_auth.py` was staged at `/app/config/music_auth.py` through the existing bind mount.
- The browser-issued Music User Token was imported through a hidden SSH terminal prompt. The CLI verified Apple access and saved it in `/app/config/private/music-user-token` (owner-only file permissions).
- Import returned 10 recent tracks. A separate `check` command reloaded the saved token, generated a fresh developer JWT, and returned 10 recent tracks without using the browser API.
- The user restarted `music-discovery-test` and repeated `check` successfully: 10 recent tracks returned using the saved token. Authorization persistence across a container restart is verified.
- The Apple `/v1/test` endpoint has a successful empty body; the CLI now handles it without attempting JSON decoding. Regression tests cover this behavior.

Local validation: 15 Python tests and the browser-flow test pass using synthetic credentials and mocked services.

Still unverified at the authorization milestone: recovery after container recreation, NAS reboot, extended unattended operation, and reauthorization after token invalidation. Subsequent collector progress below records persistent runtime configuration and cross-container database verification. The user authorized committing and pushing the authorization milestone on November 5, 2026.

## Current limitations

- NAS authorization and saved collection history survive container recreation, with subsequent scheduled polling verified. Long-term unattended operation and NAS reboot recovery remain to be tested.
- The initial manual handoff and owner-restricted persistence are implemented; automatic reauthorization is not implemented.
- The existing test container installs dependencies at startup; the deployed collector image installs pinned dependencies at build time.
- A live SQLite database now exists on the NAS and has been verified across separate one-off containers.
- Automatic polling is deployed on the NAS, with scheduled successes and healthy status verified. Playlist generation is not implemented.
- Apple Music's recent-plays API may provide incomplete historical coverage and may not provide reliable play timestamps, skip events, or full listening durations. Do not invent these fields.

## Recommended architecture

1. Browser-based Apple Music authorization.
2. Secure transfer of the Music User Token to the NAS.
3. Python collector running on the NAS.
4. SQLite database stored in `/app/data`.
5. Periodic polling of recent plays with deduplication.
6. Recommendation engine combining collected listening data, Apple Music catalog information, and AI.
7. Apple Music playlist creation.
8. Feedback and recommendation-quality improvements.

The architecture is provisional and should be validated before implementation.

## Security requirements

- Never commit private keys, developer tokens, Music User Tokens, or other credentials to GitHub.
- Never display secrets in logs or assistant responses.
- Keep credentials outside public web assets.
- Use appropriate file permissions and avoid exposing the NAS unnecessarily.
- Use GitHub Pages only for browser-side functionality that can safely be public.
- Do not put a private key in browser JavaScript.

## Collector implementation progress (November 5, 2026)

The user requested work on the always-on collector. Local implementation now includes:
- `music_config.py` plus `music_auth.py configure`: persistent nonsecret IDs/paths at `/app/config/private/apple-music.json`, retaining environment compatibility.
- `collector.py`: latest-ten polling every five minutes, ordered observation snapshots with storage deduplication, transactional SQLite persistence in `/app/data`, a process lock, scheduled retries with persisted next-attempt times, safe logs and graceful shutdown.
- Snapshots are not individual play events. Exact play counts and playback times are not inferred. This first version does not paginate or backfill.
- Local `status`, Docker `healthcheck`, and consistent standalone SQLite `backup` commands.
- `Dockerfile`, `.dockerignore`, and `compose.collector.yaml`: a separate nonroot `music-discovery-collector` service using the existing mounts, with no published ports. The existing test service is retained.
- `COLLECTOR.md`: concrete NAS build, one-poll validation, continuous startup, monitoring, backup/restore and rollback procedures.

Docker Desktop was started for local validation: the Linux/amd64 image builds and a synthetic offline smoke test passes under a read-only root filesystem and 128 MB limit (about 58 MB peak process RSS in the short test). Package versions and the tested base-image digest are pinned. All 36 Python tests, the browser-flow test and Compose configuration validation pass. The user authorized committing and pushing this collector milestone after the successful NAS recreation check.

The user approved NAS staging/build and a single live poll. Verified from the user's terminal output:
- Source files were staged and the image built successfully on the NAS.
- Nonsecret configuration was saved at `/app/config/private/apple-music.json`; a fresh helper container fetched ten recent tracks without shell Apple variables.
- `Data` was owned by UID `1027` but had mode `555`. Changing only that directory to mode `700` fixed the write failure; ownership and existing file permissions were retained.
- A single live collector poll saved a ten-track snapshot. A separate temporary container reported `healthy: true`, `successful_polls: 1`, `failed_polls: 0`, and `unique_snapshots: 1`, verifying cross-container persistence of the database.
- The user subsequently approved continuous operation and started `music-discovery-collector`. A later status check reported `healthy: true`, `successful_polls: 5`, `failed_polls: 0`, and `unique_snapshots: 1`, verifying scheduled polling with unchanged responses reusing stored snapshots.
- The user force-recreated only the collector service and checked again after scheduled polling resumed: `healthy: true`, `successful_polls: 9`, `failed_polls: 0`, `unique_snapshots: 1`. Configuration, authorization and database history survived recreation. Initial deployment checks are complete.
- Multi-day resource usage, NAS reboot recovery, live backup/restore and reauthorization after invalidation are not yet verified. The service runs detached and does not depend on the user's SSH session or sudo password cache.

User communication preference: explicitly label any required user action **Your next step** and name the terminal. Otherwise state when no action is needed. Do not imply background work continues after a response ends. Avoid mentioning the NAS repeatedly in routine output; name it only when relevant to an action or material limitation.

## Immediate next steps

1. Follow `RECOMMENDATION_DESIGN.md`: implement reproducible, context-filtered
   derived profiles, measured metadata/similarity enrichment and reviewable
   overlapping interests with a simple polished UI. Then add controllable station
   previews, contextual feedback and catalog-matched batches before playlist
   delivery. Exclude suspicious dates from temporal weighting.
   Incorporate the imported Spotify history and reconcile provenance/coverage
   across sources. The recommendation engine and editable
   taste-profile interface are not implemented yet.
2. Keep the NAS collector running and observe multi-day resource usage and
   freshness. Clean up bootstrap credential copies and validate backup/restore
   during the trial; add notifications and scheduled backups in a later iteration.
3. Future changes require authorization before pushing. Completed checkpoints:
   authorization `49ecdd0`, deployed collector `b9a635a`.

The user approved implementation of the collector after the completed authorization milestone. Ask before external deployments or production infrastructure changes. Do not automatically push changes or deploy the collector.
