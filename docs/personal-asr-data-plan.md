# Personal ASR data collection and training plan

## Status and purpose

The first collection layer is implemented; model work is not. Collection is
disabled by default and remains separate from adaptive correction. When a user
explicitly enables it and chooses an existing absolute local or mounted folder,
the daemon can retain each accepted utterance as a versioned WAV/JSON pair
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

## Implemented opt-in record (schema v1)

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
- the three text roles and review states described above.

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
events, and Rime data are never dataset fields. The five-second adaptive
ledger is not itself an audio dataset and must not be reinterpreted as one.

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

1. **Implemented:** versioned, disabled-by-default WAV/JSON collection with the
   three separate text roles, exact audio hashes, atomic publication, hot
   enable/disable/path reload, and no change to the provider stream.
2. Stabilize schema-v1 migration/validation policy before declaring long-term
   corpus compatibility; add an explicit review/delete/keep interface.
3. Implement first-party resumable Orange transfer and verify record counts,
   hashes, partial-transfer recovery, and delete behavior on both owned
   machines.
4. Build a lightweight review queue for `spoken_verbatim`; do not treat either
   cloud output or a quick preferred edit as a literal speech label. Review
   `preferred_output` independently.
5. Add language/code-switch and microphone/session annotations only with a
   documented purpose and bounded schema; do not infer them from unrelated
   desktop context.
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
