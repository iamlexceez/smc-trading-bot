# External implementation reference

- **MetaTrader 5 Python — `history_deals_get`**: <https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py>. Accessed 2026-08-13. The official documentation states that `history_deals_get` retrieves historical deals and supports filtering by a `position` ticket, returning deals whose `DEAL_POSITION_ID` matches that ticket. This is the basis for `MT5Executor.get_closed_position_outcome`, which reconciles local learning records only after broker-reported deal history confirms a closed position.
