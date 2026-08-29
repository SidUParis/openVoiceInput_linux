# Future personal ASR data plan

## Status and purpose

This is a plan, not an implemented feature. The current adaptive-correction
work stores neither microphone recordings nor transcript records and does not
train, fine-tune, or distil a model.

The eventual goal is a personal Chinese/English/French and code-switching ASR
evaluation/training corpus for domain names, server names, acronyms, accents,
and the user's ordinary acoustic environment. The corpus should improve
faithful recognition without teaching an ASR model to confuse what was spoken
with how the user prefers text to be formatted.

## Labels that must remain separate

Each explicitly retained utterance should have three distinct text roles:

- `provider_final`: the cloud provider's authoritative result. This is a useful
  pseudo-label and baseline, not unquestioned ground truth.
- `spoken_verbatim`: a reviewed label for what the speaker actually said,
  preserving language switches and spoken words.
- `preferred_output`: the text the user ultimately wants inserted, which may
  include spelling, capitalization, punctuation, normalization, or stylistic
  preferences that were not literally spoken.

For example, a formatting preference belongs in `preferred_output`; it must not
silently alter `spoken_verbatim`. This separation permits ASR acoustic/language
adaptation to use the faithful label while a later correction or formatting
layer can learn the preferred output.

## Proposed opt-in record

Collection must be disabled by default and enabled through an explicit user
choice. One record should use a random utterance ID to associate:

- one bounded audio file captured for that utterance;
- `provider_final`, plus provider/model identity needed to interpret it;
- independently reviewable `spoken_verbatim` and `preferred_output` fields;
- coarse language/code-switch tags and optional microphone/session metadata;
- label status such as `unreviewed`, `verbatim-reviewed`, or
  `preferred-reviewed`;
- an explicit keep/delete decision.

The audio and manifest stay outside the public Git repository. Keys, tokens,
desktop text surrounding the utterance, clipboard contents, global keyboard
events, and Rime data are never dataset fields. The five-second adaptive
ledger is not itself an audio dataset and must not be reinterpreted as one.

## Orange storage target

For the intended personal deployment, a user-controlled computer nicknamed
**Orange** can be the storage destination. Both endpoints are treated as trusted
local machines for this prototype, so adding application-level static
encryption is not a prerequisite. The collector still needs explicit opt-in,
predictable paths, ownership/permission checks, an observable transfer status,
and a direct way to stop collection or delete an utterance. Repository code and
documentation must never contain Orange credentials.

Transfer implementation is deferred. A future design can spool complete local
records and move them to Orange through the user's existing authenticated SSH
path, with atomic completion markers so a partial audio/manifest pair is never
presented as a usable training sample. Failure should retain a visible local
pending item rather than drop valuable data silently.

## Work list before training

1. Freeze and version the record schema, including the three separate text
   roles and consent state.
2. Implement disabled-by-default recording retention and per-utterance
   keep/delete controls without changing the current provider stream.
3. Implement resumable Orange transfer and verify record counts, hashes, and
   delete behavior on both owned machines.
4. Build a lightweight review queue for `spoken_verbatim`; do not treat either
   cloud output or a quick preferred edit as a literal speech label.
5. Create train/development/test splits by session or day so near-duplicate
   utterances do not leak across evaluation boundaries.
6. Establish baselines for Mandarin, English, French, and code-switch segments,
   including domain-term and named-entity error rates.
7. Compare non-training baselines first: vocabulary/correction memory, decoding
   bias, and a small preferred-output layer.
8. Only then select a reproducible local base ASR model and test a small
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
autoregressive model. It can improve daily use while the opt-in corpus design,
label review, and eventual training work remain independent and auditable.
