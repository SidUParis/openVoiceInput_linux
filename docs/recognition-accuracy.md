# Recognition accuracy and correction loop

openVoiceInput_linux treats a live ASR hypothesis as a draft. It can be wrong
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
- the file is loaded once at daemon startup, so changes require a restart.

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

### 3. Correction feedback, only after explicit user action

When the final result still contains a wrong proper noun, a future
“识别纠错” action should let the user enter the intended spelling. That action
adds or raises the canonical term in the personal vocabulary. It must not
silently learn the provider's wrong transcript, nor monitor normal typing.

The minimal feedback record is:

```text
canonical term | weight | last explicitly confirmed time
```

An optional spoken/wrong form can later drive an explicit replacement rule,
but replacement must be bounded to word or phrase boundaries. A global string
replacement can corrupt unrelated sentences and is not acceptable.

### 4. Advanced managed vocabulary

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
- Do not upload the full Rime user database.
- Do not log vocabulary, transcripts, API keys, or replacement pairs.
- Do not apply a late correction after the input context has lost focus.

## Evaluation

Accuracy changes need a small, consented local evaluation set with expected
text. Report character error rate separately for:

1. live draft;
2. two-pass final;
3. two-pass final plus personal vocabulary.

The audio and expected text stay outside Git. Repository tests use invented
text and protocol fixtures only.
