# ADR-P12: Sandbox and execution environment (the gate decides whether a program runs, the sandbox decides what it can reach, and a workspace is an artifact version, never a folder the box keeps)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. No entry in the decisions log settles sandboxing; v2's ADR-022 is also Proposed. This ADR carries ADR-022's lean forward in industry terms and adds one decision the contrast coding agent forces: how a workspace persists across hundreds of exec calls without the box keeping state. Amended 25 Nov 2026 after the three-agent stress test: S1 to S4 below are new and Proposed.

## Terms

Primitive PR-12 in the primitives catalogue, cluster C (acting safely). Depends on PR-04 (the workspace is built from artifact versions), PR-10 (the `exec` effect class) and PR-11 (the gate runs first). Needed by no other primitive in the core; it is what lets an agent that runs programs exist at all.

Terms used here, in the industry names of the design glossary:

- **Sandbox** is an operating-system boundary around a program the agent runs, limiting the files it can read and write, the network it can reach and the resources it can use.
- **Sandbox backend** is one implementation of the `Sandbox` interface, with a **strength**: `none`, `os-jail` (bubblewrap on Linux), `container` (Docker) or `vm`. The `refuse` backend runs nothing and answers `sandbox_unavailable`.
- **Execution environment** is what the program sees inside the box: a fresh `/work` folder built from input artifact versions, an allowlisted read-only root, a cleared environment and resource limits.
- **Effective sandbox policy** is the field-wise intersection of the run ceiling, the agent's policy, the Task's policy and the tool's declared needs. It can only narrow.
- **Workspace tree** is an artifact whose content is a content-addressed file tree (a manifest of paths to content hashes). It is how a coding agent's repository persists between exec calls.
- **Kill boundary** is the unit the host can terminate, so a forced stop is recorded as an uncertain outcome rather than lost (omp2's ADR 0011).

## Context: a harness built only for document agents would skip this, and the coding agent cannot exist without it

Neither example agent runs code, so the product requirements' safeguards are met by the policy gate alone. A general harness meets agents that do run code, and the comparison found three ways a sandbox fails.

1. **Silent fallback to an unconfined run.** The OpenAI Agents SDK's local backend runs "without OS-level confinement" on Linux (`oai:sandbox/sandboxes/unix_local.py:262-273`); several harnesses default to no sandbox (Claude Code's `srt` is off until enabled; Gemini CLI has none unless set; sandboxing note section 2).
2. **A wall on files but not the network.** The DeepSeek harness mounts `/` read-only with no network flag, and its own type says network is outside its vocabulary (`sandbox-local/src/profiles.ts:16-36`, `sandbox/src/index.ts:32-38` at `39163dd30f`). "Uses bubblewrap" says nothing about egress.
3. **A read-only host root.** Codex, `srt`, Gemini CLI and DeepSeek all mount the host's `/` read-only, so a script can read `~/.ssh`, the event store file and, in Aksha's test setup, the answer keys. Anthropic's own write-up reports "the sandbox worked perfectly, and yet the data was exfiltrated" (Anthropic Engineering, 25 May 2026, via the landscape note).

The coding agent adds a fourth problem v2 did not solve. ADR-022's lean (SB-7) is one fresh workspace per exec, built from artifacts, which keeps "a crash loses nothing" true. A coding agent edits one repository across hundreds of turns: `write_file`, then `shell("pytest")`, then another edit. If every exec starts from inputs that do not include the previous edits, the tests run against stale code. If the box keeps a folder between calls, the folder is state outside the event store, and a crash in the middle of an edit leaves it half-written.

## Decision

