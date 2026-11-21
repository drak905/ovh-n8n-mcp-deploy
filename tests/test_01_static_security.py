"""Tier 1: static checks on the repo. No VPS needed, except the real-value leak scan."""
import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, git_files

COMPOSE = ROOT / "docker" / "compose.yaml"
SERVE = ROOT / "docker" / "ts-config" / "ts-serve.json"
SCRIPTS = ROOT / "scripts"
N8N_IMAGE = "docker.n8n.io/n8nio/n8n:2.48.7"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _labels(s):
    labels = s.get("labels", {})
    return dict(x.split("=", 1) for x in labels) if isinstance(labels, list) else labels


def test_compose_name_and_services(compose):
    assert compose["name"] == "n8n"
    assert set(compose["services"]) == {"ts-n8n", "n8n", "postgres"}


def test_images_pinned_exact(compose):
    s = compose["services"]
    assert s["n8n"]["image"] == N8N_IMAGE
    assert s["postgres"]["image"] == "postgres:16.16-alpine"
    assert s["ts-n8n"]["image"] == "tailscale/tailscale:v1.104.7"
    text = COMPOSE.read_text(encoding="utf-8")
    assert not re.search(r"image:.*:(latest|stable|next|beta)\s*$", text, re.M)
    for svc in s.values():
        assert re.search(r":\d+\.\d+\.\d+|:v\d+\.\d+\.\d+|:\d+\.\d+-", svc["image"]), svc["image"]


def test_no_published_ports(compose):
    for name, s in compose["services"].items():
        assert "ports" not in s, f"{name} publishes ports"


def test_no_caddy_no_public_edge(compose):
    assert "caddy" not in json.dumps(compose).lower()
    assert "443" not in json.dumps({k: v.get("ports") for k, v in compose["services"].items()})


def test_network_bridge_with_fixed_safe_subnet(compose):
    for name, s in compose["services"].items():
        assert "network_mode" not in s, name
        nets = s["networks"] if isinstance(s["networks"], list) else list(s["networks"])
        assert nets == ["n8n-net"], name
    net = compose["networks"]["n8n-net"]
    assert net["name"] == "n8n_net"
    assert net.get("driver", "bridge") == "bridge"
    subnet = ipaddress.ip_network(net["ipam"]["config"][0]["subnet"])
    assert str(subnet) == "172.21.0.0/24"
    for used in ("172.17.0.0/16", "172.18.0.0/16", "172.19.0.0/16", "172.20.0.0/24", "100." + "64.0.0/10", "10.88.0.0/16"):
        assert not subnet.overlaps(ipaddress.ip_network(used)), used


def test_sidecar_has_no_hostname_that_shadows_the_n8n_service(compose):
    ts = compose["services"]["ts-n8n"]
    assert "hostname" not in ts
    assert ts["environment"]["TS_HOSTNAME"].startswith("${TS_HOSTNAME:-n8n")
    assert str(ts["environment"]["TS_ACCEPT_DNS"]).lower() == "false"
    assert str(ts["environment"]["TS_USERSPACE"]).lower() == "false"
    assert ts["environment"]["TS_SERVE_CONFIG"] == "/config/ts-serve.json"
    assert ts["environment"]["TS_LOCAL_ADDR_PORT"] == "127.0.0.1:9002"
    assert "./ts-config:/config:ro" in ts["volumes"]
    assert "/opt/n8n/ts-state:/var/lib/tailscale" in ts["volumes"]


