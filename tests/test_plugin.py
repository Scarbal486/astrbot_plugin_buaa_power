from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot_plugin_buaa_power.main import BuaaPowerPlugin


@pytest.mark.asyncio
async def test_check_notifies_on_every_low_balance_check(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(side_effect=[{"balance": 2}, {"balance": 8}])
    plugin.send_alert = AsyncMock(return_value=True)
    await plugin._check_once()
    plugin.fetch_meter.side_effect = [{"balance": 2}, {"balance": 8}]
    await plugin._check_once()
    assert plugin.send_alert.await_count == 2


@pytest.mark.asyncio
async def test_alert_send_failure_is_recorded_without_raising(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(side_effect=[{"balance": 2}, {"balance": 8}])
    plugin.send_alert = AsyncMock(side_effect=RuntimeError("offline"))
    result = await plugin._check_once()
    assert result["sent"] is False
    assert result["status"] == "error"
    assert "通知发送失败" in result["error"]


@pytest.mark.asyncio
async def test_alert_false_result_is_recorded_as_failure(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(side_effect=[{"balance": 2}, {"balance": 8}])
    plugin.send_alert = AsyncMock(return_value=False)
    result = await plugin._check_once()
    assert result["sent"] is False
    assert result["status"] == "error"
    assert "通知发送失败" in result["error"]


@pytest.mark.asyncio
async def test_incomplete_configuration_skips_upstream_and_send(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {"enabled": True, "air_meter_id": "", "lighting_meter_id": ""}
    plugin.fetch_meter = AsyncMock()
    plugin.send_alert = AsyncMock()
    result = await plugin._check_once()
    assert result["status"] == "incomplete"
    plugin.fetch_meter.assert_not_awaited()
    plugin.send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_requests_keep_pub_buaa_path():
    requested_paths = []

    async def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path.endswith("QueryIdData"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            text='<svg id="canvas1"><tspan>1</tspan></svg>',
        )

    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.http_client = httpx.AsyncClient(
        base_url="http://example.test",
        transport=httpx.MockTransport(handler),
    )
    plugin._options_cache = None
    await plugin.fetch_index()
    await plugin.fetch_meter("42")
    await plugin.http_client.aclose()
    assert requested_paths == ["/PubBuaa/QueryIdData", "/PubBuaa"]


def test_dashboard_assets_exist_and_use_plugin_page_bridge():
    root = Path(__file__).parents[1]
    html = (root / "pages/dashboard/index.html").read_text(encoding="utf-8")
    script = (root / "pages/dashboard/app.js").read_text(encoding="utf-8")
    main_source = (root / "main.py").read_text(encoding="utf-8")
    logo = root / "logo.png"
    assert logo.is_file()
    assert logo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "window.AstrBotPluginPage" in script
    assert "air_meter_id" in html
    assert "lighting_meter_id" in html
    assert "notify_qq" in html
    assert "check_time" in html
    assert "查询电表" in html
    for location_id in ("campus", "building", "floor", "room"):
        assert f'<select id="{location_id}"' in html
        assert f'<input id="{location_id}"' not in html
    assert '@filter.command("查询宿舍电量"' in main_source
    for endpoint in ("config", "options", "status", "check"):
        assert f'"page/{endpoint}"' in script


@pytest.mark.asyncio
async def test_query_command_returns_current_balances_without_alert(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(
        side_effect=[
            {"balance": 2.5, "power": 1.2, "reading_time": "2026-09-04 08:00"},
            {"balance": 8, "power": None, "reading_time": ""},
        ]
    )
    plugin.send_alert = AsyncMock()

    class Event:
        def plain_result(self, text):
            return text

    messages = [message async for message in plugin.query_power_command(Event())]
    assert len(messages) == 1
    assert "空调：2.5 kWh" in messages[0]
    assert "照明：8 kWh" in messages[0]
    plugin.send_alert.assert_not_awaited()
