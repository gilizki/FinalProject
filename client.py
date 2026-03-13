import socket
import os
import json
import struct
import threading
import time
import random
import urllib.parse
from rudp import RUDPReceiver, HEADER_SIZE, MAX_SEGMENT_SIZE

# ─── Constants ───────────────────────────────────────────────
# These are the default server addresses used when running locally (single machine).
# When running across two computers, DHCP_SERVER should be the server machine's LAN IP.
DHCP_SERVER       = '127.0.0.1'
DHCP_PORT         = 6767   # our custom DHCP port (real DHCP uses 67, requires admin)
DNS_PORT          = 53     # standard DNS port
APP_PORT          = 5000   # app_server.py HTTP control port
RUDP_RECEIVE_PORT = 5001   # we (the client) listen here for incoming RUDP file transfers
TCP_RECEIVE_PORT  = 5002   # we (the client) listen here for incoming TCP file transfers

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_downloads")

# ─── DHCP Constants (must match DHCP.py exactly) ─────────────
MAGIC_COOKIE     = b'\x63\x82\x53\x63'   # marks start of DHCP options in every packet
DHCP_DISCOVER    = 1
DHCP_OFFER       = 2
DHCP_REQUEST     = 3
DHCP_ACK         = 5
OPT_MESSAGE_TYPE = 53
OPT_REQUESTED_IP = 50
OPT_DNS_SERVER   = 6
OPT_END          = 255

# A fake but consistent MAC address for this client.
# In a real system this would come from the network interface.
# We keep it fixed so DHCP always recognizes us and can assign the same IP.
MY_MAC = b'\xAA\xBB\xCC\xDD\xEE\x01'

# ─── Helpers ──────────────────────────────────────────────────

def ensure_downloads():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

def pack_dhcp_option(code, data):
    """
    DHCP options use TLV format: [1 byte code][1 byte length][N bytes value]
    This just packs that structure cleanly for each option we need to include.
    """
    return struct.pack('!BB', code, len(data)) + data

# ─── Step 1: DHCP ─────────────────────────────────────────────

def build_dhcp_packet(msg_type, xid, requested_ip=None):
    """
    Build a binary DHCP packet in BOOTP format (the protocol DHCP is built on).

    BOOTP header is 236 bytes:
      op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
      ciaddr(4) yiaddr(4) siaddr(4) giaddr(4)
      chaddr(16) sname(64) file(128)
    Then: magic cookie(4) + options

    xid = transaction ID, a random number we generate.
    The server echoes it back in every reply so we can match the reply to our request.
    flags = 0x8000 = broadcast flag, tells server to reply even though we don't have an IP yet.
    """
    chaddr = MY_MAC + b'\x00' * 10   # MAC padded to 16 bytes (BOOTP chaddr field)

    header = struct.pack(
        '!BBBB I HH 4s4s4s4s 16s 64s 128s',
        1,             # op = 1 = BOOTREQUEST (client sending)
        1,             # htype = 1 = Ethernet
        6,             # hlen = 6 = MAC address length
        0,             # hops = 0 (no relay agents)
        xid,           # transaction ID
        0,             # secs elapsed since we started
        0x8000,        # flags = broadcast
        b'\x00' * 4,   # ciaddr: our current IP (unknown, so all zeros)
        b'\x00' * 4,   # yiaddr: filled in by server in OFFER/ACK
        b'\x00' * 4,   # siaddr: server IP (filled in by server)
        b'\x00' * 4,   # giaddr: relay agent (not used)
        chaddr,
        b'\x00' * 64,  # sname: server hostname (not used)
        b'\x00' * 128  # file: boot filename (not used)
    )

    options = b''
    options += pack_dhcp_option(OPT_MESSAGE_TYPE, struct.pack('!B', msg_type))
    if requested_ip:
        # In a REQUEST packet we must include the IP from the OFFER
        # so the server knows which IP we accepted
        options += pack_dhcp_option(OPT_REQUESTED_IP, socket.inet_aton(requested_ip))
    options += struct.pack('!B', OPT_END)

    return header + MAGIC_COOKIE + options

def parse_dhcp_reply(data):
    """
    Extract the offered/assigned IP and the DNS server IP from a DHCP reply.

    yiaddr ("your IP address") is always at bytes 16-20 in the BOOTP header.
    DNS server IP is in the options section under option code 6.
    """
    offered_ip = socket.inet_ntoa(data[16:20])

    dns_ip = None
    if len(data) > 240 and data[236:240] == MAGIC_COOKIE:
        i = 240   # options start right after the 4-byte magic cookie
        while i < len(data):
            code = data[i]
            if code == OPT_END:
                break
            if code == 0:   # PAD option has no length field, skip 1 byte
                i += 1
                continue
            length = data[i + 1]
            value  = data[i + 2: i + 2 + length]
            if code == OPT_DNS_SERVER and length == 4:
                dns_ip = socket.inet_ntoa(value)
            i += 2 + length

    return offered_ip, dns_ip

