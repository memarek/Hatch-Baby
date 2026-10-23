#!/usr/bin/env python3
"""Bedtime Spotify + Hatch controller.

Hatch devices cannot receive Spotify audio directly (they have no Spotify
Connect support, and Spotify does not expose raw stream URLs). This script
gets you the same result by:

  * starting a Spotify playlist/album/track on a Spotify Connect speaker in
    the nursery (Echo, Sonos, Google Nest, a phone, a laptop, ...), and
  * turning on the Hatch nightlight with no Hatch sound, so the two don't
    play over each other. Later you can pick a sound in the Hatch app as usual.

Commands:
  devices    List your Hatch devices and Spotify Connect speakers.
  play       Start Spotify and turn on the Hatch nightlight (no Hatch sound).
  stop       Pause Spotify and (optionally) turn the Hatch off.
  play-url   Have the Hatch itself stream a public MP3/WAV URL (not Spotify).

Configuration comes from environment variables (or a .env file); see
.env.example.
"""

import argparse
import asyncio
import os
import re
import sys
import time

DEFAULT_SPOTIFY_SCOPE = "user-read-playback-state user-modify-playback-state"
MQTT_FLUSH_SECONDS = 2
NO_SOUND_ID = 19998  # hatch_rest_api.const.NO_SOUND_ID, the Hatch's "no sound" id


class ControlError(Exception):
    """A problem worth showing to the user (bad config, device not found, ...)."""


def load_dotenv(path=".env"):
    """Minimal .env loader so python-dotenv isn't required."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise ControlError(f"Missing required setting {name} (see .env.example)")
    return value


def parse_hex_color(value):
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid color {value!r}, expected e.g. ff8800")
    h = match.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def to_spotify_uri(value):
    """Accept a spotify: URI or an open.spotify.com link and return a URI."""
    if value.startswith("spotify:"):
        return value
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(playlist|album|track|artist|show|episode)/([A-Za-z0-9]+)", value)
    if not match:
        raise argparse.ArgumentTypeError(f"not a Spotify URI or link: {value!r}")
    return f"spotify:{match.group(1)}:{match.group(2)}"


# --------------------------------------------------------------------------
# Spotify
# --------------------------------------------------------------------------

def spotify_auth():
    from spotipy.oauth2 import SpotifyOAuth

    return SpotifyOAuth(
        client_id=require_env("SPOTIFY_CLIENT_ID"),
        client_secret=require_env("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=DEFAULT_SPOTIFY_SCOPE,
        cache_path=os.environ.get("SPOTIFY_TOKEN_CACHE", ".spotify_token_cache"),
        open_browser=False,
    )


def spotify_client(auth=None):
    import spotipy

    return spotipy.Spotify(auth_manager=auth or spotify_auth())


def find_spotify_device(devices, name):
    """Pick a device by id or (case-insensitive, partial) name, or the active one."""
    if name:
        for device in devices:
            if device["id"] == name:
                return device
        wanted = name.lower()
        for device in devices:
            if device["name"].lower() == wanted:
                return device
        for device in devices:
            if wanted in device["name"].lower():
                return device
        return None
    for device in devices:
        if device.get("is_active"):
            return device
    return devices[0] if devices else None


def start_spotify(sp, uri, device_name, volume, shuffle):
    devices = sp.devices()["devices"]
    device = find_spotify_device(devices, device_name)
    if device is None:
        names = ", ".join(d["name"] for d in devices) or "none online"
        raise ControlError(f"Spotify speaker {device_name!r} not found. Available: {names}. "
                           "Open Spotify on that speaker once so it shows up.")

    kwargs = {"device_id": device["id"]}
    if uri.startswith("spotify:track:") or uri.startswith("spotify:episode:"):
        kwargs["uris"] = [uri]
    else:
        kwargs["context_uri"] = uri
    sp.start_playback(**kwargs)
    if volume is not None and device.get("supports_volume", True):
        sp.volume(volume, device_id=device["id"])
    if shuffle is not None:
        sp.shuffle(shuffle, device_id=device["id"])
    print(f"Spotify playing {uri} on {device['name']}")
    return device


# --------------------------------------------------------------------------
# Hatch (via the unofficial hatch_rest_api cloud library)
# --------------------------------------------------------------------------

async def with_hatch(action):
    """Connect to the Hatch cloud, run action(devices), then disconnect."""
    from aiohttp import ClientSession
    from hatch_rest_api import get_rest_devices

    async with ClientSession() as session:
        _, mqtt, devices, _ = await get_rest_devices(
            email=require_env("HATCH_EMAIL"),
            password=require_env("HATCH_PASSWORD"),
            client_session=session,
        )
        try:
            # Let each device's current state arrive before acting on it.
            await asyncio.sleep(MQTT_FLUSH_SECONDS)
            result = action(devices)
            # Device commands are published over MQTT asynchronously.
            await asyncio.sleep(MQTT_FLUSH_SECONDS)
            return result
        finally:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: mqtt.disconnect().result(10)
            )


def find_hatch_device(devices, name):
    if not devices:
        raise ControlError("No Hatch devices found on this account.")
    if not name:
        return devices[0]
    wanted = name.lower()
    for device in devices:
        if wanted in device.device_name.lower():
            return device
    names = ", ".join(d.device_name for d in devices)
    raise ControlError(f"Hatch device {name!r} not found. Available: {names}")


def stop_hatch_sound(device):
    """Stop a Hatch sound that's playing, without touching its volume or light."""
    kind = type(device).__name__
    if kind in ("RestPlus", "RestMini"):
        from hatch_rest_api import RestMiniAudioTrack, RestPlusAudioTrack

        device.set_audio_track(RestPlusAudioTrack.NONE if kind == "RestPlus" else RestMiniAudioTrack.NONE)
    else:
        # Newer models have no "sound off, light on" call (turn_off() turns the
        # light off too), so switch the sound to the "no sound" id instead.
        device._update({"current": {"sound": {"id": NO_SOUND_ID, "mute": True}}})


