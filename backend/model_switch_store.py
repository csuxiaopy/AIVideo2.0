"""Transactional configuration changes and durable, sanitized outage history."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select, update

from backend import models
from backend.database import session_scope, utc_now


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def safe_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def safe_error(value, secret=""):
    value = str(value)
    if secret:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"data:image/[^\s\"']+", "[IMAGE]", value)
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|token|authorization)[\"'\s:=]+)[^\s,\"'}]+", r"\1[REDACTED]", value)
    return value[:1000]


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    encrypted_key: str
    model: str
    enabled: bool
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row):
        return cls(row.provider, row.base_url.rstrip("/"), row.api_key_encrypted,
                   row.economy_model, getattr(row, "auto_failover_enabled", True), aware(row.updated_at))

    @property
    def key(self):
        return hashlib.sha256(json.dumps([self.provider, self.base_url, self.encrypted_key,
                                         self.model, self.enabled]).encode()).hexdigest()


STATE_FIELDS = ("failure_since", "last_success_at", "last_failure_at", "reason_code", "reason",
                "http_status", "next_attempt_at", "failed_candidates_json")


class ModelSwitchStore:
    @staticmethod
    def current():
        with session_scope() as session:
            row = session.get(models.ModelSettings, 1)
            return ModelConfig.from_row(row) if row else None

    @staticmethod
    def load_state(config):
        with session_scope() as session:
            row = session.get(models.ModelFailoverState, config.key)
            stored = ({key: aware(getattr(row, key)) if key.endswith("_at") or key == "failure_since"
                       else getattr(row, key) for key in STATE_FIELDS} if row else None)
            # Bootstrap the continuous outage from existing business logs only.
            cutoff = config.updated_at or datetime.min.replace(tzinfo=timezone.utc)
            base = (models.ModelCallLog.model == config.model,
                    models.ModelCallLog.provider == config.provider,
                    models.ModelCallLog.created_at >= cutoff)
            success = session.scalar(select(func.max(models.ModelCallLog.created_at)).where(
                *base, models.ModelCallLog.outcome == "success"))
            # A successful call may have committed just before a crash, ahead
            # of the one-second health flush. Never revive the older outage.
            if stored and (not success or (stored["last_success_at"] and stored["last_success_at"] >= aware(success))):
                return stored
            failures = [*base, models.ModelCallLog.outcome == "error"]
            if success:
                failures.append(models.ModelCallLog.created_at > success)
            first = session.scalar(select(func.min(models.ModelCallLog.created_at)).where(*failures))
            last = session.scalar(select(models.ModelCallLog).where(*failures)
                                  .order_by(models.ModelCallLog.created_at.desc()).limit(1))
            return dict(failure_since=aware(first), last_success_at=aware(success),
                        last_failure_at=aware(last.created_at) if last else None,
                        reason_code="historical_failure" if last else "",
                        reason=safe_error(last.error or "") if last else "",
                        http_status=last.http_status if last else None,
                        next_attempt_at=None, failed_candidates_json=stored["failed_candidates_json"] if stored else "{}")

    @staticmethod
    def save_state(config, values):
        with session_scope() as session:
            row = session.get(models.ModelFailoverState, config.key)
            if row is None:
                row = models.ModelFailoverState(config_key=config.key)
                session.add(row)
            for key in STATE_FIELDS:
                if key in values:
                    setattr(row, key, values[key])

    @staticmethod
    def begin(config, state):
        with session_scope() as session:
            row = models.ModelSwitchLog(
                trigger="automatic", base_url=safe_url(config.base_url), from_model=config.model,
                reason_code=state["reason_code"], http_status=state["http_status"],
                reason=f"连续故障已满30分钟；开始于 {state['failure_since'].isoformat()}。{state['reason'][:500]}",
                outcome="running")
            session.add(row)
            session.flush()
            return row.id

    @staticmethod
    def progress(incident, candidates, attempts):
        with session_scope() as session:
            session.execute(update(models.ModelSwitchLog).where(models.ModelSwitchLog.id == incident)
                            .values(candidates_json=json.dumps(candidates), attempts_json=json.dumps(attempts, ensure_ascii=False)))

    @staticmethod
    def finish(incident, config, outcome, reason, target=None):
        with session_scope() as session:
            settings = session.scalar(select(models.ModelSettings).where(models.ModelSettings.id == 1).with_for_update())
            log = session.get(models.ModelSwitchLog, incident)
            if log.outcome != "running":
                return None
            if target and (not settings or ModelConfig.from_row(settings) != config):
                outcome, reason, target = "skipped", "管理员已修改配置，取消旧探测结果", None
            if target:
                settings.economy_model, settings.updated_at = target, utc_now()
            log.outcome, log.to_model, log.completed_at = outcome, target, utc_now()
            log.reason = safe_error(log.reason + "\n" + reason)
            session.flush()
            return ModelConfig.from_row(settings) if target else None

    @staticmethod
    def interrupt_running():
        with session_scope() as session:
            session.execute(update(models.ModelSwitchLog).where(models.ModelSwitchLog.outcome == "running")
                            .values(outcome="failure", completed_at=utc_now(), reason=models.ModelSwitchLog.reason + "\n分析服务重启或配置重载，中断上一轮探测；未完成替换"))

    @staticmethod
    def save_manual(values, actor_user_id=None):
        with session_scope() as session:
            row = session.scalar(select(models.ModelSettings).where(models.ModelSettings.id == 1).with_for_update())
            previous = ModelConfig.from_row(row) if row else None
            if row is None:
                row = models.ModelSettings(id=1)
                session.add(row)
            for key, value in values.items():
                if value is not None:
                    setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            if previous and (previous.model, previous.provider, previous.base_url) != (row.economy_model, row.provider, row.base_url.rstrip("/")):
                session.add(models.ModelSwitchLog(trigger="manual", base_url=safe_url(row.base_url),
                    from_model=previous.model, to_model=row.economy_model, reason_code="manual_change",
                    reason="管理员手动修改模型配置", outcome="success", actor_user_id=actor_user_id,
                    completed_at=utc_now()))
            session.refresh(row)
            return row
