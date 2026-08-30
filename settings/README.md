# Open Voice Input Linux settings

The bounded GTK4 settings MVP is implemented inside the `murmur-ime-voice`
wheel and is available through the `open-voice-input-settings` entry point. It
provides only the controls needed for the standalone voice preview:

- save a replacement Volcengine API key through a masked `Gtk.PasswordEntry`;
- edit the explicit private personal vocabulary, one term per line;
- edit bounded, explicit wrong-to-canonical recognition corrections;
- view the per-dictation DJI Mic Mini 2 selection boundary;
- explicitly enable/disable optional local WAV/JSON collection and choose an
  existing absolute local or mounted destination folder;
- clear the validated local key through an explicit two-step action while the
  managed voice service is inactive;
- inspect, explicitly enable/start, and disable/stop the
  `murmur-ime-voice.service` user unit.

The existing key is never placed in a widget or displayed. A save attempt
always clears the password entry, does not contact Volcengine, and does not
restart the service. Vocabulary, corrections, provider-key, and
local-collection persistence reuse the daemon's validated atomic private-file
APIs. Saving the collection choice initializes/reopens
`openvoiceinput-dataset-v1` only when enabled; it does not start recording.

GTK service operations use fixed `systemctl --user` argument vectors without a
shell. Disabling/stopping the service is an explicit action and may cancel an
active dictation. Saved vocabulary, correction, and local-collection settings
are reloaded before the next dictation and do not require a service restart.
Service enable/disable controls remain explicit lifecycle actions. This alpha
also has a default-on, event-driven five-second adaptive-correction observation,
but this settings MVP does not yet provide a switch or ledger-management UI for
it.

Collection is off by default. Only an authoritative provider final accepted by
the focused IBus context publishes the exact 16 kHz mono signed 16-bit WAV and
versioned JSON. The UI calls `provider_final` an unreviewed pseudo-label and
does not fabricate `spoken_verbatim` or `preferred_output`: both remain null.
It also states that the collector does not upload to Orange or elsewhere,
train a model, or add application-level encryption. Disabling prevents
unpublished queued/staged records from becoming visible; already published
records are retained. The selected filesystem determines effective visibility.

The microphone notice describes automatic behavior rather than a system audio
control. Before each dictation, proven-online DJI selects DJI, proven-offline
uses a safe non-DJI fallback, and unknown preserves system/default behavior.
The choice is scoped to the new daemon stream, never changes a playback sink or
system default, and does not hand off mid-utterance.

This native settings entry point is part of the engine/daemon install. A
separately delivered compatibility Flatpak may expose controller/indicator
actions, but it does not implement or own microphone routing, ASR, or dataset
storage.

This MVP deliberately does not configure Rime data, global hotkeys, ASR
advanced options, a tray indicator, Orange transfer, a review/delete workflow,
model training, or Secret Service. Those integrations need their own lifecycle
and migration design. Runtime requirements are PyGObject and the GTK4
introspection data, provided on Ubuntu by `python3-gi` and
`gir1.2-gtk-4.0`.
