# Remote desktop input

An IBus commit belongs to the application and input context in the same Linux
desktop session. Text committed by Open Voice Input Linux on the client is not
a stream of keyboard events, so an RDP canvas such as Remmina cannot forward
that local preedit into a remote application's caret.

## Recommended: run voice input in the remote session

Redirect the client microphone over RDP and run Open Voice Input Linux inside
the remote desktop session:

1. In the Remmina RDP profile, set **Redirect local microphone** to
   `sys:pulse`.
2. A Snap installation of Remmina also needs its recording interface connected:

   ```bash
   snap connections remmina | grep audio-record
   sudo snap connect remmina:audio-record
   ```

3. The xrdp server needs a matching PulseAudio or PipeWire xrdp source module.
   Reconnect the RDP session after installing it, then verify from a terminal
   inside that session:

   ```bash
   pactl list short sources
   pactl get-default-source
   ```

   A redirected microphone should appear as an xrdp/RDP source rather than a
   source ending in `.monitor`.
4. Start the IBus engine and voice daemon in that same graphical session. A
   different local-console session has a different D-Bus and IBus context and
   cannot own the remote caret.

The current preview targets Ubuntu 24.04 with the IBus focus-ID API. Stock
Ubuntu 22.04 IBus does not provide that API and is intentionally rejected
rather than weakening focus and private-field protections. Multi-session
service activation is not automated in this preview, so RDP use remains an
advanced/manual setup.

Official references:

- [Remmina microphone setting](https://gitlab.com/Remmina/Remmina/-/issues/2420)
- [xrdp audio and microphone redirection](https://github.com/neutrinolabs/xrdp#access-to-remote-resources)
- [PulseAudio modules for xrdp](https://github.com/neutrinolabs/pulseaudio-module-xrdp)

## Lightweight fallback: synchronized clipboard

Remmina can synchronize the clipboard. A client-side tool can therefore copy
the authoritative final transcript and the user can paste it explicitly in the
remote field. This does not provide remote inline partial text, and clipboard
contents become available to both desktop sessions. Do not use it for secrets,
password fields, or unattended automatic paste.

## Future native bridge

A future remote mode can keep capture and recognition on the client and send
revisioned partial/final events to a small IBus helper in the selected remote
session. Such a bridge must be explicitly armed, authenticated, bound to one
focused context, reject password/private fields, and discard stale revisions.
The preview does not yet ship that protocol.
