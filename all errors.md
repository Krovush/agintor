# Agintor MVP — All Identified Spec-Compliance Errors

Audit of the current codebase against `PROJECT TARGET SPEC.md`.  
Each issue includes an expanded description and two proposed solutions.

---

## Error 1 — Stop-Policy Allows Premature Termination via `allow_best_effort` Short-Circuit

**Spec §10.4 · Severity: 🔴 High · File: `control_policy.py:53-60`**

The spec defines a strict stopping predicate that requires two consecutive steps where the best optimistic next-action utility is negative, zero unresolved goals, and a verified terminal artifact before the runtime may stop. The current implementation in `stop_policy` adds an extra escape hatch: when `best_optimistic_utility < 0` and `previous_best_utility < 0` and `unresolved_count == 0`, it also allows stopping if `allow_best_effort` is true or `verification_required` is false on the task, even when no verified terminal artifact exists. This means the runtime can terminate and return unverified output in scenarios where the spec mandates either continued execution or controlled failure, potentially producing incorrect artifacts that bypass the verifier entirely.

**Solution A:** Remove the `allow_best_effort` and `not verification_required` conditions from the third clause of `stop_policy`. The method should return `True` in that branch only when `verified_terminal` is `True`, exactly matching the spec formula. The `allow_best_effort` logic should be handled exclusively in the runner's post-loop fallback code (which already exists at `runner.py:120-123`).

**Solution B:** Restructure the stopping logic to compute the spec's stopping predicate as a standalone pure function that takes the four boolean/numeric inputs and returns a decision. Have `stop_policy` call that function, and move the `allow_best_effort` handling into the runner where it only affects the *type of output emitted* after the loop ends, not whether the loop terminates.

---

## Error 2 — Controlled-Failure Path Unreachable When Stop-Policy Fires Early

**Spec §10.4 (final paragraph) · Severity: 🟡 Medium · File: `runner.py:120-123`**

The spec states that when no verified terminal artifact exists and all admissible actions yield negative utility, the runtime must emit best-effort output only if the benchmark explicitly allows it; otherwise it must return controlled failure. The runner does implement this check after the main loop exits, setting `artifact = {"error": "controlled_failure"}` when `verified_terminal` is false and the task requires verification and does not allow best effort. However, because the stop policy (Error 1) can terminate the loop prematurely with `allow_best_effort` overriding the verified-terminal requirement, the main while-loop may break before the runner ever reaches the post-loop fallback logic, causing the runtime to return an unverified artifact instead of the controlled-failure sentinel.

**Solution A:** Fix Error 1 first (tighten the stop policy to match the spec). Once the stop policy no longer short-circuits, the post-loop fallback at `runner.py:120-123` will correctly fire whenever the loop ends without a verified terminal artifact.

**Solution B:** Add a redundant guard inside the main loop's break path: immediately after `stop_policy` returns `True`, check `verified_terminal` and `task.verification_required` before breaking, and set `artifact = {"error": "controlled_failure"}` inline if the conditions warrant it, rather than relying solely on the post-loop code.

---

## Error 3 — Model Escalation Logic Is Entirely Missing

**Spec §10.2 · Severity: 🔴 High · File: `control_policy.py:16-32`**

The spec requires that after two consecutive negative-improvement steps on the same unresolved subgoal, the control surface performs one model-class escalation (small → medium → large). This provides a recovery mechanism when a cheaper model repeatedly fails to make progress on a stubborn subgoal. The current `assign_model` implementation selects the cheapest qualifying model on every call without any memory of previous attempts. It does not track which subgoals have been attempted, how many consecutive failures have occurred, or what model class was last used for a given subgoal. As a result, the runtime will keep retrying the same cheap model indefinitely, never escalating to a more capable tier even when repeated failures clearly indicate the need for one.

**Solution A:** Add a per-subgoal failure counter to `RuntimeState` (e.g. `subgoal_negative_steps: dict[str, int]`). In `assign_model`, look up the current subgoal's failure count: if it is ≥ 2, force the next-higher model class above the one last used. Increment the counter when a step completes with negative improvement; reset it when a step succeeds.

**Solution B:** Maintain an `escalation_state: dict[str, str]` mapping `op_id → current_model_class` inside the `AgentFrame` or `PolicyContext`. On each `assign_model` call, compare the frame's previous model class with the minimum qualifying class. If the previous class was already tried and the improvement was negative (signalled by the frame metadata), bump to the next tier. Cap at `"large"`.

---

## Error 4 — Verification Request Policy Lacks Sequential Checker Escalation

**Spec §10.3 · Severity: 🟡 Medium · File: `control_policy.py:34-51`**

The spec defines a checker ladder `{local, subtree, repo, benchmark}` ordered from cheap to expensive, with a Value-of-Information (VOI) formula for each checker. The policy should run the cheapest checker with positive VOI first, then escalate to the next checker only if the cheaper one passed but uncertainty remains, the artifact is externally visible, or a parent merge depends on the child output. The current implementation computes a simplified VOI for each checker independently and returns at most one checker per call. It never runs a cheap checker first, observes its result, and then decides whether to escalate to a more expensive one. This means the runtime either skips valuable cheap checks or jumps straight to the benchmark verifier, losing the staged confidence-building that the spec intends.

