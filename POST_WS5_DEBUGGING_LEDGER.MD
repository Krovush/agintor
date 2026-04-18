# Post-WS5 Debugging Ledger

## Deferred From WS2 Closure

### ReplayProvider branch-clone determinism under concurrent horizontal execution

- Area:
  - `agintor/providers.py`
  - `ReplayProvider.shared_clone()`
  - `clone_provider()`
- Current behavior:
  - horizontal branches that clone a `ReplayProvider` share one mutable coordinator cursor
  - replay row assignment is therefore scheduler-dependent under concurrent branches
- Why not fixed in this WS2 closure pass:
  - a correct fix needs branch/request-aware replay row routing or a revised replay recording format
  - simply giving each clone its own cursor snapshot would create a different correctness bug, where multiple branches can consume the same replay rows
  - this is a real determinism gap, but the durable solution is larger than a safe WS2 patch
- Follow-up target:
  - define a replay format or lookup policy keyed by request/trace identity before changing clone semantics
