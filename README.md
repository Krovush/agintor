# Agintor

Agintor is a Python 3.12 research MVP for a bounded, auditable repository-repair
harness. The current repository-facing path is Harness V1: a typed factory,
source-hidden runtime bundle, deterministic replay solve path, evaluator-owned
proof path, and release/session/evidence boundaries.

This checkout does not make a capability claim. A staged integration smoke has
exercised the frozen OpenAI Responses adapter and the real Docker command
boundary, but the numerical G0, D0, S1, and P1 live experiments remain
`not_run`. Default tests remain offline.

## Public V1 CLI

The `agintor` console script points at `agintor.cli_v1:app`. The public V1
commands emit JSON wrappers on stdout.

```powershell
agintor build-runtime PROJECT_ROOT --request-json BUILD.json [--replay-manifest FACTORY_REPLAY.json]
agintor gate0-dry-run PROJECT_ROOT --request-json GATE0.json
agintor search-dry-run PROJECT_ROOT --request-json BUILD.json
agintor pilot-dry-run PROJECT_ROOT --request-json PILOT.json
agintor readiness-build CONTROLLED_ROOT --request-json READINESS_BUILD.json
agintor readiness-replay CONTROLLED_ROOT --request-json READINESS_REPLAY.json
agintor inspect PROJECT_ROOT
agintor solve PROJECT_ROOT --task-envelope TASK.json [--pair-key PAIR_KEY.json] --replay-provider-manifest P.json --replay-command-manifest C.json --run-id RUN --workspace-id WORKSPACE (--new-session | --session SESSION_ID) [--workspace NEW_DIR | --run-root PARENT]
agintor solve PROJECT_ROOT --task-envelope TASK.json [--pair-key PAIR_KEY.json] --live (--new-session | --session SESSION_ID) [--api-key-file KEY_FILE] [--workspace NEW_DIR | --run-root PARENT]
agintor eval --project-root PROJECT_ROOT --request-json EVALUATOR.json --output-json PUBLIC.json
```

`build-runtime` accepts typed build JSON. `execution_mode="dry_run"` performs
strict validation without release publication or callbacks.
`execution_mode="offline_scripted"` requires `--replay-manifest` and publishes
from immutable factory replay.

The three explicit dry-run commands persist typed, exact `not_run` manifests:
`gate0-dry-run` freezes the provider-call schedule without sending requests,
`search-dry-run` records proposal/evaluator opportunities with zero callbacks
and no release publication, and `pilot-dry-run` binds the active release,
reserved development task, and release-pinned session without sending model,
tool, public-verification, or evaluator calls.

`readiness-build` and `readiness-replay` launch a dedicated evaluator-role child;
the public CLI process never imports pilot or evaluator contracts. Build maps
packet-relative evidence paths to existing regular files under
`CONTROLLED_ROOT`, loads the full sealed `EvaluationContract` only in the child
to derive canary checks, and atomically publishes a content-addressed readiness
generation. Replay revalidates that immutable generation. Both commands return
only the public packet id, digest, and controlled-root-relative path in a
`not_run` wrapper. Requests, artifact sources, sealed authority, destinations,
and generations must all remain inside `CONTROLLED_ROOT`; no live calls occur.

Replay solve requires both provider and isolated-command replay manifests plus
explicit `--run-id` and `--workspace-id`. Live solve is a separate authority path
with no provider/model/backend overrides; credential inputs are references such
as `--api-key-file`, never key values. Replay and inspect scrub credential
references. The key file must be an external regular file outside project,
release, and run roots. Its path is passed to the provider child as a reference;
the credential is not placed in command arguments, request JSON, traces, or
durable evidence.

Harness V1 accepts immutable directory snapshots only. A relative snapshot URI
is resolved against the task-envelope file before the source-hidden child is
launched, while the declared task payload and digest remain unchanged. Formats
that V1 cannot materialize are rejected during task validation.

The Harness V1 OpenAI Responses adapter is available when the `openai` optional
dependency is installed. The example frozen profiles are
`agintor/examples/repair_mvp/deployment-profile-openai-luna.json` and
`agintor/examples/repair_mvp/deployment-profile-openai-terra.json`. They pin
`gpt-5.6-luna` and `gpt-5.6-terra`, respectively, with reasoning effort `none`,
the standard service tier, no response storage, and serial tool calls. Luna is
the low-cost profile for transport, cache, and other simple checks; Terra is
reserved for substantive behavior such as typed repository-tool use.

Both profiles use an explicit stable-prefix prompt-cache policy. The aggregate
ledger reserves and reconciles uncached input, cache reads, cache writes, and
output separately, including the distinct cache-write price. A cache-disabled
profile sends the explicit no-breakpoint request shape so the provider does not
perform an unbudgeted implicit write. Before dispatch, the adapter applies a
conservative UTF-8-byte upper bound to the complete frozen request payload and
prices that reservation; token-dense input and the GPT-5.6 long-context pricing
boundary therefore fail closed before credentials, client creation, or send.

Command replay is an immutable, single-use transcript verifier. It checks the
real initial workspace and every recorded before-to-after digest transition,
but it does not execute commands or materialize their side effects. Replay
outcomes are therefore deterministic test evidence only and never authorize a
capability release; live authority requires completed provider and evaluator
proofs from the frozen deployment.

`solve --pair-key PAIR_KEY.json` strictly validates the evaluator pairing
identity and, after a successful solve, assembles full controlled RunEvidence in
the run workspace. The public solve result contains only its digest-bound
`controlled_run_evidence` reference. A solve without `--pair-key` remains a
normal runtime result but is not eligible for evaluator submission.

`eval` launches the evaluator-owned entrypoint in a separate role process and
atomically writes the same public JSON wrapper to `--output-json`. The documented
evaluator path is deterministic replay/dry-run evidence with live status
`not_run`. The completed provider and Docker integration smokes do not satisfy a
numerical evaluator gate or establish repair capability.

## Development Notes

Use `AGENTS.md` for repository operating rules and `Dev Docs/REPO_MAP.md` for
the live V1 module map. `Dev Docs/DEFERRED_ISSUES_LEDGER.md` is the current
backlog surface.

Legacy LangGraph, oracle, host-runtime, old factory service, and old CLI modules
remain in the tree for deferred/non-public work. They are not the current
repository-facing V1 command path.

Docker containment acceptance tests are opt-in with `@pytest.mark.docker`. The
current acceptance run passed all three cases with the preloaded image
`docker.io/library/python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf`.
The tests use `--pull never` and do not build or pull images.

The opt-in live OpenAI integration smoke also passed: two Luna calls established
an explicit cache write followed by a cache read, and one Terra call exercised
substantive typed `repo.read` behavior. Both models used reasoning effort
`none`. This was an adapter/accounting smoke only; G0, D0, S1, and P1 numerical
live evidence remains `not_run`, and the smoke supports no capability claim.
The cumulative Terra expenditure for the integration work has a conservative
upper bound below $0.047, not an asserted exact billed amount.

Plain `pytest` runs the Harness V1 offline suite in `tests/mvp`. Tests for the
retired policy-module runtime and other legacy surfaces remain runnable by
explicit path for historical maintenance, but they are not part of the V1
default gate and may depend on resources deliberately removed at the cutover.
