# Recognition accuracy and correction loop

Open Voice Input Linux treats a live ASR hypothesis as a draft. It can be wrong
and may be replaced several times before the provider emits the authoritative
two-pass result. Only that final result is committed.

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

### 2. Explicit personal vocabulary

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

### 3. Explicit provider-side correction pairs

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
client does not run `.replace()` or any other post-hoc local rewrite, so it
cannot accidentally alter an unrelated committed phrase. Nothing is learned
from partial hypotheses or from unrelated typing or clipboard content.

See the official [streaming SDK
example](https://www.volcengine.com/docs/6561/1395846), which documents
`{"correct_words":{"deep seek":"DeepSeek"}}` inside `context`.

### 4. Bounded adaptive correction memory

After the provider's authoritative final is committed, `murmur-voice` retains
the same focused IBus context for at most five seconds. If the application
supports IBus surrounding text, the engine first anchors the exact committed
span. At the end of the observation, the daemon compares the two bounded
surrounding snapshots to prove that text outside that span is unchanged, then
extracts only the changed portion inside the span. It accepts at most one
strict replacement and derives a bounded wrong-to-canonical pair; it does not
rewrite the text that the user has already corrected.

This intentionally rejects ambiguous feedback:

- pure insertion or deletion, more than one edit, and broad sentence polishing;
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

Accepted pairs are stored in the private versioned
`adaptive-corrections.json` ledger. An entry contains only the bounded pair,
its state, and a support count: no separate transcript record, surrounding
snapshot, timestamp, audio, document context, or edit stream is retained. The
observer uses IBus surrounding text; it does not read the clipboard, AT-SPI
accessibility tree, global keyboard events, Rime database, or microphone audio.
The schema distinguishes `active`, `conflicted`, `suspended`, and `archived`
entries and caps the ledger at 500; reaching that bound fails the new update
without evicting an existing pair silently.

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

### 5. Advanced managed vocabulary

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
- Do not treat insertions, deletions, multi-edit polishing, or text outside the
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
5. two-pass final plus the bounded active adaptive-correction view.

The audio and expected text stay outside Git. Repository tests use invented
text and protocol fixtures only.

## Future opt-in personal ASR dataset

This correction loop does not retain recordings and does not train a model.
A later, separate data-capture feature may preserve consented audio and labels
on a user-controlled Orange machine, but it must keep a literal
`spoken_verbatim` label separate from the user's edited `preferred_output`.
Cloud output is a useful draft or pseudo-label, not automatically ground truth.
Training and model choice are deliberately postponed until collection quality,
consent, deletion, and evaluation rules exist. See
[personal-asr-data-plan.md](personal-asr-data-plan.md).
