# Open Voice Input Linux engine

This directory now contains a pure Python IBus prototype that demonstrates
native, caret-local voice preedit and final commit. Run it directly with:

```bash
./engine/murmur-ime-engine
```

It dynamically registers the development engine `murmur-voice` and exports
the temporary session D-Bus bridge `org.murmur.IME.Preedit1`. See
[`docs/python-preedit-prototype.md`](../docs/python-preedit-prototype.md) for
the protocol, tests, safety rules, and the stock Rime coexistence limit.

The production engine remains a planned GPL ibus-rime/librime frontend. No
audio capture, provider credentials, or network code belongs in either engine.

The executable, Python package, IBus/D-Bus identifiers, systemd unit, and
install directory retain their historical `murmur-*` / `org.murmur.*` names
as 0.x compatibility ABI. The public project and repository name is Open Voice
Input Linux / `openVoiceInput_linux`.
