"""Separate research-candidate and objective-trading decisions.

The research gate answers whether an opportunity is measurable enough to retain as
an experiment. The trading gate answers whether the same opportunity has enough
governed evidence to receive new exposure. Neither gate replaces broker,
sizing, margin, order, or software-integrity validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


_EVIDENCE_ALIASES = {
    "STRONG": "STRONG",
    "STRONG_EVIDENCE": "STRONG",
    "VALIDATED": "STRONG",
    "MODERATE": "MODERATE",
    "PROMISING": "MODERATE",
    "OBSERVED": "MODERATE",
    "WEAK": "WEAK",
    "EARLY": "WEAK",
    "UNKNOWN": "INSUFFICIENT",
    "INSUFFICIENT": "INSUFFICIENT",
    "INSUFFICIENT_EVIDENCE": "INSUFFICIENT",
    "CONFLICTED": "CONFLICTED",
}


@dataclass(frozen=True)
class GateDecision:
    """A serializable decision with independent research and trading fields."""

    research_decision: str
    trading_decision: str
    evidence_classification: str
    confidence_classification: str
    reason: str
    failures: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_decision": self.research_decision,
            "trading_decision": self.trading_decision,
            "evidence_classification": self.evidence_classification,
            "confidence_classification": self.confidence_classification,
            "reason": self.reason,
            "failures": list(self.failures),
        }


def classify_evidence(evidence: Mapping[str, Any] | None) -> str:
    """Classify only from explicitly recorded evidence; missing data is insufficient."""
    data = dict(evidence or {})
    explicit = str(data.get("evidence_classification") or data.get("evidence_strength") or data.get("confidence") or "").strip().upper()
    if explicit in _EVIDENCE_ALIASES:
        return _EVIDENCE_ALIASES[explicit]
    stage = str(data.get("evidence_stage") or "").strip().lower()
    if stage in {"forward_demo", "validated", "champion"}:
        return "MODERATE"
    if stage in {"exploration", "insufficient_evidence", ""}:
        return "INSUFFICIENT"
    return "INSUFFICIENT"


def classify_confidence(evidence: Mapping[str, Any] | None) -> str:
    """Keep confidence independent; never manufacture a numeric confidence value."""
    data = dict(evidence or {})
    raw = str(data.get("confidence_classification") or data.get("confidence") or "").strip().upper()
    if not raw or raw in {"UNKNOWN", "UNAVAILABLE", "NONE"}:
        return "UNVALIDATED"
    if raw in {"CONFLICTED", "LOW", "INSUFFICIENT", "INSUFFICIENT_EVIDENCE"}:
        return "LOW"
    if raw in {"HIGH", "VALIDATED", "STRONG_EVIDENCE", "PROMISING"}:
        return raw
    return "UNVALIDATED"


def evaluate_trading_gate(
    *,
    setup_valid: bool,
    broker_symbol_valid: bool,
    valid_market_data: bool,
    objective_permits_exposure: bool,
    evidence: Mapping[str, Any] | None,
    champion_governed: bool,
    forward_demo_experiment_allowed: bool = False,
    portfolio_approved: bool = True,
    structural_conflict: bool = False,
    required_htf_context_available: bool = True,
) -> GateDecision:
    """Evaluate objective eligibility without using a single score as authority.

    Broker sizing, margin, stop-distance, duplicate-order, and order-submit checks
    remain downstream hard safeguards and are intentionally not duplicated here.
    """
    data = dict(evidence or {})
    evidence_classification = classify_evidence(data)
    confidence_classification = classify_confidence(data)
    failures: list[str] = []
    if not broker_symbol_valid:
        failures.append("Broker-valid symbol")
    if not valid_market_data:
        failures.append("Valid market data")
    if not setup_valid:
        failures.append("Valid setup geometry")
    if not objective_permits_exposure:
        failures.append("Objective/account permits new exposure")
    if evidence_classification in {"INSUFFICIENT", "WEAK"}:
        failures.append(f"Sufficient evidence ({evidence_classification})")
    if evidence_classification == "CONFLICTED" or structural_conflict:
        failures.append("Unresolved structural conflict")
    if confidence_classification in {"LOW", "UNVALIDATED"}:
        failures.append(f"Validated confidence ({confidence_classification})")
    if not champion_governed and not forward_demo_experiment_allowed:
        failures.append("Champion/challenger governance")
    if not portfolio_approved:
        failures.append("Portfolio context")
    if not required_htf_context_available:
        failures.append("Required top-down context")

    if failures:
        if any("conflict" in item.lower() for item in failures):
            decision = "DEFERRED"
        elif any(item.startswith("Objective") or item.startswith("Champion") for item in failures):
            decision = "OBJECTIVE_INELIGIBLE"
        elif any(item.startswith("Sufficient") or item.startswith("Validated") or item.startswith("Required") for item in failures):
            decision = "INSUFFICIENT_EVIDENCE"
        else:
            decision = "TRADE_REJECTED"
        return GateDecision(
            research_decision="RESEARCH_ACCEPTED",
            trading_decision=decision,
            evidence_classification=evidence_classification,
            confidence_classification=confidence_classification,
            reason="; ".join(failures),
            failures=tuple(failures),
        )
    return GateDecision(
        research_decision="RESEARCH_ACCEPTED",
        trading_decision="TRADE_APPROVED",
        evidence_classification=evidence_classification,
        confidence_classification=confidence_classification,
        reason="Research candidate passed the independent evidence, confidence, governance, portfolio, and objective-context gate.",
    )


__all__ = ["GateDecision", "classify_confidence", "classify_evidence", "evaluate_trading_gate"]

