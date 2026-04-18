---
P1
Honor restricted network contracts on the local backend
Dismiss
resolve_docker_launch_policy() already treats network_policy values like restricted/none as requiring network disablement, but the local validation path only checks required_guarantees. A runtime whose contract sets network_policy="restricted" and leaves required_guarantees empty is therefore accepted on local, so the new isolation model fails open and can run a contract that explicitly asked for restricted networking without enforcing it.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_loader.py:282-282
P1
Don't treat any top-level artifact.error as a batch failure
Dismiss
reduce_grouped_run_results() now infers failure from any non-empty top-level artifact["error"], regardless of lifecycle or verifier outcome. That breaks valid grouped runs whose output schema legitimately includes an error string (for example {"error": "none", ...}): they get marked failed/pruned even when run_lifecycle_state == "completed" and the verifier passed. Failure detection here needs to come from run metadata, not a user payload key.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_api.py:1559-1562
P2
Finalize resume attempts when inspect or resume_support fails
Dismiss
_resolve_runtime_resume_request() has already created a durable attempt by the time control reaches inspect() and the resume_support guard, but both checks run before the surrounding try/except that calls _finalize_execution_unit(). If inspect fails, or the runtime reports resume_support=false, the method raises immediately and leaves both run_manifest.json and attempt_manifest.json stuck in running, which breaks later resume/cleanup flows and misreports the run state.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py:348-350
---
P1
Give replay branch clones independent cursors
Dismiss
clone_provider() now returns ReplayProvider.shared_clone() for replay inputs, but horizontal WS2 execution runs branches concurrently. That means the next recorded replay row is consumed by whichever branch thread reaches generate() first, so branch-specific artifacts/checker outcomes become scheduler-dependent instead of seed-dependent. Any concurrent eval or deterministic-smoke run that uses replay rows which differ by branch can therefore flip results across identical replays.


C:/Users/yaros/Desktop/Agintor MVP/agintor/providers.py:505-506
P2
Avoid recording root side-effect receipts twice
Dismiss
The root PolicyContext callback points back into _record_side_effect_receipt(), but PolicyContext.record_side_effect() has already appended the receipt to state.side_effect_receipts before invoking the callback. On normal solve/resume paths this duplicates every root provider/tool launch and completion in checkpoints, which is why a single provider call already produces duplicate provider-request.* entries and repeated reconciliation events during resume.


C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py:314-314
P2
Finalize resume attempts on pre-launch rejection
Dismiss
_resolve_runtime_resume_request() persists a new AttemptManifest, but resume() performs inspect() and the resume_support check before it enters the try/except that calls _finalize_execution_unit(). If inspect fails or the runtime advertises resume_support = false, the newly created attempt is left in running state even though nothing was launched, so the durable run metadata now reports a phantom in-flight resume.


C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_host.py:348-350
---
P1
Resolve resume targets from external checkpoint envelopes
Dismiss
Honor externally stored checkpoints when checkpoint_ref is supplied. RunStore.resolve_resume_target() currently assumes every explicit checkpoint path must sit under a durable run root and derives the manifest by walking parent directories, but WS2 now allows checkpoints to live in an external store and the envelope already carries the run_id/run_root needed to recover the manifest. As a result, any resume flow that passes an external checkpoint path now fails immediately with FileNotFoundError instead of resuming.


C:\Users\yaros\Desktop\Agintor MVP\agintor\run_store.py:307-310
P1
Preserve the actual checkpoint store on resume requests
Dismiss
Preserve the real checkpoint store when building RuntimeResumeRequest. After resolve_resume_target() picks a checkpoint, the host unconditionally sets checkpoint_store_dir to <run_root>/checkpoints; if the latest usable checkpoint came from an external store, docker resume will mount the wrong directory into /mnt/checkpoints and the runtime will be unable to open the selected checkpoint. This breaks externally stored resume points even when resolving by run_ref succeeds.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py:480-485
P2
Reject stray run results in batch responses
Dismiss
Fail closed on extra batch results. _validated_group_run_results() filters response.run_results down to rows whose run_id matches the current manifest, so any additional RunResult with an unexpected run_id is silently ignored by validation and still returned to the caller. A protocol-mismatched runtime can therefore inject spurious batch rows without tripping host-side contract checks, which is exactly the kind of response corruption this layer is supposed to detect.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py:543-545
---
P1
Finalize resume attempts when inspect/support checks fail
Dismiss
_resolve_runtime_resume_request() has already created a durable AttemptManifest, but both inspect() and the resume_support guard run before the try block that calls _finalize_execution_unit(). If inspect fails or the runtime does not advertise resume support, the exception escapes with the run and attempt still marked running, leaving stale durable state that can confuse later resume/prune logic and make the failed attempt look active.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py:348-350
P1
Block transfer episodes after a cancelled step
Dismiss
Grouped transfer episodes only stop subsequent invocations when the reduced lifecycle is paused or failed. A step that returns run_lifecycle_state="cancelled" does not set blocking_run, so later episode steps still execute even though the episode has already terminated. That breaks transfer-scored evaluation semantics and can trigger extra model/tool side effects after a cancellation.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_sdk\runtime_entry.py:245-247
P2
Preflight provider-backed batch runs before launch
Dismiss
solve() and resume() both call _preflight_solve_contract() before dispatch, but run_batch() now skips that check entirely. Any benchmark task that compiles to provider-backed nodes such as direct_response or provider-assisted tool synthesis will therefore launch the runtime even when the required runtime credentials are unavailable, turning what should be a host-side contract failure into a mid-run execution failure with partially created durable run state.


C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py:213-214
---
P1
Give replay branch clones independent cursor state
Dismiss
When a horizontal run clones a ReplayProvider for each branch, shared_clone() makes every branch compete for the same coordinator cursor. In TaskRuntime._run_branch_plan() those clones execute concurrently, so the branch that reaches generate() first consumes the next replay row. That makes replay-driven runs timing-sensitive instead of deterministic: with the same 2-branch direct-response plan and the same replay rows, simply delaying w0 flips the final artifact from {'response_a': 'row0', 'response_b': 'row2'} to {'response_a': 'row1', 'response_b': 'row3'}. Because the replay provider is the deterministic hosted-provider path used for smoke/eval and resume tests, each branch clone needs its own cursor snapshot rather than a shared mutable cursor.


C:\Users\yaros\Desktop\Agintor MVP\agintor\providers.py:180-181
---
