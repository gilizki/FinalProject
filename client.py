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
DHCP_SERVER   = '127.0.0.1'
DHCP_PORT     = 6767          # DHCP server port
DNS_SERVER    = '127.0.0.1'
DNS_PORT      = 53            # DNS server port
APP_PORT      = 5000          # app_server.py listens here
RUDP_RECEIVE_PORT = 5001      # we receive the MP3 on this port

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_downloads")

# ─── DHCP Constants (matches DHCP.py) ──────────────
MAGIC_COOKIE     = b'\x63\x82\x53\x63'
DHCP_DISCOVER    = 1
DHCP_OFFER       = 2
DHCP_REQUEST     = 3
DHCP_ACK         = 5
OPT_MESSAGE_TYPE = 53
OPT_REQUESTED_IP = 50
OPT_DNS_SERVER   = 6
OPT_END          = 255

# A fake but consistent MAC address for this client
MY_MAC = b'\xAA\xBB\xCC\xDD\xEE\x01'

# ─── DNS Constants (matches DNS.py) ────────────────
DNS_HEADER_FORMAT = '!HHHHHH'   # 6 × 2 bytes = 12 bytes

# ─── Helpers ────────────────────────────────────────────────

def ensure_downloads():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

def pack_dhcp_option(code, data):
    """Build a DHCP TLV option: [code][length][data]"""
    return struct.pack('!BB', code, len(data)) + data

def build_dhcp_packet(msg_type, xid, requested_ip=None):
    """
    Build a binary DHCP packet (DISCOVER or REQUEST).
    matches the BOOTP/DHCP binary format that the server parses.

    BOOTP header layout (236 bytes):
      op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
      ciaddr(4) yiaddr(4) siaddr(4) giaddr(4)
      chaddr(16) sname(64) file(128)
    Then: magic cookie (4) + options
    """
    chaddr = MY_MAC + b'\x00' * 10   # MAC padded to 16 bytes

    header = struct.pack(
        '!BBBB I HH 4s4s4s4s 16s 64s 128s',
        1,              # op = BOOTREQUEST
        1,              # htype = Ethernet
        6,              # hlen = MAC length
        0,              # hops
        xid,            # transaction ID (random number to match request/reply)
        0,              # secs
        0x8000,         # flags = broadcast
        b'\x00' * 4,    # ciaddr (client IP, unknown yet)
        b'\x00' * 4,    # yiaddr (your IP, filled by server)
        b'\x00' * 4,    # siaddr (server IP)
        b'\x00' * 4,    # giaddr (relay agent IP)
        chaddr,         # client hardware address
        b'\x00' * 64,   # sname
        b'\x00' * 128   # file
    )

    options = b''
    options += pack_dhcp_option(OPT_MESSAGE_TYPE, struct.pack('!B', msg_type))
    if requested_ip:
        # REQUEST must include the IP it got in the OFFER
        options += pack_dhcp_option(OPT_REQUESTED_IP, socket.inet_aton(requested_ip))
    options += struct.pack('!B', OPT_END)

    return header + MAGIC_COOKIE + options

def parse_dhcp_offer(data):
    """
    Extract the offered IP and DNS server IP from a DHCP OFFER/ACK packet.
    yiaddr (your IP address) is at bytes 16-20 in the BOOTP header.
    DNS server is in the options section (option code 6).
    """
    # yiaddr = offered IP, at fixed offset 16
    offered_ip = socket.inet_ntoa(data[16:20])

    # parse options to find DNS server
    dns_ip = None
    if data[236:240] == MAGIC_COOKIE:
        i = 240   # options start after magic cookie
        while i < len(data):
            code = data[i]
            if code == OPT_END:
                break
            if code == 0:   # PAD option
                i += 1
                continue
            length = data[i + 1]
            value  = data[i + 2: i + 2 + length]
            if code == OPT_DNS_SERVER and length == 4:
                dns_ip = socket.inet_ntoa(value)
            i += 2 + length

    return offered_ip, dns_ip

# ─── Step 1: DHCP ───────────────────────────────────────────

