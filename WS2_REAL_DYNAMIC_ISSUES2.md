# WS2 Real Dynamic Issues 2

## Immediate Before WS3

### Repo-patch execution is not bounded to the declared runtime workspace

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._execute_repo_patch_node()`
- Current behavior:
  - the repo-patch executor writes `updated_content` with `Path(path).write_text(...)` on raw `target_file_paths`
  - those paths are not normalized against a declared workspace root and are not checked against the deployment contract filesystem boundary
  - relative paths therefore resolve against the runtime process cwd, and absolute paths outside the intended repo/workspace are writable
- Why this is immediate before WS3:
  - WS2 is supposed to hand WS3 a bounded runtime contract with explicit side-effect and filesystem semantics
  - WS3 persistence and recovery work should not build on a solve-time write surface that can escape the intended workspace boundary
  - this is not just prompt-template quality; it is a runtime-side contract violation on actual side effects
- Required fix direction:
  - canonicalize repo-patch targets against the declared runtime workspace or another explicit allowed root
  - reject any path that escapes the bounded filesystem contract before writing

## Service actions do not persist a launch receipt before the external side effect

- Area:
  - `agintor/runner.py`
  - `_execute_service_action_node()`
- Current behavior:
  - the runtime performs the HTTP request first and only records a completed `service_action` receipt after the request returns
  - if a POST/PUT/PATCH/DELETE succeeds and the runtime crashes before that receipt is persisted, resume has no durable proof that the side effect already happened
  - the resumed run can therefore reissue the same external request blindly
- Why this is immediate before WS3:
  - WS2 explicitly defines receipt-backed replay/reconciliation semantics for every non-deterministic or externally meaningful action
  - WS3 durability will build directly on that frozen receipt contract
  - leaving `service_action` outside launch/completion receipt discipline means WS3 inherits a broken side-effect protocol at the solve-kernel level
- Follow-up target:
  - emit a durable launch receipt and checkpoint boundary before dispatching the external request
  - emit terminal completion/failure receipts after dispatch and route resume through the same reconciliation rules used for provider and tool side effects
