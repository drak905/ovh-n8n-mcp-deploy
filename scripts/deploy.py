"""Deploy n8n + Postgres (Tailscale sidecar, bridge network) to the VPS from the PC. Run from the repo root.

Steps (run in this order):
  python scripts/deploy.py init-env   build the local .env: copies ONLY the OVH_* names and TS_AUTHKEY from project 055
  python scripts/deploy.py audit      read-only checks, saves the host baseline once, creates /opt/n8n
  python scripts/deploy.py stage      copy files, write the VPS .env (mode 600, secrets made on the VPS)
  python scripts/deploy.py sidecar    pull images, start ONLY the sidecar, wait for login, write the n8n URLs from its DNS name
  python scripts/deploy.py up         start n8n and Postgres (and the sidecar)
  python scripts/deploy.py drop-key   remove TS_AUTHKEY from the VPS .env
  python scripts/deploy.py status

The VPS is audit-only: no package manager, no firewall, no Docker daemon settings, no host serve changes, no SSH change.
Only `sudo install -d` of /opt/n8n plus our own files and /etc/cron.d/n8n-backup. Secret values are never printed.
The n8n owner account, MCP token and API key are the owner's steps and are never created here.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from vps import Vps, load_env  # noqa: E402

SRC_ENV = Path(r"E:\002.Github\055.Unifi_OS\.env")
COPY_KEYS = ["OVH_VPS_IP", "OVH_VPS_USER", "OVH_VPS_SSH_PORT", "OVH_SSH_KEY_PATH", "TS_AUTHKEY"]
R = "/opt/n8n"
COMPOSE = f"docker compose -p n8n --env-file {R}/.env -f {R}/docker/compose.yaml"
STAGE = "/tmp/n8n-stage"
EXISTING = "arcane ts-arcane uptime-kuma ts-uptime-kuma vn-stock-scanner"
FILES = {
    "docker/compose.yaml": "docker/compose.yaml",
    "docker/ts-config/ts-serve.json": "docker/ts-config/ts-serve.json",
    "scripts/backup_n8n.sh": "scripts/backup_n8n.sh",
    "scripts/restore_test_n8n.sh": "scripts/restore_test_n8n.sh",
    "scripts/n8n-backup.cron": "scripts/n8n-backup.cron",
}


def conn():
    env = load_env(ROOT / ".env")
    return Vps.from_env(env), env


def init_env():
    """Copy only COPY_KEYS from project 055's .env into the local git-ignored .env. Nothing is printed."""
    src = load_env(SRC_ENV)
    dst_path = ROOT / ".env"
    dst = load_env(dst_path)
    added = []
    for k in COPY_KEYS:
        if k not in dst and src.get(k):
            dst[k] = src[k]
            added.append(k)
    dst.setdefault("TS_HOSTNAME", "n8n")
    dst_path.write_text("".join(f"{k}={v}\n" for k, v in dst.items()), encoding="utf-8", newline="\n")
    print("local .env keys:", " ".join(dst), "| added:", " ".join(added) or "none")


def set_env(v, key, value):
    """Set KEY=value in the VPS .env, keeping mode 600. The value travels on stdin, not in the command."""
    v.run(f"umask 077; {{ grep -v '^{key}=' {R}/.env; cat; }} > /tmp/n8n.env; "
          f"sudo install -m 600 -o ubuntu -g ubuntu /tmp/n8n.env {R}/.env; rm -f /tmp/n8n.env", input=f"{key}={value}\n")


def drop_env(v, key):
    v.run(f"umask 077; grep -v '^{key}=' {R}/.env > /tmp/n8n.env; "
          f"sudo install -m 600 -o ubuntu -g ubuntu /tmp/n8n.env {R}/.env; rm -f /tmp/n8n.env")


