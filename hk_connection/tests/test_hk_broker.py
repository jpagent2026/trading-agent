from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce

from hk_connection.broker import build_entry_order, build_stop_order, stop_leg_id


def test_entry_is_market_oto_with_gtc_stop_rounded():
    o = build_entry_order("NVDA", 7, "long", 101.23456)
    assert o.order_class == OrderClass.OTO and o.time_in_force == TimeInForce.GTC
    assert o.side == OrderSide.BUY and o.qty == 7 and o.type == OrderType.MARKET
    assert o.stop_loss.stop_price == 101.23 and o.take_profit is None
    s = build_entry_order("TSLA", 3, "short", 250.254)
    assert s.side == OrderSide.SELL and s.order_class == OrderClass.OTO and s.stop_loss.stop_price == 250.25


def test_stop_order_is_gtc_stop_market():
    s = build_stop_order("NVDA", 5, "long", 99.5)
    assert s.type == OrderType.STOP and s.time_in_force == TimeInForce.GTC and s.side == OrderSide.SELL
    assert s.stop_price == 99.5 and s.qty == 5
    assert build_stop_order("TSLA", 5, "short", 260).side == OrderSide.BUY


def test_stop_leg_id():
    order = SimpleNamespace(legs=[SimpleNamespace(id="L1", order_type="stop")])
    assert stop_leg_id(order) == "L1" and stop_leg_id(SimpleNamespace(legs=None)) is None


def _names(client):
    return [c[0] for c in client.method_calls if c[0] in ("cancel_order_by_id", "submit_order")]


def test_partial_sell_cancels_stop_then_sells_then_replaces_stop_for_remaining(broker, client):
    client.get_orders.return_value = [SimpleNamespace(id="stop-1", symbol="NVDA", order_type="stop", status="new", legs=None)]
    broker.partial_exit("NVDA", "long", 3, 17, 98.5)
    assert _names(client) == ["cancel_order_by_id", "submit_order", "submit_order"]
    client.cancel_order_by_id.assert_called_once_with("stop-1")
    sell, stop = [c.args[0] for c in client.submit_order.call_args_list]
    assert sell.qty == 3 and sell.side == OrderSide.SELL and sell.type == OrderType.MARKET
    assert stop.qty == 17 and stop.type == OrderType.STOP and stop.stop_price == 98.5 and stop.time_in_force == TimeInForce.GTC


def test_partial_sell_failure_restores_full_stop(broker, client):
    client.get_orders.return_value = []

    def submit(req):
        if req.type == OrderType.MARKET:
            raise Exception("insufficient qty")
        return SimpleNamespace(id="s")
    client.submit_order.side_effect = submit
    with pytest.raises(Exception):
        broker.partial_exit("NVDA", "long", 3, 17, 98.5)
    restored = client.submit_order.call_args_list[-1].args[0]
    assert restored.type == OrderType.STOP and restored.qty == 20


def test_cancel_includes_held_oto_legs_nested(broker, client):
    parent = SimpleNamespace(id="p", symbol="NVDA", order_type="market", status="filled",
                             legs=[SimpleNamespace(id="leg", symbol="NVDA", order_type="stop", status="held", legs=None)])
    client.get_orders.return_value = [parent]
    assert broker.cancel_stops("NVDA") == ["leg"]
