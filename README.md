# Hatch-Baby

Play Spotify at bedtime alongside your Hatch, from one command.

## Why it works this way

No Hatch model (Rest, Rest+, Rest 2nd gen, Rest Baby, Restore) can play
Spotify itself. Hatch has no Spotify Connect support, and Spotify doesn't
give out raw audio URLs the Hatch could stream. So this script:

1. starts your Spotify playlist on a **Spotify Connect speaker in the nursery**
   (Echo, Sonos, Google Nest, an old phone or tablet, ...), and
2. sets the **Hatch to nightlight mode** with its own sound muted, so the
   two don't play over each other.

You can also add a sleep timer that fades Spotify out and turns the Hatch off.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

1. **Spotify** (playback control needs **Spotify Premium**):
   - Create an app at <https://developer.spotify.com/dashboard>.
   - Add the redirect URI `http://127.0.0.1:8888/callback`.
   - Copy the Client ID and Client Secret into `.env`.
   - The first time you run the script, it prints a URL. Open it, approve,
     and paste the URL you get redirected to back into the terminal. The
     token is cached in `.spotify_token_cache`.
2. **Hatch**: put your Hatch Sleep app email and password in `.env`.
   The Hatch connection goes through the unofficial
   [`hatch_rest_api`](https://github.com/dahlb/ha_hatch) library, the same
   one the Home Assistant Hatch integration uses.

## Web UI (easiest)

```bash
python hatch_ui.py
```

<img src="docs/ui.png" width="300" align="right" alt="Bedtime UI">

1. **First time only:** on the computer running it, open
   <http://127.0.0.1:8888> and tap **Connect Spotify**. (Spotify only allows
   this login from the computer itself.)
2. After that, open `http://<computer's-IP>:8888` on your phone. Use
   *Add to Home Screen* to get an app icon.

From the page you can:
- paste a playlist link (recent ones are saved as one-tap buttons)
- pick the nursery speaker
- set a sleep timer that fades out (and can turn the Hatch off)
- pick a nightlight color and brightness
- control playback and volume
- stop everything with one button

The page is dark so it won't light up the room. The server listens on your
home network with no password, so anyone on your Wi-Fi can use it. Run
`python hatch_ui.py --host 127.0.0.1` to limit it to this computer only.

<br clear="right">

## Command line usage

```bash
# See what's available
python hatch_spotify.py devices

# Play a playlist on the nursery speaker, with an orange Hatch nightlight
python hatch_spotify.py play https://open.spotify.com/playlist/37i9dQZF1DWZd79rJ6a7lp \
    --speaker "Nursery Echo" --volume 25

# Use the defaults from .env, fade out after 45 minutes, then turn the Hatch off
python hatch_spotify.py play --minutes 45 --hatch-off-after

# Stop
python hatch_spotify.py stop --hatch-off
```

`play` options: `--color ff0000`, `--brightness 10`, `--shuffle/--no-shuffle`,
`--hatch "Nursery"` (if you have several Hatches), and `--no-hatch`.

### Bonus: audio on the Hatch speaker itself

The Rest 2nd gen and Rest Baby can stream a **public MP3/WAV URL** (your own
recordings, a lullaby file you host, and so on). This doesn't work with
Spotify:

```bash
python hatch_spotify.py play-url https://example.com/lullaby.mp3 --volume 40
```

## Automating bedtime

To run it every night at 7:30pm with cron (`crontab -e`):

```
30 19 * * * cd /path/to/Hatch-Baby && .venv/bin/python hatch_spotify.py play --minutes 60 --hatch-off-after
```

## Tests

```bash
python -m unittest -v
```

## Notes

- The Spotify speaker must show up in `devices`. If it doesn't, open Spotify
  and play something on it once.
- The Hatch API is unofficial and could break if Hatch changes its cloud.
- Keep `.env` private; it's already in `.gitignore`.
