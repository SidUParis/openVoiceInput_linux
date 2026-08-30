# Dashboard design references

This note records the product references used for the GTK4 dashboard refresh.
They are interaction references, not visual assets or implementation sources.
Open Voice Input keeps its own Linux/IBus identity, copy, information
architecture, colours, spacing, icons, and components.

## Official references reviewed

- [豆包输入法 official product page](https://srf.doubao.com/): the macOS
  material demonstrates a low-interruption flow in which the focused app stays
  visually primary, a compact recording state appears near the screen edge,
  and recognised text lands at the current cursor.
- [豆包输入法 on Apple's App Store](https://apps.apple.com/cn/app/id6752316550):
  useful only for mobile interaction principles. The keyboard and standby
  experiences shown there are iPhone UI and are not evidence of the macOS
  desktop layout.
- [OpenLess official repository](https://github.com/Open-Less/openless),
  [website](https://openless.top/), and
  [usage guide](https://github.com/Open-Less/openless/blob/beta/USAGE.md):
  demonstrate a separate control dashboard, explicit recording modes, quick
  status feedback, and a lightweight edge capsule. “OpenLess” is the actual
  project name; it is not shorthand for OpenWhispr.

## Principles adopted

- Keep configuration and aggregate status in a main window; keep eventual
  listening/processing feedback compact and low-interruption.
- Make the current state and recording trigger mode explicit.
- Put a small set of common actions near the overview rather than hiding every
  task in settings navigation.
- Use clear state colours and plain Chinese labels, with graceful unknown and
  unavailable states.

## Deliberate differences

- No third-party name, logo, mascot, screenshot, slogan, dashboard layout,
  capsule proportions, colour system, or pixel-level treatment is copied.
- The dashboard does not show recent transcripts. Usage totals come only from
  content-free `usage/<utterance_id>.json` files in the user's opted-in dataset; it never opens
  `record.json` or audio to build the overview.
- Collection-off is shown as disabled, not zero. A missing or disconnected
  selected filesystem is shown as unavailable, while ordinary dictation remains
  usable.
- Provider, push-to-talk/toggle, correction, and insertion-state surfaces are
  reserved as product concepts, but the interface does not claim unimplemented
  behaviour.
