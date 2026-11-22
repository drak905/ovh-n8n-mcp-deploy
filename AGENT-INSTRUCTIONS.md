# Agent instructions: connect to the owner's n8n and manage workflows

You are an AI agent CLI. The owner's n8n runs on a private network (Tailscale). Follow this file from top to bottom.
Do the steps in order. Where a step says STOP, stop and ask the owner. Do not work around it.

Placeholders: `<N8N_URL>` is the base address, for example `https://n8n.<tailnet>.ts.net`. Never write the real value
into this repo, a chat reply, a log or a commit. Read it from the secrets file (step 2). `<ID>` is a workflow id.

## 0. What you can and cannot do

| Task | Use |
|---|---|
| Build, edit, validate, read, run, test, publish, unpublish, archive workflows | MCP server (`<N8N_URL>/mcp-server/http`) |
| Read workflows as raw JSON, list, delete | REST API (`/api/v1/...`, header `X-N8N-API-KEY`) or `npx @n8n/cli` |
| Delete a workflow | REST or `npx @n8n/cli` only. **MCP has no delete tool.** |
| Receive webhooks from the internet (Telegram, GitHub, Stripe) | Not possible. n8n is tailnet-only. Schedule, manual and MCP/API runs work. |
| Touch Docker, the VPS, n8n settings, users, credentials, the encryption key | **Never.** |

## 1. Preconditions (check these yourself, in this order)

1. Tailscale is connected: run `tailscale status`. The first lines must list this PC and not say `Stopped` or
   `Logged out`. If it is not running, STOP and ask the owner to turn Tailscale on.
2. No proxy is in the way. The helper script in step 5 ignores proxies on purpose. If you use `curl`, `Invoke-WebRequest`
   or a browser instead, remove any `HTTP_PROXY` / `HTTPS_PROXY` and any VPN or proxy extension first.
3. The server answers: `GET <N8N_URL>/healthz` returns HTTP 200. The helper does this for you. If it fails, use the
   troubleshooting table (step 10). Do not retry in a loop. Two tries is enough.

## 2. Secrets: where they are and how to handle them

- File: `E:\002.Github\058.N8N\.env`. It is git-ignored. It holds more than you need (VPS address, SSH key path,
  Tailscale and encryption keys). **Never open, print or paste the whole file.** Read only the three names below.
- The three values:
  - URL: the owner's file uses the name `N8N_Server_URL`. Other setups use `N8N_URL`. Accept either; if both exist,
    `N8N_URL` wins. The owner's value may be the full MCP address ending in `/mcp-server/http`. Cut that tail off to get
    the base `<N8N_URL>` (tools below do this for you).
  - `N8N_MCP_TOKEN`: bearer token for MCP.
  - `N8N_API_KEY`: key for the REST API and `npx @n8n/cli`.
- Rules:
  1. Load them into the **process environment** only (snippet below, or the helper in step 5).
  2. Never print, echo, log, commit, paste or put them on a command line. Check that a value exists by its length only.
  3. Never write them into a config file. Config files in this document refer to environment variable names.
  4. If a value is missing (length 0), or any call returns 401 or 403: STOP and ask the owner. Do not guess, rotate or
     create tokens. Do not look for other copies of the secrets.

Load into a PowerShell session (prints lengths only):

```powershell
$f = 'E:\002.Github\058.N8N\.env'
foreach ($l in Get-Content $f) {
  if ($l -match '^\s*(N8N_MCP_TOKEN|N8N_API_KEY|N8N_URL|N8N_Server_URL)\s*=\s*(.*?)\s*$') {
    $n = $Matches[1]; if ($n -eq 'N8N_Server_URL' -and $env:N8N_URL) { continue }
    if ($n -eq 'N8N_Server_URL') { $n = 'N8N_URL' }
    [Environment]::SetEnvironmentVariable($n, $Matches[2].Trim('"', "'"), 'Process')
  }
}
$env:N8N_URL = $env:N8N_URL -replace '/mcp-server/http/?$', '' -replace '/$', ''
"N8N_URL=$($env:N8N_URL.Length) N8N_MCP_TOKEN=$($env:N8N_MCP_TOKEN.Length) N8N_API_KEY=$($env:N8N_API_KEY.Length)"
```

Any length of 0 means STOP. The variables live only in that shell process. Start your CLI from the same shell if
the CLI needs them (steps 3 and 4).

