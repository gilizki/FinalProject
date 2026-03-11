import socket
import os
import json
import struct
import threading
import time
import random
import urllib.parse
from rudp import RUDPReceiver, HEADER_SIZE, MAX_SEGMENT_SIZE

# ─── Constants ──────────────────────────────────────────────
DHCP_SERVER       = '127.0.0.1'
DHCP_PORT         = 6767          # DHCP server
DNS_PORT          = 53            # DNS server
APP_PORT          = 5000          # app_server.py HTTP port
RUDP_RECEIVE_PORT = 5001          # we receive MP3 over RUDP here
TCP_RECEIVE_PORT  = 5002          # we receive MP3 over TCP here

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_downloads")

# ─── DHCP Constants (must match DHCP.py) ──────────────
MAGIC_COOKIE     = b'\x63\x82\x53\x63'
DHCP_DISCOVER    = 1
DHCP_OFFER       = 2
DHCP_REQUEST     = 3
DHCP_ACK         = 5
OPT_MESSAGE_TYPE = 53
OPT_REQUESTED_IP = 50
OPT_DNS_SERVER   = 6
OPT_END          = 255

# Fake but consistent MAC address for this client
MY_MAC = b'\xAA\xBB\xCC\xDD\xEE\x01'

# ─── Helpers ─────────────────────────────────────────────────

def ensure_downloads():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

def pack_dhcp_option(code, data):
    """Build a DHCP option in TLV format: [code][length][data]"""
    return struct.pack('!BB', code, len(data)) + data

# ─── Step 1: DHCP ────────────────────────────────────────────

def build_dhcp_packet(msg_type, xid, requested_ip=None):
    """
    Build a binary DHCP packet matching the BOOTP format server parses.

    BOOTP header (236 bytes total):
      op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
      ciaddr(4) yiaddr(4) siaddr(4) giaddr(4)
      chaddr(16) sname(64) file(128)
    Then: magic cookie(4) + options
    """
    chaddr = MY_MAC + b'\x00' * 10   # MAC padded to 16 bytes

    header = struct.pack(
        '!BBBB I HH 4s4s4s4s 16s 64s 128s',
        1,             # op = BOOTREQUEST
        1,             # htype = Ethernet
        6,             # hlen = MAC address length
        0,             # hops
        xid,           # transaction ID (random, to match request with reply)
        0,             # secs elapsed
        0x8000,        # flags = broadcast
        b'\x00' * 4,   # ciaddr (our IP, unknown yet)
        b'\x00' * 4,   # yiaddr (filled by server in reply)
        b'\x00' * 4,   # siaddr (server IP)
        b'\x00' * 4,   # giaddr (relay agent, not used)
        chaddr,        # client hardware address
        b'\x00' * 64,  # sname
        b'\x00' * 128  # file
    )

    options = b''
    options += pack_dhcp_option(OPT_MESSAGE_TYPE, struct.pack('!B', msg_type))
    if requested_ip:
        # REQUEST must include the IP we received in the OFFER
        options += pack_dhcp_option(OPT_REQUESTED_IP, socket.inet_aton(requested_ip))
    options += struct.pack('!B', OPT_END)

    return header + MAGIC_COOKIE + options

def parse_dhcp_reply(data):
    """
    Extract offered IP and DNS server from a DHCP OFFER or ACK packet.
    yiaddr (your IP address) is always at bytes 16-20 in the BOOTP header.
    DNS server IP is in the options section (option code 6).
    """
    offered_ip = socket.inet_ntoa(data[16:20])

    dns_ip = None
    if len(data) > 240 and data[236:240] == MAGIC_COOKIE:
        i = 240   # options start right after the magic cookie
        while i < len(data):
            code = data[i]
            if code == OPT_END:
                break
            if code == 0:   # PAD option - skip 1 byte
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
    Full DHCP DORA handshake:
    Discover → Offer → Request → Ack
    Returns: (my_ip, dns_server_ip)
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

# ─── Step 2: DNS ─────────────────────────────────────────────

def build_dns_query(hostname):
    """
    Build a binary DNS query for an A record (IPv4 address lookup).

    DNS header: txid(2) flags(2) qdcount(2) ancount(2) nscount(2) arcount(2)
    Question:   encoded_name + qtype(2) + qclass(2)

    Name encoding: 'app.local' → \x03app\x05local\x00
    """
    txid   = random.randint(1, 0xFFFF)
    flags  = 0x0100   # standard query, recursion desired
    header = struct.pack('!HHHHHH', txid, flags, 1, 0, 0, 0)

    # encode each label: 'app.local' → b'\x03app\x05local\x00'
    encoded = b''
    for part in hostname.encode().split(b'.'):
        encoded += struct.pack('!B', len(part)) + part
    encoded += b'\x00'   # end of name marker

    question = encoded + struct.pack('!HH', 1, 1)   # qtype=A, qclass=IN
    return txid, header + question

