def test_example_config_loads_with_safe_defaults(cfg_paper):
    assert cfg_paper.leader == "0xdae4df7207feb3b350e4284c8efe5f7dac37f637"
    assert cfg_paper.mode == "paper"  # NEVER live by default
    assert cfg_paper.risk.max_drawdown_pct == -35
    assert cfg_paper.risk.mirror_parity_tolerance == 1.05
    assert cfg_paper.risk.stop_loss_overlay is None  # faithful mirror
    assert cfg_paper.dashboard.port == 8061
    assert cfg_paper.paper.start_equity == 10_000