## 3. Setup for kiro-cli (installed on this PC)

The agent config file is `C:\Users\thinh\.kiro\agents\n8n.json`. It must look like this (the `url` has the real
`<N8N_URL>` plus `/mcp-server/http`; the file holds no secret):

```json
{
  "name": "n8n",
  "description": "Builds and edits n8n workflows through the instance-level MCP server (tailnet only).",
  "prompt": null,
  "mcpServers": {
    "n8n": {
      "type": "http",
      "url": "<N8N_URL>/mcp-server/http",
      "headers": { "Authorization": "Bearer ${N8N_MCP_TOKEN}" }
    }
  },
  "tools": ["@n8n", "read", "write", "shell", "grep", "glob"],
  "allowedTools": [],
  "includeMcpJson": false
}
```

Checks and rules:

1. Read the file. The header must be exactly `Bearer ${N8N_MCP_TOKEN}`. The form `${env:N8N_MCP_TOKEN}` is **wrong**:
   kiro-cli 3.5.0 does not expand it, sends the placeholder as text, then tries OAuth and fails with "Unauthorized".
   If you find the wrong form and your tools may edit that file, change only that one string. If the edit is refused
   (the folder can be write-protected), do not look for another way. Tell the owner the one-line fix.
2. `includeMcpJson` must stay `false` so no other MCP servers load into this agent.
3. If the file does not exist, copy `docs\kiro-agent-n8n.example.json` from this repo to that path and replace the
   `url` placeholder with the real address from `.env`. Keep the real address out of the repo copy.
4. Start it from a shell that has the token in its environment:
   - `E:\002.Github\058.N8N\scripts\n8n-agent.ps1`. It reads only `N8N_MCP_TOKEN` from `.env` into its own process and
     runs `kiro-cli chat --agent n8n`. Extra arguments are passed on.
   - Or load the variables (step 2) and run `kiro-cli chat --agent n8n`.
5. Check the connection without a chat. The exit code is 3 if the server fails to start:
   `kiro-cli chat --agent n8n --no-interactive --require-mcp-startup --trust-workspace --trust-tools=@n8n/search_workflows "Call search_workflows with limit 5 and give me only the count."`
6. The agent file can also be used as a workspace file: `<workspace>\.kiro\agents\n8n.json` works when kiro-cli runs
   from that folder (use this if the global folder cannot be edited).

## 4. Setup for other agent CLIs (none is installed on this PC today)

In every case: keep the token in the environment variable `N8N_MCP_TOKEN`, load the variables first (step 2), and
keep the config file **outside this repo**. Do not use `--header "Authorization: Bearer ..."` command-line flags
(`claude mcp add --header`, `gemini mcp add --header`): they write the real token into the CLI's config file.
Checked against each CLI's docs on 2026-11-22. None of these was run here, because none of these CLIs is installed.

