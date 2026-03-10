import socket
import struct
import time
import threading
import json

# Attempt to import dnspython for external forwarding
try:
    import dns.resolver

    FORWARDING_ENABLED = True
except ImportError:
    FORWARDING_ENABLED = False
    print("[WARNING] dnspython not installed - external forwarding disabled.")

# Network Constants
DNS_PORT = 53  # DNS port
BUFFER_SIZE = 1024

# TTL Configurations
LOCAL_TTL = 86400  # 24 hours for internal project domains
INTERNET_TTL = 300  # 5 minutes for external cached domains

# DNS Protocol Constants
DNS_HEADER_LENGTH = 12
FLAG_RESPONSE_OK = 0x8180
FLAG_RESPONSE_NXDOMAIN = 0x8183
TYPE_A = 1
CLASS_IN = 1
IPV4_LEN = 4
NAME_POINTER = 0xc00c

# Struct Formats
FORMAT_DNS_HEADER = '!HHHHH'
FORMAT_DNS_ANSWER = '!HHHIH4s'

#  DNS Database & Cache
# Static project domains (Matching your DHCP static IPs)
DNS_TABLE = {
    b'agent.local': '127.0.0.1',
    b'app.local': '127.0.0.1'
}

# Cache for external domains: domain_bytes -> (ip_string, expiry_timestamp)
dns_cache = {}


def dns_management_api():
    """מאזין לעדכונים משרת ה-DHCP ומשנה את טבלת ה-DNS"""
    mgmt_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mgmt_socket.bind(('127.0.0.1', 5354))
    print("[DNS API] Listening for DHCP updates on port 5354")

    while True:
        try:
            data, _ = mgmt_socket.recvfrom(1024)
            update = json.loads(data.decode('utf-8'))

            hostname = update.get("hostname").encode('utf-8')
            ip = update.get("ip")
            action = update.get("action")

            if action == "add":
                DNS_TABLE[hostname] = ip
                print(f"[DNS API] Added/Updated: {hostname.decode()} -> {ip}")
            elif action == "remove" and hostname in DNS_TABLE:
                del DNS_TABLE[hostname]
                print(f"[DNS API] Removed: {hostname.decode()}")

        except Exception as e:
            print(f"[DNS API] Error parsing update: {e}")





def query_internet(domain_bytes):
    """
    Forwards the query to an external DNS server (e.g., 8.8.8.8) using dnspython.
    """
    if not FORWARDING_ENABLED:
        return None

    try:
        # Convert bytes to string for dnspython
        domain_str = domain_bytes.decode('utf-8')
        answers = dns.resolver.resolve(domain_str, 'A')
        ip = str(answers[0])
        print(f"[INTERNET] Forwarded {domain_str} -> {ip}")
        return ip
    except Exception as e:
        print(f"[INTERNET] Failed to resolve {domain_bytes}: {e}")
        return None


def lookup(domain_bytes):
    """
    Search logic combining Static Table, Cache, and Internet Forwarding.
    Returns: (ip_address, ttl) or (None, 0)
    """
    # 1. Check Static Table (e.g., agent.local)
    if domain_bytes in DNS_TABLE:
        return DNS_TABLE[domain_bytes], LOCAL_TTL

    # 2. Check Cache
    if domain_bytes in dns_cache:
        ip, expiry = dns_cache[domain_bytes]
        if time.time() < expiry:
            print(f"[CACHE] Hit for {domain_bytes.decode('utf-8')} -> {ip}")
            return ip, INTERNET_TTL
        else:
            del dns_cache[domain_bytes]  # Expired

    # 3. Ask the Internet (Only if it's not a .local domain)
    if not domain_bytes.endswith(b'.local'):
        ip = query_internet(domain_bytes)
        if ip:
            # Save to cache
            dns_cache[domain_bytes] = (ip, time.time() + INTERNET_TTL)
            return ip, INTERNET_TTL

    # Not found anywhere
    return None, 0


def extract_domain_name(data, offset=DNS_HEADER_LENGTH):
    """Parses the DNS query format (e.g., 5'agent'5'local'0)"""
    domain_parts = []
    try:
        while True:
            if offset >= len(data): break
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
    """Builds the binary DNS response packet."""
    transaction_id = data[:2]
    _, qdcount, _, _, _ = struct.unpack(FORMAT_DNS_HEADER, data[2:12])

    domain_bytes, question_end_offset = extract_domain_name(data)
    question_section = data[DNS_HEADER_LENGTH: question_end_offset + 4]

    print(f"[DNS Server] Query received for: {domain_bytes.decode('utf-8', errors='ignore')}")

    # Use the lookup function
    target_ip, ttl = lookup(domain_bytes)

    if target_ip:
        print(f"[DNS Server] Sending IP: {target_ip} (TTL: {ttl})")
        response_header = transaction_id + struct.pack(FORMAT_DNS_HEADER, FLAG_RESPONSE_OK, qdcount, 1, 0, 0)
        answer_section = struct.pack(FORMAT_DNS_ANSWER,
                                     NAME_POINTER, TYPE_A, CLASS_IN,
                                     ttl, IPV4_LEN, socket.inet_aton(target_ip))
        return response_header + question_section + answer_section

    else:
        print(f"[DNS Server] NXDOMAIN: Domain not found.")
        response_header = transaction_id + struct.pack(FORMAT_DNS_HEADER, FLAG_RESPONSE_NXDOMAIN, qdcount, 0, 0, 0)
        return response_header + question_section


def main():
    dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # Binding to port 53 to intercept real DNS traffic
        dns_socket.bind(('0.0.0.0', DNS_PORT))
        print(f"[DNS Server] Running on UDP Port {DNS_PORT}")
        print(f"[DNS Server] Forwarding: {'ON' if FORWARDING_ENABLED else 'OFF'}")
    except socket.error as e:
        print(f"[DNS Server] Error: Could not bind to port {DNS_PORT}. Try running as Administrator/root. {e}")
        return

    threading.Thread(target=dns_management_api, daemon=True).start()

    while True:
        try:
            data, client_address = dns_socket.recvfrom(BUFFER_SIZE)
            if len(data) >= DNS_HEADER_LENGTH:
                response_packet = build_dns_response(data)
                dns_socket.sendto(response_packet, client_address)
        except Exception as e:
            print(f"[DNS Server] Error handling query: {e}")


if __name__ == "__main__":
    main()