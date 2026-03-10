import socket
import struct
import time
import json

# Network & DHCP Constants
DHCP_SERVER_IP = "192.168.1.1"
DHCP_SERVER_PORT = 6767
CLIENT_PORT = 68
BUFFER_SIZE = 1024

POOL_START, POOL_END = 100, 199
SUBNET_MASK = "255.255.255.0"
GATEWAY = "192.168.1.1"
DNS_SERVER = "192.168.1.53"
LEASE_TIME = 600  # 10 minutes lease
OFFER_TIMEOUT = 10  # 10 seconds to respond to an offer

MAGIC_COOKIE = b'\x63\x82\x53\x63'

# Message Types
DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_ACK = 5
DHCP_NAK = 6
DHCP_RELEASE = 7

# Option Codes
OPT_PAD, OPT_SUBNET_MASK, OPT_DNS_SERVER = 0, 1, 6
OPT_REQUESTED_IP, OPT_LEASE_TIME, OPT_MESSAGE_TYPE = 50, 51, 53
OPT_SERVER_ID, OPT_END = 54, 255

# BOOTP Header Constants
BOOTP_OP_REPLY = 2
HW_TYPE_ETHERNET = 1
HW_LEN_MAC = 6
BROADCAST_FLAG = 0x8000
OPTIONS_OFFSET = 240


