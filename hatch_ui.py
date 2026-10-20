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
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' https://i.scdn.co data:; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # no-referrer makes browsers send "Origin: null" on forms
    "Cache-Control": "no-store",
}

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
        self.failures = []
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

    def check_password(self, attempt):
        """Returns True/False, or None while locked out."""
        with self.lock:
            now = time.time()
            self.failures = [t for t in self.failures if now - t < LOCKOUT_SECONDS]
            if len(self.failures) >= MAX_FAILED_LOGINS:
                return None
            ok = hmac.compare_digest(attempt.encode(), self.password.encode())
            if not ok:
                self.failures.append(now)
            return ok

    def _sign(self, value):
        # Includes the password so changing it signs everyone out.
        key = self.secret + self.password.encode()
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


class App:
    def __init__(self):
        self.auth = hs.spotify_auth()
        self.oauth_states = {}  # state -> created time, for the Spotify login round trip
        self.login_enabled = False

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
                  "login_enabled": self.login_enabled, "speakers": [], "playback": None}
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
        device = hs.start_spotify(self.sp(), uri, body.get("speaker"), body.get("volume"), body.get("shuffle"))
        if body.get("use_hatch", True):
            rgb = hs.parse_hex_color(body.get("color") or defaults()["color"])
            brightness = int(body.get("brightness", defaults()["brightness"]))
            run_hatch(lambda ds: hs.hatch_nightlight(hs.find_hatch_device(ds, body.get("hatch")), rgb, brightness))
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
}


def make_handler(app, guard, secure_cookies=False, public_url=None):
    public_host = urlparse(public_url).netloc if public_url else None

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
            for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def send_page(self, name, status=200, headers=None):
            with open(os.path.join(UI_DIR, name), "rb") as f:
                self.send(status, f.read(), "text/html; charset=utf-8", headers)

        def redirect(self, location, headers=None):
            self.send_response(303)
            self.send_header("Location", location)
            for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
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
            allowed = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host"), public_host}
            return urlparse(origin).netloc in allowed - {None}

        def read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise hs.ControlError("Request too large")
            return self.rfile.read(length)

        def login(self):
            form = parse_qs(self.read_body().decode())
            ok = guard.check_password(form.get("password", [""])[0])
            if ok:
                return self.redirect("/", self.session_cookie(guard.make_session(), SESSION_DAYS * 86400))
            time.sleep(1)  # slow down guessing
            error = "locked" if ok is None else "wrong"
            return self.redirect(f"/login?error={error}")

        def handle_request(self, method):
            url = urlparse(self.path)
            if method == "POST" and not self.same_origin():
                return self.send(403, {"error": "Cross-site request blocked"})

            if guard.enabled:
                if url.path == "/login":
                    if method == "POST":
                        return self.login()
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
                body = json.loads(self.read_body() or b"{}") if method == "POST" else {}
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
        app = App()
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
