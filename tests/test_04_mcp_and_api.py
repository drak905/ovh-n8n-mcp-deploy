"""Tier 4: MCP and public API reachability through Tailscale Serve.

The unauthenticated checks always run. The token checks skip with a clear message until the owner puts
N8N_MCP_TOKEN and N8N_API_KEY into the PC .env (git-ignored). Tokens are never printed or put in a command line.
Tests never create the owner account, a token, or an API key.
"""
import json

import pytest
import requests

from conftest import pc_env_token, pc_tailscale

MCP_PATH = "/mcp-server/http"
BUILD_TOOLS = {"search_workflows", "get_workflow_details", "create_workflow_from_code", "update_workflow",
               "validate_workflow", "archive_workflow", "publish_workflow"}


@pytest.fixture(scope="module")
def base(n8n_fqdn_remote):
    st = pc_tailscale()
    if not st or st.get("BackendState") != "Running":
        pytest.skip("PC Tailscale is not Running")
    if st.get("ExitNodeStatus"):
        pytest.skip("PC uses an exit node")
    return f"https://{n8n_fqdn_remote}"


def _rpc(base, token, method, params=None, session=None, rid=1):
    """One JSON-RPC call to the streamable-HTTP MCP endpoint. The reply is JSON or one SSE `data:` event."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    r = requests.post(base + MCP_PATH, headers=headers, json=body, timeout=60)
    data = None
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
    elif r.text.strip():
        data = r.json()
    return r, data


def test_unauthenticated_mcp_is_refused(base):
    r = requests.post(base + MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream"}, timeout=45)
    assert r.status_code in {401, 403, 404}, "MCP answered without a token"


def test_unauthenticated_api_is_refused(base):
    r = requests.get(base + "/api/v1/workflows", timeout=45)
    assert r.status_code in {401, 403, 404}


def test_bad_api_key_is_refused(base):
    r = requests.get(base + "/api/v1/workflows", headers={"X-N8N-API-KEY": "not-a-real-key"}, timeout=45)
    assert r.status_code in {401, 403, 404}


def test_mcp_initialize_and_tools_list_with_token(base, env):
    token = pc_env_token(env, "N8N_MCP_TOKEN")
    if not token:
        pytest.skip("N8N_MCP_TOKEN not in the PC .env yet (owner step: Settings > Instance-level MCP > API key tab)")
    r, data = _rpc(base, token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "n8n-deploy-test", "version": "1"}})
    assert r.status_code == 200 and data and "result" in data, "initialize failed"
    session = r.headers.get("Mcp-Session-Id")
    _rpc(base, token, "notifications/initialized", session=session, rid=None)
    r, data = _rpc(base, token, "tools/list", session=session, rid=2)
    assert r.status_code == 200
    names = {t["name"] for t in data["result"]["tools"]}
    assert BUILD_TOOLS <= names, f"missing tools: {sorted(BUILD_TOOLS - names)}"
    assert "delete_workflow" not in names, "MCP is expected to have no hard delete (use the API/CLI)"


def test_api_key_lists_workflows(base, env):
    key = pc_env_token(env, "N8N_API_KEY")
    if not key:
        pytest.skip("N8N_API_KEY not in the PC .env yet (owner step: Settings > n8n API)")
    r = requests.get(base + "/api/v1/workflows?limit=1", headers={"X-N8N-API-KEY": key}, timeout=45)
    assert r.status_code == 200
    assert "data" in r.json()
