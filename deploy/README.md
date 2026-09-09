# HTTPS directly on the VPS IP

MCP: `https://194.87.35.210/mcp` (Streamable HTTP).
API docs: `https://194.87.35.210/docs`.

The IP endpoint uses a publicly trusted Let's Encrypt certificate, not a
self-signed certificate. The old `people-mcp.194-87-35-210.sslip.io` address stays
available for existing clients, but the IP endpoint does not use that DNS name.
If the server IP changes, issue a new certificate and change the client URL.

## Install or reproduce

These are root commands for the Ubuntu VPS, with PeopleMCP in `/opt/people-mcp`,
the API bound to `127.0.0.1:8000`, Caddy running, and TCP 80/443 open. Before making
changes, back up `/etc/caddy/Caddyfile` and `/opt/people-mcp/.env` into a root-only
directory. On a different server, replace the IP in the Caddyfiles and settings;
remove the compatibility domain block if it is not yours.

The application stays unchanged. Certbot 5.8.0 lives in an isolated host venv;
it is not installed into the API image. [Certbot's IP-certificate support](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)
requires a recent client and a short-lived certificate profile.

```sh
apt-get update
apt-get install -y python3-venv
python3 -m venv /opt/people-mcp-certbot
/opt/people-mcp-certbot/bin/pip install certbot==5.8.0
cd /opt/people-mcp
install -d -o root -g root -m 0755 /var/lib/people-mcp-acme
caddy validate --config deploy/Caddyfile.bootstrap --adapter caddyfile
install -o root -g root -m 0644 deploy/Caddyfile.bootstrap /etc/caddy/Caddyfile
systemctl reload caddy
```

Test the HTTP challenge without saving an untrusted staging certificate:

```sh
/opt/people-mcp-certbot/bin/certbot certonly --dry-run --non-interactive \
  --agree-tos --register-unsafely-without-email --required-profile shortlived \
  --webroot --webroot-path /var/lib/people-mcp-acme \
  --ip-address 194.87.35.210 --cert-name people-mcp-ip
```

Then install the deploy hook and request the real certificate:

```sh
install -o root -g root -m 0755 deploy/reload-people-mcp-certificate /usr/local/sbin/reload-people-mcp-certificate
/opt/people-mcp-certbot/bin/certbot certonly --non-interactive \
  --agree-tos --register-unsafely-without-email --required-profile shortlived \
  --webroot --webroot-path /var/lib/people-mcp-acme \
  --ip-address 194.87.35.210 --cert-name people-mcp-ip \
  --deploy-hook /usr/local/sbin/reload-people-mcp-certificate
caddy validate --config examples/Caddyfile --adapter caddyfile
install -o root -g root -m 0644 examples/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

The hook copies just this certificate/key into `/etc/caddy/people-mcp-tls`, with
directory mode 0750 and file mode 0640, owned by `root:caddy`. Certbot's account
keys and original archive remain private. Caddy validates the configuration and
reloads the serving certificate without stopping the application.

Add `194.87.35.210` to `MCP_ALLOWED_HOSTS` and `https://194.87.35.210` to
`MCP_ALLOWED_ORIGINS` in `.env`, retaining the existing entries and secrets. Then:

```sh
docker compose up -d --wait --wait-timeout 600
install -o root -g root -m 0644 deploy/people-mcp-cert-renew.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/people-mcp-cert-renew.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now people-mcp-cert-renew.timer
systemctl start people-mcp-cert-renew.service
```

## Renewal and verification

The timer checks twice daily (00:00 and 12:00 server time, plus up to 15 minutes
of jitter supplied by systemd, without a second sleep inside Certbot). Missed
checks run after boot. `certbot renew` requests a new certificate
only when due, and runs the saved deploy hook after renewal. It does not force
issuance every time the timer fires. Keep port 80 and the ACME challenge route
available: renewal uses HTTP-01 without stopping Caddy. Review service failures
in the journal; this MVP has no external expiry-alerting service.

```sh
systemctl list-timers people-mcp-cert-renew.timer
journalctl -u people-mcp-cert-renew.service --no-pager -n 40
/opt/people-mcp-certbot/bin/certbot renew --cert-name people-mcp-ip --dry-run --run-deploy-hooks --no-random-sleep-on-renew
curl --fail https://194.87.35.210/health
docker compose exec -T -e API_BASE_URL=https://194.87.35.210 api python -m unittest discover -s tests -v
```

The dry run tests renewal against staging; `--run-deploy-hooks` reloads the
current trusted certificate, never the temporary staging certificate. Do not use
`--force-renewal` for routine checks and do not disable TLS verification in clients.
