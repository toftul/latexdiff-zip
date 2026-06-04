# Deploying latexdiff-zip with a Cloudflare Tunnel

[← back to README](README.md)

Publishes the web UI at **https://latexdiff.toftul.net** from a home server with **no public
IP**. `cloudflared` dials *out* to Cloudflare, so there's no port-forwarding and nothing
inbound is opened.

```
Internet ──TLS──▶ Cloudflare edge ──Tunnel──▶ cloudflared (native systemd service)
                  (WAF, rate-limit)                 │  →  http://localhost:8080
                                                    ▼
                                          webapp container (gunicorn :8080,
                                          published on 127.0.0.1 only)
```

> **Run it in a dedicated VM.** The app compiles **untrusted uploaded LaTeX** with no login.
> A throwaway VM (behind libvirt NAT) is the containment boundary — see *Security notes*.

**Assumptions:** AlmaLinux (podman 5.x), domain DNS already on Cloudflare. The container runs
rootless (your user); cloudflared is a system service (`sudo`).

---

## 1. Build the image

```sh
cd ~/latexdiff-zip
podman build -t latexdiff-zip:latest     -f Containerfile     .
podman build -t latexdiff-zip-web:latest -f Containerfile.web .
```

## 2. Run the web app (published on localhost, survives reboot)

Create `~/.config/containers/systemd/webapp.container`:

```ini
[Unit]
Description=latexdiff-zip web app

[Container]
ContainerName=webapp
Image=latexdiff-zip-web:latest
PublishPort=127.0.0.1:8080:8080
# Tighten the build watchdog (default 600s) and cap resources so a heavy or
# malicious build is killed instead of taking down the VM.
Environment=LDZ_TIMEOUT=300
PodmanArgs=--memory=3g --pids-limit=512

[Service]
Restart=always

[Install]
WantedBy=default.target
```

```sh
loginctl enable-linger "$USER"        # start at boot without a login session
systemctl --user daemon-reload
systemctl --user start webapp.service
curl -sI http://127.0.0.1:8080 | head -1   # expect: HTTP/1.1 200 OK
```

## 3. Create the tunnel and copy its token

Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**.
Name it (e.g. `latexdiff-home`), then **copy the token** — the long `eyJ...` string after
`--token` on the install screen.

## 4. Install cloudflared natively

```sh
sudo dnf install -y https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm
sudo cloudflared service install <PASTE_YOUR_TOKEN>     # installs + enables + starts the service
systemctl status cloudflared                            # expect: active (running)
```

## 5. Route the public hostname

In the tunnel → **Public Hostname** → **Add a public hostname**:

- **Subdomain** `latexdiff`, **Domain** `toftul.net`
- **Service** `HTTP` → `localhost:8080`

DNS is on Cloudflare, so the `CNAME` and edge TLS cert are created automatically. Open
`https://latexdiff.toftul.net`.

## 6. Harden the edge (there's no login)

- **WAF → Rate limiting:** throttle `POST /jobs` (e.g. 5/min per IP).
- **Bots:** Bot Fight Mode on; **Security Level** Medium/High.
- **Cache Rules:** *Bypass cache* for `latexdiff.toftul.net` (upload, log stream, and PDF must not cache).

## 7. (Recommended) Keep the VM off your LAN

libvirt NAT lets the VM reach the internet *and* your LAN. Block the LAN with one host rule:

```sh
sudo firewall-cmd --permanent --direct --add-rule ipv4 filter FORWARD 0 \
  -s 192.168.122.0/24 -d 192.168.20.0/24 -j DROP   # adjust subnets
sudo firewall-cmd --reload
```

---

## Verify, operate, troubleshoot

```sh
podman logs -f webapp            # app build logs / job events
journalctl -u cloudflared -f     # tunnel logs

# Update the app:
podman build -t latexdiff-zip:latest -f Containerfile . \
  && podman build -t latexdiff-zip-web:latest -f Containerfile.web . \
  && systemctl --user restart webapp.service
```

- **cloudflared won't connect / times out.** Check `journalctl -u cloudflared`. If the
  QUIC/UDP 7844 pre-check fails (TCP ok), force HTTP/2: `sudo systemctl edit --full
  cloudflared` and append `--protocol http2` to the `ExecStart` `run` command, then
  `daemon-reload` + restart. If **both** UDP and TCP 7844 fail, allow outbound 7844 on your
  router/ISP — cloudflared requires it.
- **502 from Cloudflare.** The app isn't up on `127.0.0.1:8080` — re-check step 2.
- **Build can't resolve `deb.debian.org`** (rootless + `systemd-resolved`). Add
  `--network=host` to that `podman build`.
- **Upload rejected as too large.** Cloudflare Free/Pro caps proxied bodies at **100 MB**;
  keep the two zips under that combined.

## Security notes

Untrusted LaTeX (plus ImageMagick/Ghostscript on uploaded figures) runs with no auth.
Shell-escape is off, but image/PDF parsers have a history of RCE bugs. Containment rests on
the **VM boundary**, **rootless Podman** (container-root maps to an unprivileged VM user),
the **LAN firewall rule** (§7), edge **rate-limiting** (§6), and **patching** (rebuild the
image periodically). Worst realistic case is a compromised, disposable VM used for outbound
abuse — bounded by §7 and the NAT. Kill switch: disable the hostname in the dashboard, then
rebuild the VM. For maximum safety, you can also disable the figure-comparison feature, which
removes the riskiest surface.
