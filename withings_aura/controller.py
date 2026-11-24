"""Poll the Aura, cache its state and serialize writes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Any

from .const import (
    DEFAULT_ALARM_PROGRAM_ID,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    PROGRAM_TYPES,
    WAKE_UP_PROGRAM_TYPE,
)
from .device import AuraDevice
from .exceptions import AuraError, ProbeChallengeError
from .models import Alarm, AuraSnapshot, LampState
from .protocol import lamp_channels
from .util import parse_optional_int, time_sync_info

_LOGGER = logging.getLogger(__name__)

DEFAULT_LAMP_RGB = (255, 180, 107)
DEFAULT_ALARM_DAYS = ["mon", "tue", "wed", "thu", "fri"]
DEFAULT_ALARM_HOUR = 7
DEFAULT_ALARM_MINUTE = 0
LAMP_HOLD_SECONDS = 5.0


class AuraController:
    """Own the device connection: periodic polling plus write operations."""

    def __init__(
        self,
        device: AuraDevice,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        slow_poll_interval: float = DEFAULT_SLOW_POLL_INTERVAL,
    ) -> None:
        self.device = device
        self.poll_interval = poll_interval
        self.slow_poll_interval = slow_poll_interval
        self.snapshot: AuraSnapshot | None = None
        self.last_error: str | None = None
        self.time_sync: dict[str, object] | None = None
        self._probe = None
        self._slow: dict[str, Any] = {}
        self._slow_stale = True
        self._slow_at = 0.0
        self._lamp_hold_until = 0.0
        self._task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        """Do a first refresh (raising on failure), then poll in the background."""
        await self.refresh()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.device.bridge.async_close()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.refresh()
            except AuraError as err:
                _LOGGER.warning("Aura refresh failed: %s", err)

    async def refresh(self) -> AuraSnapshot:
        """Read the device and replace the cached snapshot."""
        async with self._refresh_lock:
            try:
                snapshot = await self._read_snapshot()
            except ProbeChallengeError:
                self.last_error = "The Aura requires the Withings association secret"
                raise
            except AuraError as err:
                self.last_error = str(err)
                raise
            self.last_error = None
            self.snapshot = snapshot
            return snapshot

    async def _read_snapshot(self) -> AuraSnapshot:
        if self._probe is None:
            self._probe = await self.device.probe()
        status = await self.device.status()
        lamp = await self.device.lamp()
        if time.monotonic() < self._lamp_hold_until and self.snapshot is not None:
            lamp = self.snapshot.lamp  # keep the written value while the firmware fades
        settings = await self.device.settings()
        if self._slow_stale or time.monotonic() - self._slow_at >= self.slow_poll_interval:
            await self._read_slow()
        return AuraSnapshot(
            probe=self._probe,
            status=status,
            lamp=lamp,
            settings=settings,
            alarm_enabled=self._slow["alarm_enabled"],
            alarms=self._slow["alarms"],
            alarm_count=self._slow["alarm_count"],
            programs=self._slow["programs"],
            program_infos=self._slow["program_infos"],
            last_seen=time.time(),
        )

    async def _read_slow(self) -> None:
        slow: dict[str, Any] = {
            "alarms": tuple(await self.device.alarms()),
            "alarm_count": await self.device.alarm_count(),
            "alarm_enabled": await self.device.alarm_enabled(),
            "programs": {},
            "program_infos": {},
        }
        for program_type in PROGRAM_TYPES:
            slow["programs"][program_type] = await self.device.program_settings(program_type)
            slow["program_infos"][program_type] = await self.device.program_info(program_type)
        self._slow = slow
        self._slow_stale = False
        self._slow_at = time.monotonic()

    def require_snapshot(self) -> AuraSnapshot:
        if self.snapshot is None:
            raise AuraError("no state read from the Aura yet")
        return self.snapshot

    # ------------------------------------------------------------------ writes

    async def set_lamp(
        self,
        *,
        on: bool | None = None,
        rgb: tuple[int, int, int] | None = None,
        brightness: int | None = None,
    ) -> LampState:
        """Change lamp power, colour (8-bit RGB) and/or brightness (0-100 %)."""
        current = self.require_snapshot().lamp
        wants_on = current.is_on if on is None else on
        if wants_on:
            hue = rgb or (
                current.normalized_rgb if any(current.normalized_rgb) else DEFAULT_LAMP_RGB
            )
            percent = brightness if brightness is not None else (current.brightness or 100)
            red, green, blue = lamp_channels(hue, percent)
        elif brightness is None and rgb is None:
            # Off keeps the last colour and intensity so the next "on" resumes it.
            red, green, blue = current.red, current.green, current.blue
        else:
            hue = rgb or DEFAULT_LAMP_RGB
            red, green, blue = lamp_channels(hue, brightness)
        self._lamp_hold_until = time.monotonic() + LAMP_HOLD_SECONDS
        try:
            await self.device.set_lamp(red, green, blue, wants_on)
        except Exception:
            self._lamp_hold_until = 0.0
            raise
        lamp = LampState(red=red, green=green, blue=blue, state=1 if wants_on else 0)
        self._update(lamp=lamp)
        return lamp

    async def set_clock_display(self, *, display: bool | None, brightness: int | None) -> None:
        settings = self.require_snapshot().settings
        on = settings.clock_display if display is None else display
        level = settings.clock_brightness if brightness is None else brightness
        await self.device.set_clock_display(on, level)
        self._update(settings=replace(settings, clock_display=on, clock_brightness=level))

    async def set_private_mode(self, on: bool) -> None:
        await self.device.set_private_mode(on)
        self._update(settings=replace(self.require_snapshot().settings, private_mode=on))

    async def set_alarm_enabled(self, on: bool) -> None:
        await self.device.set_alarm_enabled(on)
        self._update(alarm_enabled=on)

    async def start_program(
        self,
        program_type: int,
        program_id: int | None = None,
        volume: int | None = None,
        brightness: int | None = None,
        web_radio: str | None = None,
    ) -> None:
        await self.device.start_program(program_type, program_id, volume, brightness, web_radio)
        self._schedule_refresh()

    async def trigger_alarm(
        self,
        program_id: int = DEFAULT_ALARM_PROGRAM_ID,
        volume: int | None = None,
        brightness: int | None = None,
    ) -> None:
        await self.start_program(WAKE_UP_PROGRAM_TYPE, program_id, volume, brightness)

    async def stop_program(self) -> None:
        await self.device.stop_program()
        self._schedule_refresh()

    async def snooze(self) -> None:
        await self.device.snooze()
        self._schedule_refresh()

    async def sync_time(self, now: datetime, twelve_hour: bool | None = None) -> dict[str, object]:
        info = time_sync_info(now)
        await self.device.sync_time(now, twelve_hour)
        self.time_sync = info
        return info

    async def set_program_settings(
        self,
        program_type: int,
        volume: int | None = None,
        brightness: int | None = None,
        program_id: int | None = None,
    ) -> None:
        await self.device.set_program_settings(program_type, volume, brightness, program_id)
        await self._refresh_after_write(slow=True)

    async def set_alarm(
        self,
        slot: int,
        *,
        hour: int | None = None,
        minute: int | None = None,
        days: list[str] | None = None,
        enabled: bool | None = None,
        program_id: int | None = None,
        one_shot_date: str | None = None,
        volume: int | None = None,
        brightness: int | None = None,
        setting_id: int | None = None,
        web_radio: str | None = None,
        web_radio_name: str | None = None,
    ) -> None:
        """Create or update one alarm slot; fields left as None keep their current value.

        The station name and the Spotify fields the unit reported are written back so an
        edit does not silently drop them. A name only follows its own station: choosing a
        different one takes *web_radio_name*, never the previous station's name.
        """
        existing = next((a for a in self.require_snapshot().alarms if a.slot == slot), None)
        new_station = web_radio is not None and (
            existing is None or web_radio != existing.web_radio
        )
        await self.device.set_alarm(
            slot,
            *_alarm_hour_minute(existing, hour, minute),
            days if days is not None else _alarm_days(existing),
            (existing.enabled if existing else True) if enabled is None else enabled,
            program_id if program_id is not None else _alarm_program(existing),
            one_shot_date
            if one_shot_date is not None
            else (existing.one_shot_date if existing else None),
            volume if volume is not None else _stored_int(existing, "volume"),
            brightness if brightness is not None else _stored_int(existing, "brightness"),
            setting_id if setting_id is not None else _stored_int(existing, "setting_id"),
            web_radio if web_radio is not None else (existing.web_radio if existing else None),
            web_radio_name if new_station else (existing.web_radio_name if existing else None),
            existing.spotify_tag if existing else None,
            existing.spotify_name if existing else None,
        )
        await self._refresh_after_write(slow=True)

    # ----------------------------------------------------------------- helpers

    def _update(self, **changes: Any) -> None:
        self.snapshot = replace(self.require_snapshot(), **changes, last_seen=time.time())

    def _schedule_refresh(self) -> None:
        asyncio.get_running_loop().create_task(self._delayed_refresh())

    async def _delayed_refresh(self) -> None:
        await asyncio.sleep(1.0)
        with contextlib.suppress(AuraError):
            await self.refresh()

    async def _refresh_after_write(self, *, slow: bool) -> None:
        if slow:
            self._slow_stale = True
        with contextlib.suppress(AuraError):
            await self.refresh()


def _alarm_hour_minute(
    existing: Alarm | None, hour: int | None, minute: int | None
) -> tuple[int, int]:
    default_hour = existing.hour if existing else DEFAULT_ALARM_HOUR
    default_minute = existing.minute if existing else DEFAULT_ALARM_MINUTE
    return (default_hour if hour is None else hour, default_minute if minute is None else minute)


def _alarm_days(existing: Alarm | None) -> list[str]:
    if existing and (existing.days or existing.one_shot_date):
        return existing.days  # an empty weekday list is a one-shot alarm
    return list(DEFAULT_ALARM_DAYS)


def _alarm_program(existing: Alarm | None) -> int:
    return existing.program_id if existing else DEFAULT_ALARM_PROGRAM_ID


def _stored_int(existing: Alarm | None, field: str) -> int | None:
    return parse_optional_int(getattr(existing, field)) if existing else None
