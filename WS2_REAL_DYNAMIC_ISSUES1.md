# WS2 Real Dynamic Issues 1

## Prompt-mode execution-plan coverage is still incomplete

- Area:
  - `agintor/runtime_api.py`
  - `solve_request_to_task()`
  - `agintor/runner.py`
  - `_execute_direct_response()`
  - `agintor/schemas.py`
  - `PlanNodeKind`
- Current behavior:
  - prompt-mode only compiles a few narrow templates into executable plan shapes
  - general user requests still collapse to a single `direct_response` node
  - file-inspection prompts pass only file-path strings into the model prompt and never bind or read file contents
  - bounded repo-patch and service-action prompts never materialize into runtime-native `repo_patch` or `service_action` plan nodes
- Why this is immediate before WS3:
  - WS2’s handoff to WS3 is supposed to freeze the runtime-owned solve kernel and execution-plan meaning
  - if we move on now, WS3 will persist and build on an incomplete prompt-mode contract instead of the bounded request templates WS2 explicitly defined
  - this is not an edge-case polish issue; it means major parts of the promised user-request solve surface are still text-only rather than executable runtime behavior
- Follow-up target:
  - implement deterministic prompt adaptation for at least:
    - direct answer
    - structured computation
    - file inspection
    - bounded repo patch
    - bounded service action
  - extend the runtime-native execution plan and executors so `direct_response` is only the direct-answer template, not the fallback for everything prompt-mode cannot yet do

## External checkpoint resume must fall back from stale `run_root` to `run_id`

- Area:
  - `agintor/run_store.py`
  - `RunStore.resolve_resume_target()`
- Current behavior:
  - when resume is driven by an explicit external `checkpoint_ref`, resolution prefers `CheckpointEnvelope.run_root` whenever that field is non-empty
  - if that stored root is stale or container-only (for example `/mnt/runs/...`), `load_run_manifest()` raises `unknown run_ref` even when `CheckpointEnvelope.run_id` still points to a valid local durable run manifest
- Why this is immediate before WS3:
  - WS3 is supposed to take the WS2 checkpoint envelope and resume contracts as the frozen solve-time recovery boundary
  - cross-store, copied, and container-origin checkpoints are exactly the sort of recovery surfaces WS3 will persist and index more aggressively
  - leaving resume resolution pinned to a stale embedded path means WS3 inherits a recovery contract that is already brittle before durability work even starts
- Follow-up target:
  - when resolving from `checkpoint_ref`, try `run_root` first only if it resolves locally, then fall back to `run_id` before giving up
  - keep external checkpoint resume covered in the same-run file-backed path before moving on to WS3 persistence work