def do_dhcp():
    """
    Run the full DHCP DORA handshake to get an IP address:
      DISCOVER → server sends us OFFER
      REQUEST  → server sends us ACK and the IP is officially ours

    Returns (my_ip, dns_server_ip).
    Falls back to 127.0.0.1 for both if anything fails.
    """
    print("[CLIENT] Starting DHCP handshake...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(5)

        xid = random.randint(1, 0xFFFFFFFF)   # random transaction ID

        # ── DISCOVER ──
        discover = build_dhcp_packet(DHCP_DISCOVER, xid)
        sock.sendto(discover, (DHCP_SERVER, DHCP_PORT))
        print("[DHCP] Sent DISCOVER")

        # ── OFFER ──
        offer_data, _ = sock.recvfrom(1024)
        offered_ip, dns_ip = parse_dhcp_reply(offer_data)
        print(f"[DHCP] Got OFFER → IP: {offered_ip}, DNS: {dns_ip}")

        # ── REQUEST ──
        request = build_dhcp_packet(DHCP_REQUEST, xid, requested_ip=offered_ip)
        sock.sendto(request, (DHCP_SERVER, DHCP_PORT))
        print(f"[DHCP] Sent REQUEST for {offered_ip}")

        # ── ACK ──
        ack_data, _ = sock.recvfrom(1024)
        my_ip, dns_from_ack = parse_dhcp_reply(ack_data)
        print(f"[DHCP] Got ACK → assigned IP: {my_ip}")

        sock.close()
        return my_ip, (dns_from_ack or dns_ip or '127.0.0.1')

    except Exception as e:
        print(f"[DHCP] Failed: {e}, using defaults")
        return '127.0.0.1', '127.0.0.1'

# ─── Step 2: DNS ──────────────────────────────────────────────

def build_dns_query(hostname):
    """
    Build a binary DNS A-record query packet (RFC 1035).

    DNS header (12 bytes):
      txid(2) flags(2) qdcount(2) ancount(2) nscount(2) arcount(2)
    Question section:
      encoded_name + qtype(2) + qclass(2)

    Name encoding: 'app.local' → \x03app\x05local\x00
    (each label is prefixed with its byte length, terminated by \x00)

    flags = 0x0100 = standard query with recursion desired
    """
    txid   = random.randint(1, 0xFFFF)
    flags  = 0x0100
    header = struct.pack('!HHHHHH', txid, flags, 1, 0, 0, 0)

    encoded = b''
    for part in hostname.encode().split(b'.'):
        encoded += struct.pack('!B', len(part)) + part
    encoded += b'\x00'   # end of name

    question = encoded + struct.pack('!HH', 1, 1)   # qtype=A (1), qclass=IN (1)
    return txid, header + question

def parse_dns_response(data, expected_txid):
    """
    Extract the resolved IPv4 address from a binary DNS response.

    We validate the transaction ID first (to make sure this reply matches our query),
    then skip past the question section to find the first answer record.

    Answer record format:
      name(2, pointer) type(2) class(2) ttl(4) rdlength(2) rdata(4 for IPv4)
    """
    if len(data) < 12:
        return None

    txid = struct.unpack('!H', data[0:2])[0]
    if txid != expected_txid:
        return None

    ancount = struct.unpack('!H', data[6:8])[0]
    if ancount == 0:
        return None

    # Skip the header (12 bytes) and walk past the question's name field
    i = 12
    while i < len(data):
        length = data[i]
        if length == 0:
            i += 1
            break
        if (length & 0xC0) == 0xC0:   # DNS name compression pointer (2 bytes)
            i += 2
            break
        i += length + 1
    i += 4   # skip qtype + qclass in the question section

    # Now we're at the start of the answer section
    if i + 10 <= len(data):
        i     += 10   # skip: name pointer(2) + type(2) + class(2) + ttl(4)
        rdlen  = struct.unpack('!H', data[i: i + 2])[0]
        i     += 2
        if rdlen == 4:
            return socket.inet_ntoa(data[i: i + 4])

    return None

def do_dns(hostname, dns_server_ip):
    """
    Ask our DNS server to resolve hostname to an IP address.
    Uses the binary DNS protocol (not a library) — we build the packet ourselves.
    Returns the resolved IP, or falls back to 127.0.0.1 if anything fails.
    """
    print(f"[CLIENT] DNS query for: {hostname} → asking {dns_server_ip}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)

        txid, query_packet = build_dns_query(hostname)
        sock.sendto(query_packet, (dns_server_ip, DNS_PORT))

        response, _ = sock.recvfrom(512)
        sock.close()

        ip = parse_dns_response(response, txid)
        if ip:
            print(f"[DNS] {hostname} → {ip}")
            return ip
        else:
            print(f"[DNS] No answer for {hostname}, using localhost")
            return '127.0.0.1'

    except Exception as e:
        print(f"[DNS] Failed: {e}, using localhost")
        return '127.0.0.1'

# ─── Step 3: HTTP to app server ───────────────────────────────

def http_get(path, server_ip):
    """
    Send a raw HTTP/1.1 GET request over a plain TCP socket and return the JSON body.
    We build the HTTP request string manually — no requests library needed.
    The server closes the connection after each response (Connection: close),
    so we just keep reading until recv() returns empty bytes.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(60)   # generous timeout because YouTube downloads take time
        sock.connect((server_ip, APP_PORT))

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {server_ip}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode('utf-8'))

        response = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        sock.close()

        # HTTP response = headers + \r\n\r\n + body
        # We only need the body (JSON)
        body = response.split(b'\r\n\r\n', 1)[1].decode('utf-8')
        return json.loads(body)

    except Exception as e:
        print(f"[HTTP] Error on {path}: {e}")
        return None

# ─── Step 4a: Receive file over RUDP ──────────────────────────

def receive_over_rudp(filename):
    """
    Open an RUDP receiver on RUDP_RECEIVE_PORT and wait for the server to push the file.
    The receiver handles all the reliability logic (ACKs, reordering, flow control).
    Once the transfer is complete, we save the bytes to client_downloads/.
    """
    ensure_downloads()
    print(f"[RUDP] Waiting for file on port {RUDP_RECEIVE_PORT}...")
    receiver = RUDPReceiver(RUDP_RECEIVE_PORT)
    data     = receiver.receive_file()
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(data)
    print(f"[RUDP] Saved {len(data)} bytes → {filepath}")

# ─── Step 4b: Receive file over TCP ───────────────────────────

def receive_over_tcp(filename):
    """
    Listen on TCP_RECEIVE_PORT for the server to connect and send the file.
    Note the reversed roles: the client listens, the server connects.
    This is needed because the server knows when it's ready to send,
    and the client doesn't need to repeatedly check — it just waits.
    """
    ensure_downloads()
    print(f"[TCP] Waiting for file on port {TCP_RECEIVE_PORT}...")

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(('0.0.0.0', TCP_RECEIVE_PORT))
    server_sock.listen(1)
    server_sock.settimeout(60)

    conn, addr = server_sock.accept()
    print(f"[TCP] Server connected from {addr}, receiving...")

    data = b''
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk

    conn.close()
    server_sock.close()

    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(data)
    print(f"[TCP] Saved {len(data)} bytes → {filepath}")

# ─── Download Flow ────────────────────────────────────────────

def download_song(url, title, server_ip):
    """
    Coordinate a full song download using either RUDP or TCP.

    Important ordering: the receiver thread must start FIRST and bind its port
    before we send the HTTP request to the server. If the server tries to connect
    before we're listening, the transfer will fail.

    The small time.sleep() between starting the thread and sending the HTTP request
    gives the receiver thread enough time to call bind() and be ready.
    """
    safe_title = "".join(
        c for c in title if c.isalnum() or c in (' ', '-', '_')
    ).strip() + '.mp3'

    print("\nTransfer protocol:")
    print("  1. RUDP  (our custom reliable UDP)")
    print("  2. TCP   (standard reliable TCP)")
    proto = input("Choose (1/2): ").strip()

    encoded_url   = urllib.parse.quote(url, safe='')
    encoded_title = urllib.parse.quote(title, safe='')

    if proto == '2':
        # ── TCP transfer ──
        tcp_thread = threading.Thread(
            target=receive_over_tcp,
            args=(safe_title,),
            daemon=True
        )
        tcp_thread.start()
        time.sleep(0.3)   # give the listener time to call bind() before server connects

        path   = f"/download_tcp?url={encoded_url}&title={encoded_title}&client_port={TCP_RECEIVE_PORT}"
        print(f"\n[CLIENT] Requesting TCP download: {title}")
        result = http_get(path, server_ip)

        if result and result.get('status') == 'success':
            print("[CLIENT] Server confirmed. Receiving over TCP...")
            tcp_thread.join()
            print(f"\n✅ Done! Saved to client_downloads/{safe_title}")
        else:
            print(f"[CLIENT] Server error: {result}")
    else:
        # ── RUDP transfer (default) ──
        rudp_thread = threading.Thread(
            target=receive_over_rudp,
            args=(safe_title,),
            daemon=True
        )
        rudp_thread.start()
        time.sleep(0.5)   # RUDP needs a bit more time to set up than TCP

        path   = f"/download?url={encoded_url}&title={encoded_title}&client_port={RUDP_RECEIVE_PORT}"
        print(f"\n[CLIENT] Requesting RUDP download: {title}")
        result = http_get(path, server_ip)

        if result and result.get('status') == 'success':
            print("[CLIENT] Server confirmed. Receiving over RUDP...")
            rudp_thread.join()
            print(f"\n✅ Done! Saved to client_downloads/{safe_title}")
        else:
            print(f"[CLIENT] Server error: {result}")

# ─── Terminal Menu ────────────────────────────────────────────

def show_menu():
    print("\n" + "=" * 40)
    print("MUSIC AGENT CLIENT")
    print("=" * 40)
    print("1. Search by song name")
    print("2. Direct YouTube URL")
    print("3. Vibe mode (AI suggestions)")
    print("4. Show history")
    print("5. Exit")
    print("=" * 40)
    return input("Choose (1-5): ").strip()

def handle_search(query, path_suffix, server_ip):
    """
    Send a search or vibe request to the server, display the results list,
    and let the user pick one to download.
    """
    print("\n[CLIENT] Searching...")
    result = http_get(path_suffix, server_ip)

    if not result or result.get('status') != 'success':
        print("[CLIENT] Search failed or no results")
        return

    results = result.get('results') or result.get('search_results', [])
    if not results:
        print("[CLIENT] No results found")
        return

    print(f"\n{'─' * 40}")
    for i, r in enumerate(results):
        duration = r.get('duration', 0) or 0
        mins     = int(duration // 60)
        secs     = int(duration % 60)
        print(f"{i + 1}. {r['title']} ({mins}:{secs:02d})")
    print(f"{'─' * 40}")

    if 'gemini_suggestions' in result:
        print("✨ AI suggested:")
        for s in result['gemini_suggestions']:
            print(f"   - {s['title']} by {s['artist']}")

    choice = input("\nPick a number to download (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) == 0:
        return

    idx = int(choice) - 1
    if idx < 0 or idx >= len(results):
        print("[CLIENT] Invalid choice")
        return

    download_song(results[idx]['url'], results[idx]['title'], server_ip)

def show_history(server_ip):
    result = http_get('/history', server_ip)
    if not result or not result.get('history'):
        print("\n[CLIENT] No downloads yet")
        return
    print(f"\n{'─' * 40}")
    for song in result['history']:
        print(f"{song['title']} ({song['size_kb']} KB)")
    print(f"{'─' * 40}")

# ─── Main ─────────────────────────────────────────────────────

def main():
    """
    Startup sequence:
      1. DHCP  → get our IP and the DNS server address
      2. DNS   → resolve 'app.local' to the app server's IP
      3. Loop  → show menu, handle user input, make HTTP requests to app server
    """
    print("\n[CLIENT] Starting up...")

    my_ip, dns_ip = do_dhcp()
    print(f"[CLIENT] My IP: {my_ip} | DNS server: {dns_ip}")

    server_ip = do_dns('app.local', dns_ip)
    print(f"[CLIENT] App server at: {server_ip}")

    while True:
        choice = show_menu()

        if choice == '1':
            query   = input("Song name: ").strip()
            encoded = urllib.parse.quote(query, safe='')
            handle_search(query, f"/search?q={encoded}", server_ip)

        elif choice == '2':
            url   = input("YouTube URL: ").strip()
            title = input("Title (for filename): ").strip()
            download_song(url, title, server_ip)

        elif choice == '3':
            vibe    = input("Describe the vibe: ").strip()
            encoded = urllib.parse.quote(vibe, safe='')
            handle_search(vibe, f"/vibe?q={encoded}", server_ip)

        elif choice == '4':
            show_history(server_ip)

        elif choice == '5':
            print("\n[CLIENT] Goodbye!")
            break

        else:
            print("[CLIENT] Invalid choice, try again")

if __name__ == '__main__':
    main()