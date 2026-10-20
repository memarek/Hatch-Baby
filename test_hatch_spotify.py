import argparse
import unittest
from unittest import mock

import hatch_spotify as hs


class FakeRestIot:
    def __init__(self, name="Nursery Rest"):
        self.device_name = name
        self.calls = []

    def set_volume(self, v): self.calls.append(("volume", v))
    def set_color(self, r, g, b, white=0, brightness=0): self.calls.append(("color", r, g, b, brightness))
    def set_sound_url(self, url): self.calls.append(("url", url))
    def turn_off(self): self.calls.append(("off",))
    def turn_light_off(self): self.calls.append(("light_off",))


class RestPlus:
    def __init__(self):
        self.device_name = "Rest Plus"
        self.calls = []

    def set_on(self, on): self.calls.append(("on", on))
    def set_volume(self, v): self.calls.append(("volume", v))
    def set_color(self, r, g, b, brightness, random=False): self.calls.append(("color", r, g, b, brightness))


class ParsingTests(unittest.TestCase):
    def test_uri_passthrough(self):
        self.assertEqual(hs.to_spotify_uri("spotify:playlist:abc"), "spotify:playlist:abc")

    def test_link_to_uri(self):
        self.assertEqual(
            hs.to_spotify_uri("https://open.spotify.com/intl-de/playlist/37i9dQZF1?si=xyz"),
            "spotify:playlist:37i9dQZF1",
        )

    def test_bad_link(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            hs.to_spotify_uri("https://example.com")

    def test_color(self):
        self.assertEqual(hs.parse_hex_color("#ff6a00"), (255, 106, 0))
        with self.assertRaises(argparse.ArgumentTypeError):
            hs.parse_hex_color("orange")


class SpotifyDeviceTests(unittest.TestCase):
    devices = [
        {"id": "1", "name": "Kitchen Sonos", "is_active": True},
        {"id": "2", "name": "Nursery Echo", "is_active": False},
    ]

    def test_exact_and_partial_match(self):
        self.assertEqual(hs.find_spotify_device(self.devices, "nursery echo")["id"], "2")
        self.assertEqual(hs.find_spotify_device(self.devices, "nursery")["id"], "2")

    def test_defaults_to_active(self):
        self.assertEqual(hs.find_spotify_device(self.devices, None)["id"], "1")

    def test_missing(self):
        self.assertIsNone(hs.find_spotify_device(self.devices, "garage"))

    def test_start_playlist_vs_track(self):
        sp = mock.Mock()
        sp.devices.return_value = {"devices": self.devices}
        hs.start_spotify(sp, "spotify:playlist:p", "nursery", 30, True)
        sp.start_playback.assert_called_with(device_id="2", context_uri="spotify:playlist:p")
        sp.volume.assert_called_with(30, device_id="2")
        sp.shuffle.assert_called_with(True, device_id="2")
        hs.start_spotify(sp, "spotify:track:t", "nursery", None, None)
        sp.start_playback.assert_called_with(device_id="2", uris=["spotify:track:t"])


class HatchTests(unittest.TestCase):
    def test_nightlight_rest_iot(self):
        d = FakeRestIot()
        hs.hatch_nightlight(d, (255, 106, 0), 20)
        self.assertEqual(d.calls, [("volume", 0), ("color", 255, 106, 0, 20)])

    def test_nightlight_rest_plus_powers_on(self):
        d = RestPlus()
        hs.hatch_nightlight(d, (1, 2, 3), 10)
        self.assertEqual(d.calls, [("on", True), ("volume", 0), ("color", 1, 2, 3, 10)])

    def test_off(self):
        d = FakeRestIot()
        hs.hatch_off(d)
        self.assertEqual(d.calls, [("off",), ("light_off",)])
        p = RestPlus()
        hs.hatch_off(p)
        self.assertEqual(p.calls, [("on", False)])

    def test_find_hatch_device(self):
        ds = [FakeRestIot("Living Room"), FakeRestIot("Nursery Rest")]
        self.assertIs(hs.find_hatch_device(ds, "nursery"), ds[1])
        self.assertIs(hs.find_hatch_device(ds, None), ds[0])
        with self.assertRaises(SystemExit):
            hs.find_hatch_device(ds, "garage")


class CliTests(unittest.TestCase):
    def test_play_args(self):
        args = hs.build_parser().parse_args(
            ["play", "https://open.spotify.com/album/abc", "--speaker", "Echo", "--minutes", "30", "--no-shuffle"]
        )
        self.assertEqual(args.uri, "spotify:album:abc")
        self.assertEqual(args.minutes, 30)
        self.assertFalse(args.shuffle)


if __name__ == "__main__":
    unittest.main()
