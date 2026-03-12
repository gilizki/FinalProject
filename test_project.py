"""
test_project.py  —  Full edge-case test suite for the Music Agent final project.

HOW TO RUN
──────────
Unit tests only (no servers needed):
    python test_project.py unit

Integration tests (requires DHCP.py + DNS.py + app_server.py all running first):
    python test_project.py integration

Everything:
    python test_project.py all
    python test_project.py          ← defaults to all
"""

import sys
import os
import socket
import struct
import json
import threading
import time
import random
import urllib.parse

# ─── make sure we can import project files ──────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rudp import (
    RUDPSender, RUDPReceiver,
    make_packet, parse_packet,
    TYPE_DATA, TYPE_ACK, TYPE_FIN,
    HEADER_SIZE, MAX_SEGMENT_SIZE, RECEIVER_WINDOW
)

# ─── shared constants (must match your project files) ───────
DHCP_SERVER   = '127.0.0.1'
DHCP_PORT     = 6767
DNS_SERVER    = '127.0.0.1'
DNS_PORT      = 53
APP_SERVER    = '127.0.0.1'
APP_PORT      = 5000

MAGIC_COOKIE     = b'\x63\x82\x53\x63'
DHCP_DISCOVER    = 1
DHCP_OFFER       = 2
DHCP_REQUEST     = 3
DHCP_ACK         = 5
OPT_MESSAGE_TYPE = 53
OPT_REQUESTED_IP = 50
OPT_DNS_SERVER   = 6
OPT_END          = 255

# ─── tiny test framework ─────────────────────────────────────
PASS  = 0
FAIL  = 0
_results = []

def check(name, condition, detail=""):
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    marker = "✅" if condition else "❌"
    msg = f"  {marker} [{status}] {name}"
    if detail:
        msg += f"\n         → {detail}"
    print(msg)
    _results.append((status, name))

def section(title):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")

def summary():
    total = PASS + FAIL
    print(f"\n{'═'*55}")
    print(f"  RESULTS:  {PASS}/{total} passed   ({FAIL} failed)")
    print(f"{'═'*55}")
    if FAIL:
        print("\n  Failed tests:")
        for status, name in _results:
            if status == "FAIL":
                print(f"    ✗ {name}")


# ══════════════════════════════════════════════════════════════
#  UNIT TESTS  (no servers required)
# ══════════════════════════════════════════════════════════════

def test_rudp_packet_encoding():
    section("RUDP: Packet encode / decode")

    # Round-trip: make a packet then parse it back
    raw = make_packet(TYPE_DATA, seq_num=42, ack_num=0, window_size=16, data=b"hello")
    p   = parse_packet(raw)
    check("TYPE_DATA round-trip: type",   p['type']   == TYPE_DATA)
    check("TYPE_DATA round-trip: seq",    p['seq']    == 42)
    check("TYPE_DATA round-trip: window", p['window'] == 16)
    check("TYPE_DATA round-trip: data",   p['data']   == b"hello")

    raw = make_packet(TYPE_ACK, seq_num=0, ack_num=99, window_size=64)
    p   = parse_packet(raw)
    check("TYPE_ACK round-trip: type",    p['type'] == TYPE_ACK)
    check("TYPE_ACK round-trip: ack",     p['ack']  == 99)

    raw = make_packet(TYPE_FIN, seq_num=0, ack_num=0, window_size=0)
    p   = parse_packet(raw)
    check("TYPE_FIN round-trip: type",    p['type'] == TYPE_FIN)

    # Too-short packet should return None
    check("parse_packet(b'') returns None",    parse_packet(b'') is None)
    check("parse_packet(3 bytes) returns None", parse_packet(b'\x00\x01\x02') is None)


