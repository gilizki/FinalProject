import socket
import struct
import time
import threading
import json

# We use dnspython to forward queries we can't answer ourselves (e.g. google.com)
try:
    import dns.resolver
    FORWARDING_ENABLED = True
except ImportError:
    FORWARDING_ENABLED = False
    print("[WARNING] dnspython not installed - external forwarding disabled.")

# ─── Constants ───────────────────────────────────────────────
DNS_PORT    = 53      # standard DNS port (requires admin/root to bind)
BUFFER_SIZE = 1024

# TTL = how long a client is allowed to cache this answer before asking again
LOCAL_TTL    = 86400  # 24 hours for our own project domains (they don't change)
INTERNET_TTL = 300    # 5 minutes for external domains (they could change)

# DNS protocol constants (from RFC 1035)
DNS_HEADER_LENGTH  = 12
FLAG_RESPONSE_OK       = 0x8180  # standard response, no error
FLAG_RESPONSE_NXDOMAIN = 0x8183  # name doesn't exist
TYPE_A     = 1   # A record = IPv4 address lookup
CLASS_IN   = 1   # IN = Internet class
IPV4_LEN   = 4   # IPv4 address is 4 bytes
NAME_POINTER = 0xc00c  # DNS compression pointer back to the name in the question section

# Struct formats for packing/unpacking binary DNS fields
FORMAT_DNS_HEADER = '!HHHHH'           # txid, flags, qdcount, ancount, nscount (note: missing arcount — we skip it)
FORMAT_DNS_ANSWER = '!HHHIH4s'         # name, type, class, ttl, rdlength, rdata

# ─── DNS Table ───────────────────────────────────────────────
# Our static local DNS table for project domains.
# These are the hostnames our client uses to find the servers.
# The DHCP server can also add entries here dynamically via notify_dns().
DNS_TABLE = {
    b'agent.local': '10.0.0.10',
    b'app.local':   '10.0.0.10'
}

# Cache for external domain answers so we don't forward every single query.
# Structure: domain_bytes → (ip_string, expiry_timestamp)
dns_cache = {}


def dns_management_api():
    """
    Runs in a background thread. Listens on port 5354 for JSON messages from
    the DHCP server that tell us to add or remove hostname→IP mappings.

    Example message:
      {"action": "add", "hostname": "host-aabbccddee01.local", "ip": "127.0.0.100"}

    This is how DHCP and DNS stay in sync — when a client gets an IP from DHCP,
    DHCP immediately tells DNS about it so the hostname becomes resolvable.
    """
    mgmt_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mgmt_socket.bind(('127.0.0.1', 5354))
    print("[DNS API] Listening for DHCP updates on port 5354")

    while True:
        try:
            data, _ = mgmt_socket.recvfrom(1024)
            update   = json.loads(data.decode('utf-8'))

            hostname = update.get("hostname").encode('utf-8')
            ip       = update.get("ip")
            action   = update.get("action")

            if action == "add":
                DNS_TABLE[hostname] = ip
                print(f"[DNS API] Added/Updated: {hostname.decode()} → {ip}")
            elif action == "remove" and hostname in DNS_TABLE:
                del DNS_TABLE[hostname]
                print(f"[DNS API] Removed: {hostname.decode()}")

        except Exception as e:
            print(f"[DNS API] Error parsing update: {e}")


def query_internet(domain_bytes):
    """
    If we don't have a local answer, forward the query to Google's DNS (8.8.8.8)
    using the dnspython library. This lets us resolve real domains like google.com.
    Only called for non-.local domains.
    """
    if not FORWARDING_ENABLED:
        return None

    try:
        domain_str = domain_bytes.decode('utf-8')
        answers    = dns.resolver.resolve(domain_str, 'A')
        ip         = str(answers[0])
        print(f"[INTERNET] Forwarded {domain_str} → {ip}")
        return ip
    except Exception as e:
        print(f"[INTERNET] Failed to resolve {domain_bytes}: {e}")
        return None