class DHCPServer:
    def __init__(self):
        # Dynamic pool for regular clients
        self.available_ips = [f"192.168.1.{i}" for i in range(POOL_START, POOL_END + 1)]

        # Static IPs for project servers (DNS, Agent)
        self.static_ips = {
            "00:11:22:33:44:55": "192.168.1.53",  # DNS Server
            "aa:bb:cc:dd:ee:ff": "192.168.1.80"  # Agent Server
        }

        self.assigned_ips = {}  # mac_str -> (ip, expiry_timestamp)
        self.pending_offers = {}  # mac_str -> (ip, offer_timestamp)

    def cleanup(self):
        """Cleans up expired offers and leases to free up IPs in the pool."""
        now = time.time()

        # Clean up expired offers (Client didn't complete DORA)
        expired_offers = [mac for mac, (ip, t) in self.pending_offers.items() if now - t > OFFER_TIMEOUT]
        for mac in expired_offers:
            ip, _ = self.pending_offers.pop(mac)
            if ip not in self.static_ips.values():
                self.available_ips.insert(0, ip)
            print(f"[CLEANUP] Offer expired: {ip} returned to pool.")

        # Clean up expired leases (Lease time passed without renewal)
        expired_leases = [mac for mac, (ip, exp) in self.assigned_ips.items() if now > exp]
        for mac in expired_leases:
            ip, _ = self.assigned_ips.pop(mac)
            if ip not in self.static_ips.values():
                self.available_ips.insert(0, ip)
            print(f"[CLEANUP] Lease expired: {ip} from {mac} returned to pool.")
            clean_mac = mac.replace(':', '')
            hostname = f"host-{clean_mac}.local"
            self.notify_dns(hostname, ip, "remove")

    def pack_dhcp_option(self, opt_code, data_bytes):
        """Packs a DHCP option into the standard Type-Length-Value format."""
        return struct.pack('!BB', opt_code, len(data_bytes)) + data_bytes

    def parse_dhcp_packet(self, data):
        """Extracts XID, MAC address, Message Type, and Requested IP from binary packet."""
        if len(data) < OPTIONS_OFFSET + len(MAGIC_COOKIE):
            return None, None, None, None, None

        xid = struct.unpack('!I', data[4:8])[0]
        mac_bytes = data[28:34]
        mac_str = ':'.join(f'{b:02x}' for b in mac_bytes)

        if data[236:240] != MAGIC_COOKIE:
            return None, None, None, None, None

        options_data = data[OPTIONS_OFFSET:]
        i, msg_type, requested_ip = 0, None, None

        while i < len(options_data):
            opt_code = options_data[i]
            if opt_code == OPT_END: break
            if opt_code == OPT_PAD:
                i += 1
                continue

            opt_len = options_data[i + 1]
            opt_val = options_data[i + 2: i + 2 + opt_len]

            if opt_code == OPT_MESSAGE_TYPE and opt_len == 1:
                msg_type = opt_val[0]
            elif opt_code == OPT_REQUESTED_IP and opt_len == 4:
                requested_ip = socket.inet_ntoa(opt_val)

            i += 2 + opt_len

        return xid, mac_bytes, mac_str, msg_type, requested_ip

    def create_dhcp_response(self, xid, mac_bytes, offered_ip, msg_type_code):
        """Builds the binary DHCP response packet."""
        ciaddr = socket.inet_aton('0.0.0.0')
        yiaddr = socket.inet_aton(offered_ip) if offered_ip else socket.inet_aton('0.0.0.0')
        siaddr = socket.inet_aton(DHCP_SERVER_IP)
        giaddr = socket.inet_aton('0.0.0.0')
        chaddr = mac_bytes + b'\x00' * 10
        sname, file_name = b'\x00' * 64, b'\x00' * 128

        bootp_header = struct.pack('!BBBB I HH 4s 4s 4s 4s 16s 64s 128s',
                                   BOOTP_OP_REPLY, HW_TYPE_ETHERNET, HW_LEN_MAC, 0,
                                   xid, 0, BROADCAST_FLAG,
                                   ciaddr, yiaddr, siaddr, giaddr, chaddr, sname, file_name)

        options = b''
        options += self.pack_dhcp_option(OPT_MESSAGE_TYPE, struct.pack('!B', msg_type_code))
        options += self.pack_dhcp_option(OPT_SERVER_ID, socket.inet_aton(DHCP_SERVER_IP))

        if msg_type_code in [DHCP_OFFER, DHCP_ACK]:
            options += self.pack_dhcp_option(OPT_LEASE_TIME, struct.pack('!I', LEASE_TIME))
            options += self.pack_dhcp_option(OPT_SUBNET_MASK, socket.inet_aton(SUBNET_MASK))
            options += self.pack_dhcp_option(OPT_DNS_SERVER, socket.inet_aton(DNS_SERVER))

        options += struct.pack('!B', OPT_END)
        return bootp_header + MAGIC_COOKIE + options

    def notify_dns(self, hostname, ip, action="add"):
        """שולח עדכון לשרת ה-DNS המקומי"""
        try:
            update_data = json.dumps({
                "action": action,
                "hostname": hostname,
                "ip": ip
            }).encode('utf-8')

            # שולח את העדכון לפורט הניהול של ה-DNS
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(update_data, ('127.0.0.1', 5353))
            sock.close()
        except Exception as e:
            print(f"[DHCP] Failed to notify DNS: {e}")


    def start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', DHCP_SERVER_PORT))
            s.settimeout(1)

            print(f"[DHCP] Server is running on port {DHCP_SERVER_PORT}.")
            print(f"[DHCP] Dynamic Pool: {len(self.available_ips)} IPs, Static Leases: {len(self.static_ips)}")

            while True:
                try:
                    data, addr = s.recvfrom(BUFFER_SIZE)
                    xid, mac_bytes, mac_str, m_type, requested_ip = self.parse_dhcp_packet(data)

                    if not xid or not m_type:
                        continue

                    # DISCOVER -> Server sends OFFER
                    if m_type == DHCP_DISCOVER:
                        ip = None

                        # 1. Static IP Check
                        if mac_str in self.static_ips:
                            ip = self.static_ips[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} -> Static IP match: {ip}")

                        # 2. Already Assigned Check
                        elif mac_str in self.assigned_ips:
                            ip, _ = self.assigned_ips[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} -> Re-offering locked IP: {ip}")

                        # 3. Pending Offer Check
                        elif mac_str in self.pending_offers:
                            ip, _ = self.pending_offers[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} -> Re-offering pending IP: {ip}")

                        # 4. New Dynamic IP
                        elif self.available_ips:
                            ip = self.available_ips.pop(0)
                            self.pending_offers[mac_str] = (ip, time.time())
                            print(f"[DHCP] DISCOVER from {mac_str} -> New dynamic offer: {ip}")

                        if ip:
                            resp_packet = self.create_dhcp_response(xid, mac_bytes, ip, DHCP_OFFER)
                            s.sendto(resp_packet, ('255.255.255.255', CLIENT_PORT))
                        else:
                            print(f"[DHCP] NAK: Pool empty for {mac_str}")

                    # REQUEST -> Server locks IP and sends ACK (or NAK)
                    elif m_type == DHCP_REQUEST:
                        # Find expected IP (Static, Pending, or Assigned)
                        expected_ip = self.static_ips.get(mac_str)
                        if not expected_ip and mac_str in self.pending_offers:
                            expected_ip = self.pending_offers[mac_str][0]
                        if not expected_ip and mac_str in self.assigned_ips:
                            expected_ip = self.assigned_ips[mac_str][0]

                        if requested_ip == expected_ip:
                            # Lock the IP and set expiry
                            expiry = time.time() + LEASE_TIME
                            self.assigned_ips[mac_str] = (requested_ip, expiry)

                            # Remove from pending if it was there
                            if mac_str in self.pending_offers:
                                del self.pending_offers[mac_str]

                            resp_packet = self.create_dhcp_response(xid, mac_bytes, requested_ip, DHCP_ACK)
                            s.sendto(resp_packet, ('255.255.255.255', CLIENT_PORT))
                            print(f"[DHCP] ACK: {requested_ip} locked/renewed for {mac_str}")

                            clean_mac = mac_str.replace(':', '')
                            hostname = f"host-{clean_mac}.local"
                            self.notify_dns(hostname, requested_ip, "add")

                        else:
                            # Send NAK if invalid request
                            resp_packet = self.create_dhcp_response(xid, mac_bytes, None, DHCP_NAK)
                            s.sendto(resp_packet, ('255.255.255.255', CLIENT_PORT))
                            print(f"[DHCP] NAK: Bad request from {mac_str} for IP {requested_ip}")

                    # RELEASE -> Client gives up IP, return to pool
                    elif m_type == DHCP_RELEASE:
                        if mac_str in self.assigned_ips:
                            ip, _ = self.assigned_ips.pop(mac_str)
                            if ip not in self.static_ips.values():
                                self.available_ips.insert(0, ip)
                            print(f"[DHCP] RELEASE: {ip} returned to pool from {mac_str}")
                            clean_mac = mac_str.replace(':', '')
                            hostname = f"host-{clean_mac}.local"
                            self.notify_dns(hostname, ip, "remove")

                except socket.timeout:
                    self.cleanup()
                except Exception as e:
                    print(f"[ERROR] {e}")


if __name__ == "__main__":
    try:
        DHCPServer().start()
    except KeyboardInterrupt:
        print("\n[DHCP] Server shut down gracefully by user.")