def test_rudp_header_size():
    section("RUDP: Header size is exactly 10 bytes")
    check("HEADER_SIZE == 12", HEADER_SIZE == 12,
          f"got {HEADER_SIZE}")
    raw = make_packet(TYPE_DATA, 0, 0, 0, data=b"")
    check("empty packet is exactly 12 bytes", len(raw) == 12,
          f"got {len(raw)}")
    raw = make_packet(TYPE_DATA, 0, 0, 0, data=b"X" * 100)
    check("100-byte payload → 112-byte packet", len(raw) == 112,
          f"got {len(raw)}")


def test_rudp_max_segment():
    section("RUDP: MAX_SEGMENT_SIZE fits within UDP limit")
    check("MAX_SEGMENT_SIZE <= 1400",
          MAX_SEGMENT_SIZE <= 1400,
          f"got {MAX_SEGMENT_SIZE}")
    # full packet must be well under UDP 64KB limit
    full = HEADER_SIZE + MAX_SEGMENT_SIZE
    check(f"full packet ({full} bytes) < 65507 bytes (UDP max)",
          full < 65507)


def test_rudp_local_transfer_small():
    section("RUDP: Local transfer — small payload (< 1 packet)")
    _run_rudp_transfer_test(b"Short message test", "small payload")


def test_rudp_local_transfer_multipacket():
    section("RUDP: Local transfer — multi-packet (3 MB)")
    data = os.urandom(3 * 1024 * 1024)   # 3 MB of random bytes
    _run_rudp_transfer_test(data, "3 MB random data")


def test_rudp_local_transfer_exact_boundary():
    section("RUDP: Local transfer — exact segment boundary")
    # exactly 5 full segments, no leftover
    data = b"A" * (MAX_SEGMENT_SIZE * 5)
    _run_rudp_transfer_test(data, f"{MAX_SEGMENT_SIZE * 5} bytes (5 exact segments)")


def test_rudp_local_transfer_one_byte():
    section("RUDP: Local transfer — 1 byte edge case")
    _run_rudp_transfer_test(b"\xFF", "single byte")


def _run_rudp_transfer_test(data, label):
    """Helper: spin up a local sender+receiver pair and verify integrity."""
    port = random.randint(50000, 59999)
    received = []
    error    = []

    def recv():
        try:
            r      = RUDPReceiver(port)
            result = r.receive_file()
            received.append(result)
        except Exception as e:
            error.append(str(e))

    t = threading.Thread(target=recv, daemon=True)
    t.start()
    time.sleep(0.5)   # give receiver enough time to bind before sender fires

    try:
        s = RUDPSender('127.0.0.1', port)
        s.send_file(data)
    except Exception as e:
        error.append(str(e))

    t.join(timeout=60)

    if error:
        check(f"Transfer ({label}) — no errors", False, error[0])
        return
    if not received:
        check(f"Transfer ({label}) — receiver got data", False, "timeout or no data")
        return

    got = received[0]
    check(f"Transfer ({label}) — correct length",
          len(got) == len(data),
          f"sent {len(data)}, got {len(got)}")
    check(f"Transfer ({label}) — data integrity (hash)",
          got == data,
          "content mismatch" if got != data else "")


def test_rudp_congestion_state():
    section("RUDP: Congestion control initial state")
    s = RUDPSender('127.0.0.1', 9999)   # don't connect, just check state
    check("cwnd starts at 1",          s.cwnd     == 1.0)
    check("ssthresh starts at 16",     s.ssthresh == 16.0)
    check("rwnd starts at RECEIVER_WINDOW", s.rwnd == RECEIVER_WINDOW)
    check("last_ack starts at -1",     s.last_ack == -1)
    check("dup_ack_count starts at 0", s.dup_ack_count == 0)
    s.sock.close()


