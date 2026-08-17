"""Runtime and static evidence for intelligence-module invocation.

The matrix is observability only. It never grants trading authority and it does not
substitute for integration tests or broker verification.
"""

from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ModuleContract:
    module_name: str
    scheduler_entry_point: str
    downstream_consumer: str
    persistence_boundary: str
    integration_test: str


@dataclass
class InvocationEvidence:
    module_name: str
    imported: bool
    called: bool = False
    scheduled: bool = False
    data_seen: bool = False
    output_consumed: bool = False
    persisted: bool = False
    integration_tested: bool = False
    invocation_count: int = 0
    last_invoked_at: str | None = None
    last_scheduler_entry_point: str = ""
    notes: str = ""


DEFAULT_CONTRACTS: tuple[ModuleContract, ...] = (
    ModuleContract("knowledge.context", "scheduler.market_scan", "knowledge.router", "decision_records", "tests/test_knowledge_router.py"),
    ModuleContract("knowledge.router", "scheduler.analyze_symbol", "analysis.decision_gates", "decision_records", "tests/test_knowledge_router.py"),
    ModuleContract("knowledge.combinations", "scheduler.research_refresh", "research_governance", "strategy_combination_evidence", "tests/test_knowledge_router.py"),
    ModuleContract("knowledge.specialization", "scheduler.research_refresh", "research_cohort", "instrument_specialization_profiles", "tests/test_knowledge_router.py"),
    ModuleContract("analysis.research_governance", "scheduler.research_refresh", "scheduler.enabled_symbols", "research_governance_snapshot", "tests/test_knowledge_router.py"),
    ModuleContract("analysis.drift", "scheduler.self_optimization", "model_governance", "optimization_runs", "tests/test_knowledge_router.py"),
    ModuleContract("analysis.capital_state", "scheduler.account_reconciliation", "decision_gates", "account_state", "tests/test_broker_integrity.py"),
    ModuleContract("analysis.adaptive_management", "scheduler.position_management", "executor.modify_position", "management_learning", "tests/test_upgrade_safety.py"),
    ModuleContract("communication.notification_manager", "scheduler.notification_retry", "Telegram/Slack adapters", "notification_deliveries", "tests/test_notification_manager.py"),
    ModuleContract("communication.command_bus", "Telegram/Slack handlers", "control_service", "command_audit", "tests/test_command_bus.py"),
)


class InvocationTracker:
    def __init__(self, contracts: tuple[ModuleContract, ...] = DEFAULT_CONTRACTS) -> None:
        self.contracts = {contract.module_name: contract for contract in contracts}
        self.records: dict[str, InvocationEvidence] = {}
        for name in self.contracts:
            self.records[name] = InvocationEvidence(
                module_name=name,
                imported=importlib.util.find_spec(name) is not None,
                scheduled=bool(self.contracts[name].scheduler_entry_point),
                integration_tested=self._test_exists(self.contracts[name].integration_test),
            )

    @staticmethod
    def _test_exists(path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8"):
                return True
        except OSError:
            return False

    def mark_invoked(
        self,
        module_name: str,
        *,
        scheduler_entry_point: str = "",
        data_seen: bool = True,
        output_consumed: bool = True,
        persisted: bool = False,
        notes: str = "",
    ) -> None:
        record = self.records.setdefault(
            module_name,
            InvocationEvidence(module_name=module_name, imported=importlib.util.find_spec(module_name) is not None),
        )
        record.called = True
        record.data_seen = record.data_seen or data_seen
        record.output_consumed = record.output_consumed or output_consumed
        record.persisted = record.persisted or persisted
        record.invocation_count += 1
        record.last_invoked_at = datetime.now(timezone.utc).isoformat()
        record.last_scheduler_entry_point = scheduler_entry_point
        if notes:
            record.notes = notes

    def snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name in sorted(self.records):
            record = self.records[name]
            payload = asdict(record)
            payload["status"] = "COMPLETE" if all(
                payload[field] for field in ("imported", "called", "scheduled", "data_seen", "output_consumed", "integration_tested")
            ) and payload["persisted"] else "PARTIAL"
            payload["contract"] = asdict(self.contracts[name]) if name in self.contracts else {}
            result.append(payload)
        return result

    def summary(self) -> dict[str, Any]:
        rows = self.snapshot()
        complete = sum(row["status"] == "COMPLETE" for row in rows)
        return {
            "total_modules": len(rows),
            "complete_modules": complete,
            "partial_modules": len(rows) - complete,
            "rows": rows,
        }
