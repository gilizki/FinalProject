import socket
import struct
import time

# ─── Packet Types ───────────────────────────────────────────
TYPE_DATA = 0   # carrying actual data
TYPE_ACK  = 1   # acknowledgment
TYPE_SYN  = 2   # connection start
TYPE_FIN  = 3   # connection end

# ─── Constants ──────────────────────────────────────────────
MAX_SEGMENT_SIZE  = 1400   # bytes per packet (safe under UDP's 64KB limit)
TIMEOUT           = 0.5    # seconds before retransmitting
RECEIVER_WINDOW   = 64     # how many packets receiver can buffer at once
HEADER_FORMAT     = '!B B H I I'
# ! = network byte order (big-endian)
# B = 1 byte  → packet type   (DATA/ACK/SYN/FIN)
# B = 1 byte  → flags         (reserved)
# H = 2 bytes → window size   (receiver advertises how many packets it can accept)
# I = 4 bytes → seq_num       (which packet number this is)
# I = 4 bytes → ack_num       (which packet we're acknowledging)
# Total = 10 bytes

HEADER_SIZE = struct.calcsize(HEADER_FORMAT)   # = 10


def make_packet(ptype, seq_num, ack_num, window_size, data=b''):
    """Build a complete RUDP packet: header + data"""
    header = struct.pack(HEADER_FORMAT, ptype, 0, window_size, seq_num, ack_num)
    return header + data


def parse_packet(raw_bytes):
    """Decode raw bytes into a readable dictionary"""
    if len(raw_bytes) < HEADER_SIZE:
        return None
    ptype, flags, window, seq, ack = struct.unpack(HEADER_FORMAT, raw_bytes[:HEADER_SIZE])
    return {
        'type':   ptype,
        'flags':  flags,
        'window': window,
        'seq':    seq,
        'ack':    ack,
        'data':   raw_bytes[HEADER_SIZE:]
    }


class RUDPSender:
    def __init__(self, dest_ip, dest_port):
        self.dest = (dest_ip, dest_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)

        # Congestion Control state
        self.cwnd     = 1.0
        self.ssthresh = 16.0

        # Flow Control state
        self.rwnd = RECEIVER_WINDOW

        # Fast Retransmit state
        self.last_ack      = -1
        self.dup_ack_count = 0

    def _effective_window(self):
        """
        Effective window = min(congestion window, receiver window).
        Flow control:    we cannot send more than the receiver can buffer.
        Congestion ctrl: we cannot send more than the network can handle.
        """
        return max(1, min(int(self.cwnd), self.rwnd))

    def send_file(self, data_bytes):
        """Split data into packets, send with sliding window + Go-Back-N + FR"""
        chunks = [data_bytes[i:i + MAX_SEGMENT_SIZE]
                  for i in range(0, len(data_bytes), MAX_SEGMENT_SIZE)]
        total    = len(chunks)
        base     = 0
        next_seq = 0

        print(f"[RUDP] Starting transfer: {total} packets ({len(data_bytes)} bytes) → {self.dest}")

        while base < total:
            win = self._effective_window()

            while next_seq < base + win and next_seq < total:
                packet = make_packet(
                    ptype=TYPE_DATA,
                    seq_num=next_seq,
                    ack_num=0,
                    window_size=int(self.cwnd),
                    data=chunks[next_seq]
                )
                self.sock.sendto(packet, self.dest)
                next_seq += 1

            try:
                raw, _ = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
                parsed = parse_packet(raw)
                if not parsed or parsed['type'] != TYPE_ACK:
                    continue

                ack = parsed['ack']
                self.rwnd = max(1, parsed['window'])   # flow control update

                if ack == self.last_ack:
                    self.dup_ack_count += 1
                    if self.dup_ack_count >= 3:
                        # Fast Retransmit: 3 duplicate ACKs = loss detected
                        print(f"[RUDP] Fast Retransmit at seq={base} (3 dup ACKs)")
                        self.ssthresh      = max(self.cwnd / 2, 2.0)
                        self.cwnd          = self.ssthresh + 3
                        next_seq           = base
                        self.dup_ack_count = 0

                elif ack > self.last_ack:
                    self.last_ack      = ack
                    self.dup_ack_count = 0
                    base               = ack + 1

                    if self.cwnd < self.ssthresh:
                        self.cwnd *= 2
                        print(f"[RUDP] Slow Start      → cwnd={int(self.cwnd):>4}, ssthresh={int(self.ssthresh):>4}, rwnd={self.rwnd}")
                    else:
                        self.cwnd += 1.0 / self.cwnd
                        print(f"[RUDP] Cong. Avoidance → cwnd={int(self.cwnd):>4}, ssthresh={int(self.ssthresh):>4}, rwnd={self.rwnd}")

            except socket.timeout:
                print(f"[RUDP] Timeout! Loss at seq={base} → cwnd=1, ssthresh={int(self.cwnd / 2)}")
                self.ssthresh      = max(self.cwnd / 2, 2.0)
                self.cwnd          = 1.0
                self.dup_ack_count = 0
                next_seq           = base

            except ConnectionResetError:
                pass

        fin = make_packet(TYPE_FIN, seq_num=0, ack_num=0, window_size=0)
        self.sock.sendto(fin, self.dest)
        print(f"[RUDP] Transfer complete! {total} packets sent.")
        self.sock.close()


class RUDPReceiver:
    def __init__(self, listen_port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', listen_port))
        self.rwnd = RECEIVER_WINDOW
        print(f"[RUDP] Receiver listening on port {listen_port}")

    def receive_file(self):
        """Receive packets in order, send cumulative ACKs, reassemble file"""
        received   = {}
        expected   = 0
        last_print = -1

        while True:
            raw, sender_addr = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
            parsed = parse_packet(raw)
            if parsed is None:
                continue

            if parsed['type'] == TYPE_FIN:
                print(f"[RUDP] FIN received — transfer complete ({len(received)} packets)")
                break

            if parsed['type'] == TYPE_DATA:
                seq = parsed['seq']

                if seq not in received:
                    received[seq] = parsed['data']

                while expected in received:
                    expected += 1
                ack_num = expected - 1

                # Flow Control: tell sender how much buffer we have left
                buffered          = len(received) - expected
                advertised_window = max(1, self.rwnd - buffered)

                ack_packet = make_packet(
                    ptype=TYPE_ACK,
                    seq_num=0,
                    ack_num=ack_num,
                    window_size=advertised_window
                )
                self.sock.sendto(ack_packet, sender_addr)

                # print progress every 100 packets instead of every single one
                if ack_num // 100 > last_print // 100:
                    print(f"[RUDP] Receiving... ack={ack_num}, rwnd={advertised_window}")
                    last_print = ack_num

        result = b''.join(received[i] for i in sorted(received.keys()))
        print(f"[RUDP] Reassembled {len(result)} bytes successfully.")
        return result


# ─── Self-test ──────────────────────────────────────────────
if __name__ == '__main__':
    import threading

    TEST_PORT = 9999
    test_data = b"Hello from RUDP! " * 500

    def run_receiver():
        r = RUDPReceiver(TEST_PORT)
        data = r.receive_file()
        print(f"[TEST] Receiver got {len(data)} bytes: {data[:40]}...")

    t = threading.Thread(target=run_receiver)
    t.start()
    time.sleep(0.3)

    s = RUDPSender('127.0.0.1', TEST_PORT)
    s.send_file(test_data)
    t.join()
    print("[TEST] Done!")