**Solution A:** Change `request_checks` to return a prioritized list of checkers rather than a single one. In the runner's `_maybe_verify`, iterate through the list: run each checker, and if it passes but uncertainty remains (e.g. the checker is not `"benchmark"` and the artifact is externally visible), continue to the next checker. Stop early if a checker fails or if the benchmark checker passes.

**Solution B:** Make `request_checks` stateful by accepting the results of previously-run checkers as input. The runner would call `request_checks` in a loop: call it once to get the first checker, run that checker, then call `request_checks` again with the previous result, letting the policy decide whether to escalate. This keeps the policy logic self-contained and matches the VOI escalation pattern exactly.

---

## Error 5 — Horizontal Workers Share Mutable Predictor and Safety-Guard State

**Spec §2.4 invariant 2, §13.2 · Severity: 🟡 Medium · File: `runner.py:277-326`**

The spec requires that horizontal workers share only the append-only message board with per-worker read cursors — they must not share mutable short-term state. The `_execute_isolated_frame` method correctly snapshots and restores the short-term graph, tool registry, category summaries, open handles, and long-term memory when `isolate_runtime_state` is true. However, `self.shell.predictors` (the `DecisionFamilyModelBank`) and `self.shell.safety_guard` are not isolated. If a worker were to add observations to the predictor bank or if the safety guard maintained any mutable state, those mutations would be visible to subsequent workers and the parent frame, violating the append-only-board-only sharing constraint.

**Solution A:** Extend `_execute_isolated_frame` to also snapshot and restore `self.shell.predictors._observations` and `self.shell.predictors._models` using `copy.deepcopy`, just as is done for the tool registry and long-term graph. Since `SafetyGuard` is currently stateless, no snapshot is needed for it, but add a comment documenting this assumption.

**Solution B:** Instead of snapshotting the shared shell, create a lightweight per-worker `FixedShell` clone that has its own predictor bank and safety guard instances but shares the same underlying sandbox manager and agent pool (which are read-only during task execution). Pass this cloned shell into the worker's context.

---

## Error 6 — Tool Synthesis Failure Hard-Invalidates the Entire Run

**Spec §9.3 · Severity: 🔴 High · File: `runner.py:569-574`**

The spec says that when a synthesized tool fails validation, the tool should be rejected, but the run should continue — the runtime should fall back to the next-best reusable tool or record a non-fatal operational fault. Only certain hard-invalidation conditions (benchmark adapter mutation, safety violations, etc.) should kill the run outright. The current code wraps the entire tool-synthesis path in a try/except that catches any exception and immediately raises `HardInvalidation`, which terminates the run with verifier score zero. This means a benign tool-synthesis failure (e.g. a syntax error in a generated expression) kills the entire evaluation, whereas the spec intends it to be a recoverable fault that increments the fault counter and retries with a different tool.

**Solution A:** Replace the `raise HardInvalidation` inside the tool-synthesis except block with `faults += 1` and a fallback to the next ranked reusable tool. If no reusable tool exists either, raise `HardInvalidation` only then (since no tool is available at all, which matches "no tool available after category-first discovery").

**Solution B:** Distinguish `SafetyViolation` (which should hard-invalidate) from `ValidationError` and generic exceptions (which should not). Catch `SafetyViolation` and re-raise as `HardInvalidation`; catch all other exceptions, increment the fault counter, record a tool-failure trace event, and continue to the existing fallback logic where `tool_name` might still be set from a previously-ranked reusable tool.

---

## Error 7 — Tool Promotion Missing the `safe(τ) = 1` Predicate

**Spec §9.3 · Severity: 🟡 Medium · File: `tool_policy.py:172-173`**

The spec's promotion predicate requires four conditions to all hold: the pass rate must meet the threshold, the tool must have been reused on a minimum number of distinct tasks, the tool must have passed an explicit safety check, and the tool's determinism class must be stable. The `promote_tool` method checks pass rate (`≥ 0.80`), distinct-task reuse count (`≥ 3`), and determinism class (`"stable"`), but it does not check the safety predicate at all. This means a tool that has user-facing permissions or uses operations that border on unsafe could be promoted to the permanent reusable registry without ever being safety-validated, potentially creating a persistent safety risk in the tool pool.

**Solution A:** Add a `safe` property or method to `RegisteredTool` that re-validates the tool's permissions and source against the safety guard. Call this in `promote_tool`: `and ctx.shell.safety_guard.validate_permissions(tool.spec.permissions) is None` (it raises on failure, so wrap in a try/except and return `False` on `SafetyViolation`).

**Solution B:** Store a `safety_validated: bool` flag on `RegisteredTool`, set to `True` only after `validate_tool_candidate` completes successfully. In `promote_tool`, add `and tool.safety_validated` to the return expression. This avoids re-running the safety check on every promotion query and ensures the safety check happened at least once during the tool's lifecycle.

---

## Error 8 — Tool Validation Missing Several Spec-Required Steps

**Spec §9.3 · Severity: 🟡 Medium · File: `tool_runtime.py:517-567`**

