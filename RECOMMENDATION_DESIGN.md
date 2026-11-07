# Controllable music discovery: research and implementation direction

Research date: November 6, 2026. Status: proposed architecture and evaluation
plan, not an implemented or benchmarked recommendation engine. This work adds
documentation only; no dependencies, deployment or playlist writes.

## Product model

Represent a person's taste as overlapping interests, not one average vector or
one genre per track. A station is a saved recipe selecting interests, optional
moods, familiarity mix, exploration distance and repetition limits. Mood cuts
across genre: melancholy electronic and melancholy acoustic music can coexist.
The user can rename, merge, split or correct suggested interests.

Keep two independent discovery controls, with presets hiding advanced settings:

- Familiarity: favorites/rediscovery versus tracks without known listening evidence.
- Exploration: stay close to the station versus include related musical territory.

An unfamiliar track can be very close to a station's sound. A familiar track can
bridge two interests. Absence from incomplete history is not proof of novelty;
library membership alone is not proof of familiarity or listening.

Context exclusions, track dislikes, station mismatch and temporary fatigue are
different signals. Preserve source evidence; apply versioned rules when deriving
profiles and composing stations. Feedback must be undoable and have explicit
scope: this station or the overall profile. Snoozing a track is not a permanent
dislike of its genre or artist.

## Options researched

| Approach | Useful contribution | Limitation here | Proposed role |
| --- | --- | --- | --- |
| Tags, artist/track similarity and an interpretable score | Immediate explanations and station controls | Sparse/noisy track tags; artist similarity misses differences within an artist | First baseline and candidate providers |
| Collaborative filtering, e.g. ALS/BPR | Community co-listening relationships | Our single-user collection cannot supply population structure | Reuse external relationships; do not train a population model locally |
| Pretrained audio embeddings | Sonic similarity, potentially mood and energy | Needs accessible audio or a joinable precomputed catalog; model/license/coverage matter | Optional enrichment experiment |
| Hybrid pipeline | Combines evidence, several interests, constraints and feedback | Requires measuring provider coverage and maintaining identities | Recommended architecture |
| An LLM as the sole recommender | Convenient natural-language input | Unsupported song facts and unstable choices are difficult to audit | Optional recipe parsing and naming, grounded in retrieved records |

