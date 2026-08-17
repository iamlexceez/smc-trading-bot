from analysis.small_account import AccountEconomics, evaluate_small_account_efficiency


def test_small_account_efficiency_rejects_insufficient_capital():
    economics = AccountEconomics(
        balance=10.0, equity=10.0, free_margin=5.0,
        minimum_volume=0.1, volume_step=0.1, contract_size=1.0,
        margin_required=6.0, stop_distance=10.0, risk_usd=1.0,
        expected_value_r=0.5, reward_usd=2.0, correlation_penalty=0.0,
        broker_constraints=[]
    )
    result = evaluate_small_account_efficiency(economics)
    assert result["executable"] is False
    assert "Required margin exceeds free margin" in result["reason"]


def test_small_account_efficiency_calculates_score_for_valid_trade():
    economics = AccountEconomics(
        balance=50.0, equity=50.0, free_margin=45.0,
        minimum_volume=0.1, volume_step=0.1, contract_size=1.0,
        margin_required=5.0, stop_distance=10.0, risk_usd=1.0,
        expected_value_r=0.5, reward_usd=2.0, correlation_penalty=0.1,
        broker_constraints=[]
    )
    result = evaluate_small_account_efficiency(economics)
    assert result["executable"] is True
    assert result["efficiency_score"] > 0
    assert result["metrics"]["risk_pct"] == 2.0
