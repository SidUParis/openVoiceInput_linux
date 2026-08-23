# Notices and upstream boundaries

The voice daemon now contains a narrow, attributed migration from Doubao
Murmur. Other planned foundations remain external until explicitly recorded
here. Every import must retain its original file-level notices.

## Planned upstream foundations

- **ibus-rime** — Copyright its contributors; GPL-3.0-or-later.
  <https://github.com/rime/ibus-rime>
- **librime** — Copyright its contributors; BSD-3-Clause.
  <https://github.com/rime/librime>
- **Rime Ice** — Copyright its contributors; GPL-3.0-only. It is currently an
  external data/configuration dependency and is not vendored by this
  repository.
  <https://github.com/iDvel/rime-ice>
- **Doubao Murmur** — Copyright (c) 2026 lilong7676 and contributors; MIT.
  <https://github.com/lilong7676/doubao-murmur>

## Imported Doubao Murmur portions

The following files contain adapted portions of the local Doubao Murmur Linux
implementation:

- voice/murmur_voice/audio.py — minimal 16 kHz PCM capture boundary, adapted
  from linux/src/doubao_murmur/audio_capture.py.
- voice/murmur_voice/volcengine.py — Volcengine v3 binary WebSocket framing,
  response parsing, and threaded streaming lifecycle, adapted from
  linux/src/doubao_murmur/volcengine_client.py.
- voice/murmur_voice/preedit.py — temporary IBus engine selection and the
  org.murmur.IME.Preedit1 client, adapted from
  linux/src/doubao_murmur/preedit_client.py and the host-command helper.

Source examined on 2026-08-24 in
https://github.com/SidUParis/doubao-murmur.git at commit
57d93027c8e377baffd62f067b23ed461490bab7. Open Voice Input Linux
modifications are released under GPL-3.0-only; adapted portions retain the
checkout's following MIT notice and terms:

> MIT License
>
> Copyright (c) 2026 lilong7676
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Names and services

Rime, Volcengine, ByteDance, and Doubao are names or trademarks of their
respective owners. Their mention describes compatibility only and does not
imply endorsement or affiliation.
