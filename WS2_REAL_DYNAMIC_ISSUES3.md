# WS2 Real Dynamic Issues 3

## Immediate Before WS3

### Service-action node semantics are still broader than the frozen `service/http` contract

- Area:
  - `agintor/schemas.py`
  - `_validate_service_action_node()`
  - `agintor/runner.py`
  - `_execute_service_action_node()`
  - `agintor/runtime_api.py`
  - `_service_request_spec()`
- Current behavior:
  - the new `service_action` plan node only validates that `url` is non-empty and that the HTTP verb is one of the allowed methods
  - non-HTTP schemes such as `file://...` therefore compile and execute through `urllib.request.urlopen(...)`
  - on runtimes where service actions are enabled, this broadens the solve-time contract from bounded HTTP/service calls into arbitrary URL handlers
  - `file://...` is especially bad here because it can bypass the intended `service/http` boundary and currently crashes on the response-shaping path when no HTTP status is present
- Why this is immediate before WS3:
  - WS2 is supposed to hand WS3 a frozen, typed meaning for `service_action`
  - WS3 durability, receipts, and replay semantics should not be built on top of a node kind whose actual meaning is still "any `urllib` URL" instead of bounded HTTP/service access
  - this is not just a rare prompt quirk; it is a contract-definition problem for one of the new execution-plan node kinds
- Least-retarded solution:
  - do not bolt on scattered raw-string guardrails in multiple call sites
  - define one transport-derived compatibility helper for `service_action`, driven by the node's declared meaning rather than by ad hoc URL filtering
  - derive the expected transport from plan semantics already present in the node, such as:
    - explicit metadata like `service_transport`, or
    - the existing category hint / `allowed_tool_categories` value `service/http`
  - derive allowed URL schemes from that transport family in one place
  - for the current frozen WS2 contract, `service/http` should resolve to `http` and `https`
  - use that same helper in both:
    - `agintor/schemas.py`
    - `_validate_service_action_node()`
    - `agintor/runner.py`
    - `_execute_service_action_node()`
  - this keeps the code dynamic: if a later workstream intentionally introduces another service transport, the transport-to-scheme mapping is extended once instead of hardcoding new conditionals everywhere
- Input-side hardening that should accompany the fix:
  - harden prompt adaptation so the compiler stops generating known-invalid `service_action` inputs in the first place
  - `_service_request_spec()` should normalize and validate the transport family before it emits a `service_action` task
  - if the request currently maps to `service/http`, only `http` and `https` URLs should compile into this node kind
  - non-HTTP inputs should not be silently accepted and left for the runtime to trip over later
  - if they are useful, they should either:
    - compile into a different explicit capability later, or
    - fail as a typed adaptation mismatch instead of smuggling new semantics into `service_action`
- Follow-up target:
  - freeze `service_action` as a transport-derived HTTP/service capability rather than a generic `urllib` escape hatch
  - make validator and executor share the same compatibility rule so bad inputs fail closed consistently and intentional future transports remain easy to add

### Host preflight still launches runtime-incompatible execution plans

- Area:
  - `agintor/runtime_host.py`
  - `_preflight_solve_contract()`
  - `_preflight_batch_contracts()`
  - execution-plan/runtime-contract compatibility checks
- Current behavior:
  - host preflight currently validates runtime credentials, but it does not reject prompt-mode or benchmark-mode plans whose compiled nodes exceed the runtime contract
  - requests that compile into disallowed capabilities can therefore create durable runs and attempts, launch the runtime entrypoint, and only fail after the runtime has already entered solve execution
  - a concrete example is a prompt that compiles into `service_action` on a runtime whose deployment contract forbids that network surface
- Why this is immediate before WS3:
  - WS2 explicitly defines one host/runtime isolation contract and requires plan compilation to stay within deployment-contract and runtime-plan bounds before execution enters `running`
  - if we move to WS3 with this still unfixed, WS3 will persist and build on a solve boundary where runtime-incompatible plans are accepted as launchable work instead of being rejected at the contract boundary
  - this weakens the handoff more than a reporting bug would, because it leaves the frozen solve-time contract semantically incomplete
- Least-retarded solution:
  - do not validate prompt templates or demand that plans match one narrow canonical shape
  - compile the execution plan exactly as today, then validate capability compatibility of the compiled plan against the selected runtime contract
  - add one host-side plan-requirements pass that walks the compiled nodes and derives the capabilities the plan actually needs, for example:
    - network/service surface
    - writable filesystem surface
    - provider-backed model access
    - any other explicitly declared runtime-side capability that is already frozen in WS2 contracts
  - compare those derived requirements against:
    - `CapabilityExchange`
    - deployment contract fields
    - runtime isolation policy
    - effective backend guarantees
  - reject only real contract mismatches before launch
  - this preserves flexibility: a weird or improved plan still runs as long as its requirements fit the runtime contract
- Input-side hardening that should accompany the fix:
  - harden the prompt-mode and task-to-plan compilation path so it preferentially emits nodes whose declared capability surfaces match the current request/tool intent, instead of emitting semantically questionable nodes and relying on runtime failure later
  - the compiler should stamp explicit capability intent into node metadata and categories so host preflight does not need to infer everything from brittle heuristics
  - this is not template policing; it is making the generation side produce cleaner, more explicit plan semantics
- Follow-up target:
  - add compiled-plan-vs-runtime compatibility checks during host preflight for solve and batch paths
  - reject only disallowed capability requirements, not unfamiliar but contract-compatible plan shapes
  - move the boundary so invalid plans fail before run creation or runtime launch, while still allowing novel plans that remain inside the runtime contract
