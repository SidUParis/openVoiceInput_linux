# Open Voice Input Linux settings

The bounded GTK4 settings MVP is implemented inside the `murmur-ime-voice`
wheel and is available through the `open-voice-input-settings` entry point. It
provides only the controls needed for the standalone voice preview:

- save a replacement Volcengine API key through a masked `Gtk.PasswordEntry`;
- edit the explicit private personal vocabulary, one term per line;
- edit bounded, explicit wrong-to-canonical recognition corrections;
- clear the validated local key through an explicit two-step action while the
  managed voice service is inactive;
- inspect, explicitly enable/start, and disable/stop the
  `murmur-ime-voice.service` user unit.

The existing key is never placed in a widget or displayed. A save attempt
always clears the password entry, does not contact Volcengine, and does not
restart the service. Vocabulary, corrections, and provider-key persistence
reuse the daemon's validated atomic private-file APIs.

GTK service operations use fixed `systemctl --user` argument vectors without a
shell. Disabling/stopping the service is an explicit action and may cancel an
active dictation. Saved settings take effect after the user manually disables
and stops, then enables and starts the service.

This MVP deliberately does not configure Rime data, global hotkeys, ASR
advanced options, a tray indicator, or Secret Service. Those integrations need
their own lifecycle and migration design. Runtime requirements are PyGObject
and the GTK4 introspection data, provided on Ubuntu by `python3-gi` and
`gir1.2-gtk-4.0`.
