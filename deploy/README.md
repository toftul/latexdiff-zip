# Deploying latexdiff-zip publicly via Cloudflare Tunnel

This directory hosts the public web service at **https://latexdiff.toftul.net** from a
home server with **no public IP**, using a Cloudflare Tunnel — and contains the blast
radius of running untrusted, attacker-uploaded LaTeX with no login.

```
Internet ──TLS──> Cloudflare edge ──Tunnel──> cloudflared (container)
                  (WAF, rate-limit)              │  on two podman networks:
                                                 ├─ ldz-egress  (reaches Cloudflare)
                                                 └─ ldz-internal (--internal, no route out)
                                                        │
                                                        └─> webapp (gunicorn :8080)
                                                            ONLY on ldz-internal → cannot
                                                            reach the internet at all
```

Nothing is published to the host and no inbound port is opened, so **no router/firewall
port-forwarding is required** — `cloudflared` dials *out* to Cloudflare.

> **Security note.** The webapp compiles untrusted LaTeX (a code-execution surface) with
> no auth. The big controls here are: no internet egress for the webapp, read-only root,
> dropped capabilities, no-new-privileges, resource caps, and edge rate-limiting. This
> shrinks the risk a lot but does not eliminate it; running as your *rootless* user on the
> host (not a VM) is a deliberate trade-off. A dedicated VM remains the stronger boundary —
> these same units drop into a VM unchanged.

Files here:

| File | Purpose |
|---|---|
| `ldz-internal.network` | Quadlet net unit, `--internal` (no egress) — webapp only |
| `ldz-egress.network`   | Quadlet net unit, outbound — cloudflared only |
| `webapp.container`     | Quadlet unit for the hardened web app |
| `cloudflared.container`| Quadlet unit for the tunnel daemon |
| `cloudflared.env.example` | template for the tunnel token |

All commands below are **rootless** (run as your normal user, no `sudo`). Confirmed with
podman 5.6.0 on AlmaLinux.

---

## 1. Build the images

From the repo root:

```sh
./latexdiff-zip-web.sh --build
```

This produces `latexdiff-zip:latest` and `latexdiff-zip-web:latest` (the unit files
reference these tags). Re-run after any change to the script or `webapp/`.

## 2. Create the Cloudflare Tunnel and get its token

In the Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel →
Cloudflared**:

1. Name it e.g. `latexdiff-home`, save.
2. On the "install connector" screen, **copy the token** — the long `eyJ...` string after
   `--token` in the shown command. (Ignore the install command itself; we run cloudflared
   as a container.)
3. Leave **Public Hostname** until step 5 (the webapp must be running first).

## 3. Install the Quadlet units and the token

```sh
mkdir -p ~/.config/containers/systemd
cp deploy/ldz-internal.network deploy/ldz-egress.network \
   deploy/webapp.container deploy/cloudflared.container \
   ~/.config/containers/systemd/

cp deploy/cloudflared.env.example ~/.config/containers/systemd/cloudflared.env
$EDITOR ~/.config/containers/systemd/cloudflared.env   # paste TUNNEL_TOKEN
chmod 600 ~/.config/containers/systemd/cloudflared.env
```

## 4. Start the services (and enable boot persistence)

```sh
# Let user services run without an active login (start at boot):
loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user start webapp.service cloudflared.service

systemctl --user status webapp.service cloudflared.service   # both should be active
```

The networks are created automatically as dependencies. The `[Install]` sections make both
start on boot once linger is enabled.

## 5. Route the public hostname + harden the edge

Back in the tunnel's **Public Hostname** tab → **Add a public hostname**:

- **Subdomain:** `latexdiff`  **Domain:** `toftul.net`
- **Service:** `HTTP` → `webapp:8080`
  (`webapp` resolves via podman DNS on `ldz-internal`; the port is gunicorn's.)

Save. Because `toftul.net` DNS is already on Cloudflare, the `CNAME latexdiff → <id>.cfargotunnel.com`
record and the edge TLS cert are created automatically.

Then, since there is no login, lean on Cloudflare to curb abuse:

- **Security → WAF → Rate limiting rules:** limit `POST /jobs` (e.g. 5/min per IP).
- **Security → Bots:** enable **Bot Fight Mode**; set **Security Level** to Medium/High.
- **Caching → Cache Rules:** add a *Bypass cache* rule for `latexdiff.toftul.net`
  (POST, the SSE stream, and the PDF must never be cached).
- *(Optional, if abused later)* add **Cloudflare Turnstile** or a soft **Access** gate.

---

## Verification

1. **Egress isolation (the key control):**
   ```sh
   podman exec webapp     curl -m5 https://1.1.1.1   # MUST fail (no route)
   podman exec cloudflared curl -m5 https://1.1.1.1   # should succeed
   ```
2. **Boot persistence:** reboot, then `systemctl --user status webapp cloudflared` —
   both active again.
3. **Public end-to-end** (from off-network, e.g. phone on cellular): open
   `https://latexdiff.toftul.net`, upload the repo's sample `old.zip`/`new.zip`, watch the
   live log stream to completion (this also proves the SSE heartbeat survives Cloudflare's
   ~100s idle timeout), and download the PDF.
4. **Upload cap:** a >95 MB combined upload is rejected with a clear message (client guard)
   and >100 MB is also stopped at the edge (Cloudflare 413).

---

## Operations

```sh
# Logs (the app also prints job events to stderr):
podman logs -f webapp
podman logs -f cloudflared          # tunnel health also shown in the dashboard

# Update the app after editing the script/webapp:
./latexdiff-zip-web.sh --build && systemctl --user restart webapp.service

# Update cloudflared:
podman pull docker.io/cloudflare/cloudflared:latest && \
  systemctl --user restart cloudflared.service
```

Job scratch self-prunes (dirs >1h are removed on each new job) and lives on tmpfs, so
nothing accumulates on disk across reboots.

## Troubleshooting

- **Build fails under read-only root.** TeX usually only needs `/tmp`, but if a compile
  errors on a read-only path, comment out `ReadOnly=true` (and the `Tmpfs=` lines) in
  `webapp.container`, `daemon-reload`, and restart. You keep every other control.
- **Out of memory on huge uploads.** The tmpfs scratch is RAM-backed under the 3g cap.
  For routinely large projects, switch `/tmp` to a disk-backed named volume: remove the
  `Tmpfs=/tmp...` line and add `Volume=ldz-scratch:/tmp` (podman auto-creates the volume),
  then `daemon-reload`/restart. (Note: a persistent volume won't auto-wipe on restart.)
- **SELinux (enforcing).** Rootless podman with these units works out of the box; if a
  volume mount is ever denied, append `:Z` to the `Volume=` value.
- **cloudflared can't reach the app.** Confirm `ContainerName=webapp` and that both
  containers share `ldz-internal`: `podman network inspect ldz-internal`.
