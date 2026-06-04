# Deploying latexdiff-zip with a Cloudflare Tunnel

This guide publishes the web UI at **https://latexdiff.toftul.net** from a home server
that has **no public IP**, using a Cloudflare Tunnel. `cloudflared` dials *out* to
Cloudflare, so there is **no port-forwarding** and nothing inbound is opened.

```
Internet ──TLS──▶ Cloudflare edge ──Tunnel──▶ [ your VM / host ]
                  (WAF, rate-limit)               ├─ cloudflared ──out──▶ Cloudflare
                                                   └─ webapp (gunicorn :8080)
                                                  both on one podman network "ldz"
cloudflared reaches the app by name: http://webapp:8080
```

> **Run it in a dedicated VM.** The web app compiles **untrusted, uploaded LaTeX** with no
> login — a code-execution surface. A throwaway VM (behind libvirt NAT) is the isolation
> boundary; if it's ever abused, you rebuild it and lose nothing. See *Security notes* below.

**Assumptions:** AlmaLinux 10 (podman 5.x), your domain's DNS already on Cloudflare,
rootless Podman (run everything as your normal user — no `sudo`).

---

## 1. Build the images

> **Rootless build-DNS gotcha:** on hosts using `systemd-resolved`, `apt-get` inside the
> build can't resolve DNS in the isolated build netns. Build with `--network=host` (affects
> the build only):

```sh
cd ~/latexdiff-zip
podman build --network=host -t latexdiff-zip:latest     -f Containerfile     .
podman build --network=host -t latexdiff-zip-web:latest -f Containerfile.web .
```

## 2. Create the Cloudflare Tunnel and copy its token

Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**:

1. Name it (e.g. `latexdiff-home`) and save.
2. On the "install connector" screen, **copy the token** — the long `eyJ...` string after
   `--token`. (Ignore the install command; we run cloudflared as a container.)
3. Leave the **Public Hostname** for step 5.

## 3. Write the unit files

Create the three Quadlet units and the token file under `~/.config/containers/systemd/`.

`~/.config/containers/systemd/ldz.network`
```ini
[Network]
NetworkName=ldz
```

`~/.config/containers/systemd/webapp.container`
```ini
[Unit]
Description=latexdiff-zip web app

[Container]
ContainerName=webapp
Image=latexdiff-zip-web:latest
Network=ldz.network
# Tighten the per-build watchdog (default 600s) and cap resources so a heavy or
# malicious build is killed instead of taking down the VM.
Environment=LDZ_TIMEOUT=300
PodmanArgs=--memory=3g --pids-limit=512

[Service]
Restart=always
TimeoutStartSec=120

[Install]
WantedBy=default.target
```

`~/.config/containers/systemd/cloudflared.container`
```ini
[Unit]
Description=cloudflared tunnel for latexdiff.toftul.net
After=webapp.service
Wants=webapp.service

[Container]
ContainerName=cloudflared
Image=docker.io/cloudflare/cloudflared:latest
Network=ldz.network
EnvironmentFile=%h/.config/containers/systemd/cloudflared.env
# If outbound UDP 7844 is blocked (QUIC pre-check fails), append: --protocol http2
Exec=tunnel --no-autoupdate run

[Install]
WantedBy=default.target
```

`~/.config/containers/systemd/cloudflared.env`  (then `chmod 600` it)
```sh
TUNNEL_TOKEN=eyJ...paste-your-token-here...
```

## 4. Start the services (and survive reboot)

```sh
loginctl enable-linger "$USER"          # start user services at boot, no login needed
systemctl --user daemon-reload
systemctl --user start webapp.service cloudflared.service
systemctl --user status webapp.service cloudflared.service   # both should be active
```

## 5. Route the public hostname

Back in the tunnel → **Public Hostname** tab → **Add a public hostname**:

- **Subdomain:** `latexdiff`  **Domain:** `toftul.net`
- **Service:** `HTTP` → `webapp:8080`

Since your DNS is on Cloudflare, the `CNAME latexdiff → <id>.cfargotunnel.com` record and the
edge TLS certificate are created automatically. Give it a minute, then open
`https://latexdiff.toftul.net`.

## 6. Harden the edge (there is no login)

In the Cloudflare dashboard:

- **Security → WAF → Rate limiting:** throttle `POST /jobs` (e.g. 5 requests/min per IP).
- **Security → Bots:** enable **Bot Fight Mode**; set **Security Level** to Medium/High.
- **Caching → Cache Rules:** add a **Bypass cache** rule for `latexdiff.toftul.net`
  (the upload, the live log stream, and the PDF must never be cached).

## 7. (Recommended) Stop the VM from reaching your LAN

libvirt NAT lets the VM reach the internet **and** your LAN. To keep a compromised VM from
touching other devices, add one firewall rule on the **host** (adjust the subnets):

```sh
# VM subnet 192.168.122.0/24  →  LAN 192.168.20.0/24 : drop
sudo firewall-cmd --permanent --direct --add-rule ipv4 filter FORWARD 0 \
  -s 192.168.122.0/24 -d 192.168.20.0/24 -j DROP
sudo firewall-cmd --reload
```

---

## Verify

1. **cloudflared connected:** `podman logs cloudflared` shows `Registered tunnel connection`,
   and the tunnel reads **HEALTHY** in the dashboard.
2. **Boot persistence:** reboot; `systemctl --user status webapp cloudflared` → both active.
3. **End-to-end** (from off your network, e.g. phone on cellular): open the URL, upload two
   project zips, watch the live log to completion, download the diff PDF.

## Operate

```sh
podman logs -f webapp        # app prints job events too
podman logs -f cloudflared

# Update the app after changing the code:
podman build --network=host -t latexdiff-zip:latest     -f Containerfile     .
podman build --network=host -t latexdiff-zip-web:latest -f Containerfile.web .
systemctl --user restart webapp.service

# Update cloudflared:
podman pull docker.io/cloudflare/cloudflared:latest
systemctl --user restart cloudflared.service
```

## Troubleshooting

- **cloudflared won't start / times out.** Read `podman logs cloudflared`: the connectivity
  pre-check tells you what's blocked. If **QUIC/UDP 7844** fails but TCP works, set the
  `Exec=` line to `tunnel --no-autoupdate run --protocol http2`, then `daemon-reload` +
  restart. If **both** UDP and TCP 7844 fail, allow outbound port 7844 on your router/ISP —
  cloudflared requires it.
- **cloudflared can't reach the app** (502s). Confirm both containers are on the network and
  the name matches: `podman network inspect ldz` and that `ContainerName=webapp` equals the
  Public Hostname service host.
- **Build fails resolving `deb.debian.org`.** Use `--network=host` on `podman build` (above).
- **Upload rejected as too large.** Cloudflare Free/Pro caps proxied request bodies at
  **100 MB**. Keep the two zips under that combined.

## Security notes

The service runs untrusted LaTeX (plus ImageMagick/Ghostscript on uploaded figures) with no
auth. Shell-escape is off, but image/PDF parsers have a history of RCE bugs. Containment
rests on: the **VM boundary**, **rootless Podman** (container-root maps to an unprivileged
VM user), the **LAN firewall rule** (§7), edge **rate-limiting** (§6), and **patching**
(rebuild the image periodically for ImageMagick/Ghostscript/kernel fixes). Worst realistic
case is a compromised, disposable VM being used for outbound abuse — bounded by §7 and the
NAT. If you ever suspect compromise: disable the tunnel hostname (instant kill switch) and
rebuild the VM. For maximum safety you can also turn off the figure-comparison feature,
which removes the riskiest (ImageMagick/Ghostscript) surface.