def parse_dns_response(data, expected_txid):
    """
    Extract the resolved IP from a binary DNS response.
    Skips the question section and reads the first answer record.
    Answer record: name(2) type(2) class(2) ttl(4) rdlength(2) rdata(4)
    """
    if len(data) < 12:
        return None

    txid    = struct.unpack('!H', data[0:2])[0]
    if txid != expected_txid:
        return None

    ancount = struct.unpack('!H', data[6:8])[0]
    if ancount == 0:
        return None

    # skip the header and question section to reach the answer
    i = 12
    while i < len(data):
        length = data[i]
        if length == 0:
            i += 1
            break
        if (length & 0xC0) == 0xC0:   # DNS name compression pointer
            i += 2
            break
        i += length + 1
    i += 4   # skip qtype + qclass

    # read the answer record
    if i + 10 <= len(data):
        i      += 10   # skip name pointer(2) + type(2) + class(2) + ttl(4)
        rdlen   = struct.unpack('!H', data[i: i + 2])[0]
        i      += 2
        if rdlen == 4:
            return socket.inet_ntoa(data[i: i + 4])

    return None

def do_dns(hostname, dns_server_ip):
    """
    Query the DNS server for hostname using the binary DNS protocol.
    Returns the resolved IP address.
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

# ─── Step 3: HTTP to app server (raw TCP socket) ─────────────

def http_get(path, server_ip):
    """
    Send a raw HTTP/1.1 GET request over a TCP socket.
    Returns the parsed JSON body as a Python dict.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(60)   # downloads can take time
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
        body = response.split(b'\r\n\r\n', 1)[1].decode('utf-8')
        return json.loads(body)

    except Exception as e:
        print(f"[HTTP] Error on {path}: {e}")
        return None

# ─── Step 4a: Receive file over RUDP ─────────────────────────

def receive_over_rudp(filename):
    """Open RUDP receiver, wait for incoming file, save to client_downloads/"""
    ensure_downloads()
    print(f"[RUDP] Waiting for file on port {RUDP_RECEIVE_PORT}...")
    receiver = RUDPReceiver(RUDP_RECEIVE_PORT)
    data     = receiver.receive_file()
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(data)
    print(f"[RUDP] Saved {len(data)} bytes → {filepath}")

# ─── Step 4b: Receive file over TCP ──────────────────────────

def receive_over_tcp(filename):
    """
    Listen on a TCP port for the server to connect and send the file.
    Client listens, server connects and sends all bytes, then closes.
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
    Ask user which protocol to use, then:
    1. Start receiver thread FIRST (must be ready before server sends)
    2. Tell server to download + send via HTTP GET
    3. Wait for transfer to finish
    """
    safe_title = "".join(
        c for c in title if c.isalnum() or c in (' ', '-', '_')
    ).strip() + '.mp3'

    # ask which protocol
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
        time.sleep(0.3)   # give listener time to bind

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
        time.sleep(0.5)   # give receiver time to bind

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
    print("      🎵 MUSIC AGENT CLIENT")
    print("=" * 40)
    print("1. Search by song name")
    print("2. Direct YouTube URL")
    print("3. Vibe mode (AI suggestions)")
    print("4. Show history")
    print("5. Exit")
    print("=" * 40)
    return input("Choose (1-5): ").strip()

def handle_search(query, path_suffix, server_ip):
    """Search, display results, let user pick one to download"""
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
        print(f"🎵 {song['title']} ({song['size_kb']} KB)")
    print(f"{'─' * 40}")

# ─── Main ─────────────────────────────────────────────────────

def main():
    print("\n[CLIENT] Starting up...")

    # Step 1: get our IP + DNS server address from DHCP
    my_ip, dns_ip = do_dhcp()
    print(f"[CLIENT] My IP: {my_ip} | DNS server: {dns_ip}")

    # Step 2: resolve app server hostname via DNS
    server_ip = do_dns('app.local', dns_ip)
    print(f"[CLIENT] App server at: {server_ip}")

    # Step 3: menu loop
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
            print("\n[CLIENT] Goodbye! 🎵")
            break

        else:
            print("[CLIENT] Invalid choice, try again")

if __name__ == '__main__':
    main()