[Last.fm track similarity](https://www.last.fm/api/show/track.getSimilar) is based
on listening data; its [track tags](https://www.last.fm/api/show/track.getTopTags)
and [artist similarity](https://www.last.fm/api/show/artist.getSimilar) provide
additional candidate/feature sources. Documented support is not a measurement of
coverage for our music. Fetch bounded seed sets and retain provenance, timestamps
and empty/failed responses. Respect the service's
[terms and attribution requirements](https://www.last.fm/api/tos).

[Troi](https://github.com/metabrainz/troi-recommendation-playground) is the closest
existing playlisting reference: it combines several sources and exposes
MusicBrainz-based artist/recording relationships. Its
[LB Radio modes](https://troi.readthedocs.io/en/stable/lb_radio.html) mix weighted
artist/tag sources and change how far down ranked source lists they look.
Evaluate its output as a baseline or provider rather than adopt its whole engine
before testing catalog coverage and controls. ListenBrainz's
[personal collaborative recommendations](https://listenbrainz.readthedocs.io/en/latest/users/api/recommendation.html)
are a separate account-dependent, experimental feature. Do not upload the user's
history or create a new account as a prerequisite for the first station.

[Implicit](https://github.com/benfred/implicit) supplies reusable ALS, BPR and
item-neighbor implementations, but the library does not supply an immediately
usable, identified music catalog. Training it on one user does not create the
missing community evidence.

[AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) is a useful comparison
for audio clustering and playlists, built for accessible music libraries. Its
documented minimum is 8 GB RAM; adopting its entire stack is a poor initial fit
for the existing 2 GB host and streaming-metadata workflow.

## Relationships, embeddings and vector storage

A vector index stores and searches representations; it does not provide the
music catalog, audio, identities or meaningful representations automatically.
The first reusable relationship layer should combine existing similarity edges
with tags and recording metadata. Cache a bounded neighborhood of useful seeds
rather than download a whole world catalog.

[MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) provides identities,
recording metadata and ISRC lookup. Store internal artist/recording IDs with
separate provider mappings. Corroborate ISRC matches with version, duration and
artist/title metadata; never silently merge live, remix and studio recordings.
Keep unresolved matches available for review rather than discard them.

[Essentia models](https://essentia.upf.edu/models.html) include pretrained music
embeddings and mood/arousal-valence models. MTG models have noncommercial
share-alike terms, with other licensing available. [CLAP](https://github.com/LAION-AI/CLAP)
supports audio/text representations. Both require audio input for audio-derived
track representations; embedding a title is not equivalent to analyzing the song.
The current Apple export contains metadata, not usable audio. Do not design around
extracting protected streams or assume preview analysis is available/permitted.
Any audio experiment needs accessible files or an appropriately licensed,
identified feature source, and should run as an optional batch job.

The user confirmed a collection of audio files on the storage drive, mostly from
the Last.fm era, and describes it as poorly aligned with current taste but useful
for a baseline. The supplied folder is `/volume1/music`. The user verified a
read-only inventory as runtime UID 1027:100: root readable, 5,000 entries checked,
zero directory errors, and 1,244 `.mp3`, 2,367 `.m4a`, 41 `.m4p` file candidates.
The scan hit its entry cap; these are neither whole-library totals nor a random
sample. Decoding, actual codecs, metadata quality and durations are unverified.
Exclude potentially protected M4P from the first experiment; verify MP3/M4A
decoding before selecting features. No audio has been copied or analyzed yet.
Use a small representative read-only sample
to evaluate feature extraction and whether neighbors sound related. Do not treat
file ownership or inclusion in this sample as current positive preference, and
do not train a current taste model from this collection by default. A successful
historical-audio test does not establish feature coverage or recommendation quality
for current streaming releases. Benchmark batch analysis separately from the live
collector; a full collection copy or scan is not required for the first test.

Existing feature collections need a catalog-join test. For example,
[AcousticBrainz](https://acousticbrainz.org/download) is discontinued and its
archives reflect historical submissions, so new releases are a coverage problem.
The [Deezer research dataset](https://github.com/deezer/recsys25-reacta) supplies
precomputed vectors but anonymized song identities; it is not a direct lookup
table for our Apple songs. Do not treat research benchmark size as usable coverage.

Keep SQLite as the source of truth. Begin with sparse features and cached top
neighbors; benchmark exact similarity over bounded candidate sets. If dense
vectors justify an index, evaluate [sqlite-vec](https://github.com/asg017/sqlite-vec)
(small embedded extension, currently pre-v1) or [FAISS](https://github.com/facebookresearch/faiss).
Neither requires starting with a separate vector database service. Pin model
versions, dimensions and index artifacts; never compare vectors from incompatible
embedding spaces. Profile actual memory/latency before choosing deployment.

## Proposed taste and station pipeline

1. Normalize source evidence and enforce context rules. Preserve raw snapshots,
   source semantics, provenance and match confidence. Derived profile views must
   actually omit excluded records, not merely show filtered counters beside raw
   positive lists. The earlier rain-sounds correction illustrates this requirement.
2. Enrich an informative subset: explicit preferences, meaningful historical
   artists/tracks and recent aggregate seeds, plus their candidate neighborhoods.
   Do not let the size of a saved discography dominate preference weight.
3. Compare simple tag/similarity baselines with overlapping interests derived
   using [NMF](https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.NMF.html)
   over nonnegative tag/features. Each song can have multiple interest weights.
   Compare with graph communities if relationship coverage is stronger than tags.
   Select the representation by coherence and user corrections, not a pretty map.
4. Generate candidates independently from selected interests, track/artist
   neighbors, underplayed known music, and explicitly labeled external suggestions.
   Match real recordings to the target service and storefront before export.
5. Rank using preference evidence, selected-interest fit, mood evidence when
   available, and feature/match confidence. Normalize incomparable provider scores
   before combining; missing features are unknown, not negative feedback.
6. Compose the playlist: satisfy familiarity targets, cooldowns and exclusions;
   cap artist/album repetition; balance selected interests; reduce redundant picks.
   An MMR-style diversity reranker is an appropriate small first experiment,
   adapting [the original relevance/diversity method](https://www.cs.cmu.edu/afs/cs/Web/People/jgc/publication/MMR_DiversityBased_Reranking_SIGIR_1998.pdf).
   Ordering is separate from selection: smooth or eclectic transitions should be
   chosen intentionally. Report shortfalls instead of filling with poor matches.
7. Save the recipe, candidate provenance, score contributions, random seed,
   constraints, selected order and feedback so results can be compared and replayed.

Initial scores are transparent heuristics, not calibrated probabilities of liking.
There is no defensible fixed optimal weight schedule yet. Saturate repeat counts,
keep historical and current evidence separately inspectable, and avoid aggressive
age decay that erases durable interests. Do not infer exact recent play counts
from repeated Apple polling snapshots.

Mood needs separate treatment from genre. Start with user seed songs, editable
track tags and confidence-aware tag sources. A happy lyric, major key or high
energy alone does not establish perceived happiness. Model energy and emotional
tone separately; retain uncertainty and allow user overrides. Artist-level mood
must not silently become a fact about every recording.

Adaptive exploration such as a
[contextual bandit](https://arxiv.org/abs/1003.0146) is a later option after recording
recommendation exposures and sufficient contextual feedback. It must optimize
user-valued discovery and fit, not engagement alone. A skipped track is not an
automatic dislike. A [2026 audio-model comparison](https://arxiv.org/abs/2604.23077)
also cautions that strong tagging performance does not guarantee recommendation
quality; benchmark representations before choosing a model.

## Interface and platform boundaries

Build a small polished web interface alongside the first usable engine:

- Taste: named interest cards with representative tracks, evidence, exclusions
  and editing. A constellation view is optional later, not the only navigation.
- Stations: interest selection/blending, mood/energy, familiarity preset and an
  advanced exploration control. Preview the resulting mix before saving.
- Recommendations: playable service links, concise evidence-backed reasons and
  Love / Not for this station / Already know / Snooze / Exclude controls.

Use clear loading/empty/error states, keyboard-accessible controls and undo.
The backend owns provider-neutral recipes and feedback; the UI does not expose
algorithm weights by default. Distinguish observed taste from proposed labels.

Apple is the first catalog/playback/playlist adapter. Keep authorization in
provider adapters, not in taste or ranking code. Feedback in our interface can
adjust future batches; do not imply it changes Apple Music's own recommendation
model. Playlist export produces an ordered snapshot, not a live radio engine
with custom controls inside Apple's app. Continuous replenishment or an owned
player would be a later product capability. Native-player skips cannot be assumed
available from the existing history collector.

## Next three implementation milestones

1. **Reproducible taste groups and a review UI.** Replace ad hoc analysis scripts
   with an idempotent private derived store; implement context/identity rules;
   measure enrichment coverage on roughly 50–100 representative seeds. Compare
   baseline tags/similarity with NMF/graph groups and let the user edit examples.
   Ship an accessible list/card UI before any decorative map. Done when exclusions
   survive regeneration, ambiguous recordings are visible, and groups can be
   inspected/corrected without editing JSON. Mood labels remain provisional.
2. **Controllable station previews and first discovery batches.** Generate several
   comparable mixes from the same seeds: familiar, nearby discovery and broader
   exploration, plus a user-seeded mood mix. Save reasons, recipe and constraints;
   add feedback and snooze immediately. Done when the user can judge real matched
   tracks, controls have measurable effects, Queen's keep exception and context
   exclusions hold, and insufficient supply is shown honestly. Actual personal
   examples belong only in private fixtures/data, not public documentation/tests.
3. **Playlist delivery and measured refinement.** Add an authorized Apple export
   adapter with explicit user feedback feeding later batches; measure multi-day
   freshness, cooldown behavior and user-rated fit/discovery. Add a second provider
   only when needed. Improve mood features and consider adaptive ranking only if
   comparison results justify their complexity.

Before selecting a dependency stack, compare candidate yield, recording-match
coverage, interest coherence, user-rated station/mood fit, novelty at each setting,
artist diversity, repeats, exclusion violations, memory and generation latency.
Use a valid-date historical holdout only as a secondary diagnostic; old listening
alone cannot validate current preferences. Have the user compare small blinded
batches when possible. Treat confidence/unknowns and exploration tradeoffs as
visible product behavior, not just offline metrics.

Only the bounded format/access inventory above has run. No full-audio inventory,
decoding/feature-extraction test, live similarity-provider pilot, algorithm benchmark or
recommendation UI has been completed as part of this research. These are explicit
work items, not assumed capabilities. The first Spotify extended-history delivery
has since been imported privately (see `SPOTIFY.md`); cross-source reconciliation
and feature enrichment remain work for the implementation milestone. Always
recheck source-specific permitted uses before designing around a provider's data.
