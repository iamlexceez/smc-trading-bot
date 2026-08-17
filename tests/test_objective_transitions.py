from analysis.objective_transitions import ObjectiveTransitionMatrix, audit_objective_state_isolation


def test_objective_transition_matrix_validates_known_paths():
    matrix = ObjectiveTransitionMatrix()
    
    # Valid path
    context = {"broker_connected": True, "account_verified": True, "equity": 100.0}
    result = matrix.validate_transition("ACCUMULATION", "PHASE_COMPLETE", context)
    assert result["valid"] is True
    assert result["transition"].action == "Lock profits"
    
    # Invalid path
    invalid = matrix.validate_transition("ACCUMULATION", "UNKNOWN_PHASE", context)
    assert invalid["valid"] is False
    assert "Unknown transition" in invalid["reason"]
    
    # System state failure
    disconnected = {"broker_connected": False, "account_verified": True, "equity": 100.0}
    failed = matrix.validate_transition("ACCUMULATION", "PHASE_COMPLETE", disconnected)
    assert failed["valid"] is False
    assert "broker_connected" in failed["reason"]


def test_objective_state_isolation_prevents_accidental_resets():
    assert audit_objective_state_isolation("ACCUMULATION", "lock_profits") is True
    assert audit_objective_state_isolation("ACCUMULATION", "evidence") is False
    assert audit_objective_state_isolation("PHASE_COMPLETE", "strategy_dna") is False
