# SPDX-License-Identifier: AGPL-3.0-only
"""Default-OFF operator selection; ON must not masquerade as legacy."""
import pytest


def test_activation_is_default_off(monkeypatch):
    from mycelium_live.token_path_activation import token_path_enabled
    monkeypatch.delenv('MYCELIUM_TOKEN_PATH', raising=False)
    assert token_path_enabled() is False


@pytest.mark.parametrize('value,expected', [('off',False),('on',True)])
def test_activation_exact_values(monkeypatch,value,expected):
    from mycelium_live.token_path_activation import token_path_enabled
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH',value)
    assert token_path_enabled() is expected


@pytest.mark.parametrize('value',['','1','true','ON',' on','auto'])
def test_activation_rejects_typo_instead_of_silent_legacy(monkeypatch,value):
    from mycelium_live.token_path_activation import token_path_enabled
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH',value)
    with pytest.raises(ValueError,match='token_path_activation_invalid'):
        token_path_enabled()


def test_owner_uses_operator_selection(world,monkeypatch):
    from tests.live.test_token_path_owner import owner
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    path,_,_=owner(world,enabled=None)
    assert path.enabled is True
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','off')
    path,_,_=owner(world,enabled=None)
    assert path.enabled is False


def test_explicit_off_does_not_inherit_operator_on(world,monkeypatch):
    from tests.live.test_token_path_owner import owner
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    path,_,_=owner(world,enabled=False)
    assert path.enabled is False


def test_route_refuses_unprovisioned_on_before_reading_plan(monkeypatch):
    from mycelium_live.route import PhysicalLiveRoute
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    # No controller/plan I/O, worker or device may be touched on refusal.
    with pytest.raises(ValueError,match='token_path_activation_unprovisioned'):
        PhysicalLiveRoute(controller=None,endpoints=None,run_plan=None)


def test_operator_plan_refuses_unprovisioned_on_before_file_io(monkeypatch):
    from mycelium_live.route import PhysicalLiveRoute
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    with pytest.raises(ValueError,match='token_path_activation_unprovisioned'):
        PhysicalLiveRoute.from_operator_plan(None,seed_state_root=None)


def test_operator_on_refuses_unsupported_instead_of_legacy(world,monkeypatch):
    from tests.live.test_token_path_owner import owner,prepare
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    path,box,_=owner(world,enabled=None,capable=lambda _:False)
    with pytest.raises(RuntimeError,match='token_path_activation_unsupported'):
        prepare(path,world)
    assert box.opens==0


from tests.live.test_token_path_admission import world
