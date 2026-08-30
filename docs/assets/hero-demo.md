# Hero demo assets

`hero-demo.gif`, `hero-demo-poster.png`, and `social-preview.png` are synthetic,
deterministically rendered project artwork. They contain no recording, real
transcript, API key, username, local path, screenshot, or downloaded artwork.

The animation is an interaction concept demo reconstructed from the current
product flow. It shows the implemented boundaries: press Right Alt once to
start and once again to finish in the controller flow, caret-local IBus
preedit, one authoritative Final, one strict
same-field replacement during the bounded correction window, and use of that
correction on the next provider request. It is not a screen recording or a
claim that every application renders preedit identically.

Regenerate all three files from the repository root with:

```bash
python3 scripts/generate_hero_demo.py
```

Generation requires Pillow and Noto Sans CJK. The script has no network access,
random input, current-time input, or host-data input. Re-running it with the
same Pillow/font versions produces byte-identical files.

Asset roles:

- `hero-demo.gif`: 960 x 540 looping README animation, 156 frames averaging
  12 fps;
- `hero-demo-poster.png`: still fallback taken from the final animation frame;
- `social-preview.png`: 1200 x 600 GitHub repository social preview.

Keep the persistent `交互概念演示` label if the animation is revised. Never
replace the synthetic sentences with a user's recorded or collected text.
