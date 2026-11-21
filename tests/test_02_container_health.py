"""Tier 2: the deployed stack on the VPS, and proof that the other apps and the host were not changed."""
import json
import re

import pytest

from conftest import EXISTING, REMOTE, inspect

MINE = ["ts-n8n", "n8n", "n8n-postgres"]


@pytest.mark.parametrize("name", MINE)
def test_container_running_healthy(vps, name):
    c = inspect(vps, name)
    assert c["State"]["Status"] == "running"
    assert c["State"]["Health"]["Status"] == "healthy"
    assert c["RestartCount"] <= 1
    assert c["HostConfig"]["Privileged"] is False


def test_images_are_the_pinned_tags(vps):
    assert inspect(vps, "n8n")["Config"]["Image"] == "docker.n8n.io/n8nio/n8n:2.48.7"
    assert inspect(vps, "n8n-postgres")["Config"]["Image"] == "postgres:16.16-alpine"
    assert inspect(vps, "ts-n8n")["Config"]["Image"] == "tailscale/tailscale:v1.104.7"


def test_compose_project_name(vps):
    out = vps.run("docker inspect -f '{{index .Config.Labels \"com.docker.compose.project\"}}' ts-n8n n8n n8n-postgres")
    assert set(out.split()) == {"n8n"}


def test_updater_labels_present(vps):
    for n in MINE:
        labels = inspect(vps, n)["Config"]["Labels"]
        assert labels["com.getarcaneapp.arcane.updater"] == "false", n
        assert labels["com.getarcaneapp.arcane.update-check"] == "false", n


def test_memory_limits_and_no_extra_swap(vps):
    mb = 1024 * 1024
    for name, lim in {"ts-n8n": 256, "n8n": 1536, "n8n-postgres": 512}.items():
        hc = inspect(vps, name)["HostConfig"]
        assert hc["Memory"] == lim * mb, name
        assert hc["MemorySwap"] == lim * mb, name


def test_bind_mounts_private_and_on_local_disk(vps):
    mounts = {x["Destination"]: x["Source"] for x in inspect(vps, "n8n")["Mounts"]}
    assert mounts == {"/home/node/.n8n": f"{REMOTE}/data"}
    pg = {x["Destination"]: x["Source"] for x in inspect(vps, "n8n-postgres")["Mounts"]}
    assert pg == {"/var/lib/postgresql/data": f"{REMOTE}/postgres"}
    assert vps.run(f"stat -f -c %T {REMOTE}/data") not in {"nfs", "nfs4", "cifs", "smb2", "fuseblk"}
    assert vps.run(f"stat -c '%a %u' {REMOTE}/data") == "700 1000"
    assert vps.run(f"sudo stat -c '%a' {REMOTE}/postgres") == "700"
    assert vps.run(f"sudo stat -c '%a %U' {REMOTE}/ts-state {REMOTE}/backups").splitlines() == ["700 root", "700 root"]


def test_no_docker_socket_anywhere(vps):
    for n in MINE:
        assert all("docker.sock" not in x["Source"] for x in inspect(vps, n)["Mounts"]), n


def test_network_subnet_unique_and_members(vps):
    nets = json.loads(vps.run("docker network inspect $(docker network ls -q)"))
    mine = [n for n in nets if n["Name"] == "n8n_net"]
    assert mine, "n8n_net missing"
    subs = [c["Subnet"] for n in nets for c in (n["IPAM"]["Config"] or [])]
    assert len(subs) == len(set(subs)), "duplicate docker subnet"
    assert mine[0]["IPAM"]["Config"][0]["Subnet"] == "172.21.0.0/24"
    assert {c["Name"] for c in mine[0]["Containers"].values()} == set(MINE)


