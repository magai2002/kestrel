import pytest

from strategies.registry import REGISTRY, get


@pytest.mark.parametrize("name", list(REGISTRY))
def test_every_entry_is_internally_consistent(name: str) -> None:
    entry = get(name)
    assert entry.name == name
    params = (
        entry.params_cls.model_construct()
        if name != "mean_reversion"
        else entry.params_cls(symbol="SPY")
    )
    assert isinstance(params, entry.params_cls)
    strategy = entry.strategy_cls(params) if name == "mean_reversion" else entry.strategy_cls()
    assert hasattr(strategy, "on_bar")


def test_unknown_strategy_raises_with_available_list() -> None:
    with pytest.raises(KeyError, match="trend_vol_target"):
        get("not_a_real_strategy")
