import http.client
import os
import tempfile
import threading
import unittest
import wave
from http.server import ThreadingHTTPServer
from unittest import mock

import hatch_spotify as hs
import hatch_ui
import media


class MediaHelperTests(unittest.TestCase):
    def test_token_is_stable_and_secret_specific(self):
        self.assertEqual(media.media_token(b"a" * 32), media.media_token(b"a" * 32))
        self.assertNotEqual(media.media_token(b"a" * 32), media.media_token(b"b" * 32))
        self.assertEqual(len(media.media_token(b"a" * 32)), 32)

    def test_parse_range(self):
        self.assertIsNone(media.parse_range(None, 100))
        self.assertEqual(media.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(media.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(media.parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(media.parse_range("bytes=50-500", 100), (50, 99))
        self.assertEqual(media.parse_range("bytes=200-", 100), "unsatisfiable")
        self.assertIsNone(media.parse_range("bytes=0-1,5-6", 100))  # multiple ranges: send it all

    def test_names_are_restricted(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "ok.wav"), "wb").close()
            tok = "t" * 32
            self.assertIsNotNone(media.resolve(tok, tok, "ok.wav", d))
            for bad in ("../ok.wav", "ok.txt", ".hidden.wav", "OK.WAV", "x/ok.wav"):
                self.assertIsNone(media.resolve(tok, tok, bad, d), bad)
            self.assertIsNone(media.resolve("wrong" + "t" * 27, tok, "ok.wav", d))

    def test_make_test_sounds(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("shutil.which", return_value=None):
            names = media.make_test_sounds(d)
            self.assertEqual(names, ["test-44k-mono.wav", "test-44k-stereo.wav", "test-22k-mono.wav"])
            with wave.open(os.path.join(d, "test-44k-stereo.wav")) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (2, 44100, 2))
            with open(os.path.join(d, "test-22k-mono.wav"), "rb") as f:
                self.assertEqual(hs.parse_wav_header(f.read(64)),
                                 {"type": "wav", "format": "PCM", "channels": 1, "sample_rate": 22050, "bits": 16})


class MediaServingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = bytes(range(256)) * 40
        with open(os.path.join(self.tmp.name, "song.wav"), "wb") as f:
            f.write(self.data)
        self.token = media.media_token(b"k" * 32)
        with mock.patch.object(hs, "spotify_auth", mock.Mock()):
            app = hatch_ui.App(os.path.join(self.tmp.name, "f.json"), os.path.join(self.tmp.name, "s.json"))
        guard = hatch_ui.Guard("correct horse battery", b"k" * 32)  # login is on
        handler = hatch_ui.make_handler(app, guard, media_token=self.token, media_dir=self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.print = mock.patch("builtins.print").start()

    def tearDown(self):
        mock.patch.stopall()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request(method, path, headers=headers or {})
        res = conn.getresponse()
        res.body = res.read()
        conn.close()
        return res

    def test_whole_file_without_login(self):
        res = self.get(f"/media/{self.token}/song.wav")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.body, self.data)
        self.assertEqual(res.getheader("Content-Type"), "audio/wav")
        self.assertEqual(res.getheader("Accept-Ranges"), "bytes")

    def test_byte_range(self):
        res = self.get(f"/media/{self.token}/song.wav", headers={"Range": "bytes=100-199"})
        self.assertEqual(res.status, 206)
        self.assertEqual(res.body, self.data[100:200])
        self.assertEqual(res.getheader("Content-Range"), f"bytes 100-199/{len(self.data)}")

    def test_head(self):
        res = self.get(f"/media/{self.token}/song.wav", method="HEAD")
        self.assertEqual((res.status, res.body, res.getheader("Content-Length")), (200, b"", str(len(self.data))))

    def test_bad_requests(self):
        self.assertEqual(self.get(f"/media/{'0' * 32}/song.wav").status, 404)
        self.assertEqual(self.get(f"/media/{self.token}/missing.wav").status, 404)
        self.assertEqual(self.get(f"/media/{self.token}/..%2Fs.json").status, 404)
        self.assertEqual(self.get(f"/media/{self.token}/song.wav", headers={"Range": "bytes=999999-"}).status, 416)

    def test_rest_of_site_still_needs_login(self):
        self.assertEqual(self.get("/").status, 303)
        self.assertEqual(self.get("/", method="HEAD").status, 405)

    def test_requests_are_logged(self):
        self.get(f"/media/{self.token}/song.wav", headers={"User-Agent": "HatchFirmware/11", "Range": "bytes=0-9"})
        logged = " ".join(str(c.args[0]) for c in self.print.call_args_list)
        self.assertIn("media GET song.wav range=bytes=0-9 -> 206", logged)
        self.assertIn("HatchFirmware/11", logged)


if __name__ == "__main__":
    unittest.main()
