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

    @staticmethod
    def _evidence_paths(row: Any) -> list[str]:
        paths = [item.evidence_path for item in (getattr(row, "evidences", []) or [])]
        if row.evidence_path:
            paths.append(row.evidence_path)
        return list(dict.fromkeys(path for path in paths if path))

    def run(self, override_days: int | None = None, severity: str | None = None) -> dict[str, Any]:
        retention = self.repository.get_retention_settings()
        effective_days = override_days or retention.alert_retention_days
        cutoff = utc_now() - timedelta(days=effective_days)
        rows = self.repository.list_alerts_before(cutoff, severity)
        root = self.settings.evidence_dir.resolve()
        evidence_removed = 0
        for row in rows:
            for evidence_path in self._evidence_paths(row):
                try:
                    path = (self.settings.evidence_dir / evidence_path).resolve()
                    path.relative_to(root)
                except ValueError:
                    logger.warning("Skip out-of-bounds evidence path: %s", evidence_path)
                    continue
                try:
                    if path.is_file():
                        os.remove(path)
                        evidence_removed += 1
                except OSError:
                    logger.warning("Failed to remove evidence file: %s", path)
        deleted = self.repository.delete_alerts_before(cutoff, severity)
        log_cutoff = utc_now() - timedelta(days=getattr(retention, "log_retention_days", 30))
        log_deleted = (
            self.repository.delete_logs_before(log_cutoff)
            if override_days is None and hasattr(self.repository, "delete_logs_before") else {}
        )
        return {
            "deleted": deleted,
            "evidence_removed": evidence_removed,
            "cutoff": cutoff.isoformat(),
            "days": effective_days,
            "log_deleted": log_deleted,
            "log_cutoff": log_cutoff.isoformat(),
        }

    def delete_selected(self, alert_ids: list[int]) -> dict[str, Any]:
        requested_ids = list(dict.fromkeys(alert_ids))
        rows = self.repository.list_alerts_by_ids(requested_ids)
        existing_ids = {row.id for row in rows}
        root = self.settings.evidence_dir.resolve()
        evidence_removed = 0
        for row in rows:
            for evidence_path in self._evidence_paths(row):
                try:
                    path = (self.settings.evidence_dir / evidence_path).resolve()
                    path.relative_to(root)
                except ValueError:
                    logger.warning("Skip out-of-bounds evidence path: %s", evidence_path)
                    continue
                try:
                    if path.is_file():
                        os.remove(path)
                        evidence_removed += 1
                except OSError:
                    logger.warning("Failed to remove evidence file: %s", path)
        deleted_ids = self.repository.delete_alerts_by_ids(requested_ids)
        return {
            "deleted": len(deleted_ids),
            "evidence_removed": evidence_removed,
            "deleted_ids": deleted_ids,
            "missing_ids": [alert_id for alert_id in requested_ids if alert_id not in existing_ids],
        }
