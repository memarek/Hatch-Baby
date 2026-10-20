# Hosting Bedtime securely

This puts Bedtime on an always-on machine and makes it reachable at a private
`https://…ts.net` address that only your own devices can open.

```
 your phone ──(Tailscale, encrypted)──▶ machine running Bedtime ──▶ Spotify + Hatch clouds
                                         no open ports
```

## Security model

| Layer | What it does |
|---|---|
| **Tailscale** | Only devices logged into *your* Tailscale account can reach the page. To everyone else it doesn't exist, so there's no login page to attack. |
| **No open ports** | The app listens on `127.0.0.1` only. Tailscale reaches it from inside the machine, and the cloud firewall blocks all incoming traffic, including SSH. |
| **App password** | A second lock behind Tailscale: a 12+ character password, signed session cookies (`HttpOnly`, `Secure`, `SameSite`) and a 15-minute lockout after 5 wrong tries. |
| **Request checks** | Other websites can't press the buttons for you (origin and JSON-only checks), and the Spotify login is protected with a one-time `state` value. Strict browser security headers are on. |
| **Secrets** | `.env`, the Spotify token and the session key are in `/var/lib/bedtime`, readable only by the `bedtime` service user. Nothing secret is in git. |
| **Sandboxed service** | systemd runs the app as an unprivileged user that can't change its own code or write anywhere but its state folder. |
| **Updates** | Automatic OS security updates; Python packages are pinned to exact versions. |

Spotify access is limited to controlling playback. You can revoke it anytime at
<https://www.spotify.com/account/apps/>. Use a **unique password for your Hatch
account**, because the server has to store it.

## 1. Get a machine

### Option A: Google Cloud (always-on VM)

1. Sign up at <https://cloud.google.com> and create a project.
2. **Billing → Budgets & alerts → create a $1 budget** with email alerts.
3. Open **Cloud Shell** (the `>_` icon) and create the VM:

   ```bash
   gcloud compute instances create bedtime \
     --zone=us-central1-a --machine-type=e2-micro \
     --image-family=debian-12 --image-project=debian-cloud \
     --boot-disk-size=30GB --boot-disk-type=pd-standard \
     --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
   ```

   The free tier only covers `e2-micro` in `us-west1`, `us-central1` or
   `us-east1` with a **standard** disk up to 30 GB. Other choices are billed.
   Google may charge roughly $3–4 a month for the VM's public IP address.
   Check **Billing → Reports** after the first week.

4. Connect for the first setup: `gcloud compute ssh bedtime --zone=us-central1-a`

### Option B: a Raspberry Pi or spare computer at home (free)

Any machine running Debian, Ubuntu or Raspberry Pi OS that stays on. There
are no cloud costs, and the steps below are the same.

## 2. Install

On the machine (in Cloud Shell after `gcloud compute ssh`, or a terminal on
your Pi or computer):

```bash
sudo apt-get install -y git
git clone -b claude/spotify-hatch-baby-playback-az1d3l https://github.com/memarek/Hatch-Baby.git
cd Hatch-Baby && sudo ./deploy/setup.sh
```

The script walks you through everything:
1. **Tailscale:** it prints a login link. Use the same account you'll use on
   your phone. If it says HTTPS or Serve isn't enabled for your tailnet, open
   the link it gives, enable it, and run the script again.
2. **Hatch:** it asks for your Hatch app email and password, and checks them.
3. **Spotify:** it shows the exact redirect URI to use, walks you through
   creating a Spotify developer app, then asks for its Client ID and secret
   and checks them.
4. **Done:** it prints your private address (like
   `https://bedtime.tail1234.ts.net`) and your **Bedtime password**. Save the
   password in your password manager; it's shown only once.

Passwords are hidden as you type and saved where only the app can read them.

## 3. Use it

1. Install Tailscale on your phone and log in with the same account.
2. Open the address, log in with your Bedtime password, and tap
   **Connect Spotify** (just once).
3. Tap Share → **Add to Home Screen** for an app icon.

## 4. Changing settings later

```bash
cd Hatch-Baby
sudo ./deploy/setup.sh --reconfigure    # re-enter Hatch/Spotify details
sudo ./deploy/setup.sh --new-password   # forgot the Bedtime password
```

## 5. Lock down the cloud firewall (Google Cloud only)

Once you can reach the machine via Tailscale (`ssh <you>@bedtime` from a
Tailscale device), remove Google's default open ports:

```bash
gcloud compute firewall-rules delete default-allow-ssh default-allow-rdp default-allow-icmp
```

Tailscale only makes outgoing connections, so it keeps working with every
incoming port closed. If you ever lose Tailscale access, use the VM's
**serial console** in the Google Cloud console.

## Updating

```bash
cd Hatch-Baby && git pull && sudo ./deploy/setup.sh
```

## Troubleshooting

```bash
sudo systemctl status bedtime
sudo journalctl -u bedtime -n 50
```
