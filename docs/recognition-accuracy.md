# Recognition accuracy and correction loop

Open Voice Input Linux treats a live ASR hypothesis as a draft. It can be wrong
and may be replaced several times before the provider emits the authoritative
two-pass result. Only that final result is eligible for terminal delivery; the
user can choose the raw final or conservative local deletion-only cleanup.

## Accuracy layers

### 1. Two-pass recognition by default

The Volcengine `bigmodel_async` endpoint is used with:

- `enable_nonstream: true` for the more accurate sentence-level second pass;
- `enable_ddc: true` for disfluency removal and semantic smoothing;
- `enable_itn: true` for written-form numbers and units;
- `enable_punc: true` for punctuation and sentence structure.

This layer fixes many errors without another model and must run before any
local correction rule is considered. The live preedit is intentionally
replaceable; the final text is committed once.

For Volcengine responses, `result.text` remains the compatibility fallback.
When the documented `result.utterances` structure is present, the client uses
the millisecond `start_time`/`end_time` fields to retain completed sentences
across response frames and lets `definite: true` two-pass text replace the
streaming hypothesis for the same sentence. Repeated full-result frames are
deduplicated by their provider time slot. If a later two-pass response changes
sentence boundaries, its definite time interval replaces every overlapping
older interval; an incremental later sentence cannot discard a separate,
earlier completed sentence. A connection-level terminal frame may contain no
new text, so it finishes the retained assembly instead of replacing it with an
empty value. That retained assembly includes the last nondefinite trailing
sentence when no newer two-pass tail was provided, avoiding silent loss of
already displayed speech. The client never guesses an overlap from transcript
content when the documented timing fields are absent. Incremental assembly is
bounded to the same 4,096-codepoint/16-KiB limit as the focused preedit; an
oversized provider transcript fails the utterance instead of growing retained
sentence state across frames.

Utterance parsing is all-or-nothing for each provider frame. If any member of
an advertised utterance list lacks its documented text, Boolean `definite`, or
integer time interval, none of that frame's intervals enter the retained
assembly. In validated `result_type: full` mode, a non-empty cumulative
`result.text` is delivered as the compatibility fallback for that frame; if it
is empty, the last safe assembly remains. In validated `result_type: single`
mode, that field contains only the current sentence and cannot be safely joined
without its time interval, so a malformed frame raises a content-free protocol
error and never reaches the connection-finish callback. Later fully valid
frames can still build on the definite intervals retained before a malformed
frame. Any result type other than `full` or `single` is rejected when the
client is initialized.

### 2. Faithful or clean terminal delivery

The private `output-style.json` has two modes and is frozen when each utterance
starts. A missing file means `faithful`, so an upgrade never silently changes
output. Saving during a recording affects only the next utterance.

- `faithful` commits the raw authoritative provider final unchanged.
- `clean` keeps every live partial raw, then runs a bounded deterministic local
  cleaner only after the provider terminal event. It deletes only standalone
  high-confidence hesitations and adjacent exact/self-restart fragments. It
  does not call an LLM, make an extra network request, insert words, change a
  term, number or letter case, or globally normalize punctuation. A separator
  attached to a removed filler can be deleted with that filler.

The cleaner accepts at most 4,096 codepoints and 64 deletion operations. An
exception, oversized input, excessive edit count, non-replayable result, or a
result that would remove all lexical content falls back to the raw provider
final. Cleanup therefore never turns a valid final into an input failure. Each
successful deletion retains original-codepoint offsets, source text, kind,
reason and empty replacement so an opted-in schema-v4 record can replay the
exact delivered text from `provider_final`.

When clean delivery differs from the provider final, the daemon immediately
consumes the IBus observation lease and records the content-free reason
`postprocessed-output-not-safe-for-asr-learning`. It does not run adaptive
extraction for that utterance: edits to machine-cleaned text are not safe
evidence about the raw ASR span. Explicit review still uses raw
`provider_final` as the only correction source; delivered text is read-only
context. If cleanup is unchanged or falls back to raw, normal observation is
preserved.

### 3. Explicit personal vocabulary

The standalone daemon implements an optional, explicit personal vocabulary for
names, project terms, acronyms, place names, and specialised vocabulary. It is
stored separately from the key-only provider configuration as a permission-0600
JSON file below a permission-0700 directory. Users replace it either through a
TTY, one term per line, or from a user-owned permission-0600 UTF-8 file. Terms
never appear in command arguments or logs.

The local boundary is deliberately conservative:

- at most 200 terms and at most 64 Unicode characters per trimmed term;
- NUL, CR, LF, non-strings, unsafe files, and unexpected JSON fields rejected;
- stable `casefold` deduplication that keeps the first entered spelling;
- an absent or empty file disables the feature without changing default ASR;
- the file is reloaded before each new dictation, so an idle daemon does not
  need to be restarted after a change.

For a non-empty list, every ASR request adds `request.context` as the compact
JSON string documented by Volcengine, containing `hotwords` objects with one
`word` each. Empty lists omit `context` completely. No weight, automatic
ranking, or managed table is included in this first local implementation. The
bounded GTK4 settings window edits the same explicit list one term per line.

