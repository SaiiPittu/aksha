# ADR-P12: Sandbox and execution environment (the gate decides whether a program runs, the sandbox decides what it can reach, and a workspace is an artifact version, never a folder the box keeps)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. No entry in the decisions log settles sandboxing; v2's ADR-022 is also Proposed. This ADR carries ADR-022's lean forward in industry terms and adds one decision the contrast coding agent forces: how a workspace persists across hundreds of exec calls without the box keeping state. Amended 25 Nov 2026 after the three-agent stress test: S1 to S4 below are new and Proposed. Amended 26 Nov 2026: decision 29 (Accepted) places the sandbox in tier 3, the execution tier, which holds no credentials and no state; T1 to T6 below write that out, and the parts not named in decision 29 stay Proposed.

## Terms

Primitive PR-12 in the primitives catalogue, cluster C (acting safely). Depends on PR-04 (the workspace is built from artifact versions), PR-10 (the `exec` effect class) and PR-11 (the gate runs first). Needed by no other primitive in the core; it is what lets an agent that runs programs exist at all.

Terms used here, in the industry names of the design glossary:

- **Sandbox** is an operating-system boundary around a program the agent runs, limiting the files it can read and write, the network it can reach and the resources it can use.
- **Sandbox backend** is one implementation of the `Sandbox` interface, with a **strength**: `none`, `os-jail` (bubblewrap on Linux), `container` (Docker) or `vm`. The `refuse` backend runs nothing and answers `sandbox_unavailable`.
- **Execution environment** is what the program sees inside the box: a fresh `/work` folder built from input artifact versions, an allowlisted read-only root, a cleared environment and resource limits.
- **Effective sandbox policy** is the field-wise intersection of the run ceiling, the agent's policy, the Task's policy and the tool's declared needs. It can only narrow.
- **Workspace tree** is an artifact whose content is a content-addressed file tree (a manifest of paths to content hashes). It is how a coding agent's repository persists between exec calls.
- **Kill boundary** is the unit the host can terminate, so a forced stop is recorded as an uncertain outcome rather than lost.

## Context: a harness built only for document agents would skip this, and the coding agent cannot exist without it

Neither example agent runs code, so the product requirements' safeguards are met by the policy gate alone. A general harness meets agents that do run code, and a sandbox fails in three common ways.

1. **Silent fallback to an unconfined run.** A local backend that runs without OS-level confinement, or a sandbox that is off until someone enables it, means the default is no sandbox at all.
2. **A wall on files but not the network.** A profile can mount `/` read-only with no network flag at all, so network is outside its vocabulary. "Uses bubblewrap" says nothing about egress.
3. **A read-only host root.** Mounting the host's `/` read-only still lets a script read `~/.ssh`, the event store file and, in Aksha's test setup, the answer keys. A sandbox can work perfectly and still let the data out.

The coding agent adds a fourth problem v2 did not solve. ADR-022's lean (SB-7) is one fresh workspace per exec, built from artifacts, which keeps "a crash loses nothing" true. A coding agent edits one repository across hundreds of turns: `write_file`, then `shell("pytest")`, then another edit. If every exec starts from inputs that do not include the previous edits, the tests run against stale code. If the box keeps a folder between calls, the folder is state outside the event store, and a crash in the middle of an edit leaves it half-written.

## Decision

