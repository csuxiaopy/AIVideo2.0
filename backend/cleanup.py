from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from backend.config import Settings
from backend.database import utc_now
from backend.repository import Repository


logger = logging.getLogger(__name__)


class CleanupService:
    """Removes expired alerts and their evidence files."""

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    def run(self, override_days: int | None = None, severity: str | None = None) -> dict[str, Any]:
        retention = self.repository.get_retention_settings()
        effective_days = override_days or retention.alert_retention_days
        cutoff = utc_now() - timedelta(days=effective_days)
        rows = self.repository.list_alerts_before(cutoff, severity)
        root = self.settings.evidence_dir.resolve()
        evidence_removed = 0
        for row in rows:
            if not row.evidence_path:
                continue
            try:
                path = (self.settings.evidence_dir / row.evidence_path).resolve()
                path.relative_to(root)
            except ValueError:
                logger.warning("Skip out-of-bounds evidence path: %s", row.evidence_path)
                continue
            try:
                if path.is_file():
                    os.remove(path)
                    evidence_removed += 1
            except OSError:
                logger.warning("Failed to remove evidence file: %s", path)
        deleted = self.repository.delete_alerts_before(cutoff, severity)
        return {
            "deleted": deleted,
            "evidence_removed": evidence_removed,
            "cutoff": cutoff.isoformat(),
            "days": effective_days,
        }
