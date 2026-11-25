# ADR-P28: Execution environment image (lean: a read-only, content-addressed layer of interpreter and dependencies, built once per lock-file hash by the one exec step allowed a network allowlist, and mounted at `/env` beside `/work`)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the coding stress test (the coding stress review, root cause 4 and missing primitive M-2). ADR-P12 is strict on purpose (a fresh `/work` per exec, an allowlisted read-only root, no network), which leaves a test run with no dependencies to import. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026, and none of it is first-milestone work.

## Terms

The terms:

- An **environment image** is a read-only, content-addressed layer (an interpreter plus installed dependencies) that the sandbox mounts at `/env`.
- An **environment key** is the SHA-256 over the base image digest, the build command and the blobs of the declared lock files.

## Context: what breaks without it

`run_tests` starts in a fresh `/work` with the network off and only interpreter paths readable, so `import fastapi` fails, and `uv sync` cannot reach a package index. The usual answers are to put dependencies in the box's image, or to install them per session in a long-lived box whose filesystem persists between calls. Aksha has no long-lived box, so dependencies need their own recorded, hashed input.

## Decision

1. D1, an agent or a sandbox extension declares an `EnvironmentSpec` (ADR-P20 S2). An exec tool names it in `SandboxNeeds.environment` (ADR-P12 S1).
2. D2, the image is an artifact. Its lineage is the lock files' blobs, read as path-scoped reads (ADR-P04 S3), so a lock-file edit invalidates it and an unrelated edit does not. Test reports read the image, so a dependency change re-runs the tests.
3. D3, one build step has the network. The build runs in its own box with only the lock files from `/work`, under an egress policy limited to the named package indexes (ADR-P26), and is the only exec step allowed any network.
4. D4, the first build per environment key needs approval. The policy gate treats the build as an `exec` tool with `approval` on its first build per key, because a package's install script runs with network access.
5. D5, images are cached by environment key; a hit skips the build and records the read.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. A long-lived box per session | Simple | State the event store cannot see; a crash loses it; reopens ADR-P12's fresh-box rule | Rejected |
| B. Network on for every exec | No new primitive | Every test run can exfiltrate | Rejected |
| C. A content-addressed image built once per lock hash (lean) | Recorded, hashed, cached; network on in one step only | A build step to design | Chosen |

## Interface

```python
# aksha/runtime/sandbox.py (proposal; design only)
@dataclass(frozen=True)
class EnvironmentSpec:
    name: str                                    # "python-project"
    base: str                                    # a pinned image or interpreter digest
    lock_inputs: tuple[str, ...]                 # tree paths that key the build: ("uv.lock", "pyproject.toml")
    build_argv: tuple[str, ...]                  # ("uv", "sync", "--frozen")
    egress: str                                  # an EgressPolicy naming the package indexes (ADR-P26)

def environment_key(spec: EnvironmentSpec, tree: "TreeManifest") -> str: ...   # pure
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits (unused) | No programs run |
| Patent | Fits (unused) | No programs run |
| Coding | Fits | `import fastapi` works from `/env`; a `uv.lock` edit rebuilds the image and re-runs tests |
