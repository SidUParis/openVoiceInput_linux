# Personal ASR data collection and training plan

## Status and purpose

The first collection layer is implemented; model work is not. Collection is
disabled by default and remains separate from adaptive correction. When a user
explicitly enables it and chooses an existing absolute local or mounted folder,
the daemon can retain each accepted utterance as a versioned WAV/JSON record
under `openvoiceinput-dataset-v1`.

This layer does not review labels, authenticate to or mount Orange, provide
resumable transfer, upload to Google Drive, train, fine-tune, or distil a model.
A compatible remote filesystem mounted separately by the user can still be the
selected path. In particular, an automatically paired provider result and
recording are not a gold label or a distillation-ready example.

The eventual goal is a personal Chinese/English/French and code-switching ASR
evaluation/training corpus for domain names, server names, acronyms, accents,
and the user's ordinary acoustic environment. The corpus should improve
faithful recognition without teaching an ASR model to confuse what was spoken
with how the user prefers text to be formatted.

## Labels that must remain separate

Each explicitly retained utterance has three distinct text roles:

- `provider_final`: the cloud provider's authoritative result, stored with
  `teacher-unreviewed` status. This is a useful pseudo-label and baseline, not
  unquestioned ground truth.
- `spoken_verbatim`: currently `null`/`unreviewed`; a later review workflow may
  fill it with what the speaker actually said, preserving language switches and
  spoken words.
- `preferred_output`: currently `null`/`unreviewed`; a later review workflow
  may fill it with the text the user ultimately wants inserted, including
  spelling, capitalization, punctuation, normalization, or stylistic
  preferences that were not literally spoken.

For example, a formatting preference belongs in `preferred_output`; it must not
silently alter `spoken_verbatim`. This separation permits ASR acoustic/language
adaptation to use the faithful label while a later correction or formatting
layer can learn the preferred output.

Schema v3 also stores a separate `delivery` object. It is not a human label:
its `text` is exactly what was inserted, with
`machine-derived-unreviewed` status. Faithful mode records an identity
processor. Clean mode records the bounded local processor name/version,
content-free outcome, and every deletion's original-codepoint offsets, kind,
reason, source and empty replacement. Replaying those edits against raw
`provider_final` must reproduce `delivery.text`. This audit must never be used
as `spoken_verbatim` merely because it looks cleaner.

## Implemented opt-in record (schema v3)

The GTK settings window keeps collection off by default. Enabling requires an
existing absolute local or mounted folder and initializes or reopens a marked
`openvoiceinput-dataset-v1`. The daemon reloads the setting before each
dictation, so saving enable/disable/path changes takes effect without a service
restart.

A record is offered only after the focused IBus client accepts a nonempty
authoritative provider final. Cancelled, failed, final-rejected, empty-audio,
and no-final sessions publish nothing. One random utterance directory contains:

- `audio.wav`, preserving the exact captured 16 kHz, mono, signed 16-bit PCM;
- `record.json`, with schema/dataset/utterance/session IDs, UTC time, explicit
  opt-in consent, audio format/frame count and PCM/file SHA-256 values;
- Volcengine provider/model/resource identity;
- privacy-preserving microphone provenance: the selected category and why it
  won, DJI link state at selection, plus asynchronously observed actual Pulse
  source-output route categories (including a mid-recording route change);
- the three text roles and review states described above.
- the actual faithful/clean delivery and its replayable local transformation
  audit, still explicitly machine-derived and unreviewed.

After final acceptance, the background writer also computes bounded numeric PCM
diagnostics under `audio.quality`: overall and first-second clipped-sample
fractions (absolute sample threshold 32760), RMS/peak dBFS, normalized DC
offset, zero fraction, and sample count. This is post-hoc metadata for later
filtering; it never runs in the audio callback or startup path, rejects no
record, delays no dictation result, and does not modify a PCM sample. Quality
thresholds for eventual training remain a later review decision.

The immutable utterance directory remains the established two-file
`audio.wav` + `record.json` contract. After that pair is published, a separate
dataset-level schema-v2 `usage/<utterance_id>.json` index stores only the
utterance ID, timestamp, audio duration, and non-whitespace character count of
the text actually delivered. It declares `character_count_basis` as
`delivered-text`; existing schema-v1 summaries retain their older raw-provider
count meaning. Existing training
tools therefore do not see a third file inside an utterance record.

The active recorder stores at most the 600-second product limit in bounded
memory. A bounded background queue performs WAV encoding, hashing, sync, and
publication. A record is first completed below `.pending` and atomically
renamed into `utterances/<utterance_id>`; writer failure is optional and cannot
block final text or ordinary dictation.

This is best-effort direct-to-selected-folder storage with no fallback spool.
Normal service shutdown gives the writer 10 seconds to drain inside systemd's
30-second total stop budget. A stalled/unmounted destination can leave or
remove hidden staging and lose the unpublished record; already published
records remain.

The audio and manifest stay outside the public Git repository. Keys, tokens,
desktop text surrounding the utterance, clipboard contents, global keyboard
events, raw Pulse source names, USB serials, Bluetooth addresses, custom
hardware labels, and Rime data are never dataset fields. Microphone
fingerprints are hashes of an allowlisted category/model description only;
identical models may intentionally share one fingerprint. The five-second
adaptive ledger is not itself an audio dataset and must not be reinterpreted as
one.

