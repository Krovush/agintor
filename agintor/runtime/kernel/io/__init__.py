from __future__ import annotations

from .paths import BoundedPathMixin
from .repo_patch import RepoPatchIOMixin
from .service_action import ServiceActionIOMixin

class BoundedIOMixin(ServiceActionIOMixin, RepoPatchIOMixin, BoundedPathMixin):
    pass
