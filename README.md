# Withings Aura server

A small local server that lets you control a **Withings Aura (WSD01)** bedside
unit from a Mac (or any machine on the same LAN). It talks to the Aura's
WPP-over-TCP bridge directly — no Withings cloud account, no official app — and
exposes a **web UI** and a **JSON HTTP API**.

Tested against firmware **1301** (hardware 12). Independent interoperability
project, not affiliated with Withings.

## Requirements

- Python 3.12+ (`brew install python`)
- The Aura reachable on your LAN, bridge on TCP port **7685**
- Its IP address (reserve it in your router so it does not change)

## Install and run

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m withings_aura --aura-host 192.168.1.50
```

Open <http://127.0.0.1:8686>.

| Option | Default | |
|---|---|---|
| `--aura-host` / `AURA_HOST` | required | Aura IP or hostname |
| `--aura-port` | 7685 | Aura bridge port |
| `--listen` | `127.0.0.1` | Address to serve on |
| `--port` | 8686 | HTTP port |
| `--poll-interval` | 10 s (min 5) | Status/lamp refresh |
| `--slow-poll-interval` | 300 s (min 60) | Alarms/program settings refresh |
| `--radios` | `~/.withings-aura/radios.json` | Web-radio catalogue (see below) |
| `--timezone` | system zone | IANA zone sent by *Sync time* |
| `-v` | | Debug logging |

The server exits with an error at startup if the Aura cannot be reached, and
reconnects by itself afterwards if the connection drops.

### Security

**The API has no authentication.** By default it only listens on `127.0.0.1`.
Use `--listen 0.0.0.0` only on a network you trust — anyone who can reach the
port can change your lamp and alarms.

### Start at login (launchd)

```bash
cp deploy/com.withings-aura.server.plist ~/Library/LaunchAgents/
# edit it: set the repository path and the Aura IP
launchctl load ~/Library/LaunchAgents/com.withings-aura.server.plist
```

Logs go to `/tmp/withings-aura.log`. Stop with `launchctl unload` on the same file.

## HTTP API

All bodies are JSON. Invalid input returns `400 {"error": ...}`; an unreachable
Aura returns `502`. `GET /api/state` never includes the unit's association secret.

| Method & path | Body | |
|---|---|---|
| `GET /api/state` | | Full cached state |
| `POST /api/refresh` | | Read the device now |
| `PUT /api/lamp` | `on`, `rgb` `[0-255]×3`, `brightness` 0–100 | Turning off keeps colour/intensity; turning on resumes them |
| `PUT /api/clock` | `display`, `brightness` 0–100 | |
| `PUT /api/private-mode` | `on` | |
| `PUT /api/alarms/enabled` | `enabled` | Global alarm switch |
| `PUT /api/alarms/{slot}` | `time` `"HH:MM"`, `days` (`mon`…`sun`), `enabled`, `program_id`, `one_shot_date` `"YYYY-MM-DD"`, `volume`, `brightness`, `setting_id`, `web_radio` | Slots 1–10; omitted fields keep their value; empty `days` + date = one-shot |
| `POST /api/programs/start` | `program` (`mood` `nap` `sleep` `wake_up` `retail` `media`), `program_id`, `volume`, `brightness`, `web_radio` | |
| `POST /api/programs/stop` | | |
| `PUT /api/programs/{program}/settings` | `volume`, `brightness`, `program_id` | |
| `POST /api/alarm/trigger` | `program_id`, `volume`, `brightness` | Start a wake-up program now |
| `POST /api/snooze` | | |
| `POST /api/time/sync` | `twelve_hour` | Sends time, UTC offset and next DST change |

```bash
curl -X PUT localhost:8686/api/lamp -H 'content-type: application/json' \
     -d '{"on": true, "rgb": [255, 140, 60], "brightness": 40}'
curl -X PUT localhost:8686/api/alarms/1 -d '{"time": "06:45", "days": ["mon","tue","wed","thu","fri"]}'
```

## Alarms, radio and Spotify

The web page lists your alarms and has an inline editor: time, weekdays or a one-off
date, volume, radio, and an *Advanced* section for the raw program/setting IDs. **+ Add
alarm** always creates the next free slot (alarms are numbered by their position in the
Aura's reply, so slots are filled in order). There is no delete: switch an alarm off.

**Radio.** The Aura stores a radio as an opaque numeric station ID (for example
`8559024957008661`) plus a display name (`FIP`). Withings' catalogue comes from the
[airable](https://airablenow.com/?p=479) internet-radio service, and I found no public
list of those IDs. So the server builds its own catalogue:

- Set a station on an alarm in the Withings app once; the server reads it back and saves
  `id → name` to `--radios` (a plain JSON object you can also edit by hand).
- Saved stations appear in the editor's Radio dropdown. *Another station…* takes an ID and
  a name and saves them too.
- A radio alarm also uses a different program/setting than the default sound (a real FIP
  alarm showed `program_id` 20, `setting_id` 129). When you pick a radio on a new alarm,
  the editor copies these from your existing radio alarm.

**Spotify.** Spotify on the Aura goes through Spotify Connect and needs a Spotify Premium
account linked in the Withings app, so this server cannot choose a playlist. It only
**keeps** what the unit reported: the Spotify tag and name are written back unchanged
when you edit that alarm.

> The station name and Spotify fields are sent back on every alarm save so editing does
> not drop them. Whether the firmware accepts them on write has only been tested against
> the fake bridge: try an edit on a spare slot before relying on it for an alarm you need.

## Time sync

*Sync time* sends the current UTC time, the UTC offset of the server's timezone,
the next DST transition and its offset. The zone is taken from the system
(`/etc/localtime`) unless `--timezone` is given. There is no separate timezone
setting on the Aura.

## Limitations

- No automatic discovery: pass the Aura's IP (the unit advertises
  `_withings-aura-bridge._tcp` over mDNS if you want to look it up).
- A unit that answers with a `ProbeChallenge` (command 296) requires the Withings
  association secret and is not supported.
- Single Aura per server process.

## Development

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
.venv/bin/ruff check withings_aura tests
```

The tests run against a fake bridge (`tests/fake_bridge.py`) that replays
captured replies; no hardware is needed.

## License

MIT. See [LICENSE](LICENSE).
