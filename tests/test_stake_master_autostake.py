from unittest.mock import Mock

from agents.stake_master.agent import StakeMaster


def test_autostake_skips_when_balance_insufficient(monkeypatch):
    status_snapshot = {
        'status': 'ok',
        'staked': 0,
        'rewards': 0,
        'active_staker': False,
        'min_active_stake': 100,
        'cooldown': {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock()
    staking.stake = Mock()
    monkeypatch.setenv('STAKEMASTER_PROGRESSIVE_ENABLE', 'true')
    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, '_wallet_vvv_balance', lambda self: 50)

    result = master.run_once(live=True)

    stake_action = result['stake_action']
    assert stake_action['reason'] == 'insufficient_balance'
    assert stake_action['attempted'] is False
    assert stake_action['executed'] is False
    assert staking.approve.called is False
    assert staking.stake.called is False