The spec mandates a seven-step validation pipeline for synthesized tools: (1) parse/syntax check, (2) linter and import resolution, (3) signature and schema check, (4) smoke test, (5) permission-boundary test, (6) timeout test, and (7) deterministic-output replay under a fixed seed. The current `validate_tool_candidate` function performs the syntax check via `ast.parse`, runs `py_compile` as a basic compilation check, validates safety through the safety guard, and runs deterministic-replay smoke tests. However, it omits: the linter step (no `pylint` or `flake8` invocation), the signature/schema check (the `ToolSpec.signature` field is never compared against the actual function signature in the source), the permission-boundary test (no sandbox isolation verification), and the timeout test as a distinct step (timeouts are only checked within the smoke-test subprocess, not independently).

**Solution A:** Add the missing validation steps sequentially: (1) run `ast.parse` + `py_compile` (already done), (2) invoke `subprocess.run([sys.executable, "-m", "py_compile", ...])` plus a basic import-resolution check, (3) parse the function signature from the AST and compare argument names against `spec.signature`, (4–6) run smoke tests with explicit timeout and permission checks, (7) keep existing deterministic replay. Return a richer result dict with per-step pass/fail.

**Solution B:** Create a `ToolValidationPipeline` class with pluggable steps. Each step is a callable that takes `(spec, source, sandbox_dir)` and returns `(passed, detail)`. The pipeline runs steps in order, short-circuiting on the first failure. This makes it easy to add or remove validation steps and matches the spec's staged validation design.

---

## Error 9 — Compaction Token Window Hardcoded to 512

**Spec §8.2 · Severity: 🟡 Medium · Files: `runner.py:445`, `memory_policy.py:17`**

The spec defines compaction as a global budget control mechanism that triggers when the active-history budget fraction exceeds `B_hi = 0.75` and continues until it falls below `B_lo = 0.55`. The budget fraction should be computed relative to the actual context limit. The current code computes `fraction = used_tokens / 512.0`, where `512` is a hardcoded constant in `MemoryPolicy.TOKEN_WINDOW` that does not correspond to any budget parameter in `RuntimeBudget` or to any real model context-window size. If the actual context limit is larger (e.g. 4096 or 128K tokens), compaction will trigger far too aggressively at a tiny fraction of the real capacity. Conversely, if the limit is smaller than 512, compaction will never trigger at all.

**Solution A:** Replace the hardcoded `512.0` with the actual context-window budget. Add a `context_window_tokens: int` field to `RuntimeBudget` (default perhaps 4096) and pass `budget.context_window_tokens` as the denominator. Update `MemoryPolicy.TOKEN_WINDOW` to reference this value via the policy context.

**Solution B:** Compute the token fraction dynamically by having the shell track the total token capacity as a configuration parameter. Pass it into `_compact_if_needed` via the `PolicyContext`, and let the memory policy's `select_spans_for_compaction` receive the actual fraction as its `active_fraction` argument rather than computing it internally.

---

## Error 10 — Curriculum Phase Budgets and Pass-Rate Caps Not Enforced

**Spec §11.4, §12.2 · Severity: 🟡 Medium · File: `evolution.py:209`**

The spec prescribes a default schedule of 1200 local mutations, 600 pairwise mutations, and 300 joint mutations, with recommended pass-rate caps of `(p1, p2, p3) ≤ (0.35, 0.15, 0.05)` at stages 1, 2, and 3 respectively. If pass rates exceed these caps, the evaluator should tighten thresholds before search proceeds. The current `EvolutionEngine.run(steps=10)` accepts an arbitrary step count and runs a flat loop with no awareness of per-phase budgets. The `ScopeScheduler` does track the current phase and can advance phases, but the evolution engine never enforces phase-specific mutation counts or monitors pass-rate caps. This means the search could spend all iterations on a single phase, or an overly permissive evaluator could let too many candidates through without tightening, wasting compute.

**Solution A:** Add per-phase step counters to `EvolutionEngine` (e.g. `phase_budget = {"local": 1200, "pair": 600, "joint": 300}`). In the main loop, decrement the current phase's budget on each iteration and automatically advance the phase when the budget is exhausted. Track pass rates per stage across a sliding window and call a `tighten_thresholds` method on the evaluator when caps are exceeded.

**Solution B:** Expose the 1200/600/300 defaults and the pass-rate caps as `EvolutionEngine` constructor parameters. In the `run` method, replace the flat `range(1, steps+1)` loop with a phase-aware loop that respects both the per-phase budget and the `ScopeScheduler.maybe_advance_phase` early-exit trigger. Add a pass-rate monitor that adjusts `epsilon_proxy` and `epsilon_part` upward when stage pass rates exceed their caps.

---

## Error 11 — Stage 4 Early-Rejection by Minibatch Not Implemented

**Spec §12.1 Stage 4 · Severity: 🟡 Medium · File: `evaluator.py:198-207`**

The spec allows early rejection within Stage 4 (full training suite evaluation) using a minibatch mechanism: if, after evaluating a subset of tasks, the mean child-minus-parent score delta plus 1.96 standard errors falls below a rejection margin, the candidate can be rejected without evaluating the remaining tasks, saving significant compute. The current `stage4_full` method evaluates the entire training suite in a single pass by calling `evaluate_runtime` with all training tasks at once. There is no minibatch loop, no incremental score accumulation, and no early-rejection check. This means every candidate that reaches Stage 4 must be fully evaluated even when early results strongly suggest it will be rejected, wasting potentially large amounts of compute on hopeless candidates.

