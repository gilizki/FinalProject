import socket
import struct
import time
import json

# ─── Constants ───────────────────────────────────────────────
# The server listens on port 6767 instead of the real DHCP port (67)
# because port 67 requires root/admin privileges on most systems.
# Using 6767 lets us run without sudo during development.
DHCP_SERVER_IP   = "10.0.0.10"
DHCP_SERVER_PORT = 6767
CLIENT_PORT      = 68        # standard DHCP client port (not used here, just for reference)
BUFFER_SIZE      = 1024

# IP pool: clients get addresses in the range 127.0.0.100 - 127.0.0.199
POOL_START, POOL_END = 100, 199
SUBNET_MASK = "255.0.0.0"
GATEWAY     = "127.0.0.1"
DNS_SERVER  = "10.0.0.10"
LEASE_TIME  = 600   # how long a client "owns" its IP (in seconds) = 10 minutes
OFFER_TIMEOUT = 10  # if a client doesn't REQUEST within 10 seconds, we take back the offer

# The magic cookie is a fixed 4-byte value that marks the start of the options section.
# Every DHCP packet must have this at byte 236, otherwise we ignore the packet.
MAGIC_COOKIE = b'\x63\x82\x53\x63'

# DHCP message type numbers (defined in RFC 2131)
DHCP_DISCOVER = 1   # client: "I need an IP"
DHCP_OFFER    = 2   # server: "here's one you can have"
DHCP_REQUEST  = 3   # client: "I want the IP you offered"
DHCP_ACK      = 5   # server: "confirmed, it's yours"
DHCP_NAK      = 6   # server: "no, something is wrong"
DHCP_RELEASE  = 7   # client: "I'm done with this IP, take it back"

# DHCP option codes (TLV format: code + length + value)
OPT_PAD          = 0    # padding byte, no length field
OPT_SUBNET_MASK  = 1
OPT_DNS_SERVER   = 6
OPT_REQUESTED_IP = 50   # client puts the IP it wants here (in REQUEST)
OPT_LEASE_TIME   = 51
OPT_MESSAGE_TYPE = 53   # every DHCP packet must have this option
OPT_SERVER_ID    = 54   # server puts its own IP here so client knows who answered
OPT_END          = 255  # marks end of options section

# BOOTP (the protocol DHCP is built on top of) header constants
BOOTP_OP_REPLY    = 2   # op=2 means this is a server reply
HW_TYPE_ETHERNET  = 1   # hardware type: Ethernet
HW_LEN_MAC        = 6   # MAC address is 6 bytes
BROADCAST_FLAG    = 0x8000  # tells the client to accept our reply even without an IP yet
OPTIONS_OFFSET    = 240     # options start 240 bytes into the packet (after header + magic cookie)