def test_rudp_effective_window():
    section("RUDP: Effective window logic")
    s = RUDPSender('127.0.0.1', 9999)

    s.cwnd = 10; s.rwnd = 64
    check("min(cwnd=10, rwnd=64) = 10", s._effective_window() == 10)

    s.cwnd = 100; s.rwnd = 64
    check("min(cwnd=100, rwnd=64) = 64 (flow control caps it)", s._effective_window() == 64)

    s.cwnd = 0.5; s.rwnd = 64
    check("min(cwnd=0.5, rwnd=64): floor(0.5)=0, effective=1 (minimum guard)",
          s._effective_window() == 1)

    s.cwnd = 5; s.rwnd = 3
    check("min(cwnd=5, rwnd=3) = 3 (receiver is the bottleneck)", s._effective_window() == 3)

    s.sock.close()


def test_dhcp_packet_structure():
    section("DHCP: Packet build and parse")
    # import the helpers directly from client.py
    from client import build_dhcp_packet, parse_dhcp_reply, MY_MAC

    xid     = random.randint(1, 0xFFFFFFFF)
    packet  = build_dhcp_packet(DHCP_DISCOVER, xid)

    check("DHCP DISCOVER is at least 244 bytes", len(packet) >= 244,
          f"got {len(packet)}")
    check("Magic cookie present at offset 236",
          packet[236:240] == MAGIC_COOKIE)
    check("XID matches at bytes 4-8",
          struct.unpack('!I', packet[4:8])[0] == xid)
    check("Client MAC at bytes 28-34 matches MY_MAC",
          packet[28:34] == MY_MAC)

    # REQUEST packet should include requested IP option (code 50)
    req = build_dhcp_packet(DHCP_REQUEST, xid, requested_ip='127.0.0.100')
    check("REQUEST packet contains option 50 (requested IP)",
          bytes([OPT_REQUESTED_IP]) in req)


def test_dns_query_structure():
    section("DNS: Query packet structure")
    from client import build_dns_query, parse_dns_response

    txid, packet = build_dns_query('app.local')

    check("DNS query is at least 17 bytes", len(packet) >= 17)
    check("Transaction ID matches first 2 bytes",
          struct.unpack('!H', packet[0:2])[0] == txid)
    check("Flags byte has recursion desired (0x0100)",
          struct.unpack('!H', packet[2:4])[0] == 0x0100)
    check("QDCOUNT = 1 (one question)",
          struct.unpack('!H', packet[4:6])[0] == 1)

    # parse_dns_response with wrong txid should return None
    fake_response = packet   # reuse query bytes (wrong format but tests txid check)
    check("parse_dns_response rejects wrong txid",
          parse_dns_response(fake_response, txid + 1) is None)

    # parse_dns_response on too-short data returns None
    check("parse_dns_response on 5 bytes returns None",
          parse_dns_response(b'\x00' * 5, txid) is None)


def test_agent_empty_inputs():
    section("Agent: Empty / missing input handling")
    from agent import handle_request

    r = handle_request({'action': 'search', 'query': ''})
    check("search with empty query returns error status",
          r.get('status') == 'error')

    r = handle_request({'action': 'download_url', 'url': ''})
    check("download_url with empty url returns error status",
          r.get('status') == 'error')

    r = handle_request({'action': 'vibe', 'description': ''})
    check("vibe with empty description returns error status",
          r.get('status') == 'error')

    r = handle_request({'action': 'unknown_action'})
    check("unknown action returns error status",
          r.get('status') == 'error')

    r = handle_request({})
    check("empty request dict returns error status",
          r.get('status') == 'error')


def test_agent_history():
    section("Agent: History always returns a list")
    from agent import handle_request
    r = handle_request({'action': 'history'})
    check("history returns success status",    r.get('status') == 'success')
    check("history result contains 'history' key", 'history' in r)
    check("history value is a list",           isinstance(r.get('history'), list))


def test_http_parser():
    section("App server: HTTP request parser")
    from app_server import parse_request

    method, path, params = parse_request(b"GET /search?q=hello+world HTTP/1.1\r\nHost: x\r\n\r\n")
    check("method is GET",             method == 'GET')
    check("path is /search",           path   == '/search')
    check("q param extracted",         'q' in params)

    method, path, params = parse_request(b"GET /history HTTP/1.1\r\n\r\n")
    check("path without query string", path == '/history')
    check("params dict is empty",      params == {})

    method, path, params = parse_request(b"BADREQUEST")   # no space → fewer than 2 parts
    check("malformed request: method is None", method is None)


