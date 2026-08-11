# Releasing

## TL;DR

Pushing a git tag does **not** publish anything. The release workflow triggers on
`release: published`, so you must create a GitHub Release.

```bash
# 0. start from an up-to-date main
git checkout main && git pull

# 1. bump the version and close out the changelog
$EDITOR pyproject.toml   # version = "0.2.13"
$EDITOR CHANGELOG.md     # [Unreleased] -> [0.2.13] - <date>, add the link at the bottom

# 2. open a PR — release prep goes through review like anything else
git checkout -b release/0.2.13
git commit -am "Bump version to 0.2.13"
gh pr create --base main --title "Bump version to 0.2.13"
# wait for CI, then merge

# 3. cut the release (this creates the tag and triggers publishing)
gh release create v0.2.13 --title "v0.2.13 — <short summary>" --notes "<release notes>"
```

Step 3 is the one that publishes. `gh release create` creates the tag for you, so
there is no separate tagging step.

The tag must be `v` + the exact `version` in `pyproject.toml`. The workflow hard-fails
if they disagree — a mislabelled artifact on PyPI is worse than a failed release.

## Choosing the version

| Change | Bump |
| --- | --- |
| New API, bug fix — anything additive | patch (`0.2.12` → `0.2.13`) |
| Breaking change to an existing API | minor (`0.2.x` → `0.3.0`) |

Minor is reserved for breaking changes here, which is narrower than SemVer requires for
`0.x`. It matches the history — `0.2.0`–`0.2.12` were all additive — and it matches the
other Layr8 SDKs, so "what does a minor bump mean" has one answer across all of them.

## What the workflow does

`.github/workflows/release.yaml` runs four jobs:

| Job | What it does |
| --- | --- |
| `resolve` | Works out the version from the tag (or the manual input) |
| `test` | Runs the suite against the exact tag |
| `build` | Validates tag == `pyproject.toml`, builds sdist + wheel, `twine check` |
| `publish-pypi` | Publishes to PyPI |

`test` duplicates what CI already ran on `main`. That is deliberate — a release can be
cut from any commit, so the release chain re-verifies the exact tag it is about to
publish.

## Credentials

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/)
over OIDC. The workflow mints a short-lived credential via `id-token: write` and
exchanges it with the registry.

**There is no PyPI token.** Nothing to expire, rotate, or leak. If you are editing the
publish job, do not add `PYPI_API_TOKEN` — it re-introduces exactly the long-lived
secret this avoids.

### One-time setup on PyPI

Trusted publishing has to be enabled once, by a project owner, before the first
automated release:

1. https://pypi.org/manage/project/layr8/settings/publishing/
2. Add a GitHub publisher:
   - Owner: `layr8`
   - Repository: `python-sdk`
   - Workflow: `release.yaml`
   - Environment: `pypi`
3. Create the `pypi` environment in the repository settings (Settings →
   Environments). Restrict it to protected branches/tags if you want a second gate.

Until that is done, `publish-pypi` fails with an OIDC error and everything before it
still passes — so a release that fails only at the last job means step 1 was skipped,
not that the build is broken.

## When a release partially fails

Publishing is idempotent: `skip-existing: true` means a re-drive uploads only what is
missing. To re-drive:

```bash
gh workflow run release.yaml -f version=0.2.13
```

This checks out the `v0.2.13` tag, re-runs the tests and the build, and uploads
whatever is not on PyPI yet. It is always safe to re-run.

## Checking a release landed

```bash
pip index versions layr8
```

## History

Releases up to and including `v0.2.12` were uploaded by hand — this repository had no
publish workflow, only CI. `v0.2.12` is tagged but was **not** published by automation;
if it is missing from PyPI, re-drive it with the manual trigger above.
