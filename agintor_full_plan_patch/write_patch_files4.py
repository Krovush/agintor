from __future__ import annotations
from pathlib import Path
import textwrap
ROOT = Path('/mnt/data/agintor_full_plan_patch/new_files')
files={}
def add(p,c): files[p]=textwrap.dedent(c).lstrip()

add('agintor/oracle/families/__init__.py', r'''
from __future__ import annotations

__all__: list[str] = []
''')

FAMILY_TEMPLATE = r'''
from __future__ import annotations

from typing import Sequence

from ...contracts import ClaimSpec, ValidationIntent, ValidatorSpec
from ..validator_registry import ValidatorFamily
from ...utils import stable_hash

FAMILY_ID = {family_id!r}
KEYWORDS = {keywords!r}
AUTHORITY = {authority!r}
VISIBILITY = {visibility!r}
LEAKAGE_RISK = {leakage!r}
FAILURE_ACTION = {failure_action!r}
DESCRIPTION = {description!r}


def _score(intent: ValidationIntent, claims: Sequence[ClaimSpec]) -> float:
    haystack = " ".join([*intent.task_classes, *intent.required_capabilities, *(claim.text for claim in claims)]).lower()
    hits = sum(1 for keyword in KEYWORDS if keyword in haystack)
    if FAMILY_ID in intent.task_classes:
        hits += 3
    return min(1.0, hits / max(1, min(4, len(KEYWORDS))))


def _build(intent: ValidationIntent, claims: Sequence[ClaimSpec]) -> list[ValidatorSpec]:
    applicable = [claim for claim in claims if _score(intent, [claim]) >= 0.2]
    if not applicable and claims:
        applicable = [claims[0]]
    return [
        ValidatorSpec(
            validator_id=f"{{FAMILY_ID}}.{{stable_hash(claim.claim_id, claim.text)[:10]}}",
            family_id=FAMILY_ID,
            claim_ids=[claim.claim_id],
            inputs={{
                "claim_text": claim.text,
                "task_classes": list(intent.task_classes),
                "required_capabilities": list(intent.required_capabilities),
            }},
            outputs_schema={{
                "type": "object",
                "properties": {{
                    "status": {{"enum": ["pass", "fail", "abstain", "error"]}},
                    "observations": {{"type": "object"}},
                }},
                "required": ["status"],
            }},
            authority_ceiling=AUTHORITY,
            visibility=VISIBILITY,
            independence_group=FAMILY_ID,
            leakage_risk=LEAKAGE_RISK,
            health_tests=["positive_control", "negative_control"],
            failure_action=FAILURE_ACTION,
        )
        for claim in applicable
    ]


def make_family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id=FAMILY_ID,
        description=DESCRIPTION,
        authority_ceiling=AUTHORITY,
        visibility=VISIBILITY,
        leakage_risk=LEAKAGE_RISK,
        failure_action=FAILURE_ACTION,
        can_handle=_score,
        build_specs=_build,
        health_tests=("positive_control", "negative_control"),
    )


__all__ = ["make_family"]
'''

families = {
    'exact_private_answer.py': ('exact_private_answer', ['exact','private','answer','expected','hidden'], 'A4', 'sealed', 'medium', 'reject', 'Private-answer validator family for deterministic hidden-answer checks.'),
    'schema_artifact.py': ('schema_artifact', ['schema','json','artifact','report','structured'], 'A3', 'public', 'low', 'abstain', 'Schema and artifact contract validator family.'),
    'repo_patch.py': ('repo_patch', ['repo','patch','code','test','file','swe'], 'A4', 'sealed', 'medium', 'reject', 'Repo patch validator family using public/hidden tests and diff sanity.'),
    'stateful_service.py': ('stateful_service', ['service','api','state','tool','workflow','policy'], 'A4', 'sealed', 'high', 'quarantine', 'Stateful service/tool policy validator family.'),
    'trace_state.py': ('trace_state', ['trace','trajectory','node','tool call','side effect','receipt'], 'A3', 'private', 'low', 'abstain', 'Trace and graph trajectory validator family.'),
    'factual_grounded.py': ('factual_grounded', ['factual','citation','source','grounded','research','freshness'], 'A3', 'private', 'medium', 'abstain', 'Grounded factuality and citation support validator family.'),
    'pairwise_preference.py': ('pairwise_preference', ['preference','human','compare','pairwise','rubric'], 'A2', 'private', 'medium', 'diagnostic', 'Pairwise preference validator family with authority caps.'),
    'trading_outcome.py': ('trading_outcome', ['trade','trading','portfolio','pnl','alpha','risk','order','fill'], 'A4', 'sealed', 'high', 'reject', 'Trading outcome validator family for finance runtimes.'),
    'human_audit.py': ('human_audit', ['human','audit','review','signed'], 'A5', 'private', 'medium', 'abstain', 'Signed human audit reference validator family.'),
    'inspect_runner.py': ('inspect_runner', ['inspect','sandbox','eval','scorer','task'], 'A4', 'private', 'medium', 'abstain', 'Inspect AI runner adapter validator family.'),
    'openai_eval_runner.py': ('openai_eval_runner', ['openai eval','grader','eval','completion'], 'A3', 'private', 'medium', 'abstain', 'OpenAI eval runner adapter validator family.'),
}
for filename, args in families.items():
    add('agintor/oracle/families/' + filename, FAMILY_TEMPLATE.format(
        family_id=args[0], keywords=args[1], authority=args[2], visibility=args[3], leakage=args[4], failure_action=args[5], description=args[6]
    ))

for p,c in files.items():
    t=ROOT/p; t.parent.mkdir(parents=True, exist_ok=True); t.write_text(c, encoding='utf-8')
print('wrote', len(files), 'files')
