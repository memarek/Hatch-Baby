#!/usr/bin/env python3
"""Web UI for the Spotify + Hatch bedtime controller.

Run it on a computer at home, then open http://<that-computer>:8888 from any
phone or laptop on the same Wi-Fi. Uses only the standard library on top of
hatch_spotify.py's dependencies.

    python hatch_ui.py [--host 0.0.0.0] [--port 8888]
"""

import argparse
import asyncio
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hatch_spotify as hs

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# One Hatch cloud connection at a time; each call logs in and connects to MQTT.
hatch_lock = threading.Lock()


def run_hatch(action):
    with hatch_lock:
        return asyncio.run(hs.with_hatch(action))


class SleepTimer:
    """Fades Spotify out after a delay, optionally turning the Hatch off too."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = None
        self.ends_at = None

    def start(self, minutes, sp, device_id, volume, hatch_name, hatch_off):
        self.cancel()
        cancel = threading.Event()
        with self._lock:
            self._cancel = cancel
            self.ends_at = time.time() + minutes * 60

        def run():
            if cancel.wait(minutes * 60):
                return
            try:
                hs.fade_out_and_pause(sp, device_id, volume)
                if hatch_off:
                    run_hatch(lambda ds: hs.hatch_off(hs.find_hatch_device(ds, hatch_name)))
            except Exception:
                traceback.print_exc()
            finally:
                with self._lock:
                    if self._cancel is cancel:
                        self._cancel = None
                        self.ends_at = None

        threading.Thread(target=run, daemon=True).start()

    def cancel(self):
        with self._lock:
            if self._cancel:
                self._cancel.set()
            self._cancel = None
            self.ends_at = None


timer = SleepTimer()

MODEL_NAMES = {
    "RestMini": "Rest Mini",
    "RestPlus": "Rest+",
    "RestIot": "Rest 2nd gen",
    "RestBaby": "Rest Baby",
    "RestoreIot": "Restore",
    "RestoreV4": "Restore",
    "RestoreV5": "Restore",
}


def defaults():
    env = os.environ.get
    return {
        "uri": env("SPOTIFY_DEFAULT_URI", ""),
        "speaker": env("SPOTIFY_SPEAKER", ""),
        "hatch": env("HATCH_DEVICE", ""),
        "color": "#" + env("HATCH_COLOR", "ff6a00").lstrip("#"),
        "brightness": int(env("HATCH_BRIGHTNESS", "20")),
    }


class App:
    def __init__(self):
        self.auth = hs.spotify_auth()

    def spotify_authed(self):
        return bool(self.auth.validate_token(self.auth.cache_handler.get_cached_token()))

    def sp(self):
        if not self.spotify_authed():
            raise hs.ControlError("Connect Spotify first.")
        return hs.spotify_client(self.auth)

    # ---- API handlers: take a JSON dict, return a JSON-able value ----

    def state(self, _):
        result = {"spotify_authed": self.spotify_authed(), "defaults": defaults(),
                  "timer_ends_at": timer.ends_at, "speakers": [], "playback": None}
        if result["spotify_authed"]:
            sp = self.sp()
            result["speakers"] = [
                {"id": d["id"], "name": d["name"], "type": d["type"],
                 "active": d.get("is_active", False), "volume": d.get("volume_percent")}
                for d in sp.devices()["devices"]
            ]
            pb = sp.current_playback()
            if pb and pb.get("item"):
                item = pb["item"]
                images = (item.get("album") or {}).get("images") or item.get("images") or []
                result["playback"] = {
                    "is_playing": pb.get("is_playing"),
                    "title": item.get("name"),
                    "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                    "image": images[-1]["url"] if images else None,
                    "device": (pb.get("device") or {}).get("name"),
                }
        return result

    def hatches(self, _):
        return run_hatch(lambda ds: [
            {"name": d.device_name, "model": MODEL_NAMES.get(type(d).__name__, type(d).__name__),
             "has_light": hasattr(d, "set_color"), "can_stream_url": hasattr(d, "set_sound_url")}
            for d in ds
        ])

    def play(self, body):
        uri = hs.to_spotify_uri((body.get("uri") or "").strip())
        sp = self.sp()
        volume = body.get("volume")
        device = hs.start_spotify(sp, uri, body.get("speaker"), volume, body.get("shuffle"))
        if body.get("use_hatch", True):
            rgb = hs.parse_hex_color(body.get("color") or defaults()["color"])
            brightness = int(body.get("brightness", defaults()["brightness"]))
            run_hatch(lambda ds: hs.hatch_nightlight(hs.find_hatch_device(ds, body.get("hatch")), rgb, brightness))
        minutes = float(body.get("minutes") or 0)
        if minutes > 0:
            timer.start(minutes, sp, device["id"], volume or device.get("volume_percent") or 50,
                        body.get("hatch"), bool(body.get("hatch_off_after")))
        else:
            timer.cancel()
        return {"ok": True, "message": f"Playing on {device['name']}"}

    def pause(self, body):
        timer.cancel()
        sp = self.sp()
        try:
            sp.pause_playback()
        except Exception:
            pass  # Spotify reports "nothing playing" as an error
        if body.get("hatch_off"):
            run_hatch(lambda ds: hs.hatch_off(hs.find_hatch_device(ds, body.get("hatch"))))
        return {"ok": True, "message": "Stopped"}

    def resume(self, body):
        self.sp().start_playback(device_id=body.get("speaker") or None)
        return {"ok": True}

    def skip(self, body):
        sp = self.sp()
        (sp.previous_track if body.get("back") else sp.next_track)()
        return {"ok": True}

    def volume(self, body):
        self.sp().volume(int(body["volume"]), device_id=body.get("speaker") or None)
        return {"ok": True}

    def light(self, body):
        rgb = hs.parse_hex_color(body["color"])
        brightness = int(body["brightness"])
        run_hatch(lambda ds: hs.hatch_nightlight(hs.find_hatch_device(ds, body.get("hatch")), rgb, brightness))
        return {"ok": True, "message": "Nightlight updated"}

    def hatch_off(self, body):
        run_hatch(lambda ds: hs.hatch_off(hs.find_hatch_device(ds, body.get("hatch"))))
        return {"ok": True, "message": "Hatch off"}

    def hatch_url(self, body):
        url = (body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise hs.ControlError("Enter an http(s) link to an MP3 or WAV file.")
        volume = body.get("volume")
        run_hatch(lambda ds: hs.hatch_play_url(hs.find_hatch_device(ds, body.get("hatch")), url,
                                               int(volume) if volume is not None else None))
        return {"ok": True, "message": "Hatch is streaming"}

    def cancel_timer(self, _):
        timer.cancel()
        return {"ok": True}


ROUTES = {
    ("GET", "/api/state"): "state",
    ("GET", "/api/hatches"): "hatches",
    ("POST", "/api/play"): "play",
    ("POST", "/api/stop"): "pause",
    ("POST", "/api/resume"): "resume",
    ("POST", "/api/skip"): "skip",
    ("POST", "/api/volume"): "volume",
    ("POST", "/api/light"): "light",
    ("POST", "/api/hatch-off"): "hatch_off",
    ("POST", "/api/hatch-url"): "hatch_url",
    ("POST", "/api/timer/cancel"): "cancel_timer",
}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep the console quiet; errors are still printed

        def send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def handle_request(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and url.path == "/login":
                return self.redirect(app.auth.get_authorize_url())
            if method == "GET" and url.path == "/callback":
                code = parse_qs(url.query).get("code", [None])[0]
                if code:
                    app.auth.get_access_token(code, as_dict=False)
                return self.redirect("/")

            name = ROUTES.get((method, url.path))
            if not name:
                return self.send(404, {"error": "Not found"})
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            try:
                self.send(200, getattr(app, name)(body))
            except (hs.ControlError, argparse.ArgumentTypeError, ValueError, KeyError) as e:
                self.send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self.send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

    return Handler


def main():
    hs.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("UI_HOST", "0.0.0.0"),
                        help="interface to listen on (0.0.0.0 = reachable from your Wi-Fi)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", "8888")))
    args = parser.parse_args()
    try:
        app = App()
    except hs.ControlError as e:
        raise SystemExit(str(e))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Bedtime UI running on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    if not app.spotify_authed():
        print(f"First run: open http://127.0.0.1:{args.port} on THIS computer and click 'Connect Spotify'.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
