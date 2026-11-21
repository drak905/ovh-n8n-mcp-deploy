"""Tier 5: restarts and recreate, database integrity, backup, restore test, cron, a Kuma probe, and a token-gated
workflow round trip (create via MCP, read, update, archive, delete via API).

The workflow round trip skips with a clear message until the owner supplies N8N_MCP_TOKEN and N8N_API_KEY in the PC
.env. It is UNVERIFIED against a live n8n until then (the SDK sample follows the docs shape and may need a tweak).
"""
import json
import time

import pytest
import requests

from conftest import COMPOSE, REMOTE, pc_env_token, pc_tailscale

BACKUPS = f"{REMOTE}/backups"
PSQL = "docker exec n8n-postgres psql -U n8n -d n8n -Atc"


def wait_healthy(vps, names, secs):
    end = time.time() + secs
    while time.time() < end:
        out = vps.run("docker inspect -f '{{.State.Health.Status}}' " + " ".join(names), check=False).split()
        if len(out) == len(names) and all(x == "healthy" for x in out):
            return True
        time.sleep(3)
    return False


def https_ok(vps, fqdn, ip, secs=120):
    """Whole path: tailnet IP -> sidecar TLS -> Serve -> http://n8n:5678/healthz."""
    end = time.time() + secs
    while time.time() < end:
        out = vps.run(f"curl -sS --max-time 15 --resolve {fqdn}:443:{ip} -o /dev/null -w '%{{http_code}}' https://{fqdn}/healthz", check=False)
        if out.endswith("200"):
            return True
        time.sleep(3)
    return False


def table_count(vps):
    return int(vps.run(f"{PSQL} \"select count(*) from information_schema.tables where table_schema='public'\""))


# ---- persistence ----

def test_auth_key_removed_after_first_login(vps):
    """Run after the first successful login. `deploy.py drop-key` removes the key from the VPS .env."""
    assert vps.run(f"grep -c '^TS_AUTHKEY=.' {REMOTE}/.env; true").splitlines()[-1] == "0"
    assert vps.run(f"stat -c %a {REMOTE}/.env") == "600"


def test_restart_sidecar_alone_recovers(vps, n8n_fqdn, n8n_ip):
    vps.run("docker restart ts-n8n", timeout=120)
    assert wait_healthy(vps, ["ts-n8n", "n8n"], 150)
    assert https_ok(vps, n8n_fqdn, n8n_ip)


def test_restart_n8n_alone_recovers(vps, n8n_fqdn, n8n_ip):
    vps.run("docker restart n8n", timeout=120)
    assert wait_healthy(vps, ["n8n"], 150)
    assert https_ok(vps, n8n_fqdn, n8n_ip)


def test_force_recreate_all_without_auth_key(vps, n8n_fqdn, n8n_ip):
    before = table_count(vps)
    vps.run(f"{COMPOSE} up -d --force-recreate", timeout=300)
    assert wait_healthy(vps, ["ts-n8n", "n8n-postgres", "n8n"], 240)
    assert https_ok(vps, n8n_fqdn, n8n_ip, 150)
    assert table_count(vps) == before > 20


def test_node_identity_survives_recreate(vps):
    st = json.loads(vps.run("docker exec ts-n8n tailscale status --json"))
    assert st["Self"]["HostName"] == "n8n"
    assert st["BackendState"] == "Running"


def test_postgres_integrity_and_encoding(vps):
    assert vps.run(f"{PSQL} 'select 1'") == "1"
    assert vps.run(f"{PSQL} \"select pg_encoding_to_char(encoding) from pg_database where datname='n8n'\"") == "UTF8"
    assert vps.run("docker exec n8n-postgres sh -c 'pg_isready -U n8n -d n8n'").endswith("accepting connections")


def test_state_dirs_persisted_and_private(vps):
    assert int(vps.run(f"sudo find {REMOTE}/ts-state -type f | wc -l")) > 0
    assert int(vps.run(f"sudo find {REMOTE}/postgres -maxdepth 1 | wc -l")) > 3
    assert vps.run(f"sudo test -d {REMOTE}/data && echo yes") == "yes"


