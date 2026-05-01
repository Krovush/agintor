from __future__ import annotations

from .restore import CheckpointRestoreMixin
from .publication import CheckpointPublicationMixin
from .snapshots import CheckpointSnapshotMixin
from .recovery import CheckpointRecoveryMixin
from .results import CheckpointResultMixin

class CheckpointingMixin(CheckpointResultMixin, CheckpointRecoveryMixin, CheckpointSnapshotMixin, CheckpointPublicationMixin, CheckpointRestoreMixin):
    pass