Volcengine separately documents request-level hotwords and managed hotword
tables. Its managed tables support up to 5,000 terms, a per-term weight from 1
to 10, and one table per request. See the official
[hotword documentation](https://www.volcengine.com/docs/6561/155739) and
[streaming SDK example](https://www.volcengine.com/docs/6561/1395846).

### 4. Explicit provider-side correction pairs

When the final result repeatedly contains the same wrong form, the settings
window lets the user explicitly enter `recognized as` and `correct to` values.
The pair is stored in a separate private `corrections.json`; no transcript,
timestamp, weight, or surrounding document context is retained.

The local boundary is conservative because Volcengine does not publish a
request-level pair count, phrase-length limit, or matching-boundary guarantee:

- at most 50 pairs and at most 64 Unicode characters on either side;
- strict versioned JSON with no unexpected fields;
- empty values, controls, unsafe files, and conflicting source mappings are
  rejected;
- missing or empty corrections leave the request unchanged;
- the file is reloaded before each new dictation.

For a non-empty mapping, the daemon merges `correct_words` with any existing
`hotwords` inside the same compact `request.context` JSON string documented by
Volcengine. The provider performs the correction during recognition. The
client does not run `.replace()` or a post-hoc term substitution, so it cannot
accidentally alter an unrelated committed phrase. The optional clean delivery
above is deletion-only and cannot change one spelling into another. Nothing is learned
from partial hypotheses or from unrelated typing or clipboard content.

See the official [streaming SDK
example](https://www.volcengine.com/docs/6561/1395846), which documents
`{"correct_words":{"deep seek":"DeepSeek"}}` inside `context`.

### 5. Bounded adaptive correction memory

After the provider's authoritative final is committed, `murmur-voice` retains
the same focused IBus context for at most five seconds. If the application
supports IBus surrounding text, the engine first anchors the exact committed
span. At the end of the observation, the daemon compares the two bounded
surrounding snapshots to prove that text outside that span is unchanged, then
extracts only changed portions inside the span. One high-confidence bounded
replacement can activate immediately. Several independent bounded
replacements are split into medium-confidence candidates and stay inactive
until the user confirms them; the daemon never rewrites text already corrected.

If clean terminal delivery removed anything, this observation is consumed
without extraction as described above. Unchanged clean output and faithful
output continue through the normal five-second path.

This intentionally rejects ambiguous feedback:

- pure insertion or deletion, mixed insert/delete edits, and broad polishing;
- any change outside the anchored committed span;
- focus or private-input changes, a selection still active when the observation
  finishes, timeout, engine disable,
  daemon loss, or an application that does not provide surrounding text;
- values longer than 64 Unicode characters, invalid controls, or an unchanged
  result.

The changed block may contain at most three lexical tokens on each side and
must meet a conservative similarity floor. A one-character source is accepted
only after expansion through an unchanged adjacent lexical token, so a narrow
edit such as `今天开会` to `今日开会` can become `今天` → `今日`, while a
context-free one-character mapping is rejected. Presentation-only corrections
such as `openai` → `OpenAI` remain valid.
Common unchanged edges are trimmed in linear time before multi-edit matching;
the remaining diff window is capped at 256 tokens. A wider edit returns the
visible `diff-too-complex` reason instead of doing unbounded work in the input
path.

The next `toggle` ends the observation early, allowing the next dictation to
start without waiting for the full five seconds. The temporary voice-only IBus
engine remains selected until observation finishes. Ordinary direct keys pass
through for the application to handle, but stock Rime or another previous IBus
engine cannot compose during this short interval.

Extraction is token-aware rather than a character-wide replacement. A
cross-script edit must include unchanged adjacent Latin context before it can
be learned: editing `奔驰 mark` to `bench mark` may therefore produce the
specific phrase rule, but never the broad rule `奔驰` → `bench`. The user's
vocabulary and optional installed English/French Hunspell lexicons can
canonicalize a unique separator-insensitive spelling such as `bench mark` →
`benchmark`; an ambiguous lookup is left unchanged. This is deterministic,
event-driven processing, not a continuously running local neural model.

Captured pairs are stored in the private versioned
`adaptive-corrections.json` ledger. An entry contains only the bounded pair,
its category, evidence, state, and a support count: no separate transcript, surrounding
snapshot, timestamp, audio, document context, or edit stream is retained. The
observer uses IBus surrounding text; it does not read the clipboard, AT-SPI
accessibility tree, global keyboard events, Rime database, or microphone audio.
version-2 schema distinguishes `candidate`, `active`, `conflicted`,
`suspended`, and `archived` entries and caps the ledger at 500. Existing
version-1 ledgers migrate in memory without changing their active decisions.
Each observation also stores one transcript-free result code and bounded
counts, so timeout, unsupported surrounding text, selection, conflict,
candidate capture, and activation are visible instead of failing silently.
Reaching the bound fails the new update without silently evicting a pair.

The settings window shows active, candidate, and conflicted counts, the latest
reason, confirm buttons, and four separate content-free counts: explicitly
saved vocabulary, explicitly saved manual corrections, effective adaptive
rules, and the exact combined correction count compiled for the next request.
The optional `vocabulary.json` and `corrections.json` files are created only by
an explicit save; automatic learning never creates empty files or copies its
ledger wholesale into either manual store. Their absence therefore means
"no explicit entries", not that the adaptive runtime failed to load.

For applications that cannot expose trusted IBus
surrounding text, the same page offers an explicit fallback: the daemon shows
the raw provider sentence and the user supplies what they actually said
verbatim. Delivered text may be displayed read-only but is never the correction
source. The runtime diffs raw provider and spoken text in
memory, stores only safe bounded pairs, and activates an explicitly confirmed
choice. It never reads the clipboard or global keyboard state and never stores
the two complete sentences in the adaptive ledger. The CLI exposes content-free
statistics with `murmur-voice-daemon adaptive-status`; interactive confirmation
keeps private pair text out of process arguments.

At the next dictation, the daemon builds a provider view from manual and active
adaptive corrections. Manual `corrections.json` entries always win. Conflicting
learned mappings, source or canonical overlaps, provider cascades, and cycles
are suppressed instead of silently replacing one another; more-specific active
mappings are preferred.
The private ledger may retain more observations, but the combined
`context.correct_words` view sent for one provider request remains bounded to
50 pairs. Reload happens at each dictation, with no service restart. These are
provider hints and correction memory, not post-hoc local replacement, a
generative rewrite, online model training, or an autoregressive learner.
When the user confirms a retained candidate, the runtime atomically replaces
the private adaptive ledger, reloads that on-disk generation, and recompiles
the provider view before reporting that it will be available to the next
dictation. If a manual-source conflict, cycle, cascade, overlap, or provider
capacity prevents emission, settings and `adaptive-status` report that fixed
reason instead of claiming success. Pair text never appears in status JSON.

### 6. Advanced managed vocabulary

Teams with a large stable domain list may create a table in Volcengine's
self-learning console and configure its `boosting_table_id`. This remains an
advanced option because it is tied to the user's own Volcengine project. The
default installation must not require it.

Volcengine states that hotwords take effect immediately and can improve recall
for suitable terms, while also warning that broad common words can reduce
overall quality. See the official
[hotword FAQ](https://www.volcengine.com/docs/6561/155743).

## What the application must not do

- Do not use a generative LLM to rewrite the user's meaning.
- Do not learn automatically from live partial hypotheses.
- Do not read the clipboard or selected text merely to build vocabulary.
- Do not treat insertions, deletions, broad multi-edit polishing, or text outside the
  anchored final span as adaptive correction evidence.
- Do not upload the full Rime user database.
- Do not log vocabulary, transcripts, API keys, or replacement pairs.
- Do not apply a late correction after the input context has lost focus.

## Evaluation

Accuracy changes need a small, consented local evaluation set with expected
text. Report character error rate separately for:

1. live draft;
2. two-pass final;
3. two-pass final plus personal vocabulary;
4. two-pass final plus explicit correction pairs;
5. two-pass final plus the bounded active adaptive-correction view;
6. raw provider final versus deterministic clean delivery, reported separately
   because cleanup quality is not ASR accuracy.

The audio and expected text stay outside Git. Repository tests use invented
text and protocol fixtures only.

The default-off local collector does not create an expected-text label. Its
saved `provider_final` is explicitly `teacher-unreviewed`; both
`spoken_verbatim` and `preferred_output` are null. Collected records must stay
out of CER/WER evaluation until a separate review workflow supplies the
appropriate reference label and a leakage-safe train/development/test split.
When collection was already enabled for an utterance, the post-commit learner
can add an append-only `feedback/<utterance_id>/<event_id>.json` event containing only bounded
correction pairs, categories, decisions, counts, and the result code. It never
modifies `record.json` or the strict two-file utterance directory, and no event
is written while collection is disabled.

## Optional data collection and future training

The separate collector can now retain an accepted utterance, but only after an
explicit opt-in and only below an existing local or mounted folder selected by
the user. It stores the exact 16 kHz mono signed 16-bit WAV and a versioned JSON
schema-v4 record. It retains raw `provider_final` as the pseudo-label and stores
the actual machine-derived delivery, frozen target, and replayable deletions
separately. The
audio/provider-final pair is a future review candidate, not a gold
sample or evidence that self-training is safe.

Collection is bounded and written in the background. The application does not
authenticate to or mount Orange, upload to Google Drive, train a model, or add
static encryption. A user-mounted compatible Orange filesystem can be selected
as an ordinary absolute path; complete local/Orange records can instead be
backed up asynchronously with rclone as described in
[remote-dataset-storage.md](remote-dataset-storage.md). A later workflow must
keep a reviewed literal
`spoken_verbatim` label separate from the user's edited `preferred_output`.
Training and model choice remain postponed until label quality, deletion,
language coverage, acoustic diversity, split hygiene, and evaluation rules
exist. See
[personal-asr-data-plan.md](personal-asr-data-plan.md).
