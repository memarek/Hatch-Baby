import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import hatch_spotify as hs
import hatch_ui

PASSWORD = "correct horse battery"


class FakeAuth:
    def __init__(self):
        self.cache_handler = mock.Mock(get_cached_token=lambda: None)
        self.exchanged = []

    def validate_token(self, token): return token
    def get_authorize_url(self, state=None): return f"https://accounts.spotify.com/authorize?state={state}"
    def get_access_token(self, code, as_dict=True, check_cache=True): self.exchanged.append(code)


class HttpsHeaderTests(unittest.TestCase):
    def test_hsts_and_public_origin_when_served_over_https(self):
        with mock.patch.object(hs, "spotify_auth", FakeAuth):
            app = hatch_ui.App()
        app.pause = mock.Mock(return_value={"ok": True})
        guard = hatch_ui.Guard("", b"k" * 32)
        handler = hatch_ui.make_handler(app, guard, True, "https://bedtime.example.ts.net")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def request(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request(method, path, body, headers or {})
                res = conn.getresponse()
                res.read()
                conn.close()
                return res
            self.assertEqual(request("GET", "/").getheader("Strict-Transport-Security"), "max-age=31536000")
            ok = request("POST", "/api/stop", "{}", {"Content-Type": "application/json",
                                                       "Origin": "https://bedtime.example.ts.net"})
            self.assertEqual(ok.status, 200)
        finally:
            server.shutdown()
            server.server_close()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = hatch_ui.Guard(PASSWORD, b"k" * 32)

    def test_session_round_trip(self):
        self.assertTrue(self.guard.valid_session(self.guard.make_session()))

    def test_rejects_tampered_or_expired(self):
        expires, _, sig = self.guard.make_session().partition(".")
        self.assertFalse(self.guard.valid_session(f"{int(expires) + 1}.{sig}"))
        old = str(int(time.time()) - 10)
        self.assertFalse(self.guard.valid_session(f"{old}.{self.guard._sign(old)}"))
        self.assertFalse(self.guard.valid_session(None))

    def test_password_change_signs_out(self):
        session = self.guard.make_session()
        self.assertFalse(hatch_ui.Guard("another long password", b"k" * 32).valid_session(session))

    def test_lockout(self):
        for _ in range(hatch_ui.MAX_FAILED_LOGINS):
            self.assertFalse(self.guard.check_password("nope"))
        self.assertIsNone(self.guard.check_password(PASSWORD))

    def test_lockout_is_per_client(self):
        for _ in range(hatch_ui.MAX_FAILED_LOGINS):
            self.guard.check_password("nope", "1.2.3.4")
        self.assertIsNone(self.guard.check_password(PASSWORD, "1.2.3.4"))
        self.assertTrue(self.guard.check_password(PASSWORD, "5.6.7.8"))

    def test_total_cap_across_clients(self):
        for i in range(hatch_ui.MAX_FAILED_LOGINS_TOTAL):
            self.guard.check_password("nope", f"10.0.{i}.1")
        self.assertIsNone(self.guard.check_password(PASSWORD, "5.6.7.8"))

    def test_lockout_expires(self):
        for _ in range(hatch_ui.MAX_FAILED_LOGINS):
            self.guard.check_password("nope", "1.2.3.4")
        later = time.time() + hatch_ui.LOCKOUT_SECONDS + 1
        with mock.patch.object(hatch_ui.time, "time", return_value=later):
            self.assertTrue(self.guard.check_password(PASSWORD, "1.2.3.4"))

    def test_disabled_allows_everything(self):
        self.assertTrue(hatch_ui.Guard("", b"k").valid_session(None))

    def test_secret_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "secret")
            first = hatch_ui.load_secret(path)
            self.assertEqual(first, hatch_ui.load_secret(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(hs, "spotify_auth", FakeAuth):
            cls.app = hatch_ui.App()
        cls.app.pause = mock.Mock(return_value={"ok": True})
        guard = hatch_ui.Guard(PASSWORD, b"k" * 32)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), hatch_ui.make_handler(cls.app, guard))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.session = guard.make_session()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def request(self, method, path, body=None, headers=None, logged_in=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = dict(headers or {})
        if logged_in:
            headers["Cookie"] = f"{hatch_ui.SESSION_COOKIE}={self.session}"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        res.body = res.read()
        conn.close()
        return res

    def test_requires_login(self):
        self.assertEqual(self.request("GET", "/", logged_in=False).getheader("Location"), "/login")
        self.assertEqual(self.request("GET", "/api/state", logged_in=False).status, 401)
        self.assertEqual(self.request("GET", "/login", logged_in=False).status, 200)

    def test_login(self):
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        res = self.request("POST", "/login", "password=correct+horse+battery", form, logged_in=False)
        self.assertEqual(res.getheader("Location"), "/")
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_lockout_uses_proxy_added_address(self):
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        guard = hatch_ui.Guard(PASSWORD, b"k" * 32)
        handler = hatch_ui.make_handler(self.app, guard)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def attempt(xff, password="wrong"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("POST", "/login", f"password={password}", {**form, "X-Forwarded-For": xff})
                location = conn.getresponse().getheader("Location")
                conn.close()
                return location
            with mock.patch.object(hatch_ui.time, "sleep"):
                # An attacker faking the first entry still gets counted by the last one.
                for i in range(hatch_ui.MAX_FAILED_LOGINS):
                    attempt(f"9.9.9.{i}, 203.0.113.7")
                self.assertEqual(attempt("203.0.113.7"), "/login?error=locked")
                self.assertEqual(attempt("198.51.100.2", "correct+horse+battery"), "/")
        finally:
            server.shutdown()
            server.server_close()

    def test_security_headers(self):
        res = self.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")

    def test_blocks_cross_site_post(self):
        res = self.request("POST", "/api/stop", "{}", {"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(res.status, 403)
        res = self.request("POST", "/api/stop", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(res.status, 415)
        res = self.request("POST", "/api/stop", "{}", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(res.status, 200)

    def test_rejects_large_body(self):
        res = self.request("POST", "/api/stop", "x" * (hatch_ui.MAX_BODY_BYTES + 1), {"Content-Type": "application/json"})
        self.assertEqual(res.status, 400)

    def test_spotify_login_checks_state(self):
        location = self.request("GET", "/spotify/login").getheader("Location")
        state = location.split("state=")[1]
        self.assertEqual(self.request("GET", "/callback?code=abc&state=forged").status, 400)
        res = self.request("GET", f"/callback?code=abc&state={state}")
        self.assertEqual(res.getheader("Location"), "/")
        self.assertEqual(self.app.auth.exchanged, ["abc"])
        # each state works only once
        self.assertEqual(self.request("GET", f"/callback?code=abc&state={state}").status, 400)

    def test_rejects_bad_body_sizes(self):
        for size in ("-1", "abc"):
            for path, ctype in (("/api/stop", "application/json"), ("/login", "application/x-www-form-urlencoded")):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", ctype)
                conn.putheader("Content-Length", size)
                conn.putheader("Cookie", f"{hatch_ui.SESSION_COOKIE}={self.session}")
                conn.endheaders()
                res = conn.getresponse()
                res.read()
                conn.close()
                self.assertEqual(res.status, 400, (size, path))

    def test_forwarded_host_not_trusted_for_origin(self):
        res = self.request("POST", "/api/stop", "{}", {
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "X-Forwarded-Host": "evil.example",
        })
        self.assertEqual(res.status, 403)

    def test_no_hsts_without_https(self):
        self.assertIsNone(self.request("GET", "/").getheader("Strict-Transport-Security"))

    def test_errors_hide_internals(self):
        with mock.patch.object(self.app, "volume", side_effect=RuntimeError("secret detail")):
            res = self.request("POST", "/api/volume", json.dumps({"volume": 5}), {"Content-Type": "application/json"})
        self.assertEqual(res.status, 500)
        self.assertNotIn(b"secret detail", res.body)


if __name__ == "__main__":
    unittest.main()


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with mock.patch.object(hs, "spotify_auth", FakeAuth):
            self.app = hatch_ui.App(os.path.join(self.tmp.name, "favorites.json"),
                                    os.path.join(self.tmp.name, "settings.json"))
        self.sp = mock.MagicMock()
        self.sp.playlist.return_value = {
            "name": "Lullabies", "owner": {"display_name": "Spotify"},
            "images": [{"url": "https://i.scdn.co/big"}, {"url": "https://i.scdn.co/mid"}],
        }
        self.app.sp = lambda: self.sp
        guard = hatch_ui.Guard(PASSWORD, b"k" * 32)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), hatch_ui.make_handler(self.app, guard))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.session = guard.make_session()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, logged_in=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if logged_in:
            headers["Cookie"] = f"{hatch_ui.SESSION_COOKIE}={self.session}"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        res.body = res.read()
        conn.close()
        return res

    def test_app_icon_and_manifest_are_public(self):
        for path, ctype in (("/manifest.webmanifest", "application/manifest+json"),
                            ("/icon-180.png", "image/png"), ("/icon.svg", "image/svg+xml")):
            res = self.request("GET", path, logged_in=False)
            self.assertEqual(res.status, 200, path)
            self.assertEqual(res.getheader("Content-Type"), ctype)
        # nothing else is
        self.assertEqual(self.request("GET", "/api/state", logged_in=False).status, 401)
        self.assertEqual(self.request("GET", "/ui/../.env", logged_in=False).status, 303)

    def test_csp_allows_manifest_and_spotify_images(self):
        csp = self.request("GET", "/").getheader("Content-Security-Policy")
        self.assertIn("manifest-src 'self'", csp)
        self.assertIn("https://*.spotifycdn.com", csp)

    def test_favorites_are_shared_and_described(self):
        res = self.request("POST", "/api/favorites", json.dumps({"uri": "https://open.spotify.com/playlist/abc?si=1"}))
        self.assertEqual(res.status, 200)
        self.sp.playlist.assert_called_with("abc", fields="name,images,owner(display_name)")
        self.app.spotify_authed = lambda: False
        favorites = json.loads(self.request("GET", "/api/state").body)["favorites"]
        self.assertEqual(favorites, [{"uri": "spotify:playlist:abc", "name": "Lullabies",
                                      "subtitle": "Playlist · Spotify", "image": "https://i.scdn.co/mid"}])
        # re-adding moves it to the front without duplicating
        self.sp.album.return_value = {"name": "Album", "artists": [{"name": "A"}], "images": []}
        self.request("POST", "/api/favorites", json.dumps({"uri": "spotify:album:xyz"}))
        self.request("POST", "/api/favorites", json.dumps({"uri": "spotify:playlist:abc"}))
        self.assertEqual([f["uri"] for f in self.app.load_favorites()], ["spotify:playlist:abc", "spotify:album:xyz"])
        self.request("POST", "/api/favorites/remove", json.dumps({"uri": "spotify:album:xyz"}))
        self.assertEqual([f["uri"] for f in self.app.load_favorites()], ["spotify:playlist:abc"])

    def test_bad_spotify_link_is_a_clear_error(self):
        self.sp.playlist.side_effect = RuntimeError("404")
        res = self.request("POST", "/api/favorites", json.dumps({"uri": "spotify:playlist:missing"}))
        self.assertEqual(res.status, 400)
        self.assertIn(b"Couldn't find that on Spotify", res.body)

    def test_json_body_must_be_an_object(self):
        self.assertEqual(self.request("POST", "/api/favorites", "[1, 2]").status, 400)

    def test_play_returns_before_hatch_finishes(self):
        release = threading.Event()
        self.sp.devices.return_value = {"devices": [{"id": "e1", "name": "Echo"}]}
        with mock.patch.object(hatch_ui, "run_hatch", lambda action: release.wait(5)):
            res = self.request("POST", "/api/play", json.dumps({"uri": "spotify:playlist:abc", "speaker": "e1"}))
            self.assertEqual(res.status, 200)
            self.assertTrue(self.app.hatch_job["busy"])
            release.set()
            for _ in range(50):
                if not self.app.hatch_job["busy"]:
                    break
                time.sleep(0.02)
        self.assertEqual((self.app.hatch_job["ok"], self.app.hatch_job["message"]), (True, "Nightlight on"))

    def test_hatch_failure_is_reported_without_internals(self):
        def fail(action):
            raise RuntimeError("socket details")
        with mock.patch.object(hatch_ui, "run_hatch", fail), mock.patch("traceback.print_exc"):
            self.request("POST", "/api/hatch-off", "{}")
            for _ in range(50):
                if not self.app.hatch_job["busy"]:
                    break
                time.sleep(0.02)
        self.assertFalse(self.app.hatch_job["ok"])
        self.assertNotIn("socket", self.app.hatch_job["message"])

    def wait_for_job(self):
        for _ in range(100):
            if not self.app.hatch_job["busy"]:
                return
            time.sleep(0.02)

    def test_hatch_volume_and_sound_controls(self):
        device = mock.MagicMock()
        device.device_name = "Nursery"
        device.is_light_on = False
        device.sounds = [{"id": 1, "title": "Dryer", "wavUrl": "u"}]
        type(device).__name__ = "RestIot"
        with mock.patch.object(hatch_ui, "run_hatch", lambda action: action([device])):
            self.assertEqual(self.request("POST", "/api/hatch-volume", json.dumps({"volume": 35})).status, 200)
            self.wait_for_job()
            device.set_volume.assert_called_with(35)
            self.assertEqual(self.app.hatch_job["message"], "Hatch volume 35%")

            self.request("POST", "/api/hatch-sound", json.dumps({"sound": 1}))
            self.wait_for_job()
            device.set_sound.assert_called_with(1)
            self.assertEqual(self.app.hatch_job["message"], "Playing Dryer on the Hatch")

            self.request("POST", "/api/hatch-sound", json.dumps({"sound": None}))
            self.wait_for_job()
            device.turn_off.assert_called_once()
            self.assertEqual(self.app.hatch_job["message"], "Hatch sound stopped")

            self.request("POST", "/api/hatch-sound", json.dumps({"sound": 99}))
            self.wait_for_job()
            self.assertFalse(self.app.hatch_job["ok"])
            self.assertIn("isn't available", self.app.hatch_job["message"])

        self.assertEqual(self.request("POST", "/api/hatch-volume", json.dumps({"volume": 500})).status, 400)

    def test_settings_are_validated_and_shared(self):
        self.app.spotify_authed = lambda: False
        state = json.loads(self.request("GET", "/api/state").body)
        self.assertEqual(state["settings"], {"sleep_sound": None, "bedtime_volume": None})
        ok = self.request("POST", "/api/settings", json.dumps(
            {"sleep_sound": {"id": 10127, "title": "Dryer"}, "bedtime_volume": 25, "evil": "x"}))
        self.assertEqual(ok.status, 200)
        self.assertEqual(self.app.load_settings(), {"sleep_sound": {"id": 10127, "title": "Dryer"}, "bedtime_volume": 25})
        with open(self.app.settings_path) as f:
            self.assertNotIn("evil", f.read())
        for bad in ({"bedtime_volume": 150}, {"sleep_sound": "Dryer"}, {"sleep_sound": {"id": [1], "title": "x"}}):
            self.assertEqual(self.request("POST", "/api/settings", json.dumps(bad)).status, 400, bad)
        self.request("POST", "/api/settings", json.dumps({"bedtime_volume": None}))
        self.assertIsNone(self.app.load_settings()["bedtime_volume"])
        self.assertEqual(self.app.load_settings()["sleep_sound"]["title"], "Dryer")

    def test_bedtime_volume_is_used_when_starting(self):
        self.sp.devices.return_value = {"devices": [{"id": "e1", "name": "Echo"}]}
        self.request("POST", "/api/settings", json.dumps({"bedtime_volume": 25}))
        self.request("POST", "/api/play", json.dumps({"uri": "spotify:playlist:abc", "speaker": "e1", "use_hatch": False}))
        self.sp.volume.assert_called_with(25, device_id="e1")
        self.sp.volume.reset_mock()
        self.request("POST", "/api/settings", json.dumps({"bedtime_volume": None}))
        self.request("POST", "/api/play", json.dumps({"uri": "spotify:playlist:abc", "speaker": "e1", "use_hatch": False}))
        self.sp.volume.assert_not_called()

    def test_switch_to_hatch(self):
        res = self.request("POST", "/api/switch-to-hatch", "{}")
        self.assertEqual(res.status, 400)
        self.assertIn(b"Choose a sleep sound", res.body)
        self.request("POST", "/api/settings", json.dumps({"sleep_sound": {"id": 1, "title": "Dryer"}}))
        device = mock.MagicMock()
        device.device_name = "Nursery"
        device.sounds = [{"id": 1, "title": "Dryer", "wavUrl": "u"}]
        type(device).__name__ = "RestIot"
        with mock.patch.object(hatch_ui, "run_hatch", lambda action: action([device])):
            self.assertEqual(self.request("POST", "/api/switch-to-hatch", "{}").status, 200)
            self.wait_for_job()
        self.sp.pause_playback.assert_called_once()
        device.set_sound.assert_called_with(1)
        self.assertEqual(self.app.hatch_job["message"], "Switched to Dryer")

    def test_light_switch_endpoint(self):
        device = mock.MagicMock()
        device.device_name = "Nursery"
        device.is_playing = True
        type(device).__name__ = "RestIot"
        with mock.patch.object(hatch_ui, "run_hatch", lambda action: action([device])):
            self.request("POST", "/api/hatch-light", json.dumps({"on": False}))
            self.wait_for_job()
        device.set_color.assert_called_with(0, 0, 0, brightness=0)
        device.turn_off.assert_not_called()
        self.assertEqual(self.app.hatch_job["message"], "Light off")

    def test_hatch_status_reads_live_state(self):
        device = mock.MagicMock()
        device.device_name = "Nursery"
        snapshot = {"name": "Nursery", "playing": True, "sound": "Dryer"}
        with mock.patch.object(hatch_ui, "run_hatch", lambda action: action([device])), \
             mock.patch.object(hs, "hatch_snapshot", return_value=snapshot):
            first = json.loads(self.request("GET", "/api/hatch-status").body)
            self.assertEqual(first["sound"], "Dryer")
            self.assertIn("checked_at", first)
            device.refresh.assert_not_called()
            with mock.patch.object(hatch_ui.time, "sleep"):
                self.request("GET", "/api/hatch-status?refresh=1")
            device.refresh.assert_called_once()

    def test_spotify_made_playlist_uses_link_preview(self):
        self.sp.playlist.side_effect = RuntimeError("404")
        preview = {"name": "Peaceful Piano", "image": "https://image-cdn-fa.spotifycdn.com/x"}
        with mock.patch.object(hatch_ui.App, "link_preview", return_value=preview) as lp:
            res = self.request("POST", "/api/favorites", json.dumps({"uri": "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI"}))
        self.assertEqual(res.status, 200)
        lp.assert_called_with("playlist", "37i9dQZF1DWV7EzJMK2FUI")
        self.assertEqual(self.app.load_favorites()[0], {
            "uri": "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI", "name": "Peaceful Piano",
            "subtitle": "Playlist · Spotify", "image": "https://image-cdn-fa.spotifycdn.com/x"})


class LinkPreviewTests(unittest.TestCase):
    def fake_get(self, payload, fail=False):
        res = mock.Mock()
        res.raise_for_status = mock.Mock(side_effect=RuntimeError("404") if fail else None)
        res.json.return_value = payload
        return mock.patch("requests.get", return_value=res)

    def test_parses_title_and_cover(self):
        with self.fake_get({"title": "Peaceful Piano", "thumbnail_url": "https://i.scdn.co/x"}) as get:
            self.assertEqual(hatch_ui.App.link_preview("playlist", "abc"),
                             {"name": "Peaceful Piano", "image": "https://i.scdn.co/x"})
        self.assertEqual(get.call_args.kwargs["params"], {"url": "https://open.spotify.com/playlist/abc"})

    def test_rejects_odd_responses(self):
        with self.fake_get({}, fail=True):
            self.assertIsNone(hatch_ui.App.link_preview("playlist", "abc"))
        with self.fake_get({"title": ""}):
            self.assertIsNone(hatch_ui.App.link_preview("playlist", "abc"))
        with self.fake_get({"title": "X", "thumbnail_url": "javascript:alert(1)"}):
            self.assertIsNone(hatch_ui.App.link_preview("playlist", "abc")["image"])


class HatchConnectionTests(unittest.TestCase):
    def setUp(self):
        self.logins = 0
        self.devices = []
        self.fail_with = None
        self.expiration = time.time() + 3600

        async def fake_get_rest_devices(email, password, client_session=None):
            if self.fail_with:
                raise self.fail_with
            self.logins += 1
            device = mock.MagicMock()
            device.device_name = f"Nursery {self.logins}"
            device.is_online = True
            self.devices.append(device)
            mqtt = mock.MagicMock()
            return None, mqtt, [device], self.expiration

        patcher = mock.patch("hatch_rest_api.get_rest_devices", fake_get_rest_devices)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = hatch_ui.HatchConnection("me@example.com", "pw")

    def test_logs_in_once_for_many_actions(self):
        for _ in range(5):
            names = self.conn.run(lambda ds: [d.device_name for d in ds])
        self.assertEqual(names, ["Nursery 1"])
        self.assertEqual(self.logins, 1)

    def test_reconnects_before_credentials_expire(self):
        self.expiration = time.time() + 60  # inside the refresh margin
        self.conn.run(lambda ds: None)
        self.conn.run(lambda ds: None)
        self.assertEqual(self.logins, 2)
        self.devices[0].unsubscribe.assert_called_once()

    def test_backs_off_when_rate_limited(self):
        from hatch_rest_api import RateError
        self.fail_with = RateError("429")
        with self.assertRaises(hs.ControlError) as first:
            self.conn.run(lambda ds: None)
        self.assertIn("slow down", str(first.exception))
        self.fail_with = None
        with self.assertRaises(hs.ControlError):  # still waiting: no new login attempt
            self.conn.run(lambda ds: None)
        self.assertEqual(self.logins, 0)
        self.assertEqual(self.conn.backoff, 2 * hatch_ui.HatchConnection.MIN_BACKOFF)
        self.conn.retry_at = 0  # wait is over
        self.conn.run(lambda ds: None)
        self.assertEqual(self.logins, 1)
        self.assertEqual(self.conn.backoff, hatch_ui.HatchConnection.MIN_BACKOFF)

    def test_unexpected_error_logs_in_again_next_time(self):
        with self.assertRaises(RuntimeError):
            self.conn.run(lambda ds: (_ for _ in ()).throw(RuntimeError("socket closed")))
        self.conn.run(lambda ds: None)
        self.assertEqual(self.logins, 2)

    def test_control_errors_keep_the_connection(self):
        with self.assertRaises(hs.ControlError):
            self.conn.run(lambda ds: hs.find_hatch_device(ds, "garage"))
        self.conn.run(lambda ds: None)
        self.assertEqual(self.logins, 1)

    def test_parse_expiration(self):
        self.assertEqual(hatch_ui.parse_expiration(1700000000), 1700000000.0)
        self.assertEqual(hatch_ui.parse_expiration("2023-11-14T22:13:20Z"), 1700000000.0)
        self.assertEqual(hatch_ui.parse_expiration(None, now=100), 100 + 50 * 60)