class DHCPServer:
    def __init__(self):
        # The dynamic pool is just a list of available IP strings.
        # We pop from the front when a new client connects, and insert back when they leave.
        self.available_ips = [f"127.0.0.{i}" for i in range(POOL_START, POOL_END + 1)]

        # Static IPs are pre-assigned to specific MACs (our DNS and Agent servers).
        # These never come from the pool and never expire.
        self.static_ips = {
            "00:11:22:33:44:55": "127.0.0.1",   # DNS server
            "aa:bb:cc:dd:ee:ff": "127.0.0.1"    # Agent server
        }

        # Once we send an OFFER, we hold the IP here until the client sends REQUEST.
        # key = mac_str, value = (ip, timestamp_when_we_offered)
        self.pending_offers = {}

        # Once we send an ACK, the IP is locked to the client until the lease expires.
        # key = mac_str, value = (ip, expiry_timestamp)
        self.assigned_ips = {}

    def cleanup(self):
        """
        Called every second (on socket timeout).
        Checks for expired offers and expired leases, returns those IPs to the pool.
        This simulates what a real DHCP server does to avoid running out of addresses.
        """
        now = time.time()

        # If a client got an OFFER but never sent REQUEST within OFFER_TIMEOUT seconds,
        # we assume they crashed or left — take the IP back.
        expired_offers = [mac for mac, (ip, t) in self.pending_offers.items()
                          if now - t > OFFER_TIMEOUT]
        for mac in expired_offers:
            ip, _ = self.pending_offers.pop(mac)
            if ip not in self.static_ips.values():
                self.available_ips.insert(0, ip)
            print(f"[CLEANUP] Offer expired: {ip} returned to pool.")

        # If a client's lease time ran out without renewal, take the IP back.
        expired_leases = [mac for mac, (ip, exp) in self.assigned_ips.items()
                          if now > exp]
        for mac in expired_leases:
            ip, _ = self.assigned_ips.pop(mac)
            if ip not in self.static_ips.values():
                self.available_ips.insert(0, ip)
            print(f"[CLEANUP] Lease expired: {ip} from {mac} returned to pool.")
            # Also tell DNS to remove this client's hostname record
            clean_mac = mac.replace(':', '')
            hostname  = f"host-{clean_mac}.local"
            self.notify_dns(hostname, ip, "remove")

    def pack_dhcp_option(self, opt_code, data_bytes):
        """
        DHCP options use TLV format: [1 byte code][1 byte length][N bytes value]
        This helper just packs that structure cleanly.
        """
        return struct.pack('!BB', opt_code, len(data_bytes)) + data_bytes

    def parse_dhcp_packet(self, data):
        """
        Read the raw bytes of an incoming DHCP packet and pull out the fields we care about:
        - xid: transaction ID (random number client chose, we echo it back in every reply)
        - mac_bytes / mac_str: client's hardware address
        - msg_type: DISCOVER / REQUEST / RELEASE
        - requested_ip: the IP the client is asking for (only in REQUEST packets)
        """
        if len(data) < OPTIONS_OFFSET + len(MAGIC_COOKIE):
            return None, None, None, None, None

        xid      = struct.unpack('!I', data[4:8])[0]
        mac_bytes = data[28:34]
        mac_str   = ':'.join(f'{b:02x}' for b in mac_bytes)

        # Validate magic cookie at the expected offset
        if data[236:240] != MAGIC_COOKIE:
            return None, None, None, None, None

        # Walk the options section byte by byte
        options_data = data[OPTIONS_OFFSET:]
        i, msg_type, requested_ip = 0, None, None

        while i < len(options_data):
            opt_code = options_data[i]
            if opt_code == OPT_END:
                break
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
        """
        Build the binary DHCP response packet to send back to the client.

        BOOTP header layout (236 bytes):
          op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
          ciaddr(4) yiaddr(4) siaddr(4) giaddr(4)
          chaddr(16) sname(64) file(128)
        Then: magic cookie(4) + options

        yiaddr = "your IP address" — this is where we tell the client what IP they get.
        """
        ciaddr  = socket.inet_aton('0.0.0.0')
        yiaddr  = socket.inet_aton(offered_ip) if offered_ip else socket.inet_aton('0.0.0.0')
        siaddr  = socket.inet_aton(DHCP_SERVER_IP)
        giaddr  = socket.inet_aton('0.0.0.0')
        chaddr  = mac_bytes + b'\x00' * 10   # MAC padded to 16 bytes
        sname   = b'\x00' * 64
        file_name = b'\x00' * 128

        bootp_header = struct.pack(
            '!BBBB I HH 4s 4s 4s 4s 16s 64s 128s',
            BOOTP_OP_REPLY, HW_TYPE_ETHERNET, HW_LEN_MAC, 0,
            xid, 0, BROADCAST_FLAG,
            ciaddr, yiaddr, siaddr, giaddr, chaddr, sname, file_name
        )

        # Build options section
        options = b''
        options += self.pack_dhcp_option(OPT_MESSAGE_TYPE, struct.pack('!B', msg_type_code))
        options += self.pack_dhcp_option(OPT_SERVER_ID, socket.inet_aton(DHCP_SERVER_IP))

        # OFFER and ACK also include lease time, subnet mask, and DNS server
        if msg_type_code in [DHCP_OFFER, DHCP_ACK]:
            options += self.pack_dhcp_option(OPT_LEASE_TIME, struct.pack('!I', LEASE_TIME))
            options += self.pack_dhcp_option(OPT_SUBNET_MASK, socket.inet_aton(SUBNET_MASK))
            options += self.pack_dhcp_option(OPT_DNS_SERVER, socket.inet_aton(DNS_SERVER))

        options += struct.pack('!B', OPT_END)
        return bootp_header + MAGIC_COOKIE + options

    def notify_dns(self, hostname, ip, action="add"):
        """
        After we assign or release an IP, we tell the DNS server about it
        so it can add/remove the hostname → IP mapping dynamically.
        We use a simple JSON message over UDP to port 5354 (our DNS management port).
        """
        try:
            update_data = json.dumps({
                "action":   action,
                "hostname": hostname,
                "ip":       ip
            }).encode('utf-8')

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(update_data, ('127.0.0.1', 5354))
            sock.close()
        except Exception as e:
            print(f"[DHCP] Failed to notify DNS: {e}")

    def start(self):
        """
        Main server loop. Binds to 0.0.0.0 so it accepts packets from any machine,
        not just localhost. The socket timeout of 1 second lets us run cleanup()
        periodically even when no packets are arriving.

        DORA flow we handle here:
          Client sends DISCOVER → we send OFFER
          Client sends REQUEST  → we send ACK (or NAK if something is wrong)
          Client sends RELEASE  → we free the IP
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', DHCP_SERVER_PORT))
            s.settimeout(1)

            print(f"[DHCP] Server is running on port {DHCP_SERVER_PORT}.")
            print(f"[DHCP] Dynamic Pool: {len(self.available_ips)} IPs, "
                  f"Static Leases: {len(self.static_ips)}")

            while True:
                try:
                    data, addr = s.recvfrom(BUFFER_SIZE)
                    xid, mac_bytes, mac_str, m_type, requested_ip = self.parse_dhcp_packet(data)

                    if not xid or not m_type:
                        continue

                    # ── DISCOVER → OFFER ──────────────────────────────────
                    if m_type == DHCP_DISCOVER:
                        ip = None

                        # Priority order for choosing which IP to offer:
                        # 1. Static assignment (reserved for project servers)
                        if mac_str in self.static_ips:
                            ip = self.static_ips[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} → Static IP match: {ip}")

                        # 2. Already assigned (client is reconnecting — give same IP)
                        elif mac_str in self.assigned_ips:
                            ip, _ = self.assigned_ips[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} → Re-offering locked IP: {ip}")

                        # 3. Pending offer (client sent DISCOVER twice — re-offer same IP)
                        elif mac_str in self.pending_offers:
                            ip, _ = self.pending_offers[mac_str]
                            print(f"[DHCP] DISCOVER from {mac_str} → Re-offering pending IP: {ip}")

                        # 4. New client — assign next available IP from pool
                        elif self.available_ips:
                            ip = self.available_ips.pop(0)
                            self.pending_offers[mac_str] = (ip, time.time())
                            print(f"[DHCP] DISCOVER from {mac_str} → New dynamic offer: {ip}")

                        if ip:
                            resp_packet = self.create_dhcp_response(xid, mac_bytes, ip, DHCP_OFFER)
                            s.sendto(resp_packet, addr)
                        else:
                            print(f"[DHCP] NAK: Pool empty for {mac_str}")

                    # ── REQUEST → ACK or NAK ──────────────────────────────
                    elif m_type == DHCP_REQUEST:
                        # Figure out what IP we expect this client to be requesting
                        expected_ip = self.static_ips.get(mac_str)
                        if not expected_ip and mac_str in self.pending_offers:
                            expected_ip = self.pending_offers[mac_str][0]
                        if not expected_ip and mac_str in self.assigned_ips:
                            expected_ip = self.assigned_ips[mac_str][0]

                        if requested_ip == expected_ip:
                            # Client confirmed the right IP — lock it in
                            expiry = time.time() + LEASE_TIME
                            self.assigned_ips[mac_str] = (requested_ip, expiry)

                            if mac_str in self.pending_offers:
                                del self.pending_offers[mac_str]

                            resp_packet = self.create_dhcp_response(xid, mac_bytes, requested_ip, DHCP_ACK)
                            s.sendto(resp_packet, addr)
                            print(f"[DHCP] ACK: {requested_ip} locked/renewed for {mac_str}")

                            # Register this client with DNS so it's reachable by hostname
                            clean_mac = mac_str.replace(':', '')
                            hostname  = f"host-{clean_mac}.local"
                            self.notify_dns(hostname, requested_ip, "add")

                        else:
                            # Client asked for an IP it shouldn't have — send NAK
                            resp_packet = self.create_dhcp_response(xid, mac_bytes, None, DHCP_NAK)
                            s.sendto(resp_packet, addr)
                            print(f"[DHCP] NAK: Bad request from {mac_str} for IP {requested_ip}")

                    # ── RELEASE → free the IP ─────────────────────────────
                    elif m_type == DHCP_RELEASE:
                        if mac_str in self.assigned_ips:
                            ip, _ = self.assigned_ips.pop(mac_str)
                            if ip not in self.static_ips.values():
                                self.available_ips.insert(0, ip)
                            print(f"[DHCP] RELEASE: {ip} returned to pool from {mac_str}")
                            clean_mac = mac_str.replace(':', '')
                            hostname  = f"host-{clean_mac}.local"
                            self.notify_dns(hostname, ip, "remove")

                except socket.timeout:
                    # No packets arrived this second — run cleanup instead
                    self.cleanup()
                except Exception as e:
                    print(f"[ERROR] {e}")


if __name__ == "__main__":
    try:
        DHCPServer().start()
    except KeyboardInterrupt:
        print("\n[DHCP] Server shut down gracefully by user.")