def test_vps_env_file(vps):
    assert vps.run(f"stat -c '%a %U' {REMOTE}/.env") == "600 ubuntu"
    keys = set(vps.run(f"cut -d= -f1 {REMOTE}/.env").split())
    assert {"N8N_ENCRYPTION_KEY", "POSTGRES_PASSWORD", "TS_HOSTNAME", "N8N_HOST", "N8N_EDITOR_BASE_URL",
            "N8N_WEBHOOK_URL"} <= keys
    for k in ("N8N_ENCRYPTION_KEY", "POSTGRES_PASSWORD"):  # length only, never the value
        assert int(vps.run(f"grep '^{k}=' {REMOTE}/.env | cut -d= -f2- | tr -d '\\n' | wc -c")) >= 32, k
    assert vps.run(f"grep -c '^N8N_WEBHOOK_URL=https://[a-z0-9-]*\\.[a-z0-9-]*\\.ts\\.net/$' {REMOTE}/.env; true").splitlines()[-1] == "1"
    assert vps.run(f"grep -c '^N8N_EDITOR_BASE_URL=https://[a-z0-9-]*\\.[a-z0-9-]*\\.ts\\.net$' {REMOTE}/.env; true").splitlines()[-1] == "1"


def test_vps_env_has_unix_line_endings(vps):
    assert vps.run(f"tr -cd '\\r' < {REMOTE}/.env | wc -c") == "0"


def test_urls_match_sidecar_name(vps, n8n_fqdn):
    assert vps.run(f"grep -c '^N8N_HOST={n8n_fqdn}$' {REMOTE}/.env; true").splitlines()[-1] == "1"
    assert vps.run(f"grep -c '^N8N_EDITOR_BASE_URL=https://{n8n_fqdn}$' {REMOTE}/.env; true").splitlines()[-1] == "1"
    assert vps.run(f"grep -c '^N8N_WEBHOOK_URL=https://{n8n_fqdn}/$' {REMOTE}/.env; true").splitlines()[-1] == "1"


def test_n8n_container_got_the_right_settings(vps):
    assert vps.run("docker exec n8n printenv N8N_PROXY_HOPS") == "1"
    assert vps.run("docker exec n8n printenv N8N_PROTOCOL") == "https"
    assert vps.run("docker exec n8n printenv N8N_WEBHOOK_URL").startswith("https://")
    assert vps.run("docker exec n8n sh -c 'printenv WEBHOOK_URL; true'") == ""
    assert vps.run("docker exec n8n sh -c 'test -n \"$N8N_ENCRYPTION_KEY\" && echo set'") == "set"


def test_sidecar_online_with_expected_name(vps, sidecar_status):
    assert sidecar_status.get("BackendState") == "Running"
    me = sidecar_status["Self"]
    assert me["Online"] is True
    assert me["HostName"] == "n8n"
    assert json.loads(vps.run("tailscale status --json"))["Self"]["HostName"] != me["HostName"]


def test_sidecar_serve_maps_https_to_n8n_service(vps):
    cfg = json.loads(vps.run("docker exec ts-n8n tailscale serve status --json"))
    assert cfg["TCP"]["443"]["HTTPS"] is True
    proxies = [h["Proxy"] for w in cfg["Web"].values() for h in w["Handlers"].values()]
    assert proxies == ["http://n8n:5678"]
    assert not cfg.get("AllowFunnel") or not any(cfg["AllowFunnel"].values())


def test_sidecar_reaches_n8n_by_name(vps):
    rc = vps.run("docker exec ts-n8n wget -q -T 8 -O /dev/null http://n8n:5678/healthz ; echo $?").splitlines()[-1]
    assert rc == "0"


def test_n8n_healthz_inside(vps):
    js = "fetch('http://127.0.0.1:5678/healthz').then(r=>console.log(r.status))"
    assert vps.run(f"docker exec n8n node -e \"{js}\"") == "200"


def test_n8n_uses_postgres_and_migrations_ran(vps):
    n = vps.run("docker exec n8n-postgres psql -U n8n -d n8n -Atc \"select count(*) from information_schema.tables where table_schema='public'\"")
    assert int(n) > 20
    assert vps.run("docker exec n8n-postgres psql -U n8n -d n8n -Atc \"select to_regclass('public.workflow_entity') is not null\"") == "t"


def test_logs_clean(vps):
    logs = vps.run("docker logs n8n 2>&1 | tail -n 400", check=False)
    assert logs.strip(), "no logs"
    assert not re.search(r"permission denied|ECONNREFUSED|password authentication failed", logs, re.I)
    assert "WEBHOOK_URL" not in logs or "deprecat" not in logs.lower()


