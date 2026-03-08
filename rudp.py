import socket
import struct
import time

# ─── Packet Types ───────────────────────────────────────────
TYPE_DATA = 0   # carrying actual data
TYPE_ACK  = 1   # acknowledgment
TYPE_SYN  = 2   # connection start
TYPE_FIN  = 3   # connection end

# ─── Constants ──────────────────────────────────────────────
MAX_SEGMENT_SIZE = 1400  # bytes per packet (safe under UDP's 64KB limit)
TIMEOUT          = 0.5   # seconds before retransmitting
HEADER_FORMAT    = '!B B H I I'
# ! = network byte order (big-endian)
# B = 1 byte  → packet type  (DATA/ACK/SYN/FIN)
# B = 1 byte  → flags        (reserved for future use)
# H = 2 bytes → window size  (how many packets we can send at once)
# I = 4 bytes → seq_num      (which packet number this is)
# I = 4 bytes → ack_num      (which packet we're acknowledging)
# Total header = 10 bytes

HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # = 10

def make_packet(ptype, seq_num, ack_num, window_size, data=b''):
    """Build a complete RUDP packet: header + data"""
    header = struct.pack(HEADER_FORMAT, ptype, 0, window_size, seq_num, ack_num)
    return header + data  # bytes + bytes = bytes

def parse_packet(raw_bytes):
    """Take raw bytes, return a readable dictionary"""
    if len(raw_bytes) < HEADER_SIZE:
        return None
    ptype, flags, window, seq, ack = struct.unpack(HEADER_FORMAT, raw_bytes[:HEADER_SIZE])
    data = raw_bytes[HEADER_SIZE:]  # everything after the header is data
    return {
        'type':   ptype,
        'flags':  flags,
        'window': window,
        'seq':    seq,
        'ack':    ack,
        'data':   data
    }

class RUDPSender:
    def __init__(self, dest_ip, dest_port):
        self.dest = (dest_ip, dest_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)
        self.window_size = 4

    def send_file(self, data_bytes):
        """Split data into packets and send with sliding window + retransmit"""
        # Step 1: split data into chunks
        chunks = []
        for i in range(0, len(data_bytes), MAX_SEGMENT_SIZE):
            chunks.append(data_bytes[i:i + MAX_SEGMENT_SIZE])

        total = len(chunks)
        base = 0        # first unACKed packet
        next_seq = 0    # next packet to send

        print(f"[RUDP] Sending {total} packets to {self.dest}")

        while base < total:
            # send all packets in current window
            while next_seq < base + self.window_size and next_seq < total:
                packet = make_packet(
                    ptype=TYPE_DATA,
                    seq_num=next_seq,
                    ack_num=0,
                    window_size=self.window_size,
                    data=chunks[next_seq]
                )
                self.sock.sendto(packet, self.dest)
                print(f"[RUDP SENDER] Sent packet seq={next_seq}")
                next_seq += 1

            # wait for ACK
            try:
                raw, _ = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
                parsed = parse_packet(raw)
                if parsed and parsed['type'] == TYPE_ACK:
                    ack = parsed['ack']
                    print(f"[RUDP SENDER] Got ACK for seq={ack}")
                    if ack >= base:
                        base = ack + 1  # slide the window forward

            except socket.timeout:
                # no ACK in time → go back N, resend from base
                print(f"[RUDP SENDER] Timeout! Resending from seq={base}")
                next_seq = base  # reset to resend whole window
            except ConnectionResetError:
            # Windows-only: ignore ICMP "port unreachable" errors
                pass

        # send FIN when done
        fin = make_packet(TYPE_FIN, seq_num=0, ack_num=0, window_size=0)
        self.sock.sendto(fin, self.dest)
        print("[RUDP SENDER] File sent. FIN sent.")
        self.sock.close()


class RUDPReceiver:
    def __init__(self, listen_port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', listen_port))
        self.window_size = 4
        print(f"[RUDP RECEIVER] Listening on port {listen_port}")

    def receive_file(self):
        """Receive packets, send ACKs, reconstruct file"""
        received = {}   # seq_num → data bytes
        expected = 0    # next seq we're waiting for

        while True:
            raw, sender_addr = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
            parsed = parse_packet(raw)

            if parsed is None:
                continue

            if parsed['type'] == TYPE_FIN:
                print("[RUDP RECEIVER] Got FIN, transfer complete.")
                break

            if parsed['type'] == TYPE_DATA:
                seq = parsed['seq']
                print(f"[RUDP RECEIVER] Got packet seq={seq}, expected={expected}")

                if seq not in received:
                    received[seq] = parsed['data']

                # send ACK for highest in-order packet received
                while expected in received:
                    expected += 1
                ack_num = expected - 1

                ack_packet = make_packet(
                    ptype=TYPE_ACK,
                    seq_num=0,
                    ack_num=ack_num,
                    window_size=self.window_size
                )
                self.sock.sendto(ack_packet, sender_addr)
                print(f"[RUDP RECEIVER] Sent ACK={ack_num}")

        # reassemble in order
        result = b''
        for i in sorted(received.keys()):
            result += received[i]
        print(f"[RUDP RECEIVER] Reassembled {len(result)} bytes.")
        return result

if __name__ == '__main__':
    import threading

    TEST_PORT = 9999
    test_data = b"Hello from RUDP! " * 200  # fake data to send

    def run_receiver():
        r = RUDPReceiver(TEST_PORT)
        data = r.receive_file()
        print(f"[TEST] Receiver got: {data[:50]}...")

    t = threading.Thread(target=run_receiver)
    t.start()
    time.sleep(0.3)  # give receiver time to start

    s = RUDPSender('127.0.0.1', TEST_PORT)
    s.send_file(test_data)
    t.join()
    print("[TEST] Done!")