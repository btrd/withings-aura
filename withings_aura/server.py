"""HTTP JSON API and web UI for the Withings Aura."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, tzinfo
from importlib import resources
from typing import Any

from aiohttp import web

from .const import DAYS, MAX_ALARM_SLOTS, PROGRAM_NAME_TO_TYPE, PROGRAM_TYPES
from .controller import AuraController
from .exceptions import AuraError
from .models import AuraSnapshot
from .radios import RadioCatalog
from .util import parse_hhmm, parse_optional_int

_LOGGER = logging.getLogger(__name__)

CONTROLLER_KEY = web.AppKey("controller", AuraController)
TIMEZONE_KEY = web.AppKey("timezone", tzinfo)
RADIOS_KEY = web.AppKey("radios", RadioCatalog)

MAX_WEB_RADIO_BYTES = 255
MIN_ONE_SHOT_YEAR = 2000
MAX_ONE_SHOT_YEAR = 2255


class ApiError(Exception):
    """A request the server rejects, with its HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- validation


def _optional_int(body: dict[str, Any], key: str, low: int, high: int) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ApiError(f"{key} must be an integer between {low} and {high}")
    return value


def _optional_bool(body: dict[str, Any], key: str) -> bool | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ApiError(f"{key} must be a boolean")
    return value


def _required_bool(body: dict[str, Any], key: str) -> bool:
    value = _optional_bool(body, key)
    if value is None:
        raise ApiError(f"{key} is required")
    return value


def _optional_rgb(body: dict[str, Any]) -> tuple[int, int, int] | None:
    value = body.get("rgb")
    if value is None:
        return None
    valid = (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value)
    )
    if not valid:
        raise ApiError("rgb must be a list of three integers between 0 and 255")
    return (value[0], value[1], value[2])


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_WEB_RADIO_BYTES:
        raise ApiError(f"{key} must be text of at most {MAX_WEB_RADIO_BYTES} bytes")
    return value


def _optional_web_radio(body: dict[str, Any]) -> str | None:
    return _optional_text(body, "web_radio")


def _optional_one_shot_date(body: dict[str, Any]) -> str | None:
    value = body.get("one_shot_date")
    if value is None:
        return None
    if value == "":
        return ""  # explicit clear: turns a one-shot alarm back into a recurring one
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or not MIN_ONE_SHOT_YEAR <= parsed.year <= MAX_ONE_SHOT_YEAR:
        raise ApiError(
            f"one_shot_date must be YYYY-MM-DD, years {MIN_ONE_SHOT_YEAR}-{MAX_ONE_SHOT_YEAR}"
        )
    return parsed.isoformat()


def _optional_days(body: dict[str, Any]) -> list[str] | None:
    value = body.get("days")
    if value is None:
        return None
    if not isinstance(value, list) or any(day not in DAYS for day in value):
        raise ApiError(f"days must be a list of {', '.join(DAYS)}")
    return value


def _optional_time(body: dict[str, Any]) -> tuple[int | None, int | None]:
    value = body.get("time")
    if value is None:
        return None, None
    try:
        return parse_hhmm(value)
    except (ValueError, AttributeError) as err:
        raise ApiError("time must be HH:MM") from err


def _program_type(name: str) -> int:
    if name not in PROGRAM_NAME_TO_TYPE:
        raise ApiError(f"unknown program: {name}", status=404)
    return PROGRAM_NAME_TO_TYPE[name]