def audit(v):
    print(v.run("docker ps --format '{{.Names}} {{.Status}}'; systemctl is-active hermes-agent uosserver tailscaled docker xrdp; "
                "free -m | sed -n 2p; df -h / | tail -1; echo 'listeners on 5678/5432:'; sudo ss -tlnH | grep -cE ':(5678|5432) '; "
                "echo 'docker subnets:'; docker network inspect $(docker network ls -q) | jq -r '.[].IPAM.Config[]?.Subnet' | tr '\\n' ' '; echo; "
                "ls -d /opt/n8n 2>&1 | tr '\\n' ' '; echo; "
                "echo 'wget in tailscale image:'; docker run --rm --entrypoint sh tailscale/tailscale:v1.104.7 -c 'command -v wget'; true"))
    v.run(f"sudo install -d -m 755 -o root -g root {R} && sudo install -d -m 700 -o root -g root {R}/baseline")
    b = f"{R}/baseline"
    v.run(f"test -f {b}/ufw.sha256 || sudo ufw status verbose | sha256sum | cut -d' ' -f1 | sudo tee {b}/ufw.sha256 >/dev/null; "
          f"test -f {b}/host-serve.json || sudo tailscale serve status --json | sudo tee {b}/host-serve.json >/dev/null; "
          f"test -f {b}/host-name.txt || tailscale status --json | jq -r .Self.HostName | sudo tee {b}/host-name.txt >/dev/null; "
          f"test -f {b}/listeners.txt || sudo ss -tlnH | awk '{{print $4}}' | sort -u | sudo tee {b}/listeners.txt >/dev/null; "
          f"test -f {b}/docker-started.txt || systemctl show docker -p ActiveEnterTimestamp --value | sudo tee {b}/docker-started.txt >/dev/null; "
          f"test -f {b}/containers.txt || for n in {EXISTING}; do echo \"$n $(docker inspect -f '{{{{.State.StartedAt}}}}' $n)\"; done | sudo tee {b}/containers.txt >/dev/null; "
          f"test -f {b}/free-before.txt || free -m | sudo tee {b}/free-before.txt >/dev/null; "
          f"test -f {b}/docker-stats-before.txt || docker stats --no-stream --format '{{{{.Name}}}} {{{{.MemUsage}}}}' | sudo tee {b}/docker-stats-before.txt >/dev/null")
    print("baseline saved:", v.run(f"sudo ls {b}").replace("\n", " "))


def stage(v, env):
    v.run(f"rm -rf {STAGE}; mkdir -p {STAGE}/docker/ts-config {STAGE}/scripts")
    for src, dst in FILES.items():
        v.put(str(ROOT / src), f"{STAGE}/{dst}")
    v.run(" && ".join(f"test -s {STAGE}/{d}" for d in FILES.values()))
    # data/ and files/ belong to the n8n user (uid 1000), postgres/ to the postgres user (uid 70 in the alpine image).
    # ts-state/ and backups/ are root-private.
    v.run(f"sudo install -d -m 755 -o root -g root {R} {R}/docker {R}/docker/ts-config {R}/scripts && "
          f"sudo install -d -m 700 -o 1000 -g 1000 {R}/data {R}/files && "
          f"sudo install -d -m 700 -o 70 -g 70 {R}/postgres && "
          f"sudo install -d -m 700 -o root -g root {R}/ts-state {R}/backups && "
          f"sudo install -m 644 -o root -g root {STAGE}/docker/compose.yaml {R}/docker/compose.yaml && "
          f"sudo install -m 644 -o root -g root {STAGE}/docker/ts-config/ts-serve.json {R}/docker/ts-config/ts-serve.json && "
          f"sudo install -m 755 -o root -g root {STAGE}/scripts/backup_n8n.sh {R}/scripts/backup_n8n.sh && "
          f"sudo install -m 755 -o root -g root {STAGE}/scripts/restore_test_n8n.sh {R}/scripts/restore_test_n8n.sh && "
          f"sudo install -m 644 -o root -g root {STAGE}/scripts/n8n-backup.cron /etc/cron.d/n8n-backup && "
          f"bash -n {R}/scripts/backup_n8n.sh && bash -n {R}/scripts/restore_test_n8n.sh && rm -rf {STAGE}")
    # VPS .env: created once, with umask 077. Secrets are generated here and never echoed.
    if v.run(f"test -f {R}/.env && echo yes || echo no") == "no":
        body = f"TS_HOSTNAME={env.get('TS_HOSTNAME', 'n8n')}\nTS_AUTHKEY={env.get('TS_AUTHKEY', '')}\n"
        v.run(f"umask 077; cat > /tmp/n8n.env && printf 'POSTGRES_PASSWORD=%s\\nN8N_ENCRYPTION_KEY=%s\\n' "
              f"\"$(openssl rand -hex 24)\" \"$(openssl rand -hex 32)\" >> /tmp/n8n.env && "
              f"sudo install -m 600 -o ubuntu -g ubuntu /tmp/n8n.env {R}/.env; rm -f /tmp/n8n.env", input=body)
    # Convenience copy of the encryption key into the local git-ignored .env, without printing.
    local = ROOT / ".env"
    if "N8N_ENCRYPTION_KEY" not in load_env(local):
        line = v.run(f"grep '^N8N_ENCRYPTION_KEY=' {R}/.env")
        with open(local, "a", encoding="utf-8", newline="\n") as f:
            f.write(line.strip() + "\n")
    print("stage ok; .env mode", v.run(f"stat -c %a {R}/.env"), "; keys:", v.run(f"cut -d= -f1 {R}/.env | tr '\\n' ' '"))


