#!/usr/bin/env python3
"""Web UI for the Spotify + Hatch bedtime controller.

    python hatch_ui.py [--host 127.0.0.1] [--port 8888]

By default it only listens on this computer. To use it from your phone, either
put it behind Tailscale (see deploy/README.md) or pass --host 0.0.0.0 to open
it to your home Wi-Fi. Set UI_PASSWORD whenever anyone else can reach it.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hatch_spotify as hs

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
SESSION_COOKIE = "bedtime_session"
SESSION_DAYS = 90
MAX_BODY_BYTES = 64 * 1024
MAX_FAILED_LOGINS = 5  # per visitor, before that visitor is locked out
MAX_FAILED_LOGINS_TOTAL = 50  # across everyone, caps guessing from many addresses
LOCKOUT_SECONDS = 15 * 60

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' https://*.scdn.co https://*.spotifycdn.com data:; connect-src 'self'; "
        "manifest-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # no-referrer makes browsers send "Origin: null" on forms
    "Cache-Control": "no-store",
}

# App icon and manifest: public so phones can fetch them when adding to the home screen.
STATIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
MAX_FAVORITES = 30
HATCH_CACHE_SECONDS = 10 * 60

MODEL_NAMES = {
    "RestMini": "Rest Mini",
    "RestPlus": "Rest+",
    "RestIot": "Rest 2nd gen",
    "RestBaby": "Rest Baby",
    "RestoreIot": "Restore",
    "RestoreV4": "Restore",
    "RestoreV5": "Restore",
}

# One Hatch cloud connection at a time; each call logs in and connects to MQTT.
hatch_lock = threading.Lock()


def run_hatch(action):
    with hatch_lock:
        return asyncio.run(hs.with_hatch(action))


def defaults():
    env = os.environ.get
    return {
        "uri": env("SPOTIFY_DEFAULT_URI", ""),
        "speaker": env("SPOTIFY_SPEAKER", ""),
        "hatch": env("HATCH_DEVICE", ""),
        "color": "#" + env("HATCH_COLOR", "ff6a00").lstrip("#"),
        "brightness": int(env("HATCH_BRIGHTNESS", "20")),
    }


def load_secret(path):
    """Key for signing session cookies, created on first run and kept private."""
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return bytes.fromhex(f.read().strip())


class Guard:
    """Password login with signed session cookies and a lockout on repeated failures."""

    def __init__(self, password, secret):
        self.password = password
        self.secret = secret
        self.failures = []  # (time, client) for each wrong password
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.password)

    def make_session(self):
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{expires}.{self._sign(expires)}"

    def valid_session(self, value):
        if not self.enabled:
            return True
        expires, _, sig = (value or "").partition(".")
        return (expires.isdigit() and int(expires) > time.time()
                and hmac.compare_digest(sig, self._sign(expires)))

    def check_password(self, attempt, client="unknown"):
        """Returns True/False, or None while this client (or everyone) is locked out.

        Already logged-in browsers keep working during a lockout; it only
        blocks new logins.
        """
        with self.lock:
            now = time.time()
            self.failures = [(t, c) for t, c in self.failures if now - t < LOCKOUT_SECONDS]
            mine = sum(1 for _, c in self.failures if c == client)
            if mine >= MAX_FAILED_LOGINS or len(self.failures) >= MAX_FAILED_LOGINS_TOTAL:
                return None
            ok = hmac.compare_digest(attempt.encode(), self.password.encode())
            if not ok:
                self.failures.append((now, client))
            return ok

    def _sign(self, value):
        # Includes the password so changing it signs everyone out.
        key = self.secret + self.password.encode()
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


class App:
    def __init__(self, favorites_path="favorites.json"):
        self.auth = hs.spotify_auth()
        self.oauth_states = {}  # state -> created time, for the Spotify login round trip
        self.login_enabled = False
        self.favorites_path = favorites_path
        self.favorites_lock = threading.Lock()
        self.hatch_cache = (0, None)  # (fetched at, device list)
        # Hatch commands take several seconds, so they run in the background and
        # the page polls this to show progress.
        self.hatch_job = {"busy": False, "ok": None, "message": "", "at": 0}

    def run_hatch_job(self, action, done_message):
        self.hatch_job = {"busy": True, "ok": None, "message": "Talking to the Hatch…", "at": time.time()}

        def work():
            try:
                run_hatch(action)
                self.hatch_job = {"busy": False, "ok": True, "message": done_message, "at": time.time()}
            except Exception as e:
                if not isinstance(e, hs.ControlError):
                    traceback.print_exc()
                message = str(e) if isinstance(e, hs.ControlError) else "Couldn't reach the Hatch. Try again."
                self.hatch_job = {"busy": False, "ok": False, "message": message, "at": time.time()}

        threading.Thread(target=work, daemon=True).start()

    def load_favorites(self):
        try:
            with open(self.favorites_path) as f:
                favorites = json.load(f)
            return favorites if isinstance(favorites, list) else []
        except (OSError, ValueError):
            return []

    def save_favorites(self, favorites):
        tmp = self.favorites_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(favorites, f)
        os.replace(tmp, self.favorites_path)

    def describe(self, uri):
        """Name, subtitle and cover art for a Spotify URI, for the favorites list."""
        _, kind, item_id = uri.split(":", 2)
        sp = self.sp()
        info = {"uri": uri, "name": "Spotify " + kind, "subtitle": "", "image": None}
        try:
            if kind == "playlist":
                item = sp.playlist(item_id, fields="name,images,owner(display_name)")
                info["subtitle"] = "Playlist · " + ((item.get("owner") or {}).get("display_name") or "")
            elif kind == "album":
                item = sp.album(item_id)
                info["subtitle"] = "Album · " + ", ".join(a["name"] for a in item.get("artists", []))
            elif kind == "track":
                item = sp.track(item_id)
                item["images"] = (item.get("album") or {}).get("images")
                info["subtitle"] = "Song · " + ", ".join(a["name"] for a in item.get("artists", []))
            elif kind == "artist":
                item = sp.artist(item_id)
                info["subtitle"] = "Artist"
            else:
                return info
        except Exception:
            raise hs.ControlError("Couldn't find that on Spotify. Check the link.")
        info["name"] = item.get("name") or info["name"]
        images = item.get("images") or []
        info["image"] = (images[1] if len(images) > 1 else images[0])["url"] if images else None
        return info

    def spotify_authed(self):
        return bool(self.auth.validate_token(self.auth.cache_handler.get_cached_token()))

    def sp(self):
        if not self.spotify_authed():
            raise hs.ControlError("Connect Spotify first.")
        return hs.spotify_client(self.auth)

    def spotify_login_url(self):
        now = time.time()
        self.oauth_states = {s: t for s, t in self.oauth_states.items() if now - t < 600}
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = now
        return self.auth.get_authorize_url(state=state)

    def finish_spotify_login(self, code, state):
        if not code or not state or self.oauth_states.pop(state, None) is None:
            raise hs.ControlError("Spotify login expired or didn't match. Try again.")
        self.auth.get_access_token(code, as_dict=False, check_cache=False)

    # ---- API handlers: take a JSON dict, return a JSON-able value ----

    def state(self, _):
        result = {"spotify_authed": self.spotify_authed(), "defaults": defaults(),
                  "login_enabled": self.login_enabled, "speakers": [], "playback": None,
                  "favorites": self.load_favorites(), "hatch_job": self.hatch_job}
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
                    "image": (images[1] if len(images) > 1 else images[0])["url"] if images else None,
                    "context": (pb.get("context") or {}).get("uri"),
                    "device": (pb.get("device") or {}).get("name"),
                }
        return result

    def hatches(self, query):
        fetched_at, devices = self.hatch_cache
        if devices is None or query.get("refresh") or time.time() - fetched_at > HATCH_CACHE_SECONDS:
            devices = run_hatch(lambda ds: [
                {"name": d.device_name, "model": MODEL_NAMES.get(type(d).__name__, type(d).__name__),
                 "has_light": hasattr(d, "set_color"), "can_stream_url": hasattr(d, "set_sound_url")}
                for d in ds
            ])
            self.hatch_cache = (time.time(), devices)
        return devices

    def add_favorite(self, body):
        uri = hs.to_spotify_uri((body.get("uri") or "").strip())
        info = self.describe(uri)
        with self.favorites_lock:
            favorites = [f for f in self.load_favorites() if f.get("uri") != uri]
            favorites.insert(0, info)
            self.save_favorites(favorites[:MAX_FAVORITES])
        return {"ok": True, "favorite": info, "message": f"Saved {info['name']}"}

    def remove_favorite(self, body):
        with self.favorites_lock:
            favorites = [f for f in self.load_favorites() if f.get("uri") != body.get("uri")]
            self.save_favorites(favorites)
        return {"ok": True}

    def play(self, body):
        uri = hs.to_spotify_uri((body.get("uri") or "").strip())
        device = hs.start_spotify(self.sp(), uri, body.get("speaker"), body.get("volume"), body.get("shuffle"))
        if body.get("use_hatch", True):
            rgb = hs.parse_hex_color(body.get("color") or defaults()["color"])
            brightness = int(body.get("brightness", defaults()["brightness"]))
            self.run_hatch_job(
                lambda ds: hs.hatch_nightlight(hs.find_hatch_device(ds, body.get("hatch")), rgb, brightness),
                "Nightlight on")
        return {"ok": True, "message": f"Playing on {device['name']}"}

    def pause(self, _):
        sp = self.sp()
        try:
            sp.pause_playback()
        except Exception:
            pass  # Spotify reports "nothing playing" as an error
        return {"ok": True, "message": "Spotify stopped"}

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
        self.run_hatch_job(
            lambda ds: hs.hatch_nightlight(hs.find_hatch_device(ds, body.get("hatch")), rgb, brightness),
            "Nightlight updated")
        return {"ok": True}

    def hatch_off(self, body):
        self.run_hatch_job(lambda ds: hs.hatch_off(hs.find_hatch_device(ds, body.get("hatch"))), "Hatch off")
        return {"ok": True}

    def hatch_url(self, body):
        url = (body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise hs.ControlError("Enter an http(s) link to an MP3 or WAV file.")
        volume = body.get("volume")
        run_hatch(lambda ds: hs.hatch_play_url(hs.find_hatch_device(ds, body.get("hatch")), url,
                                               int(volume) if volume is not None else None))
        return {"ok": True, "message": "Hatch is streaming"}


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
    ("POST", "/api/favorites"): "add_favorite",
    ("POST", "/api/favorites/remove"): "remove_favorite",
}


def make_handler(app, guard, secure_cookies=False, public_url=None):
    public_host = urlparse(public_url).netloc if public_url else None
    headers_to_send = dict(SECURITY_HEADERS)
    if secure_cookies:  # served over HTTPS: tell browsers never to use plain HTTP
        headers_to_send["Strict-Transport-Security"] = "max-age=31536000"

    class Handler(BaseHTTPRequestHandler):
        server_version = "Bedtime"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass  # keep the console quiet; errors are still printed

        def send(self, status, body, content_type="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in {**headers_to_send, **(headers or {})}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def send_page(self, name, status=200, headers=None):
            with open(os.path.join(UI_DIR, name), "rb") as f:
                self.send(status, f.read(), "text/html; charset=utf-8", headers)

        def redirect(self, location, headers=None):
            self.send_response(303)
            self.send_header("Location", location)
            for name, value in {**headers_to_send, **(headers or {})}.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def session_cookie(self, value, max_age):
            parts = [f"{SESSION_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
            if secure_cookies:
                parts.append("Secure")
            return {"Set-Cookie": "; ".join(parts)}

        def logged_in(self):
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            morsel = cookie.get(SESSION_COOKIE)
            return guard.valid_session(morsel.value if morsel else None)

        def same_origin(self):
            """Reject requests started by other websites (CSRF)."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # not sent by a browser making a cross-site request
            allowed = {self.headers.get("Host"), public_host}
            return urlparse(origin).netloc in allowed - {None}

        def read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise hs.ControlError("Bad request")
            if length < 0 or length > MAX_BODY_BYTES:
                raise hs.ControlError("Bad request size")
            return self.rfile.read(length)

        def client(self):
            """Best guess at who is connecting, for per-visitor lockouts.

            Behind a proxy (Tailscale), use the last X-Forwarded-For entry: the one
            the proxy itself added. Earlier entries come from the visitor and can
            be faked.
            """
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[-1].strip()
            return self.client_address[0]

        def login(self):
            form = parse_qs(self.read_body().decode())
            client = self.client()
            ok = guard.check_password(form.get("password", [""])[0], client)
            if ok:
                return self.redirect("/", self.session_cookie(guard.make_session(), SESSION_DAYS * 86400))
            print(f"Failed login from {client}" + (" (locked out)" if ok is None else ""), flush=True)
            time.sleep(1)  # slow down guessing
            error = "locked" if ok is None else "wrong"
            return self.redirect(f"/login?error={error}")

        def handle_request(self, method):
            url = urlparse(self.path)
            if method == "POST" and not self.same_origin():
                return self.send(403, {"error": "Cross-site request blocked"})
            if method == "GET" and url.path in STATIC_FILES:
                name, content_type = STATIC_FILES[url.path]
                with open(os.path.join(UI_DIR, name), "rb") as f:
                    return self.send(200, f.read(), content_type, {"Cache-Control": "public, max-age=86400"})

            if guard.enabled:
                if url.path == "/login":
                    if method == "POST":
                        try:
                            return self.login()
                        except hs.ControlError as e:
                            return self.send(400, {"error": str(e)})
                    return self.send_page("login.html")
                if url.path == "/logout" and method == "POST":
                    return self.redirect("/login", self.session_cookie("", 0))
                if not self.logged_in():
                    if url.path.startswith("/api/"):
                        return self.send(401, {"error": "Please log in again"})
                    return self.redirect("/login")

            if method == "GET" and url.path in ("/", "/index.html"):
                return self.send_page("index.html")
            if method == "GET" and url.path == "/spotify/login":
                return self.redirect(app.spotify_login_url())
            if method == "GET" and url.path == "/callback":
                query = parse_qs(url.query)
                try:
                    app.finish_spotify_login(query.get("code", [None])[0], query.get("state", [None])[0])
                except Exception as e:
                    return self.send(400, f"Spotify login failed: {e}".encode(), "text/plain; charset=utf-8")
                return self.redirect("/")

            name = ROUTES.get((method, url.path))
            if not name:
                return self.send(404, {"error": "Not found"})
            try:
                # A JSON content type can't be sent cross-site without a CORS preflight,
                # which this server never approves.
                if method == "POST" and self.headers.get("Content-Type") != "application/json":
                    return self.send(415, {"error": "Expected JSON"})
                if method == "POST":
                    body = json.loads(self.read_body() or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("Expected a JSON object")
                else:
                    body = {k: v[0] for k, v in parse_qs(url.query).items()}
                self.send(200, getattr(app, name)(body))
            except (hs.ControlError, argparse.ArgumentTypeError, ValueError, KeyError) as e:
                self.send(400, {"error": str(e)})
            except Exception:
                traceback.print_exc()
                self.send(500, {"error": "Something went wrong. Check the server log."})

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

    return Handler


def main():
    hs.load_dotenv()
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=env("UI_HOST", "127.0.0.1"),
                        help="interface to listen on (0.0.0.0 = reachable from your Wi-Fi)")
    parser.add_argument("--port", type=int, default=int(env("UI_PORT", "8888")))
    args = parser.parse_args()

    password = env("UI_PASSWORD", "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise SystemExit("Set UI_PASSWORD in .env before listening beyond this computer.")
    if password and len(password) < 12:
        raise SystemExit("UI_PASSWORD must be at least 12 characters.")
    try:
        app = App(env("FAVORITES_FILE", "favorites.json"))
    except hs.ControlError as e:
        raise SystemExit(str(e))
    guard = Guard(password, load_secret(env("UI_SECRET_FILE", ".ui_secret")))
    app.login_enabled = guard.enabled
    secure_cookies = env("UI_SECURE_COOKIES", "") == "1"

    handler = make_handler(app, guard, secure_cookies, env("UI_PUBLIC_URL"))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Bedtime UI running on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if not guard.enabled:
        print("No UI_PASSWORD set: anyone who can reach this address can use it.")
    if not app.spotify_authed():
        print("Open the page and tap 'Connect Spotify' to finish setup.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
