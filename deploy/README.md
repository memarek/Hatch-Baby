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

On the machine:

```bash
sudo apt-get install -y git
git clone https://github.com/memarek/Hatch-Baby.git
cd Hatch-Baby
sudo ./deploy/setup.sh
```

If the repository is private, `git clone` asks you to sign in. Use your GitHub
username and a [personal access token](https://github.com/settings/tokens) as
the password.

The script:
- installs the app and automatic security updates
- installs Tailscale and prints a login link (use the same account you'll use on your phone)
- generates your Bedtime password and prints it once, so save it
- prints your private address, for example `https://bedtime.tail1234.ts.net`

If it says HTTPS or Serve isn't enabled for your tailnet, open the link it
gives, enable it, and run the script again.

## 3. Fill in your settings

```bash
sudo nano /var/lib/bedtime/.env      # Hatch email/password, Spotify client ID/secret
sudo systemctl restart bedtime
```

In your Spotify developer app, add the redirect URI the script printed,
for example `https://bedtime.tail1234.ts.net/callback`.

## 4. Use it

1. Install Tailscale on your phone and log in with the same account.
2. Open the address, log in with your Bedtime password, and tap
   **Connect Spotify** (just once).
3. Tap Share → **Add to Home Screen** for an app icon.

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