**Solution A:** Modify `stage4_full` to split the training suite into minibatches (e.g. 4-task batches). After each minibatch, compute `d_bar + 1.96 * se` and reject early if this falls below `-delta_rej`. If no early rejection occurs, aggregate all minibatch results into a single `SuiteEvaluation` for archive insertion.

**Solution B:** Keep `stage4_full` as the final aggregation step but add a `stage4_early_reject` method that runs a small random subset first. If the lower confidence bound is deeply negative, skip the full evaluation. This two-pass approach is simpler to implement and still captures the major compute savings.

---

## Error 12 — Crossover Is Implemented But Never Called

**Spec §6.4, Algorithm 1 step 4 · Severity: 🟡 Medium · Files: `crossover.py`, `evolution.py`**

The spec's Algorithm 1 step 4 says: "Sample a parent from island $I_f$; optionally apply whole-method crossover." The `crossover.py` module provides a complete, correct implementation of whole-method crossover at the AST level — it can extract methods from donor runtimes and splice them into a base runtime while checking for overlapping symbol edits. However, `evolution.py` never imports `crossover_runtime` and never calls it anywhere in the evolution loop. This means the entire crossover mechanism is dead code: the search operates purely through mutation, losing the ability to recombine successful method-level innovations from different archive elites, which is a key diversity-preservation mechanism in the spec.

**Solution A:** In `EvolutionEngine.run`, after sampling a parent and before generating a mutation, add a probabilistic crossover step. With some probability (e.g. 0.15), select a second parent from a different archive cell, choose one or more mutable methods from the second parent, and call `crossover_runtime` to produce a hybrid child. Feed this hybrid into the mutation pipeline as the starting point.

**Solution B:** Add a dedicated `_maybe_crossover` method to `EvolutionEngine` that selects a donor from the archive (preferring donors with high scores on different objectives), picks disjoint mutable methods, calls `crossover_runtime`, and returns either the crossover child directory or the original parent directory if crossover was skipped. Call this method in the main loop at step 4.

---

## Error 13 — Predictor Model Bank Is Never Populated or Retrained

**Spec §5.3 · Severity: 🔴 High · Files: `predictors.py`, `evolution.py`**

The spec requires that predictors are retrained whenever 50 fully evaluated children or 10 accepted elites accumulate since the previous update, using the most recent 200 labeled examples per task family. The `DecisionFamilyModelBank` class in `predictors.py` has a fully functional implementation of `add_observation`, `train_family`, and `maybe_retrain`. However, `evolution.py` never calls any of these methods — it never adds observations from completed evaluations, never triggers retraining, and never maintains the counters for fully-evaluated children or accepted elites. As a result, the predictor bank remains empty for the entire evolution run, and all runtime decisions that should consult learned predictors fall back to hardcoded heuristic weights, completely bypassing the online learning loop that the spec considers central to the method.

**Solution A:** In the evolution loop, after each fully evaluated child (i.e. after stage 4 completes or any earlier stage produces fully labeled results), extract features and labels from the run results and trace, call `self.shell.predictors.add_observation(...)` for each relevant decision family, and call `self.shell.predictors.maybe_retrain(fully_evaluated_count, accepted_count)`. Maintain running counters for `fully_evaluated_children` and `accepted_elites` since the last retrain.

**Solution B:** Create a `PredictorUpdater` component that hooks into the evaluator's output. After each `staged_evaluate` call, the updater inspects the `stage_results` and `SuiteEvaluation`, extracts labeled observations (topology success, compaction success, retrieval success, etc.) from traces, and feeds them to the model bank. This separates the predictor-update logic from the evolution loop, making it testable and reusable.

---

## Error 14 — Predictor Labels Are Never Extracted From Traces

**Spec §5.3 · Severity: 🔴 High · File: N/A (missing code)**

The spec provides detailed definitions of success labels for every decision family: a topology action succeeds if its child contributes an accepted artifact; a compaction action succeeds if later steps do not require raw-transcript fallback; a retrieval action succeeds if the retrieved node is consumed and not contradicted; tool reuse succeeds if the tool executes and passes checks; model choice succeeds if no forced escalation occurs; verification succeeds if it changes a downstream decision; stopping succeeds if it yields a verifier-positive terminal artifact. The runtime does record rich trace events (`agent_start`, `tool_operation`, `check_result`, `compaction`, `stop`, etc.), but no code anywhere in the codebase reads these traces after-the-fact and converts them into labeled observations for the predictor model bank. Without this label-extraction pipeline, the entire predictor system cannot learn, regardless of whether the observation-adding and retraining infrastructure exists.

**Solution A:** Create a `trace_labeler.py` module with functions like `extract_topology_labels(trace) -> list[PredictorObservation]`, `extract_compaction_labels(trace) -> ...`, etc. Each function walks the trace events, identifies relevant decision points, determines success/failure by looking at subsequent events, and returns labeled observations. Call these functions from the evolution loop after each evaluation.

**Solution B:** Embed the label logic directly into the runtime by having the runner record "outcome" events at the end of each decision. For example, after a child finishes, emit a `topology_outcome` event with `success=True/False`. After the run completes, iterate over these outcome events to produce predictor observations. This approach labels in real-time rather than post-hoc.

---