def test_n8n_page_over_tailnet_tls(vps, n8n_fqdn, n8n_ip):
    """Real TLS check from the VPS: the host reaches the sidecar tailnet IP, certificate verified by name."""
    out = vps.run(f"curl -sS --max-time 40 --retry 3 --retry-delay 5 --resolve {n8n_fqdn}:443:{n8n_ip} "
                  f"-o /dev/null -w '%{{http_code}} %{{ssl_verify_result}}' https://{n8n_fqdn}/healthz")
    code, verify = out.split()
    assert code == "200"
    assert verify == "0"


# ---- Isolation: nothing on the host, nothing new public ----

def test_no_published_ports(vps):
    assert vps.run("docker port ts-n8n; docker port n8n; docker port n8n-postgres; true") == ""


@pytest.mark.parametrize("port", [5678, 5432])
def test_ports_closed_on_host_and_tailnet_ip(vps, n8n_ip, port):
    assert vps.run(f"sudo ss -tlnpH | grep -c ':{port} ' ; true").splitlines()[-1] == "0"
    rc = vps.run(f"curl -sS --max-time 5 -o /dev/null http://{n8n_ip}:{port}/ ; echo $?").splitlines()[-1]
    assert rc != "0", f"port {port} answers on the tailnet IP"


def test_no_funnel_on_host_or_sidecar(vps):
    host = vps.run("sudo tailscale serve status --json")
    assert '"AllowFunnel":true' not in host.replace(" ", "")


# ---- Coexistence: baseline taken by `deploy.py audit` before anything was changed ----

def test_ufw_unchanged(vps):
    base = vps.run(f"sudo cat {REMOTE}/baseline/ufw.sha256")
    now = vps.run("sudo ufw status verbose | sha256sum | cut -d' ' -f1")
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    assert now == base


def test_host_serve_unchanged(vps):
    baseline = json.loads(vps.run(f"sudo cat {REMOTE}/baseline/host-serve.json"))
    now = json.loads(vps.run("sudo tailscale serve status --json"))
    assert now == baseline
    assert "443" in now["TCP"] and "3389" in now["TCP"]
    assert "5678" not in now["TCP"]


def test_no_new_listeners(vps):
    base = set(vps.run(f"sudo cat {REMOTE}/baseline/listeners.txt").split())
    now = set(vps.run("sudo ss -tlnH | awk '{print $4}' | sort -u").split())
    new = {a for a in now - base if not a.startswith(("127.0.0.1:", "[::1]:"))}
    assert not new, f"new non-loopback listeners: {len(new)}"


def test_existing_apps_running_and_not_restarted(vps):
    base = dict(l.split(" ", 1) for l in vps.run(f"sudo cat {REMOTE}/baseline/containers.txt").splitlines())
    assert set(base) == set(EXISTING)
    for n in EXISTING:
        now = vps.run(f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.StartedAt}}}}' {n}")
        assert now.startswith("running"), n
        assert now.split(" ", 1)[1] == base[n], f"{n} was restarted"
    assert vps.run("docker inspect -f '{{.State.Health.Status}}' uptime-kuma ts-uptime-kuma arcane ts-arcane").split() == ["healthy"] * 4


def test_other_services_active(vps):
    for svc in ("hermes-agent", "uosserver", "tailscaled", "docker", "fail2ban", "xrdp"):
        assert vps.run(f"systemctl is-active {svc}", check=False) == "active", svc


def test_docker_daemon_not_restarted_and_no_daemon_json(vps):
    base = vps.run(f"sudo cat {REMOTE}/baseline/docker-started.txt")
    assert vps.run("systemctl show docker -p ActiveEnterTimestamp --value") == base
    assert vps.run("test -e /etc/docker/daemon.json && echo yes || echo no") == "no"


def test_host_node_name_unchanged(vps):
    base = vps.run(f"sudo cat {REMOTE}/baseline/host-name.txt")
    assert json.loads(vps.run("tailscale status --json"))["Self"]["HostName"] == base


def test_memory_headroom(vps):
    assert int(vps.run("free -m | awk '/^Mem:/{print $7}'")) >= 1000, "less than 1 GB available"
    assert int(vps.run("df -m / | awk 'NR==2{print $4}'")) >= 10000

