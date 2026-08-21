from types import SimpleNamespace

import pytest

import executors.mt5 as mt5_executor_module


async def connected():
    return True


class PositionalOnlyMT5:
    ORDER_TYPE_BUY = 0

    def __init__(self):
        self.margin_calls = []

    def symbol_select(self, symbol, enable):
        assert isinstance(symbol, str)
        assert isinstance(enable, bool)
        return True

    def symbol_info(self, symbol):
        assert isinstance(symbol, str)
        return SimpleNamespace(
            volume_min=0.1,
            volume_max=100.0,
            volume_step=0.1,
        )

    def symbol_info_tick(self, symbol):
        assert isinstance(symbol, str)
        return SimpleNamespace(ask=100.0, bid=99.9)

    def order_calc_margin(self, action, symbol, volume, price):
        self.margin_calls.append((action, symbol, volume, price))
        return 12.5

    def last_error(self):
        return (0, "ok")


@pytest.mark.asyncio
async def test_margin_probe_uses_official_positional_mt5_signature(monkeypatch):
    fake_mt5 = PositionalOnlyMT5()
    monkeypatch.setattr(mt5_executor_module, "mt5", fake_mt5, raising=False)
    monkeypatch.setattr(mt5_executor_module, "MT5_AVAILABLE", True)

    executor = mt5_executor_module.MT5Executor(1, "password", "server")
    monkeypatch.setattr(executor, "_ensure_connected", lambda: connected())

    result = await executor.get_broker_margin_for_volume(
        "Synthetic Index", "BUY", 0.1, 100.0
    )

    assert result["margin"] == 12.5
    assert fake_mt5.margin_calls == [(0, "Synthetic Index", 0.1, 100.0)]
    executor._thread_pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_order_check_and_send_receive_positional_request(monkeypatch):
    fake_mt5 = PositionalOnlyMT5()
    fake_mt5.TRADE_RETCODE_DONE = 10009
    fake_mt5.ORDER_FILLING_RETURN = 2
    fake_mt5.ORDER_FILLING_FOK = 0
    fake_mt5.ORDER_FILLING_IOC = 1
    fake_mt5.ORDER_TIME_GTC = 0
    fake_mt5.TRADE_ACTION_DEAL = 1
    calls = []

    def order_check(request):
        calls.append(("check", request))
        return SimpleNamespace(retcode=10009, comment="done")

    def order_send(request):
        calls.append(("send", request))
        return SimpleNamespace(retcode=10009, order=777, comment="done")

    fake_mt5.order_check = order_check
    fake_mt5.order_send = order_send
    fake_mt5.symbol_info = lambda symbol: SimpleNamespace(
        visible=True,
        filling_mode=0,
        point=0.01,
        trade_tick_size=0.01,
        digits=2,
        trade_stops_level=0,
        trade_freeze_level=0,
    )
    fake_mt5.symbol_info_tick = lambda symbol: SimpleNamespace(
        bid=99.9,
        ask=100.0,
    )
    monkeypatch.setattr(mt5_executor_module, "mt5", fake_mt5, raising=False)
    monkeypatch.setattr(mt5_executor_module, "MT5_AVAILABLE", True)

    executor = mt5_executor_module.MT5Executor(1, "password", "server")
    monkeypatch.setattr(executor, "_ensure_connected", lambda: connected())
    result = await executor.execute_immediate_close_order(
        symbol="Synthetic Index",
        direction="BUY",
        lot_size=0.1,
        magic=1,
    )

    assert result.success is True
    assert [kind for kind, _ in calls] == ["check", "send"]
    executor._thread_pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_order_request_retries_keyword_form_for_vps_binding(monkeypatch):
    fake_mt5 = PositionalOnlyMT5()
    fake_mt5.TRADE_RETCODE_DONE = 10009
    calls = []

    def order_check(*args, **kwargs):
        if args:
            calls.append("positional")
            return None
        calls.append("keyword")
        assert kwargs["request"] == {"action": 1}
        return SimpleNamespace(retcode=10009, comment="done")

    fake_mt5.order_check = order_check
    fake_mt5.last_error = lambda: (-2, "Unnamed arguments not allowed")
    monkeypatch.setattr(mt5_executor_module, "mt5", fake_mt5, raising=False)
    monkeypatch.setattr(mt5_executor_module, "MT5_AVAILABLE", True)

    executor = mt5_executor_module.MT5Executor(1, "password", "server")
    result = await executor._mt5_request("order_check", {"action": 1})

    assert result.retcode == 10009
    assert calls == ["positional", "keyword"]
    executor._thread_pool.shutdown(wait=True)