**Claude Code** (docs verified). Make a folder outside the repo, for example `E:\002.Github\058.N8N-work\`, and put
this in `.mcp.json` there. Claude Code expands `${VAR}` in `url` and `headers`:

```json
{
  "mcpServers": {
    "n8n": {
      "type": "http",
      "url": "${N8N_URL}/mcp-server/http",
      "headers": { "Authorization": "Bearer ${N8N_MCP_TOKEN}" }
    }
  }
}
```

Run `claude` from that folder, or `claude --mcp-config <path>\.mcp.json --strict-mcp-config`. Approve the project
server when asked. `claude mcp list` should show `n8n` connected. If it shows a missing-variable warning, the
variables are not in that shell.

**Gemini CLI** (partly verified). In `~/.gemini/settings.json` add (HTTP servers use `httpUrl`, not `url`):

```json
{
  "mcpServers": {
    "n8n": {
      "httpUrl": "<N8N_URL>/mcp-server/http",
      "headers": { "Authorization": "Bearer $N8N_MCP_TOKEN" }
    }
  }
}
```

Unverified: Gemini's own docs show `$VAR` expansion for the `env` block. Several third-party guides show it working in
`headers` too. After starting, run `/mcp` in Gemini. If it reports 401, the header was not expanded. Then STOP and
tell the owner. Do not paste the token into the file to make it work.

**Codex CLI** (docs verified). In `~/.codex/config.toml`:

```toml
[mcp_servers.n8n]
url = "<N8N_URL>/mcp-server/http"
bearer_token_env_var = "N8N_MCP_TOKEN"
startup_timeout_sec = 30
```

`bearer_token_env_var` names the variable that holds the token. Codex reads it from the environment. Run `/mcp` in
Codex to confirm.

**Any other CLI, or none:** use the helper script in step 5. It talks to n8n directly and needs only Python 3.

## 5. Self-test (run this before any real work)

Save the script below with your file tool as `n8n_helper.py` in a temp folder **outside the repo** (for example
`$env:TEMP`), then run `python n8n_helper.py selftest`. It uses only the Python standard library, ignores proxies,
reads the three values from the environment or from `.env`, and prints lengths only. Set `N8N_ENV_FILE` if the
`.env` is somewhere else.

Expected output:

```
config: url length=<n>, N8N_MCP_TOKEN length=<n>, N8N_API_KEY length=<n>
healthz: 200 (want 200)
mcp: tools = <about 60> | missing build tools: none | delete_workflow present: False (want False)
rest: GET /api/v1/workflows -> 200 (want 200)
SELFTEST PASS
```

Decision rules:

- `STOP: ... is missing` or a 401/403 line: STOP, ask the owner (step 2, rule 4).
- `healthz` is not 200 or the call times out: go to step 10. Do not continue.
- `missing build tools` is not `none`: n8n may have changed. Print the tool names from `tools/list` and tell the owner.
- `delete_workflow present: True`: n8n changed. Still use the delete rules in step 7 until the owner says otherwise.

```python
"""n8n helper for agents: self-test and worked example. Python 3.9+, standard library only.
Secrets come from the process environment or from the git-ignored .env. They are never printed (lengths only)."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("N8N_ENV_FILE", r"E:\002.Github\058.N8N\.env")
# One opener with NO proxy: a browser or HTTP proxy must never sit between this script and the tailnet.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_env_file():
    out = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8-sig"):
            m = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2).strip("\"'")
    return out


def load_config():
    """Return (url, mcp_token, api_key). Stops with a clear message if one is missing."""
    f = read_env_file()
    url = os.environ.get("N8N_URL") or f.get("N8N_URL") or f.get("N8N_Server_URL") or ""
    tok = os.environ.get("N8N_MCP_TOKEN") or f.get("N8N_MCP_TOKEN") or ""
    key = os.environ.get("N8N_API_KEY") or f.get("N8N_API_KEY") or ""
    print(f"config: url length={len(url)}, N8N_MCP_TOKEN length={len(tok)}, N8N_API_KEY length={len(key)}")
    for name, val in (("N8N_URL", url), ("N8N_MCP_TOKEN", tok), ("N8N_API_KEY", key)):
        if not val:
            sys.exit(f"STOP: {name} is missing. Do not guess it. Ask the owner.")
    if not url.startswith("https://"):
        sys.exit("STOP: the n8n URL must start with https:// (the login cookie is Secure). Ask the owner.")
    url = url.rstrip("/")
    if url.endswith("/mcp-server/http"):  # the owner's N8N_Server_URL may be the full MCP URL: keep the base only
        url = url[: -len("/mcp-server/http")]
    return url, tok, key


def http(method, url, headers=None, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


class Rest:
    def __init__(self, url, key):
        self.url, self.h = url, {"X-N8N-API-KEY": key, "Accept": "application/json"}

    def call(self, method, path, body=None):
        h = dict(self.h)
        if body is not None:
            h["Content-Type"] = "application/json"
        status, _, text = http(method, self.url + path, h, body)
        return status, (json.loads(text) if text.strip().startswith(("{", "[")) else text)


class Mcp:
    """Minimal MCP client for n8n's streamable-HTTP endpoint."""

    def __init__(self, url, token):
        self.url, self.sid, self.n = url + "/mcp-server/http", None, 0
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                  "Accept": "application/json, text/event-stream"}
        status, hdr, text = self._post("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                      "clientInfo": {"name": "agent-helper", "version": "1"}})
        if status in (401, 403):
            sys.exit("STOP: MCP answered %s. The N8N_MCP_TOKEN is wrong or was rotated. Ask the owner." % status)
        if status != 200:
            sys.exit(f"STOP: MCP initialize returned HTTP {status}")
        self.sid = hdr.get("Mcp-Session-Id")
        self._post("notifications/initialized", notify=True)

    def _post(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self.n += 1
            body["id"] = self.n
        if params is not None:
            body["params"] = params
        h = dict(self.h)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return http("POST", self.url, h, body)

    def rpc(self, method, params=None):
        status, _, text = self._post(method, params)
        if status != 200:
            sys.exit(f"STOP: MCP {method} returned HTTP {status}")
        data = None
        for line in text.splitlines():  # the reply is JSON or one SSE "data:" event
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is None:
            data = json.loads(text)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data["result"]

    def tools(self):
        return {t["name"]: t for t in self.rpc("tools/list")["tools"]}

    def tool(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        body = res.get("structuredContent")
        if body is None:
            text = res["content"][0]["text"]
            try:
                body = json.loads(text)
            except ValueError:
                body = {"text": text}
        if res.get("isError"):
            raise RuntimeError(f"{name} failed: {str(body)[:500]}")
        return body


BUILD_TOOLS = {"get_workflow_sdk_reference", "search_nodes", "get_node_types", "validate_workflow",
               "create_workflow_from_code", "update_workflow", "search_workflows", "get_workflow_details",
               "execute_workflow", "test_workflow", "publish_workflow", "unpublish_workflow", "archive_workflow"}


def selftest():
    url, tok, key = load_config()
    status, _, text = http("GET", url + "/healthz", timeout=30)
    print("healthz:", status, "(want 200)")
    mcp = Mcp(url, tok)
    tools = mcp.tools()
    print("mcp: tools =", len(tools), "| missing build tools:", sorted(BUILD_TOOLS - set(tools)) or "none",
          "| delete_workflow present:", "delete_workflow" in tools, "(want False)")
    status, body = Rest(url, key).call("GET", "/api/v1/workflows?limit=1")
    print("rest: GET /api/v1/workflows ->", status, "(want 200)")
    ok = status == 200 and not (BUILD_TOOLS - set(tools))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


SDK_CODE = """
const t = trigger({ type: 'n8n-nodes-base.manualTrigger', version: 1, config: { name: 'Start' } });
const s = node({ type: 'n8n-nodes-base.set', version: 3.4, config: { name: 'Mark', parameters: {
  assignments: { assignments: [{ id: 'a1', name: 'ok', value: 'yes', type: 'string' }] } } } });
export default workflow('agent-demo', 'zz-agent-demo').add(t.to(s));
""".strip()


def example():
    """create -> read -> update -> delete, on a throwaway zz- workflow."""
    url, tok, key = load_config()
    mcp, rest = Mcp(url, tok), Rest(url, key)
    ref = mcp.tool("get_workflow_sdk_reference", {})  # always first: it holds the SDK rules
    print("sdk reference read:", len(json.dumps(ref)), "chars")
    valid = mcp.tool("validate_workflow", {"code": SDK_CODE})
    assert valid.get("valid") is True, f"validate_workflow said: {str(valid)[:300]}"
    created = mcp.tool("create_workflow_from_code", {"code": SDK_CODE, "name": "zz-agent-demo"})
    wid = created.get("workflowId") or created.get("id")
    assert wid, "no workflow id returned"
    print("created:", wid)
    try:
        print("read (MCP): top-level keys", sorted(mcp.tool("get_workflow_details", {"workflowId": wid}))[:6])
        status, wf = rest.call("GET", f"/api/v1/workflows/{wid}")
        print("read (REST):", status, wf["name"], "nodes:", len(wf["nodes"]))
        mcp.tool("update_workflow", {"workflowId": wid,
                                     "operations": [{"type": "setWorkflowMetadata", "name": "zz-agent-demo-2"}]})
        print("renamed ->", rest.call("GET", f"/api/v1/workflows/{wid}")[1]["name"])
    finally:  # always clean up the zz- workflow, even if a step above failed
        status, _ = rest.call("DELETE", f"/api/v1/workflows/{wid}")
        if status != 200:  # on 2.48.7 archive first is optional; do it only if DELETE refuses
            mcp.tool("archive_workflow", {"workflowId": wid})
            status, _ = rest.call("DELETE", f"/api/v1/workflows/{wid}")
        print("delete:", status, "(want 200)")
    print("after delete, GET ->", rest.call("GET", f"/api/v1/workflows/{wid}")[0], "(want 404)")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    sys.exit({"selftest": selftest, "example": example}[cmd]())
```

`python n8n_helper.py example` runs the full create, read, update, delete loop from step 8 on a throwaway workflow.
In your own scripts, `from n8n_helper import load_config, Mcp, Rest` and reuse the classes.

## 6. How to build and change a workflow (MCP)

Always in this order:

1. `get_workflow_sdk_reference` first, once per session. It is long (about 55,000 characters) and holds the SDK rules.
   Workflows are written as TypeScript-like SDK code: `trigger(...)`, `node(...)`, `workflow(id, name).add(...)`,
   no `import` lines. Put `name` and `parameters` inside `config`.
2. `search_nodes` (argument `queries`) to find node types, then `get_node_types` (argument `nodeIds`) for the exact
   parameters. Use `get_workflow_best_practices` for patterns. Use `validate_node_config` to check one node.
3. `validate_workflow` with `code`. Read the result: continue only when `valid` is `true`. A reply that merely
   contains the word "valid" is not enough.
4. New workflow: `create_workflow_from_code` with `code` and `name`. The id is in `workflowId` of the result.
   Workflows created this way are automatically visible to MCP.
5. Change a workflow: `update_workflow` with `workflowId` and `operations`, a list applied together (all or nothing,
   except node-group operations). Real operation types on this server (from `tools/list`):
   `addNode`, `removeNode`, `renameNode` (renames a **node**), `setNodeParameter`, `updateNodeParameters`,
   `setNodeCredential`, `setNodeDisabled`, `setNodePosition`, `setNodeSettings`, `addConnection`, `removeConnection`,
   `addTags`, `removeTags`, `setWorkflowMetadata` (renames the **workflow**, field `name`), `setWorkflowSettings`,
   plus node-group operations. There is no `renameWorkflow`. If unsure, read the live schema: `tools/list`,
   the `inputSchema` of `update_workflow`.
6. Read: `search_workflows` (arguments `query`, `limit`), `get_workflow_details` (`workflowId`), `get_workflow_execution`,
   `get_workflow_history`. For raw JSON use REST: `GET /api/v1/workflows/<ID>`.
7. Test: `test_workflow` (`workflowId`; takes optional `pinData` for fake input, so external services are not called)
   and `execute_workflow` (`workflowId`; a real run, it can send mails, call APIs, write data). Use `test_workflow`
   first. Use `execute_workflow` only when the owner wants a real run.
8. Publish and archive: `publish_workflow`, `unpublish_workflow`, `archive_workflow` (all take `workflowId`).
9. A workflow made in the n8n editor is **not visible to MCP** until the owner turns on "Available in MCP" for it
   (Workflow settings). `search_workflows` still lists it, but details, update and run calls fail. Tell the owner.
10. Tools that touch credentials, agents, skills, data tables or user preferences exist on this server. Use them only when the
    owner's task names them. Never create or change a credential unless the owner asks.

## 7. Delete (REST or CLI, never MCP)

MCP has no delete. Ask the owner first (step 9), then use one of:

- REST: `DELETE <N8N_URL>/api/v1/workflows/<ID>` with header `X-N8N-API-KEY`. Expect HTTP 200, and a later `GET` returns
  404. On n8n 2.48.7 this also works on a workflow that is not archived. If it refuses, call MCP `archive_workflow`
  and then retry.
- CLI (PowerShell, after loading the variables in step 2, where `N8N_URL` is the base address and `N8N_API_KEY` is set):
  `npx --yes @n8n/cli workflow delete <ID>`. The CLI reads `N8N_URL` and `N8N_API_KEY` from the environment. Other
  commands: `workflow list`, `workflow get <ID>`, `workflow activate`, `workflow deactivate`.

`archive_workflow` only hides a workflow. It is the safer first step when the owner is unsure.

## 8. Worked example: create, read, update, delete

Run `python n8n_helper.py example` (step 5). It does exactly this, on a workflow named `zz-agent-demo`:

1. MCP `get_workflow_sdk_reference`, then `validate_workflow` (must be `valid: true`).
2. MCP `create_workflow_from_code` with the SDK code below. The result gives `workflowId`.
3. MCP `get_workflow_details`, and REST `GET /api/v1/workflows/<ID>` (200, name `zz-agent-demo`, 2 nodes).
4. MCP `update_workflow` with `{"type": "setWorkflowMetadata", "name": "zz-agent-demo-2"}`. REST GET shows the new name.
5. REST `DELETE /api/v1/workflows/<ID>` returns 200. A GET afterwards returns 404.
   The delete is in a `finally` block, so a failed step does not leave the test workflow behind.

SDK code used (manual trigger, then one Set node):

```ts
const t = trigger({ type: 'n8n-nodes-base.manualTrigger', version: 1, config: { name: 'Start' } });
const s = node({ type: 'n8n-nodes-base.set', version: 3.4, config: { name: 'Mark', parameters: {
  assignments: { assignments: [{ id: 'a1', name: 'ok', value: 'yes', type: 'string' }] } } } });
export default workflow('agent-demo', 'zz-agent-demo').add(t.to(s));
```

The MCP call body is JSON-RPC: `{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":"<tool>","arguments":{...}}}`
to `<N8N_URL>/mcp-server/http`, with headers `Authorization: Bearer <token>`, `Content-Type: application/json` and
`Accept: application/json, text/event-stream`. Send `initialize` first, then `notifications/initialized`, and reuse the
`Mcp-Session-Id` response header. The reply is JSON or one SSE `data:` line. The helper does all of this.

## 9. Safety rules

1. **Ask the owner before**: deleting any workflow, unpublishing any workflow, or changing a workflow you did not
   create in this session. Say which workflow (name and id) and what will happen. Wait for a clear yes.
2. **Backup before touching existing production workflows.** Before the first change to a workflow that already
   existed, ask the owner to run the VPS backup: `sudo /opt/n8n/scripts/backup_n8n.sh` (on the VPS, over SSH). Run it
   yourself only if the owner gave you SSH access in this task. Backups stay on the same VPS disk for 14 days.
   Wait until it is done.
3. **Never touch**: Docker, containers, the VPS, ufw, Tailscale settings, n8n settings, users, owner account, API keys,
   MCP tokens, the encryption key, or credentials stored in n8n. If a task seems to need this, ask the owner.
4. **Test workflows**: name them with the prefix `zz-`. Delete them when you finish (step 7). List `zz-` workflows at the
   end (`search_workflows` with `query` `zz-`) and make sure none is left.
5. Prefer `test_workflow` over `execute_workflow`. Do not publish a workflow unless the owner asked.
6. Webhook, Telegram, GitHub or Stripe triggers cannot be reached from the internet. Do not build a workflow that
   depends on them and say so.
7. Never print or log secrets. Never commit anything from `.env`. This repo is public: no real URL, tailnet name,
   IP address, token or key goes into any file here.
8. Keep requests small and sequential. Do not loop on errors. Report the error text (without secrets) to the owner.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERR_TUNNEL_CONNECTION_FAILED` | A proxy or VPN browser extension is in the way | Turn the extension off, or use the helper (it ignores proxies) |
| "name not found", DNS error, timeout on the host name | Tailscale is off, or the browser uses secure DNS | Run `tailscale status`. Start Tailscale. In the browser, turn off "secure DNS" |
| HTTP 401 or 403 on MCP | `N8N_MCP_TOKEN` wrong, or the owner rotated it | STOP. Ask the owner for a new token in `.env` |
| HTTP 401 on `/api/v1/...` | `N8N_API_KEY` wrong or deleted | STOP. Ask the owner |
| Workflow missing in details/update/run | The workflow is not "Available in MCP" | Ask the owner to turn it on in the workflow settings |
| kiro-cli says "Unauthorized" or opens an OAuth page | Header written as `${env:N8N_MCP_TOKEN}`, or the variable is not set in that shell | Use `${N8N_MCP_TOKEN}` (step 3) and start from a shell that has the variable |
| `healthz` is not 200 and Tailscale is up | The n8n containers are down or restarting | STOP. Tell the owner. Do not touch the VPS |
| `update_workflow` says "Invalid enum value" | Wrong operation type name | Use the real names in step 6, point 5 |
| `validate_workflow` returns `valid: false` | SDK code error | Read the errors, re-read `get_workflow_sdk_reference`, fix the code. Do not create until valid |
| HTTP 404 from `/mcp-server/http` | URL is wrong (for example it has a doubled path) | The base `<N8N_URL>` must not end in `/mcp-server/http` when you add that path yourself |
| Editor login loops | The page was opened with `http://` or an IP address | Always use the `https://` name |

## 11. Finish

When your task is done: delete your `zz-` workflows, tell the owner what you created or changed (names and ids, no
secrets), and list anything you did not check.
