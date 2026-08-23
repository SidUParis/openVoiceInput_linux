# Recording duration and resource limits

## What is known

The currently verified prototype has no application-level maximum recording
timer. It records until the user stops or cancels, the microphone fails, or
the remote WebSocket ends.

Audio is captured as 16 kHz, mono, signed 16-bit PCM: 32,000 bytes per second,
about 1.92 MB per minute before protocol overhead. In healthy streaming use,
audio is sent continuously rather than retained for the whole utterance.

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

Before a public preview, the daemon should provide two independent guards:

1. A default 10-minute maximum for one input-method utterance, with a visible
   warning before automatic finalisation. This is a product safety default,
   not a claimed provider limit.
2. A 5–10 second high-water limit for PCM waiting to be sent. If the network
   cannot drain that queue, recording must stop safely instead of growing
   memory without bound.

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
