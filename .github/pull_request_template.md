## Summary

Describe the user-visible change and its scope.

## Verification

- [ ] Engine unit tests pass.
- [ ] Voice and GTK/Xvfb tests pass.
- [ ] Installer and preview-bundle tests pass when those paths change.
- [ ] Ruff check and format check pass.
- [ ] Shell scripts pass `bash -n` when changed.
- [ ] No API key, dictated text, recording, vocabulary/correction file, or
      private Rime data is included.
- [ ] Focus/cancel/final-once behavior remains covered when inline input changes.
- [ ] New upstream code has licence and copyright attribution in `NOTICE.md`.
