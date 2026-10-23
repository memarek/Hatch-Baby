import argparse
import unittest
from unittest import mock

import hatch_spotify as hs


class FakeRestIot:
    def __init__(self, name="Nursery Rest", playing=False):
        self.device_name = name
        self.is_playing = playing
        self.calls = []

    def _update(self, state): self.calls.append(("update", state))
    def set_volume(self, v): self.calls.append(("volume", v))
    def set_color(self, r, g, b, white=0, brightness=0): self.calls.append(("color", r, g, b, brightness))
    def set_sound_url(self, url): self.calls.append(("url", url))
    def turn_off(self): self.calls.append(("off",))
    def turn_light_off(self): self.calls.append(("light_off",))


class RestPlus:
    def __init__(self):
        self.device_name = "Rest Plus"
        self.calls = []

    def set_audio_track(self, track): self.calls.append(("track", track.name))
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


class DotenvTests(unittest.TestCase):
    def test_quotes_only_stripped_in_pairs(self):
        import os, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nT_QUOTED=\"a b\"\nT_TRAILING=pa$$word'\nT_EQ=x=y\n")
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                hs.load_dotenv(f.name)
                self.assertEqual(os.environ["T_QUOTED"], "a b")
                self.assertEqual(os.environ["T_TRAILING"], "pa$$word'")
                self.assertEqual(os.environ["T_EQ"], "x=y")
        finally:
            os.unlink(f.name)


class SpotifyDeviceTests(unittest.TestCase):
    devices = [
        {"id": "1", "name": "Kitchen Sonos", "is_active": True},
        {"id": "2", "name": "Nursery Echo", "is_active": False},
    ]

    def test_exact_and_partial_match(self):
        self.assertEqual(hs.find_spotify_device(self.devices, "nursery echo")["id"], "2")
        self.assertEqual(hs.find_spotify_device(self.devices, "nursery")["id"], "2")

    def test_match_by_id(self):
        self.assertEqual(hs.find_spotify_device(self.devices, "2")["name"], "Nursery Echo")

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
    def test_nightlight_leaves_volume_alone(self):
        d = FakeRestIot()
        hs.hatch_nightlight(d, (255, 106, 0), 20)
        self.assertEqual(d.calls, [("color", 255, 106, 0, 20)])

    def test_nightlight_stops_playing_sound(self):
        d = FakeRestIot(playing=True)
        hs.hatch_nightlight(d, (255, 106, 0), 20)
        self.assertEqual(d.calls, [
            ("update", {"current": {"sound": {"id": hs.NO_SOUND_ID, "mute": True}}}),
            ("color", 255, 106, 0, 20),
        ])

    def test_nightlight_rest_plus_powers_on_without_sound(self):
        d = RestPlus()
        hs.hatch_nightlight(d, (1, 2, 3), 10)
        self.assertEqual(d.calls, [("on", True), ("track", "NONE"), ("color", 1, 2, 3, 10)])

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
        with self.assertRaises(hs.ControlError):
            hs.find_hatch_device(ds, "garage")


class SnapshotTests(unittest.TestCase):
    def rest_iot(self, **state):
        d = FakeRestIot()
        d.is_online, d.current_playing, d.current_id = True, "remote", 0
        d.sound_id, d.audio_track, d.volume = hs.NO_SOUND_ID, None, 0
        d.is_light_on, d.red, d.green, d.blue, d.brightness = False, 0, 0, 0, 0
        d.sounds_by_id = {10127: {"id": 10127, "title": "Dryer"}}
        d.favorites = [{"id": 7, "name": "Bedtime", "active": True}]
        for k, v in state.items():
            setattr(d, k, v)
        return d

    def test_newer_model_names_the_sound(self):
        d = self.rest_iot(is_playing=True, sound_id=10127, volume=40)
        snap = hs.hatch_snapshot(d)
        self.assertEqual((snap["playing"], snap["sound"], snap["volume"]), (True, "Dryer", 40))
        self.assertFalse(snap["light_on"])
        self.assertIsNone(snap["color"])

    def test_light_color_and_brightness(self):
        d = self.rest_iot(is_light_on=True, red=255, green=106, blue=0, brightness=20)
        snap = hs.hatch_snapshot(d)
        self.assertEqual((snap["light_on"], snap["color"], snap["brightness"]), (True, "#ff6a00", 20))
        self.assertFalse(snap["playing"])
        self.assertIsNone(snap["sound"])

    def test_running_favorite(self):
        d = self.rest_iot(current_playing="routine", current_id=7, is_playing=True, sound_id=10127)
        self.assertEqual(hs.hatch_snapshot(d)["routine"], "Bedtime")

    def test_unknown_sound_falls_back_to_track_name(self):
        from hatch_rest_api import RIoTAudioTrack
        d = self.rest_iot(is_playing=True, sound_id=1, audio_track=RIoTAudioTrack.BrownNoise)
        self.assertEqual(hs.hatch_snapshot(d)["sound"], "Brown Noise")

    def test_rest_plus(self):
        from hatch_rest_api import RestPlusAudioTrack
        d = RestPlus()
        d.is_on, d.is_playing, d.is_online = True, True, True
        d.audio_track, d.volume = RestPlusAudioTrack.PinkNoise, 55
        d.red, d.green, d.blue, d.brightness = 0, 0, 255, 30
        snap = hs.hatch_snapshot(d)
        self.assertEqual((snap["sound"], snap["volume"], snap["light_on"], snap["color"]),
                         ("Pink Noise", 55, True, "#0000ff"))

    def test_waits_for_state_to_arrive(self):
        d = self.rest_iot(is_online=False)
        with mock.patch.object(hs.time, "sleep", side_effect=lambda _: setattr(d, "is_online", True)) as sleep:
            hs.wait_for_hatch_state(d)
        self.assertEqual(sleep.call_count, 1)


class CliTests(unittest.TestCase):
    def test_play_args(self):
        args = hs.build_parser().parse_args(
            ["play", "https://open.spotify.com/album/abc", "--speaker", "Echo", "--no-shuffle"]
        )
        self.assertEqual(args.uri, "spotify:album:abc")
        self.assertFalse(args.shuffle)


if __name__ == "__main__":
    unittest.main()
