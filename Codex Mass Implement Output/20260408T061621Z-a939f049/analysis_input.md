**Findings** 

1. [P1] Benchmark-plan normalization can silently drop required family coverage while keeping `family_targets` unchanged in [runtime_builder.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_builder.py#L223).  
   I reproduced this against the current code with a fresh goal-conditioned suite and a partially-updated `BenchmarkPlan` payload that kept only `top.*` task IDs plus the synthetic goal tasks:

   ```powershell
   @'
   from agintor.goal_rubric import build_goal_spec
   from agintor.runtime_builder import build_goal_conditioned_suite, _build_benchmark_plan, _normalize_benchmark_plan_against_suite
   from agintor.runtime_profile import load_runtime_profile
   from agintor.pydantic_compat import model_dump, model_validate
   from agintor.schemas import BenchmarkPlan

   profile = load_runtime_profile()
   goal = build_goal_spec("Build a multiagent memory retrieval runtime", runtime_provider_name=profile.runtime_provider.name)
   suite = build_goal_conditioned_suite(goal, profile)
   plan = _build_benchmark_plan(goal, suite)
   payload = model_dump(plan)
   payload["train_task_ids"] = [task_id for task_id in plan.train_task_ids if task_id.startswith("top.")] + list(plan.synthetic_task_ids)
   payload["val_task_ids"] = [task_id for task_id in plan.val_task_ids if task_id.startswith("val.top.")]
   payload["test_task_ids"] = [task_id for task_id in plan.test_task_ids if task_id.startswith("test.top.")]
   normalized = _normalize_benchmark_plan_against_suite(model_validate(BenchmarkPlan, payload), suite, goal_spec=goal)
   print({
     "target_families": goal.target_families,
     "train_task_ids": normalized.train_task_ids,
     "val_task_ids": normalized.val_task_ids,
     "test_task_ids": normalized.test_task_ids,
   })
   '@ | python -
   ```

   Observed result:

   ```python
   {
     'target_families': ['mem', 'top'],
     'train_task_ids': ['top.sum_product', 'top.extrema_median', 'goal.capability....', 'goal.capability....'],
     'val_task_ids': ['val.top.range'],
     'test_task_ids': ['test.top.aggregate']
   }
   ```

   The normalized plan still advertises `family_targets = ['mem', 'top']`, but `mem.*` pressure is gone from `train`, `val`, and `test`. In Workstream 1 terms, that violates the frozen-planning contract: downstream verifier freeze, evolution, and leader validation are no longer consuming benchmark pressure faithful to the normalized goal.

2. [P1] Solve preflight rejects valid hosted-provider invocations when credentials are already present in the provider payload in [runtime_host.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_host.py#L146).  
   I reproduced this with a fresh exported runtime and an explicit `--api-key-file` solve:

   ```powershell
   'dummy-key' | Set-Content '.tmp_dummy_key.txt'
   python -m agintor.cli solve ".tmp_review_export" --prompt "Say hello" --provider minimax --api-key-file ".tmp_dummy_key.txt" --runtime-backend local --workspace ".tmp_solve_ws3"
   ```

   Observed failure:

   ```text
   RuntimeLoadError: missing required runtime environment variables for .tmp_review_export:
   one of AGINTOR_MAS_MINIMAX_API_KEY, AGINTOR_MAS_MINIMAX_KEY_FILE
   ```

   This is a real Workstream 1 boundary bug. `build_provider()` had already built a usable provider object from the explicit key-file input, but `_preflight_solve_contract()` only inspected `os.environ`, so the host rejected a valid request before launch.

3. [P1] Solve preflight under-approximates provider requirements for prompt-mode solves and can let hosted-provider failures escape past the boundary in [runtime_host.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_host.py#L326) and [memory_policy.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/templates/baseline_runtime/memory_policy.py#L69).  
   I reproduced this with a prompt-mode memory lookup request that forces compaction before task execution by stuffing large `context_items` into the request and shrinking `context_window_tokens`:

   ```powershell
   @'
   import json
   blob = 'x' * 600
   payload = {
       'prompt': 'What is the value of MEMORY_ALPHA?',
       'context_items': [
           {'symbol': 'MEMORY_ALPHA', 'value': 'alpha-value', 'blob': blob},
           {'note': blob},
           {'note': blob},
           {'note': blob},
           {'note': blob},
           {'note': blob},
       ],
       'budget_overrides': {'context_window_tokens': 32}
   }
   with open('.tmp_prompt_memory_compact.json', 'w', encoding='utf-8') as f:
       json.dump(payload, f)
   '@ | python -

   python -m agintor.cli solve ".tmp_review_export" --prompt-file ".tmp_prompt_memory_compact.json" --runtime-backend local --workspace ".tmp_solve_ws5"
   ```

   Observed failure: preflight allowed the request, but the exported runtime then crashed in `memory_policy.summarize_span()` when that path called `ctx.provider.generate()` during compaction. The host surfaced a raw runtime traceback ending in:

   ```text
   AgintorError: minimax credentials are required for hosted model calls.
   Provide one of: AGINTOR_MAS_MINIMAX_API_KEY, AGINTOR_MAS_MINIMAX_KEY_FILE.
   ```

   The current preflight heuristic only treats `direct_response` and expression-less `generated_expression` as provider-dependent. That is too narrow for the Workstream 1 protocol boundary: prompt-mode solve can still require the default hosted provider indirectly through runtime-owned side paths like memory compaction.

**Methodology**

I kept this review inside Workstream 1 and focused on frozen planning artifacts plus the host/runtime protocol boundary.

I used this sequence:

1. Static audit of the current uncommitted Workstream 1 code in:
   - [runtime_builder.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_builder.py)
   - [runtime_host.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_host.py)
   - [runtime_api.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/runtime_api.py)
   - [templates/baseline_runtime/memory_policy.py](C:/Users/yaros/Desktop/Agintor%20MVP/agintor/templates/baseline_runtime/memory_policy.py)

2. Regression check:
   - `pytest -q tests/test_runtime_builder.py tests/test_runtime_host.py`
   - This passed on this machine.

3. Fresh export repro target:
   - `python -m agintor.cli build-runtime "Build a memory retrieval runtime" --destination ".tmp_review_export" --workspace ".tmp_review_ws" --provider local --steps 1 --runtime-backend local --force`

4. Confirmed happy-path baseline before probing failures:
   - export built successfully
   - deterministic prompt-mode solve and eval still worked earlier in this workspace

5. Targeted manual repros:
   - Python inline normalization probe for the benchmark-plan issue
   - explicit `--api-key-file` CLI solve for the credential-payload issue
   - oversized-context prompt-file solve for the indirect-provider-use issue

That combination gave me one clean success baseline and three isolated failures tied to the exact boundary/normalization code under review.

**Trivial / localized fixes**

- Finding 2 is the most localized.
  - The host preflight should treat explicit provider credentials already loaded into the provider object as satisfying the contract.
  - The simplest safe fix is a helper that walks the provider object graph and accepts `api_key` or `api_key_file` on the resolved runtime provider before falling back to env-group checks.

- Finding 3 has one localized defensive improvement even if the broader preflight model stays simple.
  - Map runtime-side missing-credential failures into a structured contract-shaped solve failure instead of surfacing a raw traceback from inside the exported runtime.
  - That does not fully solve the under-approximation, but it restores boundary honesty.

**Non-trivial / design-sensitive fixes**

- Finding 1 is non-trivial because it affects frozen benchmark pressure, verifier freeze, evolution pressure, and leader validation.
  - Best fix: derive partition task IDs locally from `suite + family_targets` after provider refinement, and treat provider-supplied task lists as hints only.
  - Acceptable narrower fix: after filtering, rehydrate any partition that no longer covers all required target families back to the local default plan.
  - Add a post-normalization invariant: every family in `family_targets` must still be represented in the intended frozen pressure set.

- Finding 3’s proper fix is also non-trivial.
  - Short-term safe option: use a conservative rule for hosted default providers in prompt mode when the request shape can trigger provider-backed side paths such as memory compaction or tool synthesis.
  - Better Workstream 1 option: extend capability/preflight metadata so the host can reason about provider-dependent solve paths more honestly than the current operation-kind heuristic.
  - This should stay framed as a host/runtime contract issue, not as a broader solve-time policy redesign.

**Bottom line**

All three findings are in Workstream 1 scope when phrased correctly:

- Finding 1: frozen benchmark-pressure contract defect
- Finding 2: host/runtime credential-preflight defect
- Finding 3: host/runtime provider-requirement preflight defect
