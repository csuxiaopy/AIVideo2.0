from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
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
        analysis_evidence_removed = 0
        if override_days is None and hasattr(self.repository, "analysis_evidence_before"):
            expired_paths = set(self.repository.analysis_evidence_before(log_cutoff))
            retained_paths = self.repository.retained_evidence_paths(log_cutoff)
            for evidence_path in expired_paths - retained_paths:
                analysis_evidence_removed += self._remove_path(evidence_path, root)
        log_deleted = (
            self.repository.delete_logs_before(log_cutoff)
            if override_days is None and hasattr(self.repository, "delete_logs_before") else {}
        )
        if override_days is None and hasattr(self.repository, "retained_evidence_paths"):
            analysis_evidence_removed += self._remove_orphan_analysis_images(
                root, log_cutoff, self.repository.retained_evidence_paths(log_cutoff)
            )
        return {
            "deleted": deleted,
            "evidence_removed": evidence_removed,
            "analysis_evidence_removed": analysis_evidence_removed,
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

    def _remove_path(self, evidence_path: str, root) -> int:
        try:
            path = (self.settings.evidence_dir / evidence_path).resolve()
            path.relative_to(root)
        except ValueError:
            logger.warning("Skip out-of-bounds evidence path: %s", evidence_path)
            return 0
        try:
            if path.is_file():
                os.remove(path)
                return 1
        except OSError:
            logger.warning("Failed to remove evidence file: %s", path)
        return 0

    def _remove_orphan_analysis_images(self, root, cutoff, retained: set[str]) -> int:
        removed = 0
        for pattern in ("analysis-*.jpg", "review-*.jpg"):
            for path in root.glob(pattern):
                if path.name in retained:
                    continue
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    if modified < cutoff:
                        removed += self._remove_path(path.name, root)
                except OSError:
                    logger.warning("Failed to inspect orphan evidence file: %s", path)
        return removed
