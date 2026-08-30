# Open Voice Input Linux settings

The bounded GTK4 settings MVP is implemented inside the `murmur-ime-voice`
wheel and is available through the `open-voice-input-settings` entry point. It
provides only the controls needed for the standalone voice preview:

- select an available online ASR provider and save that provider's replacement
  API key through a masked `Gtk.PasswordEntry`;
- edit the explicit private personal vocabulary, one term per line;
- edit bounded, explicit wrong-to-canonical recognition corrections;
- arrange a complete microphone priority for the user's own equipment;
- explicitly enable/disable optional local WAV/JSON collection and choose an
  existing absolute local or mounted destination folder;
- clear the validated local key through an explicit two-step action while the
  managed voice service is inactive;
- inspect, explicitly enable/start, and disable/stop the
  `murmur-ime-voice.service` user unit.

The existing key is never placed in a widget or displayed. A save attempt
always clears the password entry, does not contact any provider, and does not
restart the service. Vocabulary, corrections, provider-key,
microphone-priority, and local-collection persistence reuse the daemon's
validated atomic private-file APIs. Saving the collection choice initializes
or reopens
`openvoiceinput-dataset-v1` only when enabled; it does not start recording.

GTK service operations use fixed `systemctl --user` argument vectors without a
shell. Disabling/stopping the service is an explicit action and may cancel an
active dictation. Saved vocabulary, correction, microphone-priority, and
local-collection settings are reloaded before the next dictation and do not
require a service restart.
Service enable/disable controls remain explicit lifecycle actions. This alpha
also has a default-on, event-driven five-second adaptive-correction observation,
but this settings MVP does not yet provide a switch or ledger-management UI for
it.

Collection is off by default. Only an authoritative provider final accepted by
the focused IBus context publishes the exact 16 kHz mono signed 16-bit WAV and
versioned JSON. The UI calls `provider_final` an unreviewed pseudo-label and
does not fabricate `spoken_verbatim` or `preferred_output`: both remain null.
It also states that the application does not mount a remote host or accept
SSH/Google Drive URLs. A local path backed by an already-mounted remote
filesystem is allowed; complete records can be backed up to Drive separately.
It does not train a model or add application-level encryption. Disabling
prevents unpublished queued/staged records from becoming visible; already
published records are retained. The selected filesystem determines effective
visibility.
See [the remote dataset storage guide](../docs/remote-dataset-storage.md) for
SSHFS disconnect/permission boundaries and asynchronous Google Drive backup.

The microphone list stores one complete priority order chosen by the user. If
no priority has ever been saved, the current alpha loads the deterministic
compatibility initialization `DJI > headset > other external > built-in`; this
is an implementation fallback, not a product recommendation. Users may move
any category up or down. Before each dictation the daemon reloads this setting,
re-enumerates currently usable sources, and falls through unavailable or
ambiguous categories.
DJI is still link-aware; Bluetooth A2DP playback alone is not treated as a
headset microphone. The choice is scoped to the new daemon stream, never
requests a playback-sink or system-default change, and does not hand off
mid-utterance. A missing priority file uses the documented default; an existing
invalid file is shown as an error and can be repaired by explicitly saving the
complete displayed order.

This native settings entry point is part of the engine/daemon install. A
separately delivered compatibility Flatpak may expose controller/indicator
actions, but it does not implement or own microphone routing, ASR, or dataset
storage.

This MVP deliberately does not configure Rime data, global hotkeys, ASR
advanced options, a tray indicator, application-owned remote-host
authentication/mounting or first-party resumable transfer, a review/delete
workflow, model training, or Secret Service. Those integrations need their own
lifecycle and migration design. Runtime requirements are PyGObject and the
GTK4 introspection data, provided on Ubuntu by `python3-gi` and
`gir1.2-gtk-4.0`.
