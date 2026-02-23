import socket
import struct

# --- Constants ---
SERVER_IP = '127.0.0.1'
SERVER_PORT = 67
CLIENT_PORT = 68
MAGIC_COOKIE = b'\x63\x82\x53\x63'

# Fake MAC address for testing: 11:22:33:44:55:66
MAC_BYTES = b'\x11\x22\x33\x44\x55\x66'


def pack_option(code, data):
    return struct.pack('!BB', code, len(data)) + data


def send_discover(sock):
    """Builds and sends a DHCP DISCOVER packet."""
    print("[Client] Sending DHCP DISCOVER...")
    xid = 12345  # Random transaction ID
    chaddr = MAC_BYTES + b'\x00' * 10

    # BOOTP Header
    header = struct.pack('!BBBB I HH 4s 4s 4s 4s 16s 64s 128s',
                         1, 1, 6, 0, xid, 0, 0x8000,
                         b'\x00' * 4, b'\x00' * 4, b'\x00' * 4, b'\x00' * 4,
                         chaddr, b'\x00' * 64, b'\x00' * 128)

    # DHCP Options (Message Type: 1 = DISCOVER)
    options = pack_option(53, struct.pack('!B', 1)) + struct.pack('!B', 255)

    packet = header + MAGIC_COOKIE + options
    sock.sendto(packet, (SERVER_IP, SERVER_PORT))


def send_request(sock, offered_ip):
    """Builds and sends a DHCP REQUEST packet for the offered IP."""
    print(f"[Client] Sending DHCP REQUEST for IP: {offered_ip}...")
    xid = 12346
    chaddr = MAC_BYTES + b'\x00' * 10

    header = struct.pack('!BBBB I HH 4s 4s 4s 4s 16s 64s 128s',
                         1, 1, 6, 0, xid, 0, 0x8000,
                         b'\x00' * 4, b'\x00' * 4, b'\x00' * 4, b'\x00' * 4,
                         chaddr, b'\x00' * 64, b'\x00' * 128)

    # Options: Message Type: 3 = REQUEST, Requested IP = offered_ip
    options = b''
    options += pack_option(53, struct.pack('!B', 3))
    options += pack_option(50, socket.inet_aton(offered_ip))
    options += struct.pack('!B', 255)

    packet = header + MAGIC_COOKIE + options
    sock.sendto(packet, (SERVER_IP, SERVER_PORT))


def run_test():
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    try:
        client_sock.bind(('0.0.0.0', CLIENT_PORT))
    except OSError as e:
        print(f"[Error] Could not bind to port {CLIENT_PORT}. Try running as Admin or change CLIENT_PORT.")
        return

    client_sock.settimeout(3)

    try:
        # Step 1: Send DISCOVER
        send_discover(client_sock)

        # Step 2: Receive OFFER
        data, _ = client_sock.recvfrom(1024)
        print("[Client] Received DHCP OFFER!")

        # Extract the offered IP (yiaddr is at bytes 16-19)
        offered_ip = socket.inet_ntoa(data[16:20])
        print(f"[Client] Server offered IP: {offered_ip}")

        # Step 3: Send REQUEST
        send_request(client_sock, offered_ip)

        # Step 4: Receive ACK
        data, _ = client_sock.recvfrom(1024)
        print("[Client] Received DHCP ACK! Test successful.")

    except socket.timeout:
        print("[Client] Test failed: Timeout waiting for server response.")
    finally:
        client_sock.close()


if __name__ == "__main__":
    run_test()