"""Radio catalogue and radio/Spotify preservation tests."""

from __future__ import annotations

import json
import struct
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.fake_bridge import FakeBridge
from withings_aura.const import (
    CMD_SET_ALARM,
    PROGRAM_SETTING_BRIGHTNESS,
    PROGRAM_SETTING_ID,
    PROGRAM_SETTING_SPOTIFY_NAME,
    PROGRAM_SETTING_SPOTIFY_TAG,
    PROGRAM_SETTING_VOLUME,
    PROGRAM_SETTING_WEB_RADIO,
    PROGRAM_SETTING_WEB_RADIO_NAME,
    TLV_PROGRAM_SETTING,
    TLV_SET_ALARM,
)
from withings_aura.controller import AuraController
from withings_aura.device import AuraDevice
from withings_aura.models import Alarm
from withings_aura.protocol import build_program_settings, decode_tlvs, tlv
from withings_aura.radios import RadioCatalog
from withings_aura.server import create_app
from withings_aura.transport import AuraBridge

FIP_ID = "8559024957008661"


def _radio_alarms_body() -> bytes:
    """One enabled Mon-Thu 08:15 radio alarm, shaped like the reply seen on a real unit."""
    alarm = struct.pack(">BBBBBBB", 8, 15, 0x80 | 0b0011110, 0, 0, 0, 20)
    settings = build_program_settings(
        {
            PROGRAM_SETTING_VOLUME: "28",
            PROGRAM_SETTING_BRIGHTNESS: "50",
            PROGRAM_SETTING_ID: "129",
            PROGRAM_SETTING_WEB_RADIO: FIP_ID,
            PROGRAM_SETTING_WEB_RADIO_NAME: "FIP",
            PROGRAM_SETTING_SPOTIFY_TAG: "playlist-tag",
            PROGRAM_SETTING_SPOTIFY_NAME: "Morning mix",
        }
    )
    return tlv(TLV_SET_ALARM, alarm) + settings


@pytest.fixture
async def radio_client(tmp_path):
    bridge = FakeBridge({293: _radio_alarms_body()})
    await bridge.start()
    controller = AuraController(AuraDevice(AuraBridge(bridge.host, bridge.port)))
    await controller.refresh()
    catalog = RadioCatalog(tmp_path / "radios.json")
    client = TestClient(TestServer(create_app(controller, ZoneInfo("Europe/Paris"), catalog)))
    await client.start_server()
    client.bridge = bridge
    client.catalog_path = tmp_path / "radios.json"
    try:
        yield client
    finally:
        await client.close()
        await controller.stop()
        await bridge.stop()


def _written_settings(bridge: FakeBridge) -> dict[int, str]:
    values = decode_tlvs(bridge.last_request(CMD_SET_ALARM))
    return {
        value[0]: value[2 : 2 + value[1]].decode()
        for kind, value in values
        if kind == TLV_PROGRAM_SETTING
    }


async def test_state_learns_and_persists_stations_from_alarms(radio_client) -> None:
    state = await (await radio_client.get("/api/state")).json()
    assert state["radios"] == [{"id": FIP_ID, "name": "FIP"}]
    assert json.loads(radio_client.catalog_path.read_text()) == {FIP_ID: "FIP"}


async def test_editing_an_alarm_writes_back_station_name_and_spotify(radio_client) -> None:
    response = await radio_client.put("/api/alarms/1", json={"time": "08:30"})
    assert response.status == 200
    written = _written_settings(radio_client.bridge)
    assert written[PROGRAM_SETTING_WEB_RADIO] == FIP_ID
    assert written[PROGRAM_SETTING_WEB_RADIO_NAME] == "FIP"
    assert written[PROGRAM_SETTING_SPOTIFY_TAG] == "playlist-tag"
    assert written[PROGRAM_SETTING_SPOTIFY_NAME] == "Morning mix"
    assert written[PROGRAM_SETTING_ID] == "129"


async def test_changing_station_never_keeps_the_previous_name(radio_client) -> None:
    await radio_client.put("/api/alarms/1", json={"web_radio": "111"})
    written = _written_settings(radio_client.bridge)
    assert written[PROGRAM_SETTING_WEB_RADIO] == "111"
    assert PROGRAM_SETTING_WEB_RADIO_NAME not in written


async def test_new_station_with_a_name_is_saved_to_the_catalogue(radio_client) -> None:
    await radio_client.put("/api/alarms/1", json={"web_radio": "222", "web_radio_name": "Jazz"})
    assert _written_settings(radio_client.bridge)[PROGRAM_SETTING_WEB_RADIO_NAME] == "Jazz"
    assert json.loads(radio_client.catalog_path.read_text())["222"] == "Jazz"


async def test_known_station_gets_its_catalogue_name(radio_client) -> None:
    await radio_client.get("/api/state")  # learns FIP
    await radio_client.put("/api/alarms/2", json={"time": "09:00", "web_radio": FIP_ID})
    assert _written_settings(radio_client.bridge)[PROGRAM_SETTING_WEB_RADIO_NAME] == "FIP"


def test_catalogue_round_trips_and_sorts_by_name(tmp_path) -> None:
    path = tmp_path / "nested" / "radios.json"
    catalog = RadioCatalog(path)
    catalog.add("2", "b-radio")
    catalog.add("1", "A-radio")
    reloaded = RadioCatalog(path)
    assert [entry["name"] for entry in reloaded.entries()] == ["A-radio", "b-radio"]
    assert reloaded.name_for("2") == "b-radio"


def test_catalogue_ignores_unset_marker_and_unnamed_stations() -> None:
    catalog = RadioCatalog()
    catalog.learn(
        [
            Alarm(1, 7, 0, True, 0, None, 15, web_radio="18446744073709551615", web_radio_name="x"),
            Alarm(2, 7, 0, True, 0, None, 15, web_radio="5"),
        ]
    )
    assert catalog.entries() == []


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"1": 2}'])
def test_invalid_catalogue_file_is_reported(tmp_path, content) -> None:
    path = tmp_path / "radios.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="radio catalogue"):
        RadioCatalog(path)
