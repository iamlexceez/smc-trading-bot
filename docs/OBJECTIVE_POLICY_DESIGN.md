# Objective-Wide Policy Design

## Single policy contract

The existing `Opportunity` ranking remains the only cross-symbol ranking engine. It will accept an optional broker-observed capacity context and return an auditable ranking plus capacity/no-trade metadata. The existing `RiskManager.check_all()` remains the final policy and broker gate. The existing `TradeManager` remains the only active-position management engine.

The policy contract is extended with experimental concentration variables rather than permanent trading rules:

| Variable | Meaning | Governance |
|---|---|---|
| `max_positions` | Optional experiment-selected simultaneous-position capacity | Candidate policy; enforced only by existing risk checks |
| `max_trades_per_day` | Optional experiment-selected frequency response | Existing policy field; no default expansion |
| `protection_response` | Experimental sensitivity to capital vulnerability in approved management actions | Existing policy field; no stop widening |
| `selection_model` | Selection hypothesis identifier for telemetry | Candidate policy metadata |
| `concentration_model` | One-versus-multiple opportunity hypothesis | Candidate policy metadata |

## Account-capacity context

The scanner will pass fresh broker account state, free margin, equity, objective phase, open-position count, open risk, protected-position count, and the active policy to the existing opportunity ranker. The ranker will not invent margin or correlation facts. It will use broker-provided account facts and closed-candle return signatures when available, and label missing information as unknown.

Low-capital or protection states increase selectivity and reduce capacity for additional exposure. This is an account-state description and a policy input, not an override of the final broker gate. A candidate is never forced merely because it is the highest-ranked remaining candidate.

## No-trade decision

The scheduler will explicitly record `NO_TRADE` when no candidate is thesis-qualified, when account state blocks new exposure, when capacity is exhausted, when every candidate conflicts with protected/current exposure, or when the strongest candidate remains too uncertain for the active selection policy. This event is descriptive and will be persisted through the existing `execution_events` table.

## Concentration learning

The policy generator will include controlled one-position, two-position, and three-position capacity candidates. They remain challengers until chronological training, validation, locked OOS, and broker-realized forward-DEMO evidence support a promotion. The generator will not promote a larger capacity simply because the account has more equity.

## Protection

The existing capital-protection context and `TradeManager` remain authoritative. The new integration exposes protection level, protected-position count, and phase progress to ranking and telemetry. Milestone protection continues to prefer broker-valid SL improvement and only closes when protection cannot be confirmed, as implemented in commit `9989417`.
