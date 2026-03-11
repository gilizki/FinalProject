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
        'window': window,   # receiver's advertised window (flow control)
        'seq':    seq,
        'ack':    ack,
        'data':   raw_bytes[HEADER_SIZE:]
    }


class RUDPSender:
    def __init__(self, dest_ip, dest_port):
        self.dest = (dest_ip, dest_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)

        # ── Congestion Control state ──
        self.cwnd     = 1.0    # congestion window (float for smooth CA growth)
        self.ssthresh = 16.0   # slow start threshold

        # ── Flow Control state ──
        self.rwnd = RECEIVER_WINDOW  # receiver's advertised window (updated from ACKs)

        # ── Fast Retransmit state ──
        self.last_ack      = -1   # last ACK number we received
        self.dup_ack_count = 0    # how many times we've seen the same ACK

    def _effective_window(self):
        """
        Effective window = min(congestion window, receiver window).
        Flow control: we can't send more than the receiver can buffer.
        Congestion control: we can't send more than the network can handle.
        """
        return max(1, min(int(self.cwnd), self.rwnd))

    def send_file(self, data_bytes):
        """Split data into packets, send with sliding window + Go-Back-N + FR"""
        chunks = [data_bytes[i:i + MAX_SEGMENT_SIZE]
                  for i in range(0, len(data_bytes), MAX_SEGMENT_SIZE)]
        total    = len(chunks)
        base     = 0   # oldest unACKed packet
        next_seq = 0   # next packet to send

        print(f"[RUDP] Sending {total} packets to {self.dest}")

        while base < total:
            win = self._effective_window()

            # ── Send all packets within the window ──
            while next_seq < base + win and next_seq < total:
                packet = make_packet(
                    ptype=TYPE_DATA,
                    seq_num=next_seq,
                    ack_num=0,
                    window_size=int(self.cwnd),
                    data=chunks[next_seq]
                )
                self.sock.sendto(packet, self.dest)
                print(f"[RUDP SENDER] Sent packet seq={next_seq}")
                next_seq += 1

            # ── Wait for ACK ──
            try:
                raw, _ = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
                parsed = parse_packet(raw)
                if not parsed or parsed['type'] != TYPE_ACK:
                    continue

                ack = parsed['ack']

                # ── Flow Control: update receiver window from every ACK ──
                self.rwnd = max(1, parsed['window'])

                # ── Check for duplicate ACK (Fast Retransmit) ──
                if ack == self.last_ack:
                    self.dup_ack_count += 1
                    print(f"[FR] Duplicate ACK={ack} (count={self.dup_ack_count})")

                    if self.dup_ack_count >= 3:
                        # 3 duplicate ACKs = packet lost, retransmit immediately
                        print(f"[FR] 3 duplicate ACKs! Fast retransmit seq={base}")
                        self.ssthresh = max(self.cwnd / 2, 2.0)
                        self.cwnd     = self.ssthresh + 3   # inflate window
                        next_seq      = base                # retransmit from base
                        self.dup_ack_count = 0

                elif ack > self.last_ack:
                    # ── New ACK: advance base, update CC ──
                    self.last_ack      = ack
                    self.dup_ack_count = 0
                    base               = ack + 1

                    if self.cwnd < self.ssthresh:
                        # Slow Start: double window each RTT (×2 per ACK is approx)
                        self.cwnd *= 2
                        print(f"[CC] Slow Start → cwnd={int(self.cwnd)}, ssthresh={int(self.ssthresh)}, rwnd={self.rwnd}")
                    else:
                        # Congestion Avoidance: grow by 1 per RTT
                        # Adding 1/cwnd per ACK ≈ +1 per full window of ACKs = +1/RTT
                        self.cwnd += 1.0 / self.cwnd
                        print(f"[CC] Cong. Avoidance → cwnd={int(self.cwnd)}, ssthresh={int(self.ssthresh)}, rwnd={self.rwnd}")

            except socket.timeout:
                # Timeout = packet loss → Go-Back-N
                print(f"[CC] Timeout! cwnd={int(self.cwnd)} → 1, ssthresh={int(self.cwnd / 2)}")
                self.ssthresh      = max(self.cwnd / 2, 2.0)
                self.cwnd          = 1.0
                self.dup_ack_count = 0
                next_seq           = base   # retransmit all unACKed

            except ConnectionResetError:
                pass   # Windows: ignore ICMP port unreachable

        # ── Done: send FIN ──
        fin = make_packet(TYPE_FIN, seq_num=0, ack_num=0, window_size=0)
        self.sock.sendto(fin, self.dest)
        print("[RUDP SENDER] File sent. FIN sent.")
        self.sock.close()


class RUDPReceiver:
    def __init__(self, listen_port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', listen_port))
        # Flow control: how many packets we can buffer
        # We advertise this in every ACK so sender respects it
        self.rwnd = RECEIVER_WINDOW
        print(f"[RUDP RECEIVER] Listening on port {listen_port}")

    def receive_file(self):
        """Receive packets in order, send cumulative ACKs, reassemble file"""
        received = {}   # seq_num → data bytes
        expected = 0    # next in-order seq we want

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

                # store packet (ignore duplicates)
                if seq not in received:
                    received[seq] = parsed['data']

                # advance expected to highest in-order received
                while expected in received:
                    expected += 1
                ack_num = expected - 1

                # ── Flow Control: advertise how much buffer we have left ──
                # reduce rwnd as buffer fills, increase as we consume data
                buffered = len(received) - expected  # out-of-order packets buffered
                advertised_window = max(1, self.rwnd - buffered)

                ack_packet = make_packet(
                    ptype=TYPE_ACK,
                    seq_num=0,
                    ack_num=ack_num,
                    window_size=advertised_window   # tells sender our available buffer
                )
                self.sock.sendto(ack_packet, sender_addr)
                print(f"[RUDP RECEIVER] Sent ACK={ack_num}, rwnd={advertised_window}")

        # reassemble in order
        result = b''.join(received[i] for i in sorted(received.keys()))
        print(f"[RUDP RECEIVER] Reassembled {len(result)} bytes.")
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