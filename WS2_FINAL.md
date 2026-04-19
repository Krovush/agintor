**Overall diagnosis**
These reviews point to one main story: WS2 successfully introduced a richer runtime contract, but several critical semantics are still owned by scattered leaf helpers instead of a single source of truth. Paths, category permissions, checkpoint discovery, side-effect journaling, and replay allocation are each being interpreted in multiple places, so local solve, Docker solve, resume, and concurrent branch execution no longer behave as one coherent system.

I would keep almost all of the pasted findings. The Windows path-with-spaces report is a duplicate of the earlier regex finding, and the `service/*` report is really a narrower symptom of the broader wildcard/category-matching bug rather than a separate root cause. The strongest themes are the file/path contract problems and the recovery/state-journaling problems.

**Core themes**
- `File references are not a first-class runtime contract.` This is the strongest repeated signal. [runtime_api.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_api.py:53) parses prompt paths heuristically, [tool_runtime.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/tool_runtime.py:285) resolves relative file paths against process CWD instead of the runtime workspace, and [container_runtime.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/container_runtime.py:604) only containerizes run roots, not request/task file refs. Those are three manifestations of the same ownership mistake: file identity is being inferred ad hoc instead of compiled once and transported explicitly.

- `Capability scopes are being treated as strings, not as permission families.` The `filesystem/*` and `service/*` findings both land here. [runtime_api.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_api.py:516) only does literal-or-prefix string checks, so wildcard families stop matching concrete categories, and [schemas.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/schemas.py:800) then feeds `service/*` into transport resolution as if it were a concrete transport. That means the code has no central model for “family scope” versus “concrete executable capability.”

- `Resume and side-effect recovery still have split sources of truth.` [run_store.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/run_store.py:284) looks only inside the run-local checkpoint store when resuming by `run_ref`, even though host finalization can expose an external checkpoint path via the manifest. [runner.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runner.py:3927) mutates the filesystem before durably recording the write receipt. Both issues come from the same gap: the system says receipts/checkpoints are authoritative for recovery, but some recovery-relevant state still lives outside that authority boundary.

- `Concurrency was added before deterministic replay ownership was redesigned.` [providers.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/providers.py:505) shares one replay coordinator across branch clones, while WS2 horizontal mode now truly runs branches concurrently. That makes replay row assignment timing-dependent. This theme is supported by fewer independent findings than the others, so it is a slightly weaker synthesis, but the underlying issue looks real.

**Recommended redesign path**
1. Build a single request-file reference contract. Compile prompt/user/benchmark file inputs into typed refs with explicit provenance, workspace semantics, and container mount requirements. After that, all local and Docker execution should consume only those refs, never reparse prompt text or raw path strings.

2. Replace string matching with a real capability-scope layer. Introduce one matcher/expander that understands exact scopes, family wildcards like `filesystem/*`, and concrete service transports like `service/http`. Prompt adaptation, plan requirements, and service transport validation should all call that same layer.

3. Make recovery state truly authoritative. Resume-by-`run_ref` should consult manifest-level `latest_checkpoint_ref` as well as the local checkpoint index, and every externally meaningful side effect should follow an intent-then-completion journal pattern so crashes cannot strand mutated state without durable proof.

4. Redesign replay semantics for concurrent branches. Either pre-allocate deterministic replay slices per branch/node or explicitly disallow shared replay under concurrent branch execution. Right now the implementation sits in an unsafe middle ground.

**Open questions**
- Should Docker prompt-mode support arbitrary host file paths at all, or should compile/preflight reject anything outside declared mountable roots?
- Should `service/*` mean “all known service transports” or should callers be forced to pick a concrete transport family such as `service/http`?
- Is replay expected to validate real concurrent branching, or is it acceptable to serialize branch model calls whenever the replay provider is active?

**Execution handoff**
`workstream_candidates`
- Request/file reference contract spanning prompt adaptation, runtime execution, and Docker transport.
- Capability-scope normalization covering wildcards, category checks, and service transport resolution.
- Recovery hardening covering manifest checkpoint lookup and two-phase side-effect journaling.
- Deterministic replay redesign for concurrent branch execution.

`shared_file_risks`
- [runtime_api.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_api.py:1) is shared by prompt adaptation, plan compilation, file refs, and capability matching.
- [container_runtime.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/container_runtime.py:1) will collide with any file-ref contract change.
- [runner.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runner.py:1) and [run_store.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/run_store.py:1) both affect recovery semantics.
- [providers.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/providers.py:1) and [runner.py](/C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runner.py:1) both affect replay determinism.

`ordering_constraints`
- Define the file-ref contract before fixing Docker path rewriting; otherwise the transport patch will bake in the wrong path model.
- Define wildcard/service semantics before changing prompt adaptation behavior or its tests.
- Fix recovery source-of-truth before polishing host resume UX.
- Decide replay policy before relying on horizontal replay smoke tests.

`validation_invariants`
- Prompt-mode file inspection and repo patch must work for absolute Windows paths with spaces.
- Relative request file refs must resolve against the runtime workspace, not process CWD.
- The same compiled file refs must work on both local and Docker backends.
- `filesystem/*` and `service/*` must authorize the intended concrete operations.
- Resume by `run_ref` must succeed when the only latest checkpoint is external.
- Filesystem writes must never occur without a durable pre-write recovery record.
- Replay-backed horizontal runs must be deterministic across repeated executions.

`open_decisions`
- Whether raw host absolute paths are a supported Docker input or a compile-time error.
- Whether wildcard service scopes expand eagerly or remain declarative until backend selection.
- Whether replay determinism is solved by branch-specific allocation or by disabling concurrent replay.