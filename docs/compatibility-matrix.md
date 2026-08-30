# Compatibility matrix

This page records user-visible environments that were exercised end to end.
Unit tests and synthetic IBus smokes do not by themselves add a row. A row
needs a named release or commit, an actual graphical session, an application,
and a real user-facing result. Tests must use invented non-sensitive text.

The current public alpha is scoped to Ubuntu 24.04 x86_64 with IBus. Empty
cells are validation work, not an implied failure or support claim.

| Release / commit | Distribution | Desktop / display server | Application / toolkit | Microphone | Partial at caret | Final once | Previous engine restored | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _Awaiting external beta reports_ |  |  |  |  |  |  |  |  |

## How to contribute a result

Open a
[compatibility report](https://github.com/SidUParis/openVoiceInput_linux/issues/new?template=compatibility_report.yml)
for one environment. Report the exact release or commit, distribution,
desktop, X11/Wayland session, application, and broad microphone category.

Do not attach an API key, audio, actual dictation, personal vocabulary,
dataset JSON, device serial number, account identifier, or unredacted log.
Maintainers add a matrix row only after the report establishes the visible
partial/final and restoration outcome. A result for one version or application
does not establish support for every Linux distribution or toolkit.