## Error 15 — Archive `trace_refs` Duplicated Across All Objective Cells (Minor)

**Spec §2.3 · Severity: 🟢 Low · File: `archive.py:231`**

The spec requires `ArchiveEntry` to have a `trace_refs` field referencing the traces from the full evaluation. The current code populates `trace_refs` correctly from `run.trace_path` for each run result. However, the archive insertion loop iterates over every objective score in the evaluation and creates a separate `ArchiveEntry` for each, with each entry carrying the same complete set of `trace_refs`. This is not a correctness violation (all required fields are present), but it means the same trace paths are stored redundantly across potentially dozens of archive cells, wasting memory.

**Solution A:** This is cosmetic. No fix strictly required. If desired, store trace refs on the `RuntimeDescriptor` or a shared evaluation record, and reference them by hash from `ArchiveEntry` to avoid duplication.

**Solution B:** Accept the duplication as a minor memory cost. Archive entries are lightweight data objects, and the trace refs are just file-path strings. The overhead is negligible compared to the actual trace files on disk.

---

## Error 16 — Long-Term Memory Carryover Validation Is a No-Op

**Spec §3.3, §13.5 · Severity: 🟡 Medium · File: `shell.py:122-127`**

The spec lists "long-term memory carries across tasks when transfer is not explicitly scored" as a hard-invalidation condition. The `FixedShell.validate_invariants` method is called on every step of the runtime loop and is intended to enforce these invariants. However, the body of the non-transfer-scored branch is simply `pass` — a placeholder that performs no actual validation. This means that if a bug caused long-term memory to leak across independent tasks (e.g. if `reset_for_task` were accidentally skipped), the runtime would not detect or flag the invariant violation, silently producing results contaminated by cross-task information leakage.

**Solution A:** Add an actual check: if `transfer_scored is False`, verify that `self.long_term` is empty (or was last reset at the start of the current task). Maintain a `_last_reset_task_id` flag on the shell, set it in `reset_for_task`, and in `validate_invariants` assert that it matches the current task being evaluated.

**Solution B:** Remove the no-op and replace it with an assertion that `len(self.long_term.nodes) == 0` when `transfer_scored is False` and the method is called at the beginning of a task run (before any context ingestion). Since `reset_for_task` clears long-term memory, this assertion would catch any case where the reset was skipped or where nodes were added by a previous task.

---

## Error 17 — Predictors Not Reset Between Tasks

**Spec §3.2 · Severity: 🟢 Low · File: `shell.py:108-114`**

The spec states that "dynamic agents, dynamic tools, and short-term memory always reset between tasks" and that "candidate-specific learned predictor parameters do not leak validation or test information back into mutation." The `reset_for_task` method resets short-term memory, the message board, open handles, and task-local tools, but it does not reset `self.predictors`. While the predictor bank is not task-specific per se (it accumulates observations across the entire evolution run), within a single candidate's multi-task evaluation, observations from earlier tasks could influence predictions for later tasks, potentially creating an information leak within the evaluation unit that the spec wants to prevent.

**Solution A:** Add `self.predictors.freeze()` at the start of each evaluation and `self.predictors.unfreeze()` after, where `freeze` prevents any new observations from being added or any retraining from occurring during the evaluation. This matches the spec's requirement that "surrogates are frozen during every parent-child comparison."

**Solution B:** Store the predictor state at the beginning of `evaluate_runtime` and restore it at the end, so that observations added during one candidate's evaluation do not carry into the next. This is equivalent to treating predictor state as part of the shell state that should be isolated per evaluation.

---

## Error 18 — Merge Order for Horizontal Workers (PASS)

**Spec §7.5, §13.9 · Severity: ✅ Pass · File: `topology_policy.py:131-142`**

The spec requires deterministic merge order: verified artifacts first, then by verifier support score descending, then by predicted solve probability descending, then by unresolved-critical-count ascending, and finally by lexicographic worker ID. The `merge_ensemble` method sorts by the tuple `(0 if verified else 1, -verifier_support, -predicted_solve, unresolved_critical, worker_id)`, which exactly matches the spec's ordering. This is correct and requires no fix.

---

## Error 19 — Scope Credit Only Updated for Stage-4 Completions

**Spec §6.3 · Severity: 🟡 Medium · File: `evolution.py:233-235`**

The spec states: "Credit is updated for every fully evaluated child, whether or not it enters the archive." This means the scope scheduler should receive credit signals from all children that complete evaluation, including those rejected at stages 2, 3, or even those that complete Stage 4 but are not inserted because they fail the elite-replacement criterion. The current code only updates scope credit inside the `if child_dir is not None and stage4 is not None and stage4.suite_evaluation is not None and not stage4.suite_evaluation.invalid` block, meaning children that complete Stage 2 or Stage 3 but fail to advance to Stage 4, or Stage 4 children with invalid evaluations, never contribute to scope credit. This biases the credit signal toward successful mutations only, starving underperforming scopes of the negative-credit signal they need.

**Solution A:** Move the `scheduler.update_scope_credit` call outside the Stage-4-only block. Compute a credit delta for any child that has at least one `SuiteEvaluation` (from Stage 2, 3, or 4). Use the best available evaluation to compute the delta, even if the child did not complete Stage 4.