def hatch_nightlight(device, rgb, brightness):
    """Turn on the Hatch light and stop any Hatch sound so it doesn't play over Spotify.

    The Hatch volume is left as it is, so a sound picked later in the Hatch app
    plays normally.
    """
    kind = type(device).__name__
    if kind == "RestPlus":
        device.set_on(True)  # powering on resumes the last track, so always stop it
    if kind == "RestPlus" or getattr(device, "is_playing", False):
        stop_hatch_sound(device)
    if hasattr(device, "set_color"):
        r, g, b = rgb
        device.set_color(r, g, b, brightness=brightness)
        print(f"Hatch '{device.device_name}': light #{r:02x}{g:02x}{b:02x} at {brightness}%, no Hatch sound")
    else:
        print(f"Hatch '{device.device_name}' ({kind}) has no light; Hatch sound stopped")


def wait_for_hatch_state(device, seconds=4):
    """Give the device's current state a moment to arrive from Hatch's cloud."""
    deadline = time.time() + seconds
    while not getattr(device, "is_online", True) and time.time() < deadline:
        time.sleep(0.2)


def hatch_snapshot(device):
    """What the Hatch is doing right now: sound, volume and light, as plain data."""
    kind = type(device).__name__
    playing = bool(getattr(device, "is_playing", False))
    sound = routine = None
    track = getattr(device, "audio_track", None)
    if kind not in ("RestPlus", "RestMini"):
        if getattr(device, "current_playing", None) == "routine":
            favorites = getattr(device, "favorites", None) or []
            match = next((f for f in favorites if f.get("id") == getattr(device, "current_id", None)), None)
            routine = (match or {}).get("name") or "A Hatch favorite"
        if playing:
            info = (getattr(device, "sounds_by_id", None) or {}).get(getattr(device, "sound_id", None)) or {}
            sound = info.get("title")
    if playing and not sound:
        # Enum names like PinkNoise -> "Pink Noise"
        sound = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", track.name) if track is not None else "A Hatch sound"

    has_light = hasattr(device, "set_color")
    if kind == "RestPlus":
        light_on = bool(getattr(device, "is_on", False) and getattr(device, "brightness", 0))
    else:
        light_on = bool(getattr(device, "is_light_on", False))
    rgb = tuple(int(getattr(device, c, 0) or 0) for c in ("red", "green", "blue"))
    return {
        "name": device.device_name,
        "online": bool(getattr(device, "is_online", True)),
        "playing": playing,
        "sound": sound,
        "routine": routine,
        "volume": getattr(device, "volume", None),
        "has_light": has_light,
        "light_on": has_light and light_on,
        "color": "#%02x%02x%02x" % rgb if light_on else None,
        "brightness": getattr(device, "brightness", None) if light_on else None,
    }


def hatch_off(device):
    kind = type(device).__name__
    if kind == "RestPlus":
        device.set_on(False)
    else:
        if hasattr(device, "turn_off"):
            device.turn_off()
        if hasattr(device, "turn_light_off"):
            device.turn_light_off()
    print(f"Hatch '{device.device_name}' turned off")