1. **The gate first, the sandbox second, never instead.** An `exec` call passes every layer of the policy gate (ADR-P11). The sandbox then limits what the allowed program can reach. The sandbox never decides whether the agent may run a program, and the gate never relies on the sandbox to stop a call it should have refused.
2. **No unconfined fallback, anywhere.** The backend is chosen once at run start from side-effect-free probes. If no available backend meets a tool's `min_strength`, the gate refuses `sandbox_unavailable` and nothing spawns. `refuse` is always available. There is no `danger-full-access` mode, no model-chosen flag and no fallback to a plain subprocess; widening is an agent definition change, caught by the definition hash.
3. **The box reads an allowlisted root, not the host's `/`.** System paths needed to run the interpreter, plus `/work`, and nothing else (SB-6 lean A). This is the one place Aksha can be stricter than every compared local sandbox at little cost, because every input arrives as an artifact.
4. **Network off on every shipped backend.** The policy type reserves `allowlist` so the schema never changes, and every shipped backend refuses it at probe time (SB-4 lean A). An effect on the outside world, such as `git push`, is an `external` tool outside the box that goes through the transactional outbox (ADR-P10 decision 2).
5. **The workspace is an artifact version, and the box keeps nothing (new).** A coding agent's repository is a workspace tree artifact. Each exec materialises one tree version into a fresh `/work`, runs, and, if the tool declares the tree writable, collects `/work` back into a new tree version whose lineage records the old tree and the exec's other inputs. The step executor saves the new version on the runtime thread, in the same transaction as `step.completed`. A crash mid-exec leaves no new version, so the rerun starts from the same tree and produces the same result (`rerun` recovery, fresh box). `write_file` is a `store` tool that writes a new tree version directly, with no box. This keeps SB-7's "one fresh workspace per exec" while giving the coding agent continuity, and it means invalidation (PR-05) and review diffs (the design patterns note P-4) work on code exactly as they work on documents.
6. **Materialisation is cached by content hash, below the semantics.** Copying a whole repository for every `pytest` is slow. A materialisation cache keyed by tree hash keeps a read-only copy per tree version and gives each exec a copy-on-write overlay (overlayfs or hardlinks, chosen by the backend). The cache is a performance detail the backend owns; deleting it changes nothing a run can observe. Later, not the first milestone.
7. **One wrapped command, a cleared environment, hard limits, killed at the deadline.** An argv, never a shell string assembled by the runtime (a `shell` tool passes its command as one argv to `sh -c` inside the box, which is the tool's choice and is confined). The environment is built from a fixed base and never inherited, so no key reaches the box. Wall time, CPU, memory, processes, open files and output size are limited (sandboxing 4.1: 30 seconds, 512 MB, 64 processes). At the deadline the process group is killed, which is the kill boundary.
8. **Outputs come back as data.** Declared outputs under `/work/out` (or the whole tree for a writable workspace) are collected symlink-safe and size-capped, and returned as staged save requests; a blocked write or connect is a structured denial in the result, which the model reads as data. Exec stdout passes the same instruction-text flags as any tool result.
9. **The record makes an exec inspectable.** `step.completed` for an exec holds the backend, its version, the effective policy digest, the input tree and artifact hashes, the exit status, the denials and the output hashes. The backend chosen at run start and every probe result ride on `run.started`, as the DeepSeek harness records its sandbox mode as a log event (`sandbox-policy/src/session-mode.ts:27-54`).
10. **The interface lives in the runtime core; backends do not.** The `Sandbox` protocol, the policy types and `refuse` sit in `aksha/runtime/sandbox.py`, because the gate and the step executor call them. `bwrap`, `docker` and the conformance suite live in `aksha/sandbox/`, an extension package the runtime core never imports and a run without exec tools never loads (Decode imports each backend inside its own branch, `decode:sandbox/__init__.py:23`).
11. **An exec timeout is not retried.** A program that looped once will loop again (my reading); a retry only spends budget (ADR-P14).

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, an execution environment image. With a fresh `/work` per exec, an allowlisted read-only root and the network off, `import fastapi` fails: nothing gives the box the project's dependencies. That is a missing primitive, designed in ADR-P28. `SandboxNeeds` gains `environment: str | None`, the name of an `EnvironmentSpec`, mounted read-only at `/env`.
2. S2, nested tree manifests. `TreeManifest` is a flat path-to-hash map. For a 5,000-file repository the coding review estimates about 550 KB per version and about 220 MB over 400 versions, against about 13 KB and about 5 MB for nested tree objects per folder, as Git stores them (all estimates). The manifest becomes nested: a version points at a root tree object, and a one-file edit writes only the tree objects on its path.
3. S3, `protected` is a default an agent may clear for its own tool. `.git` stays read-only under a writable path by default; an agent that runs git inside the box may clear it for that one exec tool, and the loader records the choice in the permission matrix.
4. S4, the workspace tree kind is the sandbox extension's, not the core's, matching ADR-P04 D10 and S5.

Kept out of v1: long-lived process sessions (a dev server or debugger across calls). Within one exec a script can still start a server on loopback, query it and stop it. Decode made the same call in its ADR-0012, replacing a persistent shell with a fresh process per command (as recorded in the maintainer's coding-agent course notes).

## Alternatives considered

| Option | What it is | Prior art | For | Against | Verdict |
|---|---|---|---|---|---|
| A. No OS sandbox; a kill boundary only | Tools run in a unit the host can terminate; forced termination recorded as uncertainty | omp2, `docs/adr/0011-cancellation-needs-a-kill-boundary.md:29-42` at `2f92f3b5aa` | No platform dependence | Nothing stops a script reading the store or calling out | Rejected as the wall; the kill boundary is kept (decision 7) |
| B. bubblewrap plus seccomp, host root read-only | The common Linux pattern | Codex (`linux-sandbox/src/bwrap.rs:266-290`, `:388-404`; Seatbelt at `seatbelt.rs:101-105`, at `1263a383`); Gemini CLI's `--unshare-all` with an 8-instruction seccomp program (`bwrapArgsBuilder.ts:45-65`, `LinuxSandboxManager.ts:72-88` at `62bab5b`) | Proven, fast to start | Host root readable; Aksha's answer keys and store would be visible | Taken with an allowlisted root instead (decision 3) |
| C. Proxy egress with credential sentinels | Network on through one proxy socket; real keys swapped in on the way out | Claude Code's `srt` (`credential-sentinel.ts:2-8`, `README.md:122` at `0bbebed`) | Lets code call approved services | Needs TLS termination and a per-domain review; the coding agent's only external effect is a push, which belongs in the outbox | Rejected for v1; `allowlist` reserved in the type |
| D. A persistent workspace per Task, with snapshot and restore | The box keeps a folder; the SDK persists and hydrates it | OpenAI Agents SDK `persist_workspace` and `hydrate_workspace` (`base_sandbox_session.py:1156-1166` at `6c4e4a2`); Decode's bind-mounted workspace with `WorkspaceEscape` on a symlink out (`src/decode/sandbox/executor.py:45`) | Fast; matches how a developer works | Box state lives outside the event store; a crash mid-edit leaves a half-written folder; lineage and invalidation cannot see edits | Rejected; its speed comes back through the cache of decision 6 |
| E. Workspace as a content-addressed tree artifact, fresh box per exec (my lean) | Decision 5 | Git's object model (general usage, not in our notes); Exo keeps configuration as versioned JSON artifacts (`crates/executor/src/harness_config.rs:11-44`, from the Exo lessons note) | Crash-safe, inspectable, diffable; reuses PR-04 and PR-05 unchanged | Large trees cost storage; needs the cache to be fast | Chosen |
| F. Hosted sandboxes only | A provider runs the box | OpenAI Agents SDK hosted backends (landscape note section 5) | Strong isolation | Paid and networked; the product requirements require the whole project to run offline | Later, behind the same interface (SB-5) |

## Interface

```python
# aksha/runtime/sandbox.py (proposal; design only). Full policy types: sandboxing 4.1.
Strength = Literal["none", "os-jail", "container", "vm"]       # ordered

@dataclass(frozen=True)
class SandboxPolicy:
    writable: tuple[str, ...] = ("out", "tmp")                  # relative to /work
    protected: tuple[str, ...] = (".git",)                      # read-only even under a writable path
    host_read: tuple[str, ...] = ()                             # must be empty in v1 (decision 3)
    network: Literal["none", "allowlist"] = "none"              # every shipped backend refuses "allowlist"
    limits: "Limits" = ...                                      # wall_ms, cpu_s, memory_mb, max_processes, ...
    def narrow(self, other: "SandboxPolicy") -> "SandboxPolicy": ...   # raises PolicyWidened
    def digest(self) -> str: ...

@dataclass(frozen=True)
class SandboxNeeds:                                             # ToolSpec.sandbox, exec tools only
    min_strength: Strength = "os-jail"
    policy: SandboxPolicy = SandboxPolicy()
    tree_input: str | None = None                               # argument naming a workspace tree ref
    tree_writable: bool = False                                 # collect /work back as a new tree version
    inputs: tuple[str, ...] = ()                                # arguments carrying other artifact refs
    outputs: tuple["OutputSpec", ...] = ()

class Sandbox(Protocol):
    name: str                                                   # "refuse" | "bwrap" | "docker"
    strength: Strength
    def probe(self) -> "Availability": ...                      # side-effect free, cached per process
    def open(self, policy: SandboxPolicy, invocation_id: str) -> "Workspace": ...
    def materialise(self, ws: "Workspace", tree: "TreeManifest", blobs: "BlobReader") -> None: ...
    def wrap(self, req: "ExecRequest", policy: SandboxPolicy, ws: "Workspace") -> "Command": ...
    def collect(self, ws: "Workspace", needs: SandboxNeeds) -> "Collected": ...   # symlink-safe, capped
    def parse_denials(self, raw: "RawResult") -> tuple["Denial", ...]: ...

class ExecAPI(Protocol):                                        # ToolContext.exec, exec tools only
    def run(self, argv: Sequence[str], inputs: Mapping[str, str], stdin: bytes = b"") -> "ExecOutcome": ...

@dataclass(frozen=True)
class ExecOutcome:
    status: Literal["ok", "exec_failed", "exec_timeout", "exec_limit", "output_rejected"]
    exit_code: int | None
    stdout: str                                                 # decoded, capped, flagged like any result
    stderr: str
    denials: tuple["Denial", ...]
    staged: tuple["SaveRequest", ...]                           # new tree version and declared outputs
    record: "ExecRecord"                                        # backend, version, policy digest, hashes

def effective_policy(ceiling: SandboxPolicy, agent: SandboxPolicy | None,
                     task: SandboxPolicy | None, needs: SandboxNeeds) -> SandboxPolicy: ...
def select_backend(choice: Literal["auto", "refuse", "bwrap", "docker"],
                   available: Mapping[str, Callable[[], Sandbox]]) -> tuple[Sandbox, tuple["Availability", ...]]: ...

# Workspace tree: an artifact kind the sandbox extension declares (S4, ADR-P04 S5); the core keeps blobs.
@dataclass(frozen=True)
class TreeManifest:
    entries: Mapping[str, str]                                  # path -> content hash of the blob
    def digest(self) -> str: ...
```

Events it writes: probe results and the chosen backend on `run.started`; the exec record on `step.completed`; the new tree version as an ordinary `artifact.saved` with lineage to the old tree. No new event types.

Projections it reads: The current version of the workspace tree artifact (through the artifact store, PR-04) and the run ceiling from `run.started`.

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Fits | The agent runs no programs | Nothing new: The sandbox extension is never imported |
| Patent | Fits | No exec tool, so no backend loads | Nothing new |
| Coding | Missing primitive | `run_tests` in a fresh `/work` with the network off cannot import the project's dependencies | S1 points at ADR-P28; S2 nested manifests; S3 a clearable `protected` default; S4 the tree kind's owner |

Verdict after the amendments: fits for the two example agents; the coding agent needs ADR-P28 and S2 before it can run a test. All of it is later.

Open for the maintainer: S3 (lean: allow clearing per exec tool, recorded in the permission matrix).

## Sources (public)

1. Max McGuinness and others, "How we contain Claude across products", Anthropic Engineering, 25 May 2026 (page, via the landscape note).
