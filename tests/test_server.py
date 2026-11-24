"""HTTP API tests against a fake Aura bridge."""

from __future__ import annotations

import struct
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from withings_aura.const import (
    CMD_SET_ALARM,
    CMD_SET_LAMP,
    CMD_SET_SETTINGS,
    CMD_START_PROGRAM,
    CMD_TIME_SET,
    TLV_LAMP_RGB,
    TLV_LAMP_STATE,
    TLV_PROGRAM_SETTING,
    TLV_SET_ALARM,
)
from withings_aura.controller import AuraController
from withings_aura.device import AuraDevice
from withings_aura.protocol import decode_tlvs
from withings_aura.server import create_app
from withings_aura.transport import AuraBridge


@pytest.fixture
async def client(fake_bridge):
    controller = AuraController(AuraDevice(AuraBridge(fake_bridge.host, fake_bridge.port)))
    await controller.refresh()
    test_client = TestClient(TestServer(create_app(controller, ZoneInfo("Europe/Paris"))))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()
        await controller.stop()


async def test_state_hides_the_association_secret(client) -> None:
    response = await client.get("/api/state")
    body = await response.json()
    assert response.status == 200
    assert "secret" not in body["device"]
    assert body["device"]["mac"] == "00:11:22:33:44:55"
    assert set(body["programs"]) == {"mood", "nap", "sleep", "wake_up", "retail", "media"}
    assert body["alarms"]["items"][0]["time"] == "06:45"


async def test_index_page_is_served(client) -> None:
    response = await client.get("/")
    assert response.status == 200
    page = await response.text()
    assert "<title>Aura</title>" in page
    assert 'id="card-alarms"' in page


async def test_lamp_turn_on_sends_colour_and_state(client, fake_bridge) -> None:
    response = await client.put(
        "/api/lamp", json={"on": True, "rgb": [255, 0, 0], "brightness": 50}
    )
    assert response.status == 200
    values = dict(decode_tlvs(fake_bridge.last_request(CMD_SET_LAMP)))
    assert struct.unpack(">HHH", values[TLV_LAMP_RGB]) == (32768, 0, 0)
    assert values[TLV_LAMP_STATE] == b"\x01"
    state = await (await client.get("/api/state")).json()
    assert state["lamp"] == {"on": True, "rgb": [255, 0, 0], "brightness": 50}


async def test_lamp_turn_off_keeps_colour(client, fake_bridge) -> None:
    await client.put("/api/lamp", json={"on": True, "rgb": [0, 255, 0], "brightness": 40})
    await client.put("/api/lamp", json={"on": False})
    values = dict(decode_tlvs(fake_bridge.last_request(CMD_SET_LAMP)))
    assert values[TLV_LAMP_STATE] == b"\x00"
    assert struct.unpack(">HHH", values[TLV_LAMP_RGB])[1] > 0
    state = await (await client.get("/api/state")).json()
    assert state["lamp"]["on"] is False
    assert state["lamp"]["rgb"] == [0, 255, 0]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/lamp", {"rgb": [256, 0, 0]}),
        ("/api/lamp", {"brightness": 101}),
        ("/api/lamp", {"on": "yes"}),
        ("/api/clock", {}),
        ("/api/alarms/1", {"time": "25:00"}),
        ("/api/alarms/1", {"days": ["funday"]}),
        ("/api/alarms/1", {"one_shot_date": "2026-13-45"}),
        ("/api/alarms/1", {"one_shot_date": "1999-01-01"}),
        ("/api/alarms/1", {"web_radio": "x" * 256}),
        ("/api/alarms/1", {"volume": True}),
    ],
)
async def test_invalid_input_is_rejected_without_touching_the_device(
    client, fake_bridge, path, body
) -> None:
    writes_before = len(fake_bridge.requests)
    response = await client.put(path, json=body)
    assert response.status == 400
    assert "error" in await response.json()
    assert len(fake_bridge.requests) == writes_before


async def test_non_json_body_is_rejected(client) -> None:
    response = await client.put("/api/lamp", data=b"not json")
    assert response.status == 400


async def test_unknown_slot_and_program_are_404(client) -> None:
    assert (await client.put("/api/alarms/11", json={"enabled": True})).status == 404
    assert (await client.put("/api/alarms/abc", json={"enabled": True})).status == 404
    assert (await client.put("/api/programs/disco/settings", json={"volume": 5})).status == 404


async def test_alarm_update_preserves_untouched_fields(client, fake_bridge) -> None:
    response = await client.put("/api/alarms/1", json={"time": "07:30"})
    assert response.status == 200
    values = decode_tlvs(fake_bridge.last_request(CMD_SET_ALARM))
    alarm = next(value for kind, value in values if kind == TLV_SET_ALARM)
    hour, minute, mask, *_rest = struct.unpack(">BBBBBBB", alarm)
    assert (hour, minute) == (7, 30)
    assert mask == 0x3E  # weekdays kept, still disabled as in the fixture
    assert any(kind == TLV_PROGRAM_SETTING for kind, _ in values)  # settings carried over


async def test_start_program_uses_program_name(client, fake_bridge) -> None:
    response = await client.post("/api/programs/start", json={"program": "nap", "volume": 30})
    assert response.status == 200
    assert fake_bridge.last_request(CMD_START_PROGRAM) is not None


async def test_clock_update_keeps_other_value(client, fake_bridge) -> None:
    response = await client.put("/api/clock", json={"brightness": 80})
    assert response.status == 200
    ((_, value),) = decode_tlvs(fake_bridge.last_request(CMD_SET_SETTINGS))
    assert value == bytes([1, 80])  # display flag unchanged (on), brightness updated


async def test_time_sync_uses_configured_zone(client, fake_bridge) -> None:
    response = await client.post("/api/time/sync", json={})
    body = await response.json()
    assert response.status == 200
    assert body["timezone"] == "Europe/Paris"
    assert fake_bridge.last_request(CMD_TIME_SET) is not None


async def test_unreachable_aura_returns_502(client, fake_bridge) -> None:
    await fake_bridge.stop()
    response = await client.post("/api/refresh")
    assert response.status == 502
    assert "error" in await response.json()


async def test_empty_one_shot_date_clears_it(client, fake_bridge) -> None:
    response = await client.put("/api/alarms/2", json={"one_shot_date": ""})
    assert response.status == 200
    values = decode_tlvs(fake_bridge.last_request(CMD_SET_ALARM))
    alarm = next(value for kind, value in values if kind == TLV_SET_ALARM)
    _hour, _minute, _mask, day, month, year, _program = struct.unpack(">BBBBBBB", alarm)
    assert (day, month, year) == (0, 0, 0)