**Solution B:** Add an `else` branch after the Stage-4 success block that still calls `scheduler.update_scope_credit` with a negative delta (e.g. `−0.01`) for children that were evaluated but failed. This ensures that scopes producing consistently failing mutations receive negative credit, which the stagnation and need counters can then use to adjust sampling probabilities.

---

## Error 20 — `RuntimeState` Does Not Encapsulate All Spec-Required State Components

**Spec §3.1 · Severity: 🟢 Low · File: `runtime_api.py:72-86`**

The spec defines runtime state $z_t$ as containing the active agent queue, short-term execution graph, long-term graph, visible tool-registry slice, budget state, open async handles, verifier evidence state, and current confidence/unresolved-goal statistics. The `RuntimeState` dataclass contains the queue, visible tool names, unresolved goals, confidence, mode, and various counters, but the short-term graph, long-term graph, budget, and verifier evidence are stored separately on the `FixedShell` and `RuntimeBudget` objects, accessed through the `PolicyContext`. This means `RuntimeState` alone cannot fully describe the runtime's state at any point — it must always be paired with the shell and budget to reconstruct the spec's $z_t$, which complicates serialization, checkpointing, and debugging.

**Solution A:** Add references to the shell's graphs and budget directly on `RuntimeState`, making it a true encapsulation of the spec's $z_t$. This could be done by adding `short_term: ShortTermGraph`, `long_term: LongTermGraph`, and `budget: RuntimeBudget` fields to the dataclass.

**Solution B:** Leave `RuntimeState` as-is but create a `RuntimeSnapshot` method on `PolicyContext` that packages `state`, `shell.short_term`, `shell.long_term`, `budget`, and `shell.open_handles` into a single frozen object for logging, checkpointing, and debugging purposes. This avoids restructuring the existing dataflow while providing spec-compliant state encapsulation when needed.

---

## Error 21 — `validate_invariants` Does Not Re-Check Short-Term Compaction Reachability

**Spec §3.3 · Severity: 🟢 Low · Files: `shell.py`, `memory_graph.py`**

The spec lists "short-term compaction destroys raw-output reachability" as a hard-invalidation condition. The `ShortTermGraph.summary_replace` method does validate reachability inline via `_validate_raw_reachability`, which checks that all replaced raw nodes are reachable from the new summary node through backlinks. This works correctly at the moment of compaction. However, `shell.validate_invariants` (called on every step of the main loop) does not re-check this invariant. If any code were to later delete or modify edges in the short-term graph (which is supposed to be append-only but is not enforced at the type level), the reachability invariant could be silently broken without detection.

**Solution A:** Add a `validate_reachability` method to `ShortTermGraph` that iterates over all hidden nodes and verifies each is reachable from at least one Summary node. Call this from `shell.validate_invariants`.

**Solution B:** Make `ShortTermGraph` truly append-only by removing any ability to delete nodes or edges. If nodes and edges can only be added, the reachability invariant cannot degrade after initial validation in `summary_replace`, making the re-check unnecessary.

---

## Error 22 — No Support for Ordered Episode Evaluation Units

**Spec §3.2 · Severity: 🟢 Low · File: `evaluator.py:48-52`**

The spec defines an evaluation unit as either a single task (when transfer is not scored) or an ordered episode of tasks $(x_1, ..., x_m)$ when transfer is explicitly part of the benchmark objective. For episodes, long-term memory should carry across tasks within the episode, while still resetting between independent evaluation units. The current evaluator processes all tasks in a flat loop, calling `runner.run_task(task, seed)` independently for each. The `BenchmarkTask` schema has a `transfer_scored` field that controls whether long-term memory is reset, but there is no grouping mechanism that identifies which tasks form an episode or ensures they are evaluated in order. If a benchmark required evaluating a sequence of tasks where later tasks depend on knowledge stored from earlier tasks, the evaluator would not preserve the correct evaluation order or long-term memory state.

**Solution A:** Add an `episode_id` and `episode_order` field to `BenchmarkTask`. In the evaluator, group tasks by `episode_id`, sort within each group by `episode_order`, and evaluate them sequentially without resetting long-term memory between tasks in the same episode.

**Solution B:** Add an `Episode` schema containing an ordered list of task IDs and a `transfer_scored: bool` flag. In `BenchmarkSuite`, maintain a list of episodes. The evaluator iterates over episodes rather than individual tasks, resetting long-term memory only at episode boundaries.

---

## Error 23 — Benchmark Adapter Immutability Not Enforced

**Spec §3.3, §16.1 · Severity: 🟢 Low · File: `benchmarks.py`**

The spec states that mutating or bypassing the benchmark adapter is a hard-invalidation condition, and that "benchmark graders, storage backends, sandbox boundaries, benchmark prompts, safety prompts, environment caches, and graph query engines may not [mutate]." The `BenchmarkSuite` and `BenchmarkTask` objects are plain mutable Pydantic models and dataclasses. Any mutable policy method could accidentally modify a task's `expected` field, `prompt`, or `operations` list during evaluation without triggering any error. Since these objects are shared across multiple seeds and evaluations, such mutations would corrupt all subsequent evaluations silently.

**Solution A:** Make `BenchmarkTask` and `BenchmarkSuite` frozen. For Pydantic v2, set `model_config = ConfigDict(frozen=True)`. For v1, set `Config.allow_mutation = False`. This raises an error on any attempt to modify fields after construction.