### Schema-v1/v2 migration policy

Existing schema-v1 `record.json` files remain immutable and valid. The dataset
marker and directory name remain version 1, so a dataset may contain both old
v1, v2 and new v3 utterances without moving or rewriting audio. Schema v2 added
the optional top-level `microphone` object and numeric `audio.quality` summary.
Schema v3 adds required `delivery` while keeping raw `labels.provider_final`
and both null human-review labels unchanged. A strict older reader should skip
records whose `schema_version` it does not support. A migrated reader may
accept 1/2/3, treating missing v2 metadata as “not observed” and missing
delivery as “not recorded”; it must never synthesize old delivery, microphone
provenance, or quality from filenames or current desktop state.

`microphone.actual.status` is `unknown` until the read-only observer has matched
the daemon's live Pulse source-output; the selected source is never substituted
as an alleged actual route. Observation is front-loaded at stream open and then
backs off to a five-second interval. Source details are fetched only when the
source-output index changes, and observation stops at the bounded transition
limit. None of this gates startup, drops audio, or adds a quality delay.

The GTK dashboard reads only the dataset marker and bounded
`usage/<utterance_id>.json`
summaries on a background thread. It does not open `record.json`, audio, or
display recent transcript text. Records created before the summary was added
remain valid training candidates but are reported as unindexed rather than
silently inspecting their labels. With collection disabled, the dashboard does
not scan a previously selected folder; a disconnected mount is reported as
unavailable rather than as zero records.

Saving disable or a new destination shares the publication lock with the
background writer. Once that settings save returns, older queued or staged,
unpublished records cannot become visible. Already published records remain;
the application currently provides no review/delete UI. Uninstall preserves
both `data-collection.json` and datasets below user-selected folders.

## Orange storage target

For the intended personal deployment, a user-controlled computer nicknamed
**Orange** can be the storage destination. Both endpoints are treated as trusted
local machines for this prototype. The implemented collector does not add
application-level static encryption; the selected local or mounted filesystem
determines effective visibility, sharing, backup, and at-rest protection. The
user can already mount an Orange directory with SSHFS and select that local
mount path. The application does not establish or authenticate that connection,
and repository code/documentation must never contain Orange credentials.

Direct mounted writes have no fallback spool, so a disconnect can lose an
unpublished record without blocking ordinary dictation. Google Drive should be
an asynchronous rclone backup of complete local/Orange records, not the live
collection filesystem. SSHFS setup, remote-side permission verification,
disconnect semantics, Google OAuth, and backup commands are in
[remote-dataset-storage.md](remote-dataset-storage.md).

First-party transfer remains deferred. A future design can spool complete local
records and move them to Orange through an existing authenticated SSH path,
with atomic completion markers so a partial audio/manifest pair is never
presented as a usable training sample. Failure should retain a visible local
pending item rather than drop valuable data silently.

## Work list before training

1. **Implemented:** versioned, disabled-by-default WAV/JSON collection with a
   transcript-free usage summary and private aggregate dashboard, plus the
   three separate human label roles, raw-versus-delivered audit, exact audio
   hashes, atomic publication, hot
   enable/disable/path reload, and no change to the provider stream.
2. Add an explicit review/delete/keep interface and publish a standalone
   schema validator before declaring long-term corpus compatibility.
3. Implement first-party resumable Orange transfer and verify record counts,
   hashes, partial-transfer recovery, and delete behavior on both owned
   machines.
4. Build a lightweight review queue for `spoken_verbatim`; do not treat either
   cloud output or a quick preferred edit as a literal speech label. Review
   `preferred_output` independently.
5. Add language/code-switch annotations only with a documented purpose and
   bounded schema; do not infer them from unrelated desktop context.
6. Create train/development/test splits by session or day so near-duplicate
   utterances do not leak across evaluation boundaries.
7. Establish baselines for Mandarin, English, French, and code-switch segments,
   including domain-term and named-entity error rates.
8. Compare non-training baselines first: vocabulary/correction memory, decoding
   bias, and a small preferred-output layer.
9. Only then select a reproducible local base ASR model and test a small
   speaker/domain adapter or parameter-efficient fine-tune. Keep an untouched
   multilingual test split and compare against the cloud baseline.

Training is deliberately postponed. The amount of useful data depends more on
label accuracy, language coverage, acoustic diversity, and evaluation hygiene
than on a guessed minimum number of utterances. A model should not be released
or made the default merely because its training loss improves.

## Relationship to adaptive correction

The implemented adaptive feature is a low-cost feedback loop for the next
provider request: one strict same-focus replacement becomes a bounded
wrong-to-canonical hint. It is intentionally not called self-training or an
autoregressive model. It can improve daily use while the separate opted-in
collector accumulates review candidates. Adaptive entries are not labels, and
neither feature makes the current corpus training-ready; label review and
eventual training remain independent and auditable.

Clean terminal delivery is a third, separate layer. If it changes a provider
final, automatic surrounding-text learning is consumed without extraction so
the machine deletion cannot masquerade as an ASR correction. The explicit
review tool always uses raw `provider_final` against human-entered
`spoken_verbatim`; its read-only delivered text is context only. A later review
workflow may decide whether that delivery belongs in `preferred_output`, but
the current writer leaves both human labels null.