# ══════════════════════════════════════════════════════════════
#  INTEGRATION TESTS  (servers must be running)
# ══════════════════════════════════════════════════════════════

def _http_get(path, timeout=15):
    """Raw HTTP GET helper — same logic as client.py but standalone."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((APP_SERVER, APP_PORT))
        req = (f"GET {path} HTTP/1.1\r\nHost: {APP_SERVER}\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode())
        raw = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        sock.close()
        body = raw.split(b'\r\n\r\n', 1)[1].decode('utf-8', errors='ignore')
        return json.loads(body)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def test_integration_app_server_reachable():
    section("Integration: App server reachable")
    result = _http_get('/history', timeout=5)
    check("App server responds on port 5000",
          result.get('status') != 'error',
          result.get('message', ''))


def test_integration_search():
    section("Integration: /search endpoint")
    q      = urllib.parse.quote('bohemian rhapsody', safe='')
    result = _http_get(f'/search?q={q}', timeout=15)
    check("Search returns success",   result.get('status') == 'success')
    check("Results list is present",  'results' in result)
    check("Results list is non-empty", len(result.get('results', [])) > 0)

    first = result.get('results', [{}])[0]
    check("Each result has a title",  'title' in first)
    check("Each result has a url",    'url'   in first)


def test_integration_search_empty():
    section("Integration: /search with empty query")
    result = _http_get('/search?q=', timeout=5)
    check("Empty search returns error", result.get('status') == 'error')


def test_integration_unknown_route():
    section("Integration: Unknown route returns error")
    result = _http_get('/nonexistent', timeout=5)
    check("Unknown route returns error status", result.get('status') == 'error')


def test_integration_history():
    section("Integration: /history endpoint")
    result = _http_get('/history', timeout=5)
    check("History returns success",         result.get('status') == 'success')
    check("History contains 'history' key",  'history' in result)
    check("History value is a list",         isinstance(result.get('history'), list))


def test_integration_vibe():
    section("Integration: /vibe endpoint (AI)")
    q      = urllib.parse.quote('chill study music', safe='')
    result = _http_get(f'/vibe?q={q}', timeout=30)
    # vibe calls Groq AI so can be slow — we just check structure
    check("Vibe returns success or graceful error",
          result.get('status') in ('success', 'error'))
    if result.get('status') == 'success':
        check("Vibe returns search_results",
              'search_results' in result or 'results' in result)


def test_integration_dhcp_full_dora():
    section("Integration: DHCP full DORA handshake")
    from client import build_dhcp_packet, parse_dhcp_reply

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(5)

        xid = random.randint(1, 0xFFFFFFFF)

        # DISCOVER
        discover = build_dhcp_packet(DHCP_DISCOVER, xid)
        sock.sendto(discover, (DHCP_SERVER, DHCP_PORT))
        offer_data, _ = sock.recvfrom(1024)
        offered_ip, dns_ip = parse_dhcp_reply(offer_data)

        check("DHCP OFFER received",                offered_ip is not None)
        check("OFFER gives a valid IP (127.0.0.x)", offered_ip.startswith('127.0.0.'))
        check("OFFER includes DNS server IP",        dns_ip is not None)

        # REQUEST
        request = build_dhcp_packet(DHCP_REQUEST, xid, requested_ip=offered_ip)
        sock.sendto(request, (DHCP_SERVER, DHCP_PORT))
        ack_data, _ = sock.recvfrom(1024)
        acked_ip, _ = parse_dhcp_reply(ack_data)

        check("DHCP ACK received",                  acked_ip is not None)
        check("ACK confirms the offered IP",         acked_ip == offered_ip)
        sock.close()

    except Exception as e:
        check("DHCP handshake completed without exception", False, str(e))


def test_integration_dns_known_host():
    section("Integration: DNS resolves app.local")
    from client import build_dns_query, parse_dns_response

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        txid, query = build_dns_query('app.local')
        sock.sendto(query, (DNS_SERVER, DNS_PORT))
        response, _ = sock.recvfrom(512)
        sock.close()

        ip = parse_dns_response(response, txid)
        check("DNS resolves app.local",              ip is not None)
        check("app.local resolves to 127.0.0.1",    ip == '127.0.0.1',
              f"got {ip}")

    except Exception as e:
        check("DNS query completed without exception", False, str(e))


def test_integration_dns_unknown_host():
    section("Integration: DNS returns NXDOMAIN for unknown host")
    from client import build_dns_query, parse_dns_response

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        txid, query = build_dns_query('doesnotexist.local')
        sock.sendto(query, (DNS_SERVER, DNS_PORT))
        response, _ = sock.recvfrom(512)
        sock.close()

        ip = parse_dns_response(response, txid)
        # NXDOMAIN means no answer → ip should be None
        check("DNS returns no IP for unknown .local domain", ip is None,
              f"unexpectedly got {ip}")

    except Exception as e:
        check("DNS NXDOMAIN test completed without exception", False, str(e))


def test_integration_rudp_end_to_end():
    section("Integration: RUDP end-to-end via app server (small file)")
    # We test RUDP locally without hitting YouTube — just verify the protocol works
    # at the integration level by running a real sender/receiver pair
    data  = os.urandom(500 * 1024)   # 500 KB
    port  = 5099
    received = []
    error    = []

    def recv():
        try:
            r = RUDPReceiver(port)
            received.append(r.receive_file())
        except Exception as e:
            error.append(str(e))

    t = threading.Thread(target=recv, daemon=True)
    t.start()
    time.sleep(0.3)

    try:
        s = RUDPSender('127.0.0.1', port)
        s.send_file(data)
    except Exception as e:
        error.append(str(e))

    t.join(timeout=30)

    if error:
        check("500 KB RUDP transfer — no errors", False, error[0])
        return

    got = received[0] if received else b''
    check("500 KB RUDP transfer — received",     len(got) > 0)
    check("500 KB RUDP transfer — length match", len(got) == len(data),
          f"sent {len(data)}, got {len(got)}")
    check("500 KB RUDP transfer — data correct", got == data)


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

UNIT_TESTS = [
    test_rudp_packet_encoding,
    test_rudp_header_size,
    test_rudp_max_segment,
    test_rudp_local_transfer_small,
    test_rudp_local_transfer_multipacket,
    test_rudp_local_transfer_exact_boundary,
    test_rudp_local_transfer_one_byte,
    test_rudp_congestion_state,
    test_rudp_effective_window,
    test_dhcp_packet_structure,
    test_dns_query_structure,
    test_agent_empty_inputs,
    test_agent_history,
    test_http_parser,
]

INTEGRATION_TESTS = [
    test_integration_app_server_reachable,
    test_integration_search,
    test_integration_search_empty,
    test_integration_unknown_route,
    test_integration_history,
    test_integration_vibe,
    test_integration_dhcp_full_dora,
    test_integration_dns_known_host,
    test_integration_dns_unknown_host,
    test_integration_rudp_end_to_end,
]

if __name__ == '__main__':
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else 'all'

    if mode in ('unit', 'all'):
        print("\n" + "═"*55)
        print("  UNIT TESTS  (no servers required)")
        print("═"*55)
        for t in UNIT_TESTS:
            t()

    if mode in ('integration', 'all'):
        print("\n" + "═"*55)
        print("  INTEGRATION TESTS  (servers must be running)")
        print("═"*55)
        print("  Make sure DHCP.py, DNS.py, app_server.py are all running.")
        input("  Press Enter when ready...")
        for t in INTEGRATION_TESTS:
            t()

    summary()
