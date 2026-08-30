# Preview release process

This checklist separates what can be prepared in the private repository from
steps that require a controlled public transition. A checksum proves file
integrity; it does not prove who published the file.

## One-time private-repository setup

1. Protect the default branch with strict required checks named `security`,
   `engine`, `voice`, and `preview-bundle`, all produced by GitHub Actions.
   Require pull requests, resolved conversations, linear history, and prevent
   deletion or non-fast-forward updates. A single-maintainer repository can
   require a pull request with zero approving reviews.
2. Limit Actions to GitHub-owned actions and require full commit-SHA pinning.
3. Enable immutable releases for future releases. This protects a release only
   after it is published; it is not a substitute for build provenance.
4. Keep the current non-public security and conduct-reporting route documented
   in `SECURITY.md` and `CODE_OF_CONDUCT.md`: `sunxusidney@gmail.com`, using
   the suggested subjects `[Open Voice Input Linux Security] <short summary>`
   and `[Open Voice Input Linux Conduct] <short summary>`. Confirm access to
   the mailbox immediately before making the repository public. Initial
   reports must not contain keys, recordings, raw dictated text, or
   credential-bearing logs; arrange a safer transfer method first if sensitive
   evidence is essential.
5. Configure a maintainer-controlled SSH or GPG signing identity, including a
   documented recovery/rotation plan. Do not rewrite old unsigned history.

## Per-release preparation

1. Revoke every development provider key that might have been disclosed.
   Neither CI nor the release workflow may receive a provider key.
2. Run the current-file, index, and reachable-history secret scan from a clean
   checkout and retain the CI result.
3. Record the fresh Ubuntu graphical-user matrix status in
   `docs/open-source-readiness.md`. An explicitly labelled alpha may publish
   with missing physical-microphone/provider/application-matrix evidence only
   when the release notes disclose that validation gap. A supported or
   distribution-native release must complete the matrix with a newly rotated
   key entered locally. Include DJI link-state and a real mounted collection
   destination in the matrix before claiming those paths fully validated.
   Exercise the saved microphone ordering with built-in-only, headset plus
   built-in, DJI plus headset plus built-in, disconnect/reconnect, and an
   unavailable higher-priority source before claiming physical routing fully
   validated.
   Keep collection disabled unless the run explicitly uses a private disposable
   folder. Record no key, selected dataset path, dictated text, audio, dataset,
   or credential-bearing log in release evidence.
4. Require all protected checks on the exact release commit. Build both the
   offline/source archive and standalone Ubuntu `.deb` from that trusted
   commit. The package builder must consume the hash-locked runtime wheelhouse
   from the matching verified bundle, never an adjacent project wheel. Verify
   both artifacts against the repository ref and compare independently built
   archive and package bytes on the supported Ubuntu 24.04 `amd64`/CPython
   3.12 builder.
5. Create a signed annotated release tag and verify that GitHub reports the tag
   signature as valid.
6. Release notes for a collector-enabled alpha must say that collection is off
   by default, `provider_final` is unreviewed, there is no application-owned
   Orange authentication/mounting or resumable transfer, encryption,
   review/delete UI, training, or fallback spool, and shutdown only gives the
   writer a bounded drain. User-mounted filesystems must be described as a
   separate storage trust boundary. Also disclose that DJI selection is
   per-dictation/app-scoped with no mid-utterance handoff. A release that adds
   microphone ordering must also state the recommended default, saved-order
   fallback behavior, and the remaining physical-device validation gap.

## Controlled public transition

1. Make the repository public only after the confidential contact route and
   branch protection have been verified.
2. Immediately enable GitHub private vulnerability reporting, read the setting
   back, and test the reporting instructions before announcing the preview.
3. Re-run the required checks in public visibility. Public repositories can
   use GitHub Actions artifact attestations; the private personal repository
   cannot. Add a separate attestation job only after that feature is available,
   with `contents: read`, `id-token: write`, and `attestations: write` limited
   to that job.

## Immutable release publication

1. Create a draft prerelease from the verified signed tag.
2. Attach exactly four verified assets: the `.tar.gz`, its `.tar.gz.sha256`,
   the Ubuntu 24.04 `amd64` `.deb`, and its `.deb.sha256`. Do not reuse
   artifacts built for an untrusted pull request.
3. Download all four draft assets into an empty directory. Verify each checksum
   with `sha256sum --check`, unpack the archive safely, and run
   `scripts/verify_preview_bundle.py` against the exact source tag. Inspect the
   package identity, version, architecture, file list, `BUILD-INFO`, and
   package-scoped SBOM with `dpkg-deb` before using a disposable clean Ubuntu
   24.04 machine to exercise install, command discovery, settings launch, and
   removal. Confirm removal leaves no packaged command or running project unit
   while preserving deliberately user-owned configuration and dataset paths.
4. Confirm the two SBOM scopes separately: the archive SBOM inventories its
   project wheel and runtime wheelhouse; the package SBOM identifies the
   application/source commit and the four bundled Python runtime wheels. The
   latter is not a file-level inventory of Ubuntu system dependencies.
5. Publish the draft only after every asset passes. With immutable releases
   enabled, the published tag and assets become locked; corrections require a
   new version rather than replacing an existing asset.
6. Verify the immutable release and each asset with a current GitHub CLI or the
   GitHub API, then record the release URL, tag verification, source commit,
   archive and package SHA256 values, both SBOM serials/scopes, and CI run in
   the release notes.

Official GitHub references:

- [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Actions permissions](https://docs.github.com/en/rest/actions/permissions)
- [Immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
- [Artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
- [Private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
- [Commit and tag signature verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
