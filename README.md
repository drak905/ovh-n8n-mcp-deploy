# n8n on a VPS, reachable only over Tailscale

n8n 2.48.7 with Postgres 16, behind a Tailscale sidecar. Nothing is published on the host, there is no public edge
(no Caddy, no ports 80/443), and the host firewall, packages and SSH are not touched.

```
PC / phone on the tailnet --https--> ts-n8n (Tailscale Serve, trusted cert)
                                        |  http://n8n:5678   (Docker network n8n_net, 172.21.0.0/24)
                                        v
                                      n8n  --> n8n-postgres
```

Bind mounts on the VPS: `/opt/n8n/{data,postgres,ts-state,backups}`. Memory caps: sidecar 256m, n8n 1536m, Postgres 512m
(swap equals memory, so these containers do not use host swap).

## Deploy

```
python scripts/deploy.py init-env   # local .env: OVH_* names and TS_AUTHKEY only, copied from project 055
python scripts/deploy.py audit      # read-only checks and a baseline (ufw hash, listeners, serve config, containers)
python scripts/deploy.py stage      # files, cron, and /opt/n8n/.env (secrets are generated on the VPS)
python scripts/deploy.py sidecar    # pull images, start the sidecar, write the n8n URLs from its DNS name
python scripts/deploy.py up
python scripts/deploy.py drop-key   # remove the Tailscale auth key from the VPS .env
```

Tests (run each tier file as its own command): `python -m pytest tests/test_01_static_security.py`, then
`test_02` to `test_05`. Tier 4 and 5 token tests skip until the owner steps below are done.

## Owner steps (nobody else does these)

1. Open `https://n8n.<tailnet>.ts.net` and create the owner account right away. Until you do, anyone on the tailnet
   can reach the setup form. Always use the `https://` name, never `http://<ip>:5678` (the login cookie is Secure).
2. Settings > Instance-level MCP > enable. Then Connect. Prefer OAuth where the client supports it, so no token file
   exists. The API-key tab gives a bearer token that is shown once.
3. Settings > n8n API > create an API key. Choose workflow scopes only, if the picker offers it.
4. Put `N8N_URL`, `N8N_API_KEY` and `N8N_MCP_TOKEN` into this repo's git-ignored `.env`. Only the tests read it.
5. Save the **encryption key** in your password manager: `grep '^N8N_ENCRYPTION_KEY=' /opt/n8n/.env` on the VPS.
   It cannot be recovered. The backup archive does not contain it, so without it saved credentials are lost.
6. In the Tailscale admin console, turn off key expiry for the new `n8n` node (as for Kuma and Arcane).

## How the agent talks to n8n: two paths

- **MCP** (`/mcp-server/http`) for building and editing: `get_workflow_sdk_reference` first, then `validate_workflow`,
  then `create_workflow_from_code` or `update_workflow` (rename is `setWorkflowMetadata`). Also search, details, execute,
  test, publish, archive and restore. MCP has **no delete**. Archive is the closest.
- **n8n-cli / REST API** for delete and bulk work. `npx @n8n/cli workflow delete <id>` with `N8N_URL` and `N8N_API_KEY`
  in the environment, or `DELETE /api/v1/workflows/{id}` with `X-N8N-API-KEY`. On 2.48.7 `DELETE` also works on a
  workflow that is not archived (verified live), so the archive step is optional.
- Verified live (tier 5 round trip): MCP create, read, update and archive, then REST delete, then GET returns 404.

### Start the agent (Kiro CLI)

One time: copy `docs/kiro-agent-n8n.example.json` to `~/.kiro/agents/n8n.json` and put your tailnet name in the `url`.
This is a separate agent config, not the global `~/.kiro/settings/mcp.json`, so other Kiro sessions do not load the n8n
server (`includeMcpJson` is false). `kiro-cli mcp add` has no `--header` flag, which is why the entry is a file. OAuth is
the alternative if you prefer no token at all. The token is never in the file: the header reads `${N8N_MCP_TOKEN}` from the
environment. Use the plain `${VAR}` form. `${env:VAR}` is not expanded by Kiro CLI 3.5.0 and ends in an OAuth error.

```
kiro-cli chat --agent n8n
```

The token must be in that shell's environment. `scripts/n8n-agent.ps1` reads `N8N_MCP_TOKEN` from the git-ignored
`.env` into its own process and starts `kiro-cli chat --agent n8n` (extra arguments are passed on). Check the
connection without a chat: `kiro-cli chat --agent n8n --no-interactive --require-mcp-startup "hi"` exits 3 if the
server fails to start. For delete, run `npx @n8n/cli` in a shell that has `N8N_URL` and `N8N_API_KEY`.


## Limits

- **Webhooks from the internet do not work.** Telegram, GitHub or Stripe cannot call a tailnet-only n8n, and a Telegram
  Trigger will not activate. Schedule and manual runs, MCP/API runs, and n8n calling out all work.
  Later options (owner decision): a second small sidecar with Tailscale Funnel that proxies only `/webhook/` (needs the
  `funnel` attribute in the tailnet policy), or a Cloudflare Tunnel route for `/webhook/*` only. Then set
  `N8N_WEBHOOK_URL` to the public name and keep `N8N_EDITOR_BASE_URL` on the tailnet name.
- Arcane sees these containers (it has the Docker socket) and can stop or restart them. Updater labels are off, so it
  does not offer image updates.
- Kuma monitoring for `/healthz` is not added by this repo.

## Backup and restore

`/etc/cron.d/n8n-backup` runs `backup_n8n.sh` at 03:40: a `pg_dump -Fc` (checked with `pg_restore -l`) plus the data
folder, minus n8n's `config` file, as `/opt/n8n/backups/n8n-*.tar.gz` (mode 600, 14 days kept). Backups stay on the same
disk, so copy them off the VPS for real disaster recovery.

`restore_test_n8n.sh <archive>` loads the dump into a scratch database, checks it and drops it. The live database is not
touched. A real restore: stop `n8n`, drop and recreate the `n8n` database in `n8n-postgres`, run
`pg_restore -U n8n -d n8n --no-owner` on the dump from the archive, copy the archive's `data/` back to `/opt/n8n/data`
(owner uid 1000), put the saved encryption key in `/opt/n8n/.env`, start `n8n`. This live path is documented, not tested.

## Recovery over SSH

```
cd /opt/n8n/docker && docker compose -p n8n --env-file /opt/n8n/.env up -d --force-recreate
```
