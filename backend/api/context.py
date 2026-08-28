from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from backend.config import Settings, get_settings
from backend.pipeline import MonitoringRuntime
from backend.repository import Repository
from backend.security import SecretCipher


@dataclass
class ApplicationContext:
    """Long-lived application services shared by API routers."""

    settings: Settings
    repository: Repository
    cipher: SecretCipher
    runtime: MonitoringRuntime | None = None

    def require_runtime(self) -> MonitoringRuntime:
        if self.runtime is None:
            raise HTTPException(status_code=503, detail="运行时尚未初始化")
        return self.runtime


def create_context() -> ApplicationContext:
    settings = get_settings()
    return ApplicationContext(
        settings=settings,
        repository=Repository(),
        cipher=SecretCipher(settings.app_encryption_key),
    )


context = create_context()