def hatch_play_url(device, url, volume):
    if not hasattr(device, "set_sound_url"):
        raise ControlError(f"{type(device).__name__} can't stream URLs; only Rest 2nd gen / Rest Baby models can.")
    device.set_sound_url(url)
    if volume is not None:
        device.set_volume(volume)
    print(f"Hatch '{device.device_name}' streaming {url}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_devices(args):
    def show(devices):
        print("Hatch devices:")
        for d in devices:
            print(f"  - {d.device_name} ({type(d).__name__})")

    asyncio.run(with_hatch(show))
    print("Spotify Connect speakers:")
    for d in spotify_client().devices()["devices"]:
        active = " [active]" if d.get("is_active") else ""
        print(f"  - {d['name']} ({d['type']}){active}")


def cmd_check_hatch(args):
    def show(devices):
        names = ", ".join(f"{d.device_name} ({type(d).__name__})" for d in devices)
        print(f"Hatch login works. Found: {names}")

    try:
        asyncio.run(with_hatch(show))
    except ControlError:
        raise
    except Exception as e:
        raise ControlError(f"Hatch login failed: {e}")


def cmd_check_spotify(args):
    from spotipy.cache_handler import MemoryCacheHandler
    from spotipy.oauth2 import SpotifyClientCredentials

    creds = SpotifyClientCredentials(
        client_id=require_env("SPOTIFY_CLIENT_ID"),
        client_secret=require_env("SPOTIFY_CLIENT_SECRET"),
        cache_handler=MemoryCacheHandler(),
    )
    try:
        creds.get_access_token(as_dict=False, check_cache=False)
    except Exception as e:
        raise ControlError(f"Couldn't verify the Spotify client ID/secret: {e}")
    print("Spotify app ID and secret work.")


def cmd_play(args):
    sp = spotify_client()
    start_spotify(sp, args.uri, args.speaker, args.volume, args.shuffle)

    if not args.no_hatch:
        asyncio.run(with_hatch(
            lambda ds: hatch_nightlight(find_hatch_device(ds, args.hatch), args.color, args.brightness)
        ))


def cmd_stop(args):
    sp = spotify_client()
    try:
        sp.pause_playback()
        print("Spotify paused")
    except Exception as e:  # nothing playing is reported as an error
        print(f"Spotify: {e}")
    if args.hatch_off:
        asyncio.run(with_hatch(lambda ds: hatch_off(find_hatch_device(ds, args.hatch))))


def cmd_play_url(args):
    asyncio.run(with_hatch(
        lambda ds: hatch_play_url(find_hatch_device(ds, args.hatch), args.url, args.volume)
    ))


def build_parser():
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("devices", help="list Hatch devices and Spotify speakers")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("check-hatch", help="test the Hatch login in .env")
    p.set_defaults(func=cmd_check_hatch)

    p = sub.add_parser("check-spotify", help="test the Spotify app ID/secret in .env")
    p.set_defaults(func=cmd_check_spotify)

    p = sub.add_parser("play", help="play Spotify and put the Hatch in nightlight mode")
    p.add_argument("uri", nargs="?", type=to_spotify_uri, default=env("SPOTIFY_DEFAULT_URI"),
                   help="Spotify playlist/album/track URI or open.spotify.com link")
    p.add_argument("--speaker", default=env("SPOTIFY_SPEAKER"), help="Spotify Connect speaker name")
    p.add_argument("--hatch", default=env("HATCH_DEVICE"), help="Hatch device name (default: first)")
    p.add_argument("--volume", type=int, help="Spotify volume 0-100")
    p.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--color", type=parse_hex_color, default=parse_hex_color(env("HATCH_COLOR", "ff6a00")),
                   help="nightlight color as hex, e.g. ff6a00 (warm orange)")
    p.add_argument("--brightness", type=int, default=int(env("HATCH_BRIGHTNESS", "20")),
                   help="nightlight brightness 0-100")
    p.add_argument("--no-hatch", action="store_true", help="only start Spotify")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("stop", help="pause Spotify")
    p.add_argument("--hatch", default=env("HATCH_DEVICE"))
    p.add_argument("--hatch-off", action="store_true", help="also turn the Hatch off")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("play-url", help="stream a public MP3/WAV URL on the Hatch itself")
    p.add_argument("url")
    p.add_argument("--hatch", default=env("HATCH_DEVICE"))
    p.add_argument("--volume", type=int)
    p.set_defaults(func=cmd_play_url)
    return parser


def main(argv=None):
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "play" and not args.uri:
        sys.exit("Give a Spotify URI/link or set SPOTIFY_DEFAULT_URI")
    try:
        args.func(args)
    except ControlError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
