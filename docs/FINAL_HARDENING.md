# Final System Hardening

This update repairs the remaining state/governance conflicts found during repository inspection.

## 1. Research vs objective execution

A forward-DEMO challenger is now allowed to generate isolated DEMO observations when the active policy is explicitly attached to an experiment. It remains unable to bypass broker, portfolio, objective, stop, margin, or software-integrity gates and cannot self-promote.

## 2. Objective phase boundaries

Completing a phase no longer closes positions merely because the milestone was reached. The boundary attempts broker-confirmed profit protection for profitable positions, then records the milestone and advances. Positions that cannot be protected immediately remain open and are handed back to the independent position manager.

## 3. Margin pressure

`MARGIN_PRESSURE` and `CRITICAL_CAPITAL` are now exposure-state restrictions rather than bot-wide runtime halts. They do not pause the process or disable the independent position manager. Individual new orders must still pass fresh broker margin and execution validation. `CAPITAL_EXHAUSTED` remains terminal only after the broker confirms the account is flat and balance is <= 5.

## 4. Capital reduction

The existing isolated capital-reduction route continues to use broker-preflighted immediate-close orders when supported, avoiding invalid protective SL/TP requirements on deliberate short-lived reduction transactions. Target proximity remains strict near the finish.

## Verification

- `python -m compileall -q .`: passed.
- Focused pytest suite: passed after the hardening changes.
- Full broker-connected DEMO behavior cannot be certified in this environment because MetaTrader 5 and the repository's external Python dependencies are not installed here.
