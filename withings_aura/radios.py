"""A small name catalogue of web-radio station IDs, kept in a JSON file.

The Aura stores a radio as an opaque numeric station ID plus a display name. There is no
public list of IDs, so the catalogue is learned from the alarms already on the unit
(set a station once in the Withings app) and can be extended by hand: the file is a plain
``{"<station id>": "<name>"}`` mapping.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from .const import UNSET_VALUE
from .models import Alarm

_LOGGER = logging.getLogger(__name__)


class RadioCatalog:
    """Station ID -> name, optionally persisted to *path*."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._names: dict[str, str] = {}
        if path is not None and path.exists():
            self._names = _read(path)

    def entries(self) -> list[dict[str, str]]:
        return [
            {"id": id_, "name": name} for id_, name in sorted(self._names.items(), key=_by_name)
        ]

    def name_for(self, station_id: str) -> str | None:
        return self._names.get(station_id)

    def add(self, station_id: str, name: str) -> None:
        if self._names.get(station_id) == name:
            return
        self._names[station_id] = name
        self._save()

    def learn(self, alarms: Iterable[Alarm]) -> None:
        """Record every named station found on the unit's alarms."""
        for alarm in alarms:
            if alarm.web_radio and alarm.web_radio != UNSET_VALUE and alarm.web_radio_name:
                self.add(alarm.web_radio, alarm.web_radio_name)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._names, indent=2, ensure_ascii=False) + "\n")
            os.replace(temporary, self.path)
        except OSError as err:
            _LOGGER.warning("Could not save radio catalogue %s: %s", self.path, err)


def _by_name(item: tuple[str, str]) -> tuple[str, str]:
    return item[1].casefold(), item[0]


def _read(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise ValueError(f"cannot read radio catalogue {path}: {err}") from err
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError(f"radio catalogue {path} must be a JSON object of id -> name")
    return data