def test_other_stacks_untouched_by_recreate(vps):
    base = vps.run(f"sudo grep '^vn-stock-scanner ' {REMOTE}/baseline/containers.txt | cut -d' ' -f2-")
    assert vps.run("docker inspect -f '{{.State.StartedAt}}' vn-stock-scanner") == base


# ---- backup ----

def test_scripts_installed_root_owned(vps):
    assert vps.run(f"stat -c '%U %a' {REMOTE}/scripts/backup_n8n.sh") in {"root 755", "root 750", "root 700"}
    assert vps.run(f"stat -c '%U %a' {REMOTE}/scripts/restore_test_n8n.sh") in {"root 755", "root 750", "root 700"}
    vps.run(f"bash -n {REMOTE}/scripts/backup_n8n.sh && bash -n {REMOTE}/scripts/restore_test_n8n.sh")


def _backup(vps):
    return vps.run(f"sudo {REMOTE}/scripts/backup_n8n.sh", timeout=240).splitlines()[-1]


def test_backup_runs_and_archive_is_valid(vps):
    path = _backup(vps)
    assert path.startswith(f"{BACKUPS}/n8n-") and path.endswith(".tar.gz")
    assert vps.run(f"sudo stat -c %a {path}") == "600"
    names = vps.run(f"sudo tar -tzf {path}").splitlines()
    assert any(n.endswith("n8n.dump") for n in names)
    assert vps.run(f"sudo tar -xzOf {path} ./n8n.dump | head -c 5") == "PGDMP"


def test_archive_does_not_hold_the_encryption_key_or_env(vps):
    path = _backup(vps)
    names = " ".join(vps.run(f"sudo tar -tzf {path}").splitlines())
    assert not any(n.rstrip("/").endswith("/.env") or n == ".env" for n in names.split())
    # Search the unpacked copy for the key by value, print only a count.
    out = vps.run(f"d=$(mktemp -d) && sudo tar -xzf {path} -C $d && "
                  f"k=$(grep '^N8N_ENCRYPTION_KEY=' {REMOTE}/.env | cut -d= -f2-) && "
                  f"sudo grep -rlF -- \"$k\" $d | wc -l; sudo rm -rf $d")
    assert out.splitlines()[-1] == "0", "the encryption key is inside the backup"


def test_restore_test_into_scratch_database(vps):
    """Restore the newest archive into a scratch database next to the live one, check it, drop it."""
    path = _backup(vps)
    out = vps.run(f"sudo {REMOTE}/scripts/restore_test_n8n.sh {path}", timeout=240).splitlines()[-1]
    assert out.startswith("RESTORE_OK tables=")
    assert int(out.split("=")[1]) > 20
    assert vps.run(f"{PSQL} \"select count(*) from pg_database where datname='n8n_restore_test'\"") == "0"
    assert vps.run(f"{PSQL} 'select 1'") == "1"


def test_retention_removes_only_old_n8n_archives(vps):
    vps.run(f"sudo touch -d '40 days ago' {BACKUPS}/n8n-19990101-000000.tar.gz {BACKUPS}/other-old.txt")
    _backup(vps)
    assert vps.run(f"sudo test -e {BACKUPS}/n8n-19990101-000000.tar.gz && echo yes || echo no") == "no"
    assert vps.run(f"sudo test -e {BACKUPS}/other-old.txt && echo yes || echo no") == "yes"
    vps.run(f"sudo rm -f {BACKUPS}/other-old.txt")


def test_backup_dir_private_and_no_temp_leftovers(vps):
    assert vps.run(f"sudo stat -c %a {BACKUPS}") in {"700", "750"}
    assert vps.run(f"sudo ls {BACKUPS} | grep -c partial ; true").splitlines()[-1] == "0"


def test_backup_lock_prevents_overlap(vps):
    rc = vps.run(f"sudo bash -c 'exec 9>{BACKUPS}/.backup.lock; flock -n 9 && {REMOTE}/scripts/backup_n8n.sh >/dev/null 2>&1; echo $?' ; true")
    assert rc.splitlines()[-1] == "1"


