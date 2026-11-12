"""Audio files the Bedtime server hosts for the Hatch to download.

Files live in MEDIA_DIR (inside the server's state folder) and are served at
/media/<token>/<name>, where the token is derived from the server's secret:
the Hatch can fetch them without logging in, but nobody can guess the address.
"""

import hashlib
import hmac
import math
import os
import re
import shutil
import struct
import subprocess
import wave

MEDIA_DIR = "media"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}\.(wav|mp3)$")
CONTENT_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg"}


def media_token(secret):
    return hmac.new(secret, b"bedtime-media", hashlib.sha256).hexdigest()[:32]


def read_secret(path=".ui_secret"):
    """The server's signing key, created the first time the server starts."""
    with open(path) as f:
        return bytes.fromhex(f.read().strip())


def media_url(public_url, token, name):
    return f"{public_url.rstrip('/')}/media/{token}/{name}"


def resolve(token, expected_token, name, media_dir=MEDIA_DIR):
    """Path and content type for a request, or None if it isn't one of ours."""
    if not hmac.compare_digest(token, expected_token) or not NAME_RE.match(name):
        return None
    path = os.path.join(media_dir, name)
    if not os.path.isfile(path):
        return None
    return path, CONTENT_TYPES[name.rsplit(".", 1)[1]]


def parse_range(header, size):
    """(start, end) inclusive for a single "bytes=a-b" range, or None to send it all."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match or size == 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":  # the last N bytes
        start, end = max(0, size - int(last)), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        return "unsatisfiable"
    return start, end


def write_tone(path, rate=44100, channels=1, bits=16, seconds=15):
    """A gentle repeating two-note chime, easy to recognise and not startling."""
    frames = bytearray()
    peak = (1 << (bits - 1)) - 1
    for i in range(int(rate * seconds)):
        t = i / rate
        beat = t % 1.5
        freq = 523.25 if (t // 1.5) % 2 == 0 else 659.25  # C5, E5
        envelope = math.exp(-3 * beat)  # soft strike, fading out
        sample = int(peak * 0.35 * envelope * math.sin(2 * math.pi * freq * t))
        packed = struct.pack("<h", sample) if bits == 16 else struct.pack("<B", (sample >> 8) + 128)
        frames += packed * channels
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


TEST_SOUNDS = [
    # name, sample rate, channels
    ("test-44k-mono.wav", 44100, 1),
    ("test-44k-stereo.wav", 44100, 2),
    ("test-22k-mono.wav", 22050, 1),
]


def make_test_sounds(media_dir=MEDIA_DIR):
    """Write the test files. Returns their names; MP3 only if ffmpeg is installed."""
    os.makedirs(media_dir, exist_ok=True)
    names = []
    for name, rate, channels in TEST_SOUNDS:
        write_tone(os.path.join(media_dir, name), rate=rate, channels=channels)
        names.append(name)
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", os.path.join(media_dir, "test-44k-mono.wav"),
                        "-codec:a", "libmp3lame", "-b:a", "128k", os.path.join(media_dir, "test-44k-mono.mp3")],
                       check=True)
        names.append("test-44k-mono.mp3")
    return names
