# Releasing Aksha

*Status on 26 Nov 2026: the package builds, passes `twine check --strict`, and its wheel installs and runs offline in a fresh environment (`make release-check`, and the `package` job in CI). Nothing has been published. The release workflow is ready but cannot publish until the maintainer does the one-time setup below.*

## What is published

| Item | Value |
|---|---|
| Distribution name | `aksha-harness`. The name `aksha` on PyPI belongs to an unrelated project, so `pip install aksha` and a bare `uvx aksha` fetch that project, not this one. |
| Import name and command | `aksha` (`import aksha`, `aksha --help`) |
| Version | from the git tag `vX.Y.Z`, through setuptools-scm. An untagged build is `0.1.devN+g<commit>`, a local version PyPI refuses, so only a tag can produce an uploadable version. |
| Python | 3.12 and newer |
| Extras | `server` (`aksha serve`), `otel` (`aksha trace`), `postgres` (the PostgreSQL event store), `ingest` (the document parsers), `live` (LiteLLM, for live models). Development tools are a dependency group (`dev`), not an extra. |
| Bundled data | the agents' prompts, and the synthetic worlds and eval suites under `src/aksha/data/`, so `aksha demo` and `aksha eval` work with no clone |
| Not in the sdist | `.claude/`, `.gemini/`, `.github/`, `scratch-pad/`, `web/`, `.coderabbit.yaml`, `AGENTS.md`, `CLAUDE.md`, `REVIEWING.md` (`MANIFEST.in`; `tests/package/test_wheel.py` fails if one appears) |

## Check a release locally

```sh
make release-check
```

This builds the sdist and the wheel into `dist/` (`uv build`), runs `twine check --strict` on both, then runs `tests/package/`: it installs the wheel into a fresh virtual environment in a temporary folder, outside the repository, and runs `aksha --help`, `aksha --version`, `aksha doctor`, `aksha demo` and one bundled eval case there, with a throwaway home and every proxy pointed at a closed port. It uploads nothing.

## One-time setup (maintainer only)

The workflow `.github/workflows/release.yml` cannot publish until these are done. Each is a decision or an account action for the maintainer; no agent and no script does them.

1. **Choose a license.** Add a `LICENSE` file at the repository root, and in `pyproject.toml` under `[project]` add `license = "<SPDX identifier>"` and `license-files = ["LICENSE"]`. Do not add a `License ::` classifier; the SPDX string replaces it. The release workflow's first step refuses to build without both.
2. **Register pending trusted publishers.** On PyPI (pypi.org, Account settings, Publishing, "Add a new pending publisher") and again on TestPyPI (test.pypi.org), add a GitHub publisher with:

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `aksha-harness` | `aksha-harness` |
   | Owner | `SaiiPittu` | `SaiiPittu` |
   | Repository name | `aksha` | `aksha` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `testpypi` |

   A pending publisher does not reserve the name; the first successful upload creates the project. No API token is created or stored.
3. **Create the two GitHub environments.** In the repository's Settings, Environments, create `testpypi` and `pypi`. On `pypi`, add yourself as a required reviewer. On both, limit deployment to tags matching `v*.*.*`.

## Cut a release

1. On `main`, with CI green, run `make release-check`.
2. Pick the version. The first release is `v0.1.0`, Development Status Alpha.
3. Tag and push the tag:

   ```sh
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

4. The workflow builds, checks that the built version equals the tag, smoke-tests the wheel, and publishes to TestPyPI.
5. Check the TestPyPI page and try the install from there:

   ```sh
   uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ --from aksha-harness aksha demo
   ```

6. Approve the `pypi` job in the workflow run. It publishes the same files to PyPI.

An upload to PyPI is public and permanent: a version number can never be reused, even after the release is deleted. If something is wrong after the upload, yank the release on PyPI and tag a new patch version.

## How the workflow is locked down

- It runs only on a pushed tag `vX.Y.Z`. There is no branch trigger and no `workflow_dispatch`, and a fork's tag skips every job.
- The workflow's default token is read-only. Only the two publish jobs get `id-token: write`, the permission trusted publishing needs; the build job, which runs the project's code, never has it.
- Every third-party action is pinned to a full commit SHA, with its version in a comment, as in `ci.yml`.
- The `pypi` environment's required reviewer means the final upload waits for the maintainer, even after the tag is pushed.