**Solution B:** Deep-copy each `BenchmarkTask` before passing it to `runner.run_task`, so that any mutations during the run are scoped to that copy and do not affect the original suite. This is more permissive but prevents cross-evaluation corruption.

---

## Error 24 — `launch_async` Closes File Handles Before Subprocess Finishes Writing

**Severity: 🔴 High (Bug) · File: `tool_runtime.py:380-384`**

The `launch_async` method opens file handles for stdout and stderr, passes them to `subprocess.Popen` as the subprocess's output destinations, and then immediately closes both handles on the very next lines. On Windows, when the parent process closes a file handle that a child subprocess inherited for writing, the child's writes may fail with a broken-pipe or access-denied error, especially if the child has not yet started writing or if the output is buffered. This is a race condition: for very fast tools it may work because the subprocess finishes before the close, but for any tool that takes more than a few milliseconds, the subprocess may crash or produce truncated output. The `wait_async` method then reads potentially empty or incomplete files, leading to spurious tool failures.

**Solution A:** Do not close the file handles immediately. Store them alongside the process in `self._async_processes` (e.g. as a tuple `(process, stdout_handle, stderr_handle)`) and close them only in `wait_async` after the process has completed. This ensures the file handles remain valid for the entire duration of the subprocess.

**Solution B:** Use `subprocess.Popen` with `subprocess.PIPE` instead of file handles, and capture the output in `wait_async` using `process.communicate(timeout=...)`. Write the captured output to the stdout/stderr files after the process completes. This avoids the handle-lifetime issue entirely and is more idiomatic for subprocess management.

---

## Error 25 — `OpenAIPatchMutator` Pre-Copies Runtime Directory Unnecessarily

**Severity: 🟢 Low · File: `mutator.py:148`**

The `OpenAIPatchMutator.mutate` method copies the parent runtime directory to a child directory and sets the candidate's `runtime_dir` to this copy. However, the evaluator's `stage0_patch_integrity` creates its own separate copy from the original `parent_dir` and applies the patch there, completely ignoring the candidate's `runtime_dir` field. This means the mutator performs a full directory copy (`shutil.copytree`) that is never used for evaluation — it's dead work that wastes I/O and disk space. Additionally, the `runtime_dir` on the `MutationCandidate` points to an unpatched copy, which could confuse any code that tries to inspect or log the candidate's source files.

**Solution A:** Remove the `shutil.copytree` from `OpenAIPatchMutator.mutate` and set `runtime_dir` to `str(context.runtime_dir)` (the parent directory). The evaluator will create its own patched copy as it already does. This eliminates the wasted I/O.

**Solution B:** Keep the copy but apply the patch in the mutator as well (symmetric with `HeuristicPatchMutator`), and update the evaluator to use the candidate's `runtime_dir` as the starting point for `stage0_patch_integrity` instead of the parent directory. This makes the mutator self-contained but requires adjusting the evaluator interface.

---

## Error 26 — Validation Task `val.e2e.bundle` Missing `symbolic_seeds`

**Severity: 🟢 Low · File: `benchmarks.py:223-236`**

The `val.e2e.bundle` benchmark task uses `context_items` containing a symbol `RATE` with value `2` and its operation uses `requires_exact_symbol="RATE"` for memory lookup. However, unlike all other e2e tasks in the suite (which set `symbolic_seeds=["FEE_RATE"]`, `symbolic_seeds=["SCALE"]`, etc.), this task does not set `symbolic_seeds` at all, letting it default to an empty list. Memory retrieval in `_execute_memory_lookup` falls back to `context.task.symbolic_seeds` when `operation.requires_exact_symbol` is not set, but in this case `requires_exact_symbol` is set so the lookup still works. Nonetheless, the missing `symbolic_seeds` means this task cannot benefit from the spec's exact-symbol-dominance retrieval path for any operations that do not explicitly set `requires_exact_symbol`, creating an inconsistency with the other e2e tasks.

**Solution A:** Add `symbolic_seeds=["RATE"]` to the `val.e2e.bundle` task definition to match the pattern used by all other e2e tasks.

**Solution B:** Add a validation step to `BenchmarkSuite` (or a test) that checks every task using `memory_lookup` operations has consistent `symbolic_seeds` matching the symbols referenced in its `context_items` and `requires_exact_symbol` fields.

---

## Error 27 — `HeuristicPatchMutator` Applies Patches Directly, Duplicating Evaluator Work

**Spec §11.1 · Severity: 🟡 Medium · File: `mutator.py:97-124`**

The spec requires that patches are exact SEARCH/REPLACE blocks applied uniquely. The evaluator's `stage0_patch_integrity` is responsible for applying these patches with full validation: checking uniqueness, verifying mutable boundaries, enforcing block and line limits, and parsing the AST. The `HeuristicPatchMutator` applies the patches itself using `str.replace` directly to files in a copied child directory, and then builds the SEARCH/REPLACE blocks as the patch text. When the evaluator later processes this candidate, it copies the *original parent* directory (not the mutator's pre-patched copy) and applies the patch text to that fresh copy. This means the heuristic mutator's direct file modifications are always overwritten, making the entire `shutil.copytree` and `str.replace` work in the mutator redundant. More importantly, if the mutator's string replacement logic ever diverges from the evaluator's (e.g. handling edge cases differently), the patched-and-returned candidate directory would be inconsistent with the actually-evaluated code.

