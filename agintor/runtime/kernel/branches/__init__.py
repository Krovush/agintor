from __future__ import annotations

from .budget import BranchBudgetMixin
from .providers import BranchProviderMixin
from .resume import BranchResumeMixin
from .execution import BranchRunMixin
from .results import BranchResultMixin

class BranchExecutionMixin(BranchResultMixin, BranchRunMixin, BranchResumeMixin, BranchProviderMixin, BranchBudgetMixin):
    pass
