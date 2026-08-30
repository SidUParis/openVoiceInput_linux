# Recording duration and resource limits

## What is known

The earlier Doubao Murmur prototype had no application-level maximum recording
timer. The self-contained daemon under voice now enforces a 600-second maximum
per utterance. At 540 seconds its status reports recording-limit-warning; at
600 seconds it stops capture and requests the provider's authoritative final.
If that final does not arrive within 20 seconds, it cancels preedit rather than
committing a live hypothesis.

Audio is captured as 16 kHz, mono, signed 16-bit PCM: 32,000 bytes per second,
about 1.92 MB per minute before protocol overhead. In healthy streaming use,
audio is sent continuously. With default-off local collection disabled, it is
not retained for the whole utterance. When collection is explicitly enabled,
the exact current utterance is additionally held in bounded memory, up to
19.2 MB of PCM at the 600-second product limit, before nonblocking handoff to a
bounded background writer.

The Volcengine documentation describes `bigmodel_async` as long-audio,
bidirectional streaming and recommends sending 100–200 ms audio packets. It
does not currently publish a numeric maximum lifetime for one
`bigmodel_async` WebSocket connection or a numeric idle-packet timeout. The
documented “2.0 小时版” name identifies the model version and hourly billing
resource; it must not be presented as a two-hour connection limit.

Official references:

- [BigModel streaming ASR API](https://www.volcengine.com/docs/6561/1354869)
- [BigModel ASR product overview](https://www.volcengine.com/docs/6561/1354871)
- [Product updates](https://www.volcengine.com/docs/6561/162929)

## Public-preview policy

The daemon implements two independent guards:

1. A default 10-minute maximum for one input-method utterance, with an
   observable status warning one minute before automatic finalisation. This
   is a product safety default, not a claimed provider limit. The headless MVP
   exposes the warning through its control status; a later indicator must turn
   that state into an automatically visible notification.
2. A 10-second high-water limit for PCM waiting to be sent. If the network
   cannot drain that queue, recording stops safely instead of growing memory
   without bound.

Optional local collection has a separate two-record writer queue. WAV encoding,
hashing, sync, and publication run outside the capture callback and session
lock. A full queue, stalled mount, or write error loses only that unpublished
optional record and reports a fixed status; it does not block final text or
grow the queue. This is best-effort direct-to-selected-folder storage with no
fallback local spool.

On normal service shutdown, the writer gets a bounded 10 seconds to drain
accepted records inside systemd's 30-second total stop budget. It never extends
shutdown indefinitely. If the selected filesystem is stalled or unmounted, a
hidden staging directory may be cleaned up (or remain hidden if the filesystem
operation itself cannot complete) and the unpublished record may be lost.
Records already atomically published under `utterances/` remain.

Long meetings should use rolling, focus-bound segments rather than one
unbounded input-method transaction. A user-configurable longer limit may be
offered only after continuous 5/30/60/120-minute tests and confirmation of the
account-specific service expectations from Volcengine support.

## Test matrix

- Continuous voiced input for 5, 30, 60, and 120 minutes.
- Network stall while capture continues; confirm bounded RSS and safe cancel.
- Server close before final; confirm no partial is silently committed.
- Stop near the configured limit; confirm exactly one final commit.
- Focus change before auto-finalisation; confirm no text reaches the new field.
- Quota, authentication, and rate-limit errors; confirm ordinary Rime typing
  remains available.
- Enabled local collection at the 600-second limit; confirm bounded RSS,
  nonblocking final, correct WAV/hash metadata, and atomic publication.
- Full writer queue, stalled/unmounted destination, disable during staging, and
  service shutdown; confirm bounded stop time, no publication after disable,
  and no damage to already published records.