def _slot(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_ALARM_SLOTS:
        raise ApiError(f"slot must be between 1 and {MAX_ALARM_SLOTS}", status=404)
    return int(raw)


async def _json_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError as err:
        raise ApiError("body must be valid JSON") from err
    if not isinstance(body, dict):
        raise ApiError("body must be a JSON object")
    return body


# ------------------------------------------------------------- serialization


def snapshot_to_json(snapshot: AuraSnapshot) -> dict[str, Any]:
    """Render the cached state without the unit's association secret."""
    probe = asdict(snapshot.probe)
    probe.pop("secret", None)
    return {
        "device": probe,
        "status": {
            "name": snapshot.status.name,
            "code": snapshot.status.status,
            "playing": snapshot.status.playing,
        },
        "lamp": {
            "on": snapshot.lamp.is_on,
            "rgb": list(snapshot.lamp.normalized_rgb),
            "brightness": snapshot.lamp.brightness,
        },
        "clock": {
            "display": snapshot.settings.clock_display,
            "brightness": snapshot.settings.clock_brightness,
        },
        "private_mode": snapshot.settings.private_mode,
        "alarms": {
            "enabled": snapshot.alarm_enabled,
            "slots": snapshot.alarm_count,
            "items": [alarm.as_dict() for alarm in snapshot.alarms],
        },
        "programs": {
            name: {
                "volume": parse_optional_int(snapshot.programs.get(code, {}).get("1")),
                "brightness": parse_optional_int(snapshot.programs.get(code, {}).get("2")),
                "program_id": parse_optional_int(snapshot.programs.get(code, {}).get("3")),
            }
            for code, name in PROGRAM_TYPES.items()
        },
        "last_seen": snapshot.last_seen,
    }


# ------------------------------------------------------------------ handlers


def _controller(request: web.Request) -> AuraController:
    return request.app[CONTROLLER_KEY]


async def get_state(request: web.Request) -> web.Response:
    controller = _controller(request)
    snapshot = controller.require_snapshot()
    catalog = request.app[RADIOS_KEY]
    catalog.learn(snapshot.alarms)
    return web.json_response(
        {
            **snapshot_to_json(snapshot),
            "radios": catalog.entries(),
            "error": controller.last_error,
            "time_sync": controller.time_sync,
        }
    )


async def post_refresh(request: web.Request) -> web.Response:
    snapshot = await _controller(request).refresh()
    return web.json_response(snapshot_to_json(snapshot))


async def put_lamp(request: web.Request) -> web.Response:
    body = await _json_body(request)
    lamp = await _controller(request).set_lamp(
        on=_optional_bool(body, "on"),
        rgb=_optional_rgb(body),
        brightness=_optional_int(body, "brightness", 0, 100),
    )
    return web.json_response(
        {"on": lamp.is_on, "rgb": list(lamp.normalized_rgb), "brightness": lamp.brightness}
    )


async def put_clock(request: web.Request) -> web.Response:
    body = await _json_body(request)
    display = _optional_bool(body, "display")
    brightness = _optional_int(body, "brightness", 0, 100)
    if display is None and brightness is None:
        raise ApiError("provide display and/or brightness")
    await _controller(request).set_clock_display(display=display, brightness=brightness)
    return web.json_response({"ok": True})


async def put_private_mode(request: web.Request) -> web.Response:
    body = await _json_body(request)
    await _controller(request).set_private_mode(_required_bool(body, "on"))
    return web.json_response({"ok": True})


async def put_alarms_enabled(request: web.Request) -> web.Response:
    body = await _json_body(request)
    await _controller(request).set_alarm_enabled(_required_bool(body, "enabled"))
    return web.json_response({"ok": True})


async def put_alarm(request: web.Request) -> web.Response:
    slot = _slot(request.match_info["slot"])
    body = await _json_body(request)
    hour, minute = _optional_time(body)
    station = _optional_web_radio(body)
    station_name = _optional_text(body, "web_radio_name")
    catalog = request.app[RADIOS_KEY]
    if station and station_name:
        catalog.add(station, station_name)
    await _controller(request).set_alarm(
        slot,
        hour=hour,
        minute=minute,
        days=_optional_days(body),
        enabled=_optional_bool(body, "enabled"),
        program_id=_optional_int(body, "program_id", 0, 255),
        one_shot_date=_optional_one_shot_date(body),
        volume=_optional_int(body, "volume", 0, 100),
        brightness=_optional_int(body, "brightness", 0, 100),
        setting_id=_optional_int(body, "setting_id", 0, 255),
        web_radio=station,
        web_radio_name=(station_name or catalog.name_for(station)) if station else None,
    )
    return web.json_response({"ok": True})


async def post_start_program(request: web.Request) -> web.Response:
    body = await _json_body(request)
    program = body.get("program")
    if not isinstance(program, str):
        raise ApiError("program is required")
    await _controller(request).start_program(
        _program_type(program),
        _optional_int(body, "program_id", 0, 255),
        _optional_int(body, "volume", 0, 100),
        _optional_int(body, "brightness", 0, 100),
        _optional_web_radio(body),
    )
    return web.json_response({"ok": True})


async def post_trigger_alarm(request: web.Request) -> web.Response:
    body = await _json_body(request)
    program_id = _optional_int(body, "program_id", 0, 255)
    await _controller(request).trigger_alarm(
        *([] if program_id is None else [program_id]),
        volume=_optional_int(body, "volume", 0, 100),
        brightness=_optional_int(body, "brightness", 0, 100),
    )
    return web.json_response({"ok": True})


async def post_stop_program(request: web.Request) -> web.Response:
    await _controller(request).stop_program()
    return web.json_response({"ok": True})


async def post_snooze(request: web.Request) -> web.Response:
    await _controller(request).snooze()
    return web.json_response({"ok": True})


async def put_program_settings(request: web.Request) -> web.Response:
    program_type = _program_type(request.match_info["program"])
    body = await _json_body(request)
    volume = _optional_int(body, "volume", 0, 100)
    brightness = _optional_int(body, "brightness", 0, 100)
    program_id = _optional_int(body, "program_id", 0, 255)
    if volume is None and brightness is None and program_id is None:
        raise ApiError("provide volume, brightness and/or program_id")
    await _controller(request).set_program_settings(program_type, volume, brightness, program_id)
    return web.json_response({"ok": True})


async def post_sync_time(request: web.Request) -> web.Response:
    body = await _json_body(request)
    now = datetime.now(request.app[TIMEZONE_KEY])
    info = await _controller(request).sync_time(now, _optional_bool(body, "twelve_hour"))
    return web.json_response(info)


async def get_index(_request: web.Request) -> web.Response:
    page = resources.files("withings_aura").joinpath("static/index.html").read_text("utf-8")
    return web.Response(text=page, content_type="text/html")


# --------------------------------------------------------------- application


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as err:
        return web.json_response({"error": str(err)}, status=err.status)
    except AuraError as err:
        _LOGGER.warning("Aura command failed: %s", err)
        return web.json_response({"error": f"Aura unavailable: {err}"}, status=502)


def create_app(
    controller: AuraController, timezone: tzinfo, radios: RadioCatalog | None = None
) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app[CONTROLLER_KEY] = controller
    app[TIMEZONE_KEY] = timezone
    app[RADIOS_KEY] = radios or RadioCatalog()
    app.add_routes(
        [
            web.get("/", get_index),
            web.get("/api/state", get_state),
            web.post("/api/refresh", post_refresh),
            web.put("/api/lamp", put_lamp),
            web.put("/api/clock", put_clock),
            web.put("/api/private-mode", put_private_mode),
            web.put("/api/alarms/enabled", put_alarms_enabled),
            web.put("/api/alarms/{slot}", put_alarm),
            web.post("/api/programs/start", post_start_program),
            web.post("/api/programs/stop", post_stop_program),
            web.put("/api/programs/{program}/settings", put_program_settings),
            web.post("/api/alarm/trigger", post_trigger_alarm),
            web.post("/api/snooze", post_snooze),
            web.post("/api/time/sync", post_sync_time),
        ]
    )
    return app