1. **The gate first, the sandbox second, never instead.** An `exec` call passes every layer of the policy gate (ADR-P11). The sandbox then limits what the allowed program can reach. The sandbox never decides whether the agent may run a program, and the gate never relies on the sandbox to stop a call it should have refused.
2. **No unconfined fallback, anywhere.** The backend is chosen once at run start from side-effect-free probes. If no available backend meets a tool's `min_strength`, the gate refuses `sandbox_unavailable` and nothing spawns. `refuse` is always available. There is no `danger-full-access` mode, no model-chosen flag and no fallback to a plain subprocess; widening is an agent definition change, caught by the definition hash.
3. **The box reads an allowlisted root, not the host's `/`.** System paths needed to run the interpreter, plus `/work`, and nothing else (SB-6 lean A). This is the one place Aksha can be stricter than a typical local sandbox at little cost, because every input arrives as an artifact.
4. **Network off on every shipped backend.** The policy type reserves `allowlist` so the schema never changes, and every shipped backend refuses it at probe time (SB-4 lean A). An effect on the outside world, such as `git push`, is an `external` tool outside the box that goes through the transactional outbox (ADR-P10 decision 2).
5. **The workspace is an artifact version, and the box keeps nothing (new).** A coding agent's repository is a workspace tree artifact. Each exec materialises one tree version into a fresh `/work`, runs, and, if the tool declares the tree writable, collects `/work` back into a new tree version whose lineage records the old tree and the exec's other inputs. The step executor saves the new version on the runtime thread, in the same transaction as `step.completed`. A crash mid-exec leaves no new version, so the rerun starts from the same tree and produces the same result (`rerun` recovery, fresh box). `write_file` is a `store` tool that writes a new tree version directly, with no box. This keeps SB-7's "one fresh workspace per exec" while giving the coding agent continuity, and it means invalidation (PR-05) and review diffs (the design patterns note P-4) work on code exactly as they work on documents.
6. **Materialisation is cached by content hash, below the semantics.** Copying a whole repository for every `pytest` is slow. A materialisation cache keyed by tree hash keeps a read-only copy per tree version and gives each exec a copy-on-write overlay (overlayfs or hardlinks, chosen by the backend). The cache is a performance detail the backend owns; deleting it changes nothing a run can observe. Later, not the first milestone.
7. **One wrapped command, a cleared environment, hard limits, killed at the deadline.** An argv, never a shell string assembled by the runtime (a `shell` tool passes its command as one argv to `sh -c` inside the box, which is the tool's choice and is confined). The environment is built from a fixed base and never inherited, so no key reaches the box. Wall time, CPU, memory, processes, open files and output size are limited (sandboxing 4.1: 30 seconds, 512 MB, 64 processes). At the deadline the process group is killed, which is the kill boundary.
8. **Outputs come back as data.** Declared outputs under `/work/out` (or the whole tree for a writable workspace) are collected symlink-safe and size-capped, and returned as staged save requests; a blocked write or connect is a structured denial in the result, which the model reads as data. Exec stdout passes the same instruction-text flags as any tool result.
9. **The record makes an exec inspectable.** `step.completed` for an exec holds the backend, its version, the effective policy digest, the input tree and artifact hashes, the exit status, the denials and the output hashes. The backend chosen at run start and every probe result ride on `run.started`, so the sandbox mode is part of the record.
10. **The interface lives in the runtime core; backends do not.** The `Sandbox` protocol, the policy types and `refuse` sit in `aksha/runtime/sandbox.py`, because the gate and the step executor call them. `bwrap`, `docker` and the conformance suite live in `aksha/sandbox/`, an extension package the runtime core never imports and a run without exec tools never loads; each backend is imported inside its own branch.
11. **An exec timeout is not retried.** A program that looped once will loop again (my reading); a retry only spends budget (ADR-P14).

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, an execution environment image. With a fresh `/work` per exec, an allowlisted read-only root and the network off, `import fastapi` fails: nothing gives the box the project's dependencies. That is a missing primitive, designed in ADR-P28. `SandboxNeeds` gains `environment: str | None`, the name of an `EnvironmentSpec`, mounted read-only at `/env`.
2. S2, nested tree manifests. `TreeManifest` is a flat path-to-hash map. For a 5,000-file repository the coding review estimates about 550 KB per version and about 220 MB over 400 versions, against about 13 KB and about 5 MB for nested tree objects per folder, as Git stores them (all estimates). The manifest becomes nested: a version points at a root tree object, and a one-file edit writes only the tree objects on its path.
3. S3, `protected` is a default an agent may clear for its own tool. `.git` stays read-only under a writable path by default; an agent that runs git inside the box may clear it for that one exec tool, and the loader records the choice in the permission matrix.
4. S4, the workspace tree kind is the sandbox extension's, not the core's, matching ADR-P04 D10 and S5.

### Amendment of 26 Nov 2026: the sandbox is tier 3 (decision 29)

The [architecture](../02-architecture.md#3-three-tiers-state-runtime-and-execution) splits Aksha into three tiers: state (the event store, the blob store and the outbox), the runtime, and execution. This ADR is the whole of tier 3, apart from the `Sandbox` port and the `refuse` backend, which stay in the runtime because the gate and the step executor call them (decision 10 above).

1. T1, worker and box (Accepted with decision 29). A **sandbox worker** is a host-side process that provisions boxes, enforces limits and kills at the deadline; a **box** is one confined execution of one program. The worker is trusted to enforce limits, and the box is assumed hostile.
2. T2, tier 3 holds no credentials and no durable state (Accepted with decision 29). The box's environment is built from a fixed base (decision 7 above), so no provider key, receiver credential or store connection is ever inside it, and the workspace it ran on is an artifact version (decision 5 above), so a crash in tier 3 loses nothing. A worker's own identity for its port connection, such as mutual TLS on a remote pool, is not a credential to the store or to an outside system, and the box never sees it. Proved by `test_exec_env_has_no_key` against the first real backend.
3. T3, tier 3 only answers (Accepted with decision 29). The `Sandbox` port is request and response only: the runtime sends argv, a workspace tree manifest, the effective policy and the limits, and the worker returns outputs, exit status and denials. Only the runtime writes the event store or the blob store; outputs always come back through the step executor, which is the only caller of the port. The conformance suite asserts that a connect from the box to the store fails.
4. T4, a remote backend joins the local ones (Proposed). A backend of strength `vm`, a pool of microVMs reached over the network, implements the same protocol. The materialisation cache (decision 6 above) then lives on the worker host, keyed by blob hash over nested manifests (S2), so a test run after a six-file edit sends six files, not the whole tree. How blobs reach a remote box is open: the runtime streams them with the request (my lean, which keeps T2 literal), or it hands the worker short-lived read-only capabilities per hash.
5. T5, untrusted parsing and external merge programs run in tier 3 (Proposed). Agent tool bodies are trusted Python and run in the runtime (ADR-P20); the sandbox is for the programs an agent runs. A tool that parses hostile third-party bytes, such as a fetched page or a third-party PDF, would otherwise run that parser next to every key. The proposed split: a fetch tool in the runtime stores the raw bytes as a blob, and an `exec` parse tool reads that blob in a box with the network off and returns text. A `ToolSpec` flag, `parses_untrusted`, lets the loader refuse a tool that declares untrusted parsing but is not dispatched to tier 3. A merge that needs an external program, such as a three-way file merge, runs as an exec step for the same reason.
6. T6, the kill boundary belongs to the worker (Proposed, with ADR-P18). On the runtime's instruction the worker kills the box's process group, so a stop during a long exec does not wait for the program to finish.

Kept out of v1: long-lived process sessions (a dev server or debugger across calls). Within one exec a script can still start a server on loopback, query it and stop it. A fresh process per command replaces a persistent shell.

### Amendments from the build (26 Nov 2026, Proposed)

Built on 26 Nov 2026: the port, the `refuse` backend, selection, the effective policy, the exec API with its kill boundary, the bubblewrap backend with a conformance suite, and the records. Where the build had to choose, it took this ADR's lean, and where this ADR is silent, the leans of the workspace strategy note (WS-1 to WS-15), which are recommendations still awaiting the maintainer.

1. A1, the port is one call (decision 10, T3). `Sandbox.run(request, blobs, unit) -> BoxResult` replaces `open`, `materialise`, `wrap`, `collect` and `parse_denials`, which are the backend's own steps. `BoxRequest` is data only: argv, the effective policy with its limits, inline files, a workspace tree manifest, an environment mount and stdin. The backend reads tree blobs through the reader it is given (T4's lean: the runtime streams them) and attaches the running box to `unit`. `version()` joins `probe()`. `SandboxPolicy.digest()` and `TreeManifest.digest()` are named `hash_ref()`, because the runtime's term check keeps an example agent's word out of the core.
2. A2, where the backends live (decision 10). The bubblewrap backend is `aksha/adapters/sandbox/bwrap.py`, beside the other adapters, not an `aksha/sandbox/` extension package; the runtime core still never imports it, and only `app.py` chooses it. The port, `refuse`, `select_backend` and `effective_policy` are in `aksha/runtime/sandbox/`.
3. A3, `AKSHA_SANDBOX` (decision 2). `bwrap` (or `bwrap:os-jail`), `auto` and `refuse` choose a built-in backend, which is probed once per process; a built-in backend that does not probe available becomes `refuse` carrying the probe's reason, never a weaker box. `<name>:<strength>` with any other name declares an outer box the operator attests the process runs in: it satisfies the headless start (ADR-P11 D12, replacing B7's declaration for built-in names) and runs no program, so an exec tool is still refused `sandbox_unavailable`. Unset, no backend is chosen; `auto` is not the default, so a headless start stays the operator's explicit choice. The choice, every probe, the backend version, the run ceiling's hash (`ceiling_hash`) and the network setting ride on `run.started` under `sandbox` (decision 9).
4. A4, where the limits live and how they are enforced (decision 7). Limits are declared beside the policy, not in it: `SandboxNeeds.limits` and the run ceiling carry them, where the sketch had `SandboxPolicy.limits`, so a declared `SandboxPolicy()` keeps its meaning and the extension API change is minor (2.4); `ExecPolicy` joins the narrowed policy and the limits for one box. `Limits` holds `wall_ms`, `cpu_s`, `memory_mb`, `max_processes`, `max_open_files`, `max_output_bytes` (stdout and stderr each), `max_file_bytes` and `max_collect_bytes`, defaulting to 30 s, 512 MB and 64 processes. bubblewrap applies them with `prlimit` inside the box, so the process limit counts the box's own user namespace, not every process of the host user; the wall time is the host's, and at the deadline the box's process group is killed, with `--die-with-parent` and the PID namespace taking every child. `/tmp` is a tmpfs capped at `max_collect_bytes` on bubblewrap 0.10 or later. The backend probes unavailable without `prlimit`.
5. A5, what narrowing means (decision 2). A narrower layer's writable paths must each lie under one of the wider layer's (`.` is the whole tree), host reads must be a subset, and the network cannot go from `none` to `allowlist`; any of these raises `PolicyWidened`. Protected paths add up. Each limit takes the smaller of the ceiling's and the tool's rather than raising. A writable tree needs `.` writable at every layer.
6. A6, the layers that exist. `effective_policy(ceiling, agent, task, needs)` takes all four, but no agent or Task declares a sandbox policy yet, so the loop passes the run ceiling (`RUN_CEILING`: `.` writable, network off, default limits) and the tool's needs. A widening refuses every run of the attempt (`policy_widened`) before anything spawns.
7. A7, the exec API (decision 8). `ToolContext.exec` is a `BoxedExec` per call attempt; `run(argv, files, tree=, stdin=)` never raises and returns an `ExecOutcome`: status (`ok`, `exec_failed`, `exec_timeout`, `exec_limit`, `output_rejected`, or `refused` with a code: `sandbox_unavailable`, `network_unsupported`, `policy_widened`, `run_stopped`, `exec_invalid`, `backend_error`, or an environment's refusal), exit code, decoded stdout and stderr, denials, the declared outputs (all of `out` by default) and, for a writable tree, the new `TreeManifest`, whose blobs the runtime writes. The body stages the save of its own tree kind (S4: the runtime has no tree kind). The manifest is flat; S2's nested tree objects are not built.
8. A8, denials. A write, connect, process or limit denial is read from stderr by pattern; a skipped symlink and an over-cap collection come from the collector. They are data, never exceptions.
9. A9, the exec step is its own kill boundary (T6, decision 11). The loop gives an exec tool's step an `ExecUnit`, the step executor's `unit` (ADR-P18 D6): a stop or the tool's timeout ends the running box's process group, later runs in the attempt are refused `run_stopped`, and a step with a unit is never retried.
10. A10, the exec record (decision 9). Each run appends to `step.completed`'s `value["exec"]`: invocation, argv, backend, strength, version, the effective policy's hash (`policy_hash`), input file hashes, the input tree's hash, the environment (key, and the build or hit record of ADR-P28), status, exit code, denials, output hashes and the new tree's hash. No new event types.
11. A11, the workspace strategy leans taken as defaults (recommendations awaiting the maintainer). WS-1 A: a workspace is a tree version and the box keeps nothing (built: the box's folder is deleted after every run). WS-2: one record per tool step. WS-6: SHA-256 is the record (`TreeManifest`). WS-9: tier 3 holds no credentials and no durable state (the box's environment is built from a fixed base; proved by `test_exec_env_has_no_key`). WS-10: no memory snapshots. WS-14: environment pre-approval by policy (ADR-P28 A4). WS-15: Aksha-native exec tools only. Not built, and taken as the plan when they are: WS-3 notes as tracked paths, WS-4 and WS-5 cache paths per environment spec with clean runs for completion checks (no cache exists yet, so every run is clean), WS-7 rewind as a forward save, WS-8 A interactive sessions through a mirrored checkout, WS-11 the warm lease as a later capability, WS-12 a `.git` synthesised at materialise, WS-13 repository import split across tiers.
12. A12, S3 (the open lean). `protected` has no runtime default (ADR-P11 B6); an exec tool declares its own protected paths in its needs, which is clearing per tool. Recording the choice in the permission matrix is not built.
13. A13, decision 7: box-wide budgets are sampled, not kernel-enforced. `prlimit` limits each process, so 64 processes could each hold `memory_mb`, and the writable paths are host folders that bubblewrap's `--size` (which caps only `/tmp`) does not bound. An unprivileged host has no cgroup or filesystem quota to lean on, so a host-side watcher samples the box every 20 ms. It sums the private memory (`RssAnon`) of every process under bubblewrap against `memory_mb` (`exec_limit`), and the bytes and entries under the writable paths against `max_collect_bytes` and 10,000 entries (`output_rejected`). On a breach it kills the box. Between two samples a box can go over by what it allocates or writes in one interval, and each file is still capped at `max_file_bytes` and each process at `memory_mb`. A cgroup-backed limit, where the host delegates one, is the stronger option and is not built.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. No OS sandbox; a kill boundary only | Tools run in a unit the host can terminate; forced termination recorded as uncertainty | No platform dependence | Nothing stops a script reading the store or calling out | Rejected as the wall; the kill boundary is kept (decision 7) |
| B. bubblewrap plus seccomp, host root read-only | The common Linux pattern | Proven, fast to start | Host root readable; Aksha's answer keys and store would be visible | Taken with an allowlisted root instead (decision 3) |
| C. Proxy egress with credential sentinels | Network on through one proxy socket; real keys swapped in on the way out | Lets code call approved services | Needs TLS termination and a per-domain review; the coding agent's only external effect is a push, which belongs in the outbox | Rejected for v1; `allowlist` reserved in the type |
| D. A persistent workspace per Task, with snapshot and restore | The box keeps a folder; the SDK persists and hydrates it | Fast; matches how a developer works | Box state lives outside the event store; a crash mid-edit leaves a half-written folder; lineage and invalidation cannot see edits | Rejected; its speed comes back through the cache of decision 6 |
| E. Workspace as a content-addressed tree artifact, fresh box per exec (my lean) | Decision 5 | Crash-safe, inspectable, diffable; reuses PR-04 and PR-05 unchanged | Large trees cost storage; needs the cache to be fast | Chosen |
| F. Hosted sandboxes only | A provider runs the box | Strong isolation | Paid and networked; the product requirements require the whole project to run offline | Later, behind the same interface (SB-5) |

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