def lookup(domain_bytes):
    """
    Three-stage lookup for a domain name:
      1. Check our static DNS table (local project domains)
      2. Check our cache (previously forwarded external domains)
      3. Forward to the internet if it's not a .local domain

    Returns (ip, ttl) or (None, 0) if not found anywhere.
    """
    # Stage 1: static table (fastest — always check here first)
    if domain_bytes in DNS_TABLE:
        return DNS_TABLE[domain_bytes], LOCAL_TTL

    # Stage 2: cache — check if we already looked this up recently
    if domain_bytes in dns_cache:
        ip, expiry = dns_cache[domain_bytes]
        if time.time() < expiry:
            print(f"[CACHE] Hit for {domain_bytes.decode('utf-8')} → {ip}")
            return ip, INTERNET_TTL
        else:
            del dns_cache[domain_bytes]   # expired, remove it

    # Stage 3: forward to internet (only for real domains, not .local)
    if not domain_bytes.endswith(b'.local'):
        ip = query_internet(domain_bytes)
        if ip:
            dns_cache[domain_bytes] = (ip, time.time() + INTERNET_TTL)
            return ip, INTERNET_TTL

    return None, 0


def extract_domain_name(data, offset=DNS_HEADER_LENGTH):
    """
    DNS uses a special wire format for domain names.
    'app.local' is encoded as: \x03app\x05local\x00
    (each label is prefixed with its length, terminated by a zero byte)

    We read each label until we hit a zero byte, then join them with dots.
    Returns (domain_as_bytes, offset_after_name).
    """
    domain_parts = []
    try:
        while True:
            if offset >= len(data):
                break
            part_length = data[offset]
            if part_length == 0:
                offset += 1
                break
            part = data[offset + 1: offset + 1 + part_length]
            domain_parts.append(part)
            offset += part_length + 1
        return b'.'.join(domain_parts), offset
    except IndexError:
        return b'', offset


def build_dns_response(data):
    """
    Given a raw DNS query packet, build the correct binary response.

    DNS response structure:
      Header (12 bytes): txid, flags, qdcount, ancount, nscount, arcount
      Question section: copy of the question from the query (so client can match it)
      Answer section:   name pointer + type + class + ttl + rdlength + IP bytes
    """
    transaction_id = data[:2]
    _, qdcount, _, _, _ = struct.unpack(FORMAT_DNS_HEADER, data[2:12])

    domain_bytes, question_end_offset = extract_domain_name(data)
    # Copy the full question section (name + qtype + qclass) into our response
    question_section = data[DNS_HEADER_LENGTH: question_end_offset + 4]

    print(f"[DNS Server] Query received for: {domain_bytes.decode('utf-8', errors='ignore')}")

    target_ip, ttl = lookup(domain_bytes)

    if target_ip:
        print(f"[DNS Server] Sending IP: {target_ip} (TTL: {ttl})")
        # ancount=1 means we have one answer record
        response_header = (transaction_id +
                           struct.pack(FORMAT_DNS_HEADER, FLAG_RESPONSE_OK, qdcount, 1, 0, 0))
        # NAME_POINTER (0xc00c) is a compression pointer back to the name in the question
        answer_section = struct.pack(
            FORMAT_DNS_ANSWER,
            NAME_POINTER, TYPE_A, CLASS_IN,
            ttl, IPV4_LEN, socket.inet_aton(target_ip)
        )
        return response_header + question_section + answer_section

    else:
        print(f"[DNS Server] NXDOMAIN: Domain not found.")
        # ancount=0 means no answer — NXDOMAIN
        response_header = (transaction_id +
                           struct.pack(FORMAT_DNS_HEADER, FLAG_RESPONSE_NXDOMAIN, qdcount, 0, 0, 0))
        return response_header + question_section


def main():
    """
    Main server loop. Binds to 0.0.0.0:53 so it accepts queries from any machine.
    Each incoming query gets a response built and sent back in the same loop iteration
    (DNS is stateless — one query, one response, no connection needed).
    The DHCP management listener runs in a separate daemon thread.
    """
    dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        dns_socket.bind(('0.0.0.0', DNS_PORT))
        print(f"[DNS Server] Running on UDP Port {DNS_PORT}")
        print(f"[DNS Server] Forwarding: {'ON' if FORWARDING_ENABLED else 'OFF'}")
    except socket.error as e:
        print(f"[DNS Server] Error: Could not bind to port {DNS_PORT}. "
              f"Try running as Administrator/root. {e}")
        return

    # Start the DHCP update listener in the background
    threading.Thread(target=dns_management_api, daemon=True).start()

    try:
        while True:
            try:
                data, client_address = dns_socket.recvfrom(BUFFER_SIZE)
                if len(data) >= DNS_HEADER_LENGTH:
                    response_packet = build_dns_response(data)
                    dns_socket.sendto(response_packet, client_address)
            except Exception as e:
                print(f"[DNS Server] Error handling query: {e}")
    except KeyboardInterrupt:
        print("\n[DNS Server] Server shut down gracefully by user.")
    finally:
        dns_socket.close()

if __name__ == "__main__":
    main()