def test_least_privilege(compose):
    text = COMPOSE.read_text(encoding="utf-8")
    assert "SYS_MODULE" not in text and "docker.sock" not in text
    for name, s in compose["services"].items():
        assert not s.get("privileged"), name
        assert s.get("network_mode") != "host", name
        assert s.get("pid") != "host", name
        assert "cgroup" not in s, name
    assert compose["services"]["ts-n8n"]["cap_add"] == ["NET_ADMIN"]
    assert compose["services"]["ts-n8n"]["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert "cap_add" not in compose["services"]["n8n"] and "cap_add" not in compose["services"]["postgres"]


def test_ordering_and_healthchecks(compose):
    s = compose["services"]
    assert s["n8n"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert s["n8n"]["depends_on"]["ts-n8n"]["condition"] == "service_healthy"
    n8n_hc = s["n8n"]["healthcheck"]["test"]
    assert n8n_hc[0] == "CMD" and n8n_hc[1] == "node"
    assert "127.0.0.1:5678/healthz" in " ".join(n8n_hc)
    assert s["postgres"]["healthcheck"]["test"][:2] == ["CMD", "pg_isready"]
    pg = " ".join(s["postgres"]["healthcheck"]["test"])
    assert "-U" in pg and "-d" in pg and "-h" not in pg
    assert "9002/healthz" in " ".join(s["ts-n8n"]["healthcheck"]["test"])


def test_n8n_environment(compose):
    e = compose["services"]["n8n"]["environment"]
    assert e["DB_TYPE"] == "postgresdb"
    assert e["DB_POSTGRESDB_HOST"] == "postgres"
    assert e["DB_POSTGRESDB_PASSWORD"].startswith("${POSTGRES_PASSWORD")
    assert e["N8N_ENCRYPTION_KEY"].startswith("${N8N_ENCRYPTION_KEY")
    assert "WEBHOOK_URL" not in e, "deprecated name, use N8N_WEBHOOK_URL"
    assert e["N8N_WEBHOOK_URL"].startswith("${N8N_WEBHOOK_URL")
    assert e["N8N_EDITOR_BASE_URL"].startswith("${N8N_EDITOR_BASE_URL")
    assert e["N8N_HOST"].startswith("${N8N_HOST")
    assert str(e["N8N_PROTOCOL"]) == "https"
    assert str(e["N8N_PROXY_HOPS"]) == "1"
    assert str(e["N8N_PORT"]) == "5678"
    assert e["GENERIC_TIMEZONE"] == "Asia/Ho_Chi_Minh" and e["TZ"] == "Asia/Ho_Chi_Minh"
    assert str(e["EXECUTIONS_DATA_PRUNE"]).lower() == "true"
    assert str(e["EXECUTIONS_DATA_MAX_AGE"]) == "168"
    assert e["NODE_OPTIONS"] == "--max-old-space-size=1100"
    for off in ("N8N_DIAGNOSTICS_ENABLED", "N8N_VERSION_NOTIFICATIONS_ENABLED", "N8N_PERSONALIZATION_ENABLED"):
        assert str(e[off]).lower() == "false", off
    assert "N8N_SSL_KEY" not in e and "N8N_SSL_CERT" not in e
    assert "N8N_RUNNERS_ENABLED" not in e
    assert "N8N_SECURE_COOKIE" not in e or str(e["N8N_SECURE_COOKIE"]).lower() == "true"


def test_postgres_settings(compose):
    pg = compose["services"]["postgres"]
    cmd = " ".join(pg["command"]) if isinstance(pg["command"], list) else pg["command"]
    assert "shared_buffers=128MB" in cmd and "max_connections=50" in cmd
    assert pg["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD")
    assert pg["environment"]["POSTGRES_USER"] == "n8n" and pg["environment"]["POSTGRES_DB"] == "n8n"


def test_bind_mounts_under_opt_n8n(compose):
    s = compose["services"]
    assert "/opt/n8n/data:/home/node/.n8n" in s["n8n"]["volumes"]
    assert "/opt/n8n/postgres:/var/lib/postgresql/data" in s["postgres"]["volumes"]
    assert "volumes" not in compose, "named volumes would make backups depend on the project name"


def test_updater_labels_on_all(compose):
    for name, s in compose["services"].items():
        labels = _labels(s)
        assert str(labels["com.getarcaneapp.arcane.updater"]).lower() == "false", name
        assert str(labels["com.getarcaneapp.arcane.update-check"]).lower() == "false", name


def test_resource_and_log_limits(compose):
    want = {"ts-n8n": "256m", "n8n": "1536m", "postgres": "512m"}
    for name, lim in want.items():
        s = compose["services"][name]
        assert s["mem_limit"] == lim, name
        assert s["memswap_limit"] == lim, name
        assert s["restart"] == "unless-stopped"
        assert s["logging"]["options"]["max-size"] == "10m"
        assert str(s["logging"]["options"]["max-file"]) == "5"
        assert "deploy" not in s, "use mem_limit like the other stacks"


def test_container_names(compose):
    names = {k: v["container_name"] for k, v in compose["services"].items()}
    assert names == {"ts-n8n": "ts-n8n", "n8n": "n8n", "postgres": "n8n-postgres"}


def test_serve_json_proxies_to_n8n_service_and_never_funnel():
    cfg = json.loads(SERVE.read_text(encoding="utf-8"))
    assert cfg["TCP"]["443"]["HTTPS"] is True
    web = cfg["Web"]
    assert list(web) == ["${TS_CERT_DOMAIN}:443"]
    assert web["${TS_CERT_DOMAIN}:443"]["Handlers"]["/"]["Proxy"] == "http://n8n:5678"
    assert cfg["AllowFunnel"] == {"${TS_CERT_DOMAIN}:443": False}


def test_gitignore_blocks_secrets_and_state():
    probes = [".env", ".env.local", "a.key", "a.pem", "database.sqlite", "ts-state/x", "backups/x", "data/x",
              "postgres/x", "reports/x", "x.tar.gz", "x.dump", "n8n-key.txt", ".n8n-cli/config.json",
              "tailscale-state/x", "my-secret.txt", "my-credentials.json"]
    for p in probes:
        r = subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT)
        assert r.returncode == 0, f"{p} is not ignored"
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode == 1


def test_env_never_tracked():
    assert ".env" not in {p.name for p in git_files()}


def test_line_endings_lf_and_gitattributes():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "eol=lf" in attrs
    cfg = subprocess.run(["git", "config", "--local", "core.autocrlf"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert cfg == "false"
    bad = []
    for p in list(ROOT.glob("docker/**/*")) + list(ROOT.glob("scripts/*")) + list(ROOT.glob("tests/*.py")) + list(ROOT.glob("*.toml")) + [ROOT / "README.md"]:
        if p.is_file() and p.suffix in {".sh", ".yaml", ".json", ".cron", ".py", ".toml", ".md"} and b"\r" in p.read_bytes():
            bad.append(p.name)
    assert not bad, f"CR found in {bad}"


def test_backup_script_hardening():
    sh = SCRIPTS / "backup_n8n.sh"
    text = sh.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    for needle in ("umask 077", "mktemp -d", "trap ", "flock", "-mtime", "chmod 600", "pg_dump", "-Fc", "pg_restore -l",
                   "tar -tzf", ".partial", "-name 'n8n-*.tar.gz'", "--exclude=./config"):
        assert needle in text, needle
    assert "N8N_ENCRYPTION_KEY" not in text and ".env" not in text, "the key must never go into a backup"
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", str(sh)], capture_output=True).returncode == 0


def test_restore_test_script():
    sh = SCRIPTS / "restore_test_n8n.sh"
    text = sh.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "n8n_restore_test" in text and "DROP DATABASE" in text and "pg_restore" in text
    assert "--clean" not in text, "the test must never touch the live database"
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", str(sh)], capture_output=True).returncode == 0


def test_cron_file():
    c = (SCRIPTS / "n8n-backup.cron").read_text(encoding="utf-8")
    assert re.search(r"^40 3 \* \* \* root /opt/n8n/scripts/backup_n8n\.sh", c, re.M)
    assert c.endswith("\n") and "\r" not in c


def test_scripts_stay_in_scope():
    """No package manager, firewall, daemon.json, prune, Docker restart, or other stacks' compose projects."""
    forbidden = [r"apt(-get)?\s+(install|upgrade|update)", r"\bufw\s+(allow|deny|enable|default|delete|reload|--force)",
                 r"daemon\.json\s*[>|]", r"\bprune\b", r"docker\s+system", r"unattended", r"winget", r"paramiko",
                 r"docker\s+compose\b[^\n]*\sconfig(\s|$)", r"kuma\.db", r"gh\s+repo\s+create", r"git\s+push",
                 r"systemctl\s+(restart|stop|reload)\s+docker", r"fail2ban", r"\bfallocate\b|\bmkswap\b", r"sshd_config"]
    for p in SCRIPTS.glob("*"):
        if p.suffix == ".pyc" or p.is_dir():
            continue
        text = p.read_text(encoding="utf-8")
        for pat in forbidden:
            assert not re.search(pat, text), f"{p.name} matches {pat}"
        for line in text.splitlines():
            if re.search(r"compose", line):
                assert not re.search(r"uptime-kuma|vn-stock-scanner|-p arcane", line), f"{p.name}: compose touches another stack"


def test_no_forbidden_files():
    for p in ROOT.rglob("*"):
        if ".git" in p.parts or ".venv" in p.parts or "__pycache__" in p.parts or ".pytest_cache" in p.parts:
            continue
        low = p.name.lower()
        assert "caddy" not in low and "harden" not in low, p.name


def test_env_example_has_no_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("TS_AUTHKEY=", "N8N_ENCRYPTION_KEY=", "POSTGRES_PASSWORD=", "N8N_API_KEY=", "N8N_MCP_TOKEN=")):
            assert line.endswith("="), "example must hold no value"
    for k in ("N8N_API_KEY=", "N8N_MCP_TOKEN=", "N8N_URL="):
        assert k in text


def test_readme_covers_owner_steps_and_limits():
    t = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("owner", "MCP", "n8n-cli", "Funnel", "Cloudflare", "encryption key", "delete", "--header", "OAuth",
                   "restore", "tailnet"):
        assert needle.lower() in t.lower(), needle


def _gitleaks(*args):
    exe = shutil.which("gitleaks")
    if not exe:
        pytest.skip("gitleaks not installed")
    return subprocess.run([exe, *args, "--no-banner", "--redact", "-c", str(ROOT / ".gitleaks.toml")],
                          cwd=ROOT, capture_output=True, text=True)


def test_gitleaks_working_tree_clean():
    assert _gitleaks("dir", ".").returncode == 0, "gitleaks found something in the working tree"


def test_gitleaks_history_clean():
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("no commits yet")
    assert _gitleaks("git", ".").returncode == 0, "gitleaks found something in history"


def _scan_text(text):
    exe = shutil.which("gitleaks")
    if not exe:
        pytest.skip("gitleaks not installed")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "x.txt").write_text(text + "\n", encoding="utf-8")
        return subprocess.run([exe, "dir", d, "--no-banner", "--redact", "-c", str(ROOT / ".gitleaks.toml")],
                              capture_output=True, text=True).returncode


def test_gitleaks_rules_catch_sample_leaks():
    """Positive control: strings are built at run time so this file itself stays clean."""
    blob = "0123456789abcdef" * 4
    jwt = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghij1234567890"
    samples = [
        "tskey-" + "auth-" + "kABCDEF1234567-" + "abcdefghijklmnopqrstuvwxyz",
        "host: tail" + "1234.ts" + ".net",
        "ip: 100." + "101.12.34",
        "ip6: fd7a:" + "115c:a1e0:ab12::1",
        "N8N_ENCRYPTION" + "_KEY=" + blob,
        "N8N_ENCRYPTION" + "_KEY: '" + blob + "'",
        "POSTGRES_" + "PASSWORD=" + "Abcdef0123456789Abcdef",
        "DB_POSTGRESDB_" + "PASSWORD: Abc" + "def0123456789Abcdef",
        "N8N_API" + "_KEY=" + "abcdefghij1234567890abcd",
        "N8N_MCP" + "_TOKEN=" + "abcdefghij1234567890abcd",
        "token " + jwt,
    ]
    for s in samples:
        assert _scan_text(s) == 1, "a rule did not fire for a sample"


def test_gitleaks_allows_placeholders():
    ok = ("https://n8n.<tailnet>.ts" + ".net and https://example.ts" + ".net and 100.100.100.100\n"
          "N8N_ENCRYPTION" + "_KEY: ${N8N_ENCRYPTION" + "_KEY:?missing}\n"
          "POSTGRES_" + "PASSWORD: ${POSTGRES_" + "PASSWORD:?missing}\n"
          "DB_POSTGRESDB_" + "PASSWORD: ${POSTGRES_" + "PASSWORD:?missing}\n"
          "N8N_API" + "_KEY=\nN8N_MCP" + "_TOKEN=\n")
    assert _scan_text(ok) == 0


def test_real_values_absent_from_tracked_files(env, vps):
    """Real VPS IP, tailnet suffix, account name, keys and passwords must not appear in tracked files."""
    needles = {"vps_ip": env.get("OVH_VPS_IP", ""), "enc_key": env.get("N8N_ENCRYPTION_KEY", ""),
               "authkey": env.get("TS_AUTHKEY", ""), "pg_pw": env.get("POSTGRES_PASSWORD", ""),
               "api_key": env.get("N8N_API_KEY", ""), "mcp_token": env.get("N8N_MCP_TOKEN", "")}
    try:
        st = json.loads(vps.run("tailscale status --json", timeout=40))
        needles["tailnet_suffix"] = (st.get("MagicDNSSuffix") or "").strip()
        needles["tailnet_name"] = ((st.get("CurrentTailnet") or {}).get("Name") or "").strip()
        uid = str((st.get("Self") or {}).get("UserID", ""))
        needles["account"] = (((st.get("User") or {}).get(uid) or {}).get("LoginName") or "").strip()
        needles["host_ip"] = next(iter((st.get("Self") or {}).get("TailscaleIPs") or []), "")
    except Exception:
        pytest.skip("VPS not reachable, could only check the IP")
    needles = {k: v for k, v in needles.items() if len(v) >= 4}
    assert needles.get("vps_ip"), "no VPS IP to check"
    hits = []
    for p in git_files():
        if not p.is_file():
            continue
        data = p.read_text(encoding="utf-8", errors="ignore").lower()
        for k, v in needles.items():
            if v.lower() in data:
                hits.append(f"{p.name}:{k}")
    assert not hits, f"real values found: {hits}"
