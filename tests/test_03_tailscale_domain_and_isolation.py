"""Tier 3: from the PC, over Tailscale, plus public-WAN and tailnet-IP negative checks.

Skips with a reason when the PC is not on the tailnet or uses an exit node.
The tests never change the PC's Tailscale settings and never open the owner setup form.
"""
import ipaddress
import socket

import pytest
import requests

from conftest import pc_tailscale


@pytest.fixture(scope="module")
def pc():
    st = pc_tailscale()
    if not st or st.get("BackendState") != "Running":
        pytest.skip("PC Tailscale is not Running; start it and re-run tier 3")
    if st.get("ExitNodeStatus") or any(p.get("ExitNode") for p in (st.get("Peer") or {}).values()):
        pytest.skip("PC uses an exit node; run `tailscale set --exit-node=` and re-run tier 3")
    return st


@pytest.fixture
def fqdn(pc, request):
    """Resolved after `pc`, so a stopped PC Tailscale skips instead of erroring."""
    return request.getfixturevalue("n8n_fqdn_remote")


def _closed(host, port, secs=8):
    s = socket.socket()
    s.settimeout(secs)
    try:
        return s.connect_ex((host, port)) != 0
    finally:
        s.close()


def test_domain_resolves_to_cgnat(pc, fqdn):
    ip = ipaddress.ip_address(socket.gethostbyname(fqdn))
    assert ip in ipaddress.ip_network("100." + "64.0.0/10")


def test_healthz_200_with_trusted_certificate(pc, fqdn):
    r = requests.get(f"https://{fqdn}/healthz", timeout=45, verify=True)
    assert r.status_code == 200


def test_owner_setup_page_loads(pc, fqdn):
    """GET only. The page must load, and this test never submits the form."""
    r = requests.get(f"https://{fqdn}/", timeout=45, verify=True)
    assert r.status_code == 200
    assert "n8n" in r.text.lower()


def test_editor_urls_are_the_tailnet_https_name(pc, fqdn):
    """Proves N8N_PROXY_HOPS and the URL settings: the app builds https links with the tailnet name."""
    r = requests.get(f"https://{fqdn}/rest/settings", timeout=45, verify=True)
    assert r.status_code == 200
    body = r.text
    assert f"https://{fqdn}" in body
    assert "http://" + fqdn not in body and ":5678" not in body


def test_plain_http_and_app_ports_closed_on_tailnet_name_and_ip(pc, fqdn):
    ip = socket.gethostbyname(fqdn)
    for port in (80, 5678, 5432):
        assert _closed(fqdn, port), f"port {port} open on the tailnet name"
        assert _closed(ip, port), f"port {port} open on the tailnet IP"


def test_public_ip_nothing_new_open(pc, env):
    for port in (80, 443, 5678, 5432):
        assert _closed(env["OVH_VPS_IP"], port), f"port {port} open on the public IP"
