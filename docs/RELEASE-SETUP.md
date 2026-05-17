# Release Pipeline Setup

Manual steps required before the release workflow will work.

## 1. PyPI — OIDC Trusted Publisher

The `publish-pypi` job uses [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) (no API tokens needed). Set this up once:

1. Go to https://pypi.org/manage/account/publishing/
2. If the `layr8` package doesn't exist yet, use "Add a new pending publisher"
3. Fill in:
   - **PyPI project name:** `layr8`
   - **Owner:** `layr8`
   - **Repository:** `python-sdk`
   - **Workflow name:** `release.yaml`
   - **Environment name:** `pypi`
4. Save

## 2. GitHub Environment

The workflow references an `environment: pypi` for the publish job. This adds a manual approval gate and scopes the OIDC token.

1. Go to https://github.com/layr8/python-sdk/settings/environments
2. Create environment named **`pypi`**
3. (Optional) Add required reviewers if you want manual approval before PyPI publish
4. (Optional) Restrict to the `main` branch under "Deployment branches"

## 3. GitHub Actions Permissions

The workflow needs `id-token: write` (for PyPI OIDC) and `packages: write` (for ghcr.io). Verify:

1. Go to https://github.com/layr8/python-sdk/settings/actions
2. Under "Workflow permissions," ensure **"Read and write permissions"** is selected
3. Ensure **"Allow GitHub Actions to create and approve pull requests"** is checked (needed for the compat-gate job to call the reusable workflow)

## 4. ghcr.io Package Visibility

After the first compat image push, the package may be created as private by default:

1. Go to https://github.com/orgs/layr8/packages
2. Find `python-sdk/compat`
3. Go to Package Settings → Danger Zone → Change visibility to **Public** (so the compat-suite orchestrator can pull it without auth)

The `LABEL org.opencontainers.image.source=https://github.com/layr8/python-sdk` in the Dockerfile auto-links the package to this repo, so `GITHUB_TOKEN` has push access.

## 5. Compat-suite Gate Access

The `compat-gate` job calls `layr8/compat-suite/.github/workflows/gate.yml@main` as a reusable workflow. This requires:

1. The `gate.yml` workflow in `layr8/compat-suite` must have `on: workflow_call` (already does)
2. The compat-suite repo must allow workflow calls from `layr8/python-sdk`:
   - Go to https://github.com/layr8/compat-suite/settings/actions
   - Under "Access," select **"Accessible from repositories in the 'layr8' organization"**

## Release Checklist

When you're ready to cut a release:

1. Bump `version` in `pyproject.toml` (e.g., `"0.2.0"`)
2. Merge the version bump PR to `main`
3. Create a GitHub Release:
   - Tag: `v0.2.0` (must match pyproject.toml)
   - Target: `main`
   - Title: `v0.2.0`
   - Write release notes
4. The release workflow runs automatically:
   - Tests + compat unit tests
   - Version validation (tag vs pyproject.toml)
   - PyPI publish
   - Compat image build + push to ghcr.io
   - Compat-suite gate (informational)
5. If the gate fails, investigate but note the SDK is already published
