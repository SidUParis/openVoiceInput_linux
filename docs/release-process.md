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
3. Complete the fresh Ubuntu graphical-user matrix in
   `docs/open-source-readiness.md` with a newly rotated key entered locally.
   Record no key, dictated text, audio, or credential-bearing log.
4. Require all protected checks on the exact release commit. Build the archive
   from that trusted commit, verify it against the repository ref, and compare
   the independently built archive checksum when the supported build
   environment is available.
5. Create a signed annotated release tag and verify that GitHub reports the tag
   signature as valid.

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
2. Attach only the verified `.tar.gz` and its `.sha256`. Do not reuse artifacts
   built for an untrusted pull request.
3. Download the draft assets, verify the outer checksum, unpack safely, and run
   `scripts/verify_preview_bundle.py` against the exact source tag.
4. Publish the draft only after every asset passes. With immutable releases
   enabled, the published tag and assets become locked; corrections require a
   new version rather than replacing an existing asset.
5. Verify the immutable release and each asset with a current GitHub CLI or the
   GitHub API, then record the release URL, tag verification, source commit,
   archive SHA256, SBOM serial, and CI run in the release notes.

Official GitHub references:

- [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Actions permissions](https://docs.github.com/en/rest/actions/permissions)
- [Immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
- [Artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
- [Private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
- [Commit and tag signature verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