def do_dhcp():
    """
    Perform the DHCP DORA handshake (Discover → Offer → Request → Ack).
    Returns: (my_ip, dns_server_ip)
    """
    print("[CLIENT] Starting DHCP handshake...")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(5)

        xid = random.randint(1, 0xFFFFFFFF)   # random transaction ID

        # ── Step 1a: DISCOVER ──
        discover = build_dhcp_packet(DHCP_DISCOVER, xid)
        sock.sendto(discover, (DHCP_SERVER, DHCP_PORT))
        print("[DHCP] Sent DISCOVER")

        # ── Step 1b: Wait for OFFER ──
        offer_data, _ = sock.recvfrom(1024)
        offered_ip, dns_ip = parse_dhcp_offer(offer_data)
        print(f"[DHCP] Got OFFER → IP: {offered_ip}, DNS: {dns_ip}")

        # ── Step 1c: REQUEST ──
        request = build_dhcp_packet(DHCP_REQUEST, xid, requested_ip=offered_ip)
        sock.sendto(request, (DHCP_SERVER, DHCP_PORT))
        print(f"[DHCP] Sent REQUEST for {offered_ip}")

        # ── Step 1d: Wait for ACK ──
        ack_data, _ = sock.recvfrom(1024)
        my_ip, dns_from_ack = parse_dhcp_offer(ack_data)
        print(f"[DHCP] Got ACK → assigned IP: {my_ip}")

        sock.close()

        # use DNS IP from DHCP if available, otherwise fall back
        final_dns = dns_from_ack or dns_ip or '127.0.0.1'
        return my_ip, final_dns

    except Exception as e:
        print(f"[DHCP] Failed: {e}, using defaults")
        return '127.0.0.1', '127.0.0.1'

# ─── Step 2: DNS ────────────────────────────────────────────

def build_dns_query(hostname):
    """
    Build a binary DNS query packet for an A record (IPv4).

    DNS header: transaction_id(2) flags(2) qdcount(2) ancount(2) nscount(2) arcount(2)
    Question:   encoded_name + qtype(2) + qclass(2)
    """
    transaction_id = random.randint(1, 0xFFFF)

    # standard query flags: recursion desired
    flags    = 0x0100
    qdcount  = 1   # one question
    header   = struct.pack('!HHHHHH', transaction_id, flags, qdcount, 0, 0, 0)

    # encode hostname as DNS labels: "app.local" → \x03app\x05local\x00
    encoded_name = b''
    for part in hostname.encode().split(b'.'):
        encoded_name += struct.pack('!B', len(part)) + part
    encoded_name += b'\x00'   # end of name

    qtype  = struct.pack('!H', 1)   # A record
    qclass = struct.pack('!H', 1)   # IN (internet)

    return transaction_id, header + encoded_name + qtype + qclass

def parse_dns_response(data, expected_txid):
    """
    Extract the IP address from a DNS response.
    The answer section contains the resolved IP in the last 4 bytes of each answer record.
    """
    if len(data) < 12:
        return None

    txid = struct.unpack('!H', data[0:2])[0]
    if txid != expected_txid:
        return None

    ancount = struct.unpack('!H', data[6:8])[0]   # number of answers
    if ancount == 0:
        return None

    # skip header (12 bytes) + question section
    # question section: skip name (variable), then qtype(2) + qclass(2)
    i = 12
    # skip the question name
    while i < len(data):
        length = data[i]
        if length == 0:
            i += 1
            break
        if (length & 0xC0) == 0xC0:   # pointer (compression)
            i += 2
            break
        i += length + 1
    i += 4   # skip qtype + qclass

    # now we're at the answer section
    # answer record: name(2 pointer) type(2) class(2) ttl(4) rdlength(2) rdata(rdlength)
    if i + 10 <= len(data):
        # skip name pointer (2 bytes), type (2), class (2), ttl (4)
        i += 10
        rdlength = struct.unpack('!H', data[i: i + 2])[0]
        i += 2
        if rdlength == 4:
            return socket.inet_ntoa(data[i: i + 4])

    return None