def sidecar_log(v):
    return v.run("docker logs ts-n8n --tail 60 2>&1 | sed -E 's/tskey-[A-Za-z0-9-]+/tskey-REDACTED/g'", check=False)


def sidecar(v):
    v.run(f"{COMPOSE} pull", timeout=1200)
    v.run(f"{COMPOSE} up -d --no-deps ts-n8n", timeout=120)
    st = {}
    for i in range(30):
        out = v.run("docker exec ts-n8n tailscale status --json", check=False)
        try:
            st = json.loads(out)
        except ValueError:
            st = {}
        if st.get("BackendState") == "Running" and (st.get("Self") or {}).get("DNSName"):
            break
        log = sidecar_log(v)
        if i > 6 and re.search(r"invalid key|key.*(expired|revoked|invalid)|unauthorized|requires.*(auth|login)", log, re.I) \
                and st.get("BackendState") in {"NeedsLogin", None, ""}:
            print("AUTH KEY PROBLEM. Log tail:\n" + log[-1500:])
            return False
        time.sleep(4)
    else:
        print("sidecar not Running after 2 min. Log tail:\n" + sidecar_log(v)[-1500:])
        return False
    fqdn = st["Self"]["DNSName"].rstrip(".")
    set_env(v, "N8N_HOST", fqdn)
    set_env(v, "N8N_EDITOR_BASE_URL", f"https://{fqdn}")
    set_env(v, "N8N_WEBHOOK_URL", f"https://{fqdn}/")
    print("sidecar logged in; node:", st["Self"]["HostName"], "; N8N_HOST, N8N_EDITOR_BASE_URL, N8N_WEBHOOK_URL written")
    return True


def up(v):
    if v.run(f"grep -c '^N8N_WEBHOOK_URL=https://' {R}/.env; true").splitlines()[-1] != "1":
        sys.exit("N8N_WEBHOOK_URL missing: run the sidecar step first")
    v.run(f"{COMPOSE} up -d", timeout=600)
    print(v.run("docker ps --filter name=n8n --filter name=ts-n8n --format '{{.Names}} {{.Status}}'"))


def drop_key(v):
    drop_env(v, "TS_AUTHKEY")
    print("keys now:", v.run(f"cut -d= -f1 {R}/.env | tr '\\n' ' '"))


def status(v):
    print(v.run(f"{COMPOSE} ps; docker exec ts-n8n tailscale serve status 2>&1 | head -5; true"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init-env":
        init_env()
        sys.exit(0)
    v, env = conn()
    if cmd == "audit":
        audit(v)
    elif cmd == "stage":
        stage(v, env)
    elif cmd == "sidecar":
        sys.exit(0 if sidecar(v) else 2)
    elif cmd == "up":
        up(v)
    elif cmd == "drop-key":
        drop_key(v)
    elif cmd == "status":
        status(v)
    else:
        sys.exit("unknown command")