def test_cron_entry_installed(vps):
    assert vps.run("stat -c '%U %a' /etc/cron.d/n8n-backup") == "root 644"
    body = vps.run("cat /etc/cron.d/n8n-backup")
    assert "\r" not in body
    assert "40 3 * * * root /opt/n8n/scripts/backup_n8n.sh" in body


def test_healthz_probe_from_kuma(vps):
    """Read-only probe inside the Kuma container: the path a Kuma HTTPS monitor would use."""
    host = json.loads(vps.run("docker exec ts-n8n tailscale status --json"))["Self"]["DNSName"].rstrip(".")
    js = (f"fetch('https://{host}/healthz',{{signal:AbortSignal.timeout(20000)}}).then(r=>console.log(r.status))"
          ".catch(e=>console.log('ERR '+(e.cause&&e.cause.code||e.message)))")
    assert vps.run(f"docker exec uptime-kuma node -e \"{js}\"", timeout=60) == "200"


# ---- token-gated workflow round trip (owner tokens needed) ----

SDK_CODE = """
const t = trigger({ type: 'n8n-nodes-base.manualTrigger', version: 1, config: { name: 'Start' } });
const s = node({ type: 'n8n-nodes-base.set', version: 3.4, config: { name: 'Mark', parameters: {
  assignments: { assignments: [{ id: 'a1', name: 'ok', value: 'yes', type: 'string' }] } } } });
export default workflow('deploy-smoke', 'zz-deploy-smoke').add(t.to(s));
""".strip()


def _tool(base, token, session, name, args, rid):
    from test_04_mcp_and_api import _rpc
    r, data = _rpc(base, token, "tools/call", {"name": name, "arguments": args}, session=session, rid=rid)
    assert r.status_code == 200 and data and "result" in data, f"{name} failed"
    res = data["result"]
    if res.get("structuredContent"):
        return res["structuredContent"]
    return json.loads(res["content"][0]["text"])


def test_workflow_round_trip_mcp_create_api_delete(n8n_fqdn_remote, env):
    token, key = pc_env_token(env, "N8N_MCP_TOKEN"), pc_env_token(env, "N8N_API_KEY")
    if not (token and key):
        pytest.skip("N8N_MCP_TOKEN / N8N_API_KEY not in the PC .env yet (owner steps), round trip not run")
    st = pc_tailscale()
    if not st or st.get("BackendState") != "Running":
        pytest.skip("PC Tailscale is not Running")
    base = f"https://{n8n_fqdn_remote}"
    from test_04_mcp_and_api import _rpc
    r, _ = _rpc(base, token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "n8n-deploy-test", "version": "1"}})
    session = r.headers.get("Mcp-Session-Id")
    _rpc(base, token, "notifications/initialized", session=session, rid=None)
    valid = _tool(base, token, session, "validate_workflow", {"code": SDK_CODE}, 2)
    assert valid.get("valid") is True, f"validate_workflow said: {str(valid)[:300]}"
    created = _tool(base, token, session, "create_workflow_from_code", {"code": SDK_CODE, "name": "zz-deploy-smoke"}, 3)
    wid = created.get("workflowId") or created.get("id")
    assert wid, "no workflow id returned"
    h = {"X-N8N-API-KEY": key}
    try:
        got = requests.get(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=45)
        assert got.status_code == 200 and got.json()["name"] == "zz-deploy-smoke"
        upd = _tool(base, token, session, "update_workflow",
                    {"workflowId": wid, "operations": [{"type": "setWorkflowMetadata", "name": "zz-deploy-smoke-2"}]}, 4)
        assert upd, "update returned nothing"
        renamed = requests.get(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=45)
        assert renamed.status_code == 200 and renamed.json()["name"] == "zz-deploy-smoke-2"
    finally:
        d = requests.delete(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=45)
        if d.status_code != 200:  # the review left open whether delete needs archive first
            _tool(base, token, session, "archive_workflow", {"workflowId": wid}, 5)
            d = requests.delete(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=45)
        assert d.status_code == 200, "cleanup delete failed, remove the zz-deploy-smoke workflow by hand"
    assert requests.get(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=45).status_code == 404