def do_dns(hostname, dns_server_ip):
    """
    Query the DNS server for hostname, return its IP address.
    Uses the binary DNS protocol that matches DNS.py.
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
    Send a raw HTTP/1.1 GET request over TCP.
    Returns the parsed JSON response body as a Python dict.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(60)   # downloads can take a while
        sock.connect((server_ip, APP_PORT))

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {server_ip}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode('utf-8'))

        # read the full response
        response = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        sock.close()

        # HTTP response = headers + blank line + body
        body = response.split(b'\r\n\r\n', 1)[1].decode('utf-8')
        return json.loads(body)

    except Exception as e:
        print(f"[HTTP] Error on {path}: {e}")
        return None

# ─── Step 4: Receive file over RUDP ─────────────────────────

def receive_over_rudp(filename):
    """Open RUDP receiver, wait for incoming file, save to client_downloads/"""
    ensure_downloads()
    print(f"[RUDP] Waiting for file on port {RUDP_RECEIVE_PORT}...")
    receiver = RUDPReceiver(RUDP_RECEIVE_PORT)
    data = receiver.receive_file()
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(data)
    print(f"[RUDP] Saved {len(data)} bytes → {filepath}")
    return filepath

# ─── Download flow ───────────────────────────────────────────

def download_song(url, title, server_ip):
    """
    Full download flow:
    1. Start RUDP receiver thread (must be ready before server starts sending)
    2. Send HTTP GET /download to app server
    3. Wait for RUDP transfer to finish
    """
    safe_title = "".join(
        c for c in title if c.isalnum() or c in (' ', '-', '_')
    ).strip() + '.mp3'

    # start receiver FIRST so it's listening before server sends
    rudp_thread = threading.Thread(
        target=receive_over_rudp,
        args=(safe_title,),
        daemon=True
    )
    rudp_thread.start()
    time.sleep(0.5)   # give receiver time to bind to port

    # tell server to download and send
    encoded_url   = urllib.parse.quote(url, safe='')
    encoded_title = urllib.parse.quote(title, safe='')
    path = f"/download?url={encoded_url}&title={encoded_title}&client_port={RUDP_RECEIVE_PORT}"

    print(f"\n[CLIENT] Requesting download: {title}")
    result = http_get(path, server_ip)

    if result and result.get('status') == 'success':
        print("[CLIENT] Server confirmed. Receiving over RUDP...")
        rudp_thread.join()   # wait for full transfer
        print(f"\n✅ Done! Saved to client_downloads/{safe_title}")
    else:
        print(f"[CLIENT] Server error: {result}")

# ─── Terminal Menu ───────────────────────────────────────────

def show_menu():
    print("\n" + "=" * 40)
    print("MUSIC DOWNLOAD AGENT CLIENT")
    print("=" * 40)
    print("1. Search by song name")
    print("2. Direct YouTube URL")
    print("3. Vibe mode (AI suggestions)")
    print("4. Show history")
    print("5. Exit")
    print("=" * 40)
    return input("Choose (1-5): ").strip()

def handle_search(query, path_suffix, server_ip):
    """Search, show results, let user pick one to download"""
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
        mins = int(duration // 60)
        secs = int(duration % 60)
        print(f"{i + 1}. {r['title']} ({mins}:{secs:02d})")
    print(f"{'─' * 40}")

    if 'gemini_suggestions' in result:
        print("Gemini suggested:")
        for s in result['gemini_suggestions']:
            print(f"   - {s['title']} by {s['artist']}")

    choice = input("\nPick a number to download (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) == 0:
        return

    idx = int(choice) - 1
    if idx < 0 or idx >= len(results):
        print("[CLIENT] Invalid choice")
        return

    song = results[idx]
    download_song(song['url'], song['title'], server_ip)

def show_history(server_ip):
    result = http_get('/history', server_ip)
    if not result or not result.get('history'):
        print("\n[CLIENT] No downloads yet")
        return
    print(f"\n{'─' * 40}")
    for song in result['history']:
        print(f"🎵 {song['title']} ({song['size_kb']} KB)")
    print(f"{'─' * 40}")

# ─── Main ────────────────────────────────────────────────────
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
            query = input("Song name: ").strip()
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