# Tailnet browser access

Deployed 2026-11-08 at `/opt/harvest/tailnet-access` on OVH. Any device allowed
by tailnet policy to reach this endpoint gets the shared operator's access:

- SpiderFoot (OVH): `https://<TAILNET_HOST>:8443/`

Hostnames and addresses live in `site.env` (ignored by Git; see `site.env.example`).

**2026-11-10:** Harvest was shut down, and its route (`:443` to `127.0.0.1:18083`)
was removed from Tailscale Serve. The nginx `18083` server block is left in place but
unused. To restore it, rerun the `--https=443` Serve command below and the Harvest
checks from Git history.

Port 3000 is SpiderFoot, not Harvest. The original HTTP ports remain available
with their original authentication for existing clients. Use the HTTPS links
above for automatic login.

## Trust boundary

Tailscale Serve terminates HTTPS and forwards to nginx on host loopback port
18084 (18083 is the retired Harvest listener). Neither gateway listener accepts LAN/public connections.
Serve is private to the tailnet; Funnel is not enabled. Tagged devices work,
since access does not depend on user identity headers (Serve omits them for tags).
Local host processes are trusted. Tailnet ACLs still control network access.

Nginx replaces Authorization with the existing Harvest operator token or the
existing SpiderFoot integration key (admin role). It rejects foreign Origin
headers, forwards streaming/upgraded connections, and never prints credentials.
Original application authentication remains enabled. No schema, jobs, workers,
application images, or existing port mappings were changed.

SpiderFoot's HTML loads a same-origin bootstrap script before its deferred app
bundle. It sets `tailnet-proxy`, a public, non-secret UI marker, in the existing
token store. This is not a valid API credential. The real key stays in the server's
root-readable nginx config. Each page load restores the marker, so fresh sessions,
deep links and cleared browser storage work without a credential form. `/login`
redirects to the dashboard. Sign-out through this gateway does not revoke other
operator sessions; reloading authenticates again through Tailscale. Disconnect
Tailscale to end access.

The HTTP gateway upstreams remain on the same host. Do not move this gateway to
another host or expose its loopback listeners without redesigning the trust boundary.

## Deploy / rotate credentials

Copy this directory to `/opt/harvest/tailnet-access`, create `site.env` from
`site.env.example` (mode 0600), then on the VPS:

```sh
cd /opt/harvest/tailnet-access
sudo python3 render.py
sudo docker compose config --quiet
sudo docker compose run --rm --no-deps gateway -t
sudo docker compose up -d
sudo tailscale serve --bg --https=8443 http://127.0.0.1:18084
```

`render.py` fills in `site.env`, and reads only the two required credentials from the
Harvest API container (it can be stopped)
and pins the gateway to the locally installed nginx frontend image by image ID.
The generated `nginx.conf` (0600) and `.env` are ignored by Git. After credential
rotation, rerun the renderer and `sudo docker compose restart gateway`.
Do not use `nginx -T`, which prints the credential-bearing rendered configuration.

Run `python3 verify.py` from a tailnet node with a `site.env`. Checks cover the
authorized route, foreign-Origin rejection, direct API authentication, and spoofed
headers/marker rejection. Run `python3 verify-browser.py` on a workstation with Playwright/Chromium
installed to check Dashy click-through, fresh profiles, direct links, and cleared
browser storage. Tests do not start scans or jobs.

## Rollback

```sh
sudo tailscale serve --https=8443 off
cd /opt/harvest/tailnet-access
sudo docker compose down
```

The Serve entry was absent before this deployment. Restore the Dashy
backup `conf.yml.before-tailnet-login-20261108` on Proxmox if rolling back the
links as well. Old direct endpoints and credentials remain usable.

## Verification on 2026-11-08

- Both apps open authenticated in fresh Chromium contexts, no JavaScript errors.
- Eight read-only HTTP authorization checks pass.
- Both apps also return authenticated HTTP 200 from the tagged Proxmox node.
- Gateway healthy; loopback listeners and tailnet-only HTTPS bindings verified.
- Dashy links distinguish Harvest, SpiderFoot (OVH), and SpiderFoot (Proxmox).
- Browser screenshots retained locally on the workstation.

## Verification on 2026-11-11

- After moving hosts into `site.env`, the rendered `nginx.conf` is byte-identical to
  the deployed one; `verify.py` passes 4/4.