**Solution A:** Remove the file-editing logic from `HeuristicPatchMutator.mutate`. Have it only select the SEARCH/REPLACE blocks and build the patch text, setting `runtime_dir` to the parent directory. The evaluator will handle the actual file copying and patching. This eliminates the redundant work and the risk of divergence.

**Solution B:** Switch the evaluator to use the mutator's pre-patched directory as the starting point instead of re-copying from the parent. Modify `stage0_patch_integrity` to accept a `child_dir` directly and validate the already-applied patch by comparing the child source against the parent source, rather than re-applying the patch. This respects the mutator's work but requires rethinking the evaluator's trust model.

---

## Error 28 — Spec Document Lists "Four Goals" But Only Enumerates Three

**Spec §1 · Severity: ℹ️ Note · File: `PROJECT TARGET SPEC.md:19-23`**

The spec text at line 18 says "The design is constrained by four goals" but then lists exactly three numbered items: (1) Bounded mutability, (2) Deterministic replay, and (3) Subsystem co-evolution. The fourth goal is missing from the enumeration. This is a spec-document error, not a code error.

**Solution A:** Review the spec and add the missing fourth design goal. Based on the spec's emphasis throughout, the fourth goal is likely something related to "Safety and immutable boundaries" or "Verifier-driven selection."

**Solution B:** Change "four goals" to "three goals" in the spec text if no fourth goal was intended.

---

# Additional Code Review Issues

These items are not duplicates of the existing spec-audit errors above. They come from review of the current uncommitted changes and are included here so the document captures both spec-level gaps and patch-level regressions.

---

## Error 29 - Mutable-method boundary check only validates the search span

**Severity: High - File: `agintor/evaluator.py:119-123`**

The new mutable-method boundary enforcement only computes the protected line range from `block.search`, then applies `block.replace` without checking whether the replacement text extends beyond the allowed method body. That means a patch can match text inside a permitted method and replace it with dedented code that adds a helper function or other top-level statements outside the contract boundary, while still passing stage 0 as long as the file parses. I reproduced this by replacing the tail of `rank_tools()` with the same lines plus a top-level `def`, and `stage0_patch_integrity()` still returned success. This is a real gap in the new guardrail, because the evaluator now appears to enforce method-level mutability while still allowing mutations to escape the intended surface.

**Solution A:** Validate the replacement span, not just the matched span. Compute the starting line from the unique search match, compute the replacement end line from `block.replace`, and reject the patch unless the entire replacement range stays inside one of the allowed method ranges.

**Solution B:** Apply the replacement to an in-memory copy of the source first and then compare the original and updated line ranges or AST structure to determine which lines actually changed. This is more robust because it catches insertions, dedents, and other edits that begin inside an allowed method but end outside it.

---

## Error 30 - Smoke determinism check now depends on volatile trace fields

**Severity: Medium - File: `agintor/evaluator.py:151-161`**

`stage1_smoke()` now compares the full serialized trace for equality across two runs, which makes the determinism gate fail whenever the smoke task emits trace fields that are expected to vary between otherwise identical executions. The clearest example is `model_response` tracing: `PolicyContext.consume_model_response()` records `latency_s`, and that latency naturally changes from run to run even under the deterministic local provider. I confirmed this by making `proxy.tool.provider_synthesis` the first proxy task in a test suite; artifact, verifier score, and execution mode matched on both runs, but `stage1_smoke()` still reported nondeterminism because the trace payloads were not byte-for-byte identical. As a result, valid children can be rejected solely because the smoke task made a provider call or emitted timing-sensitive trace metadata.

**Solution A:** Normalize traces before comparison by stripping, rounding, or otherwise ignoring volatile fields such as `latency_s`, timestamps, process IDs, or other runtime-specific metadata that is not part of semantic determinism.

**Solution B:** Stop using full-trace equality as the determinism criterion and compare only stable invariants, such as artifact, verifier score, mode, ordered event names, and a small allowlist of deterministic payload fields.

---

## Error 31 - Pytest cache override points at a path that already warns on Windows

**Severity: Low - File: `pyproject.toml:40`**

The new global `cache_dir = "tests/_artifacts/pytest/cache"` setting currently produces `PytestCacheWarning: could not create cache path ... Access is denied` on a normal Windows `pytest -q` run in this repository, which means the cache is effectively disabled and otherwise clean test runs now emit warnings. Because this path is configured at the project level, every local run and CI invocation inherits the same behavior until that directory layout and permission state happen to line up. Even though this does not break the suite outright, it is still a real regression in developer experience and test hygiene, because it adds noise to routine runs and removes the benefit of pytest's cache in environments where the path is not writable.

**Solution A:** Remove the explicit `cache_dir` override and let pytest use its default cache location. That restores the previous behavior immediately and avoids path-specific permission issues.

**Solution B:** Keep a repo-local cache only if you first move it to a path that is known to be writable in your target environments and is not affected by artifact cleanup or existing directory permissions. If you keep it under `tests/_artifacts`, add a setup step that guarantees the full parent directory tree exists with the correct permissions before pytest starts.
