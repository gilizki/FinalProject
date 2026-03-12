import socket
import struct
import time

# ─── Packet Types ───────────────────────────────────────────
TYPE_DATA = 0   # regular data packet
TYPE_ACK  = 1   # acknowledgment (receiver telling sender what it got)
TYPE_SYN  = 2   # reserved for future use (connection start)
TYPE_FIN  = 3   # signals end of file transfer (like TCP FIN)

# ─── Constants ──────────────────────────────────────────────
MAX_SEGMENT_SIZE = 1400
# UDP max is 64KB per packet, so we stay under it
# 1400 bytes leaves room for IP + UDP headers so we don't get fragmented.

TIMEOUT = 0.5
# If we don't get an ACK within 0.5 seconds, we assume the packet was lost
# and retransmit from the last unacknowledged packet (Go-Back-N).

RECEIVER_WINDOW = 64
# Max number of packets the receiver can hold in its buffer at once.
# The receiver tells the sender this number in every ACK (flow control).

HEADER_FORMAT = '!B B H I I'
# How we pack/unpack the 10-byte header using Python's struct module:
#   !  = network byte order (big-endian) — the standard for all network protocols
#   B  = 1 byte  → packet type (DATA / ACK / FIN)
#   B  = 1 byte  → flags (unused, always 0, reserved for future features)
#   H  = 2 bytes → receiver window size (rwnd) — used for flow control
#   I  = 4 bytes → sequence number — which packet number this is
#   I  = 4 bytes → ack number — receiver saying "I got everything up to here"
# Total: 10 bytes per header

HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # = 10


def make_packet(ptype, seq_num, ack_num, window_size, data=b''):
    """Pack a header and optional data payload into raw bytes ready to send."""
    header = struct.pack(HEADER_FORMAT, ptype, 0, window_size, seq_num, ack_num)
    return header + data


def parse_packet(raw_bytes):
    """Unpack raw bytes back into a readable dictionary. Returns None if too short."""
    if len(raw_bytes) < HEADER_SIZE:
        return None
    ptype, flags, window, seq, ack = struct.unpack(HEADER_FORMAT, raw_bytes[:HEADER_SIZE])
    return {
        'type':   ptype,
        'flags':  flags,
        'window': window,   # rwnd advertised by the receiver
        'seq':    seq,
        'ack':    ack,
        'data':   raw_bytes[HEADER_SIZE:]
    }


# ─────────────────────────────────────────────────────────────
class RUDPSender:
    """
    Sender side of our custom reliable UDP protocol.

    Implements three mechanisms on top of plain UDP:

    1. Reliability        - Go-Back-N: on timeout or 3 dup-ACKs, retransmit
                            everything from the last unacknowledged packet.

    2. Congestion Control - Slow Start + Congestion Avoidance + Fast Retransmit.
                            Grow cwnd fast at first, then slow down, then back off
                            hard when congestion is detected.

    3. Flow Control       - Never send more packets than the receiver can buffer.
                            The receiver tells us its available space (rwnd) in
                            every ACK, and we cap our send rate accordingly.
    """

    def __init__(self, dest_ip, dest_port):
        self.dest = (dest_ip, dest_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)
        self._fr_active = False

        # Congestion control state
        self.cwnd     = 1.0
        # Congestion window. Starts at 1: send one packet, wait for ACK, then grow.
        # Controls how many packets can be "in flight" (sent but not yet ACKed).

        self.ssthresh = 16.0
        # Slow start threshold. Two growth modes depending on where cwnd is:
        #   cwnd < ssthresh  →  Slow Start: cwnd doubles every RTT (fast growth)
        #   cwnd >= ssthresh →  Congestion Avoidance: cwnd grows by 1 every RTT (careful)

        # Flow control state
        self.rwnd = RECEIVER_WINDOW
        # Receiver's advertised window. Updated every time we get an ACK.
        # Effective window = min(cwnd, rwnd) — we respect whichever is smaller.

        # Fast Retransmit state
        self.last_ack      = -1
        self.dup_ack_count = 0
        # If we get the same ACK number 3 times in a row, a packet was lost
        # but later ones got through. We retransmit immediately without waiting
        # for the full timeout. This is called Fast Retransmit.

        # Clean print tracking — only print when cwnd changes by a whole integer
        self._last_printed_cwnd = -1

    def _effective_window(self):
        """
        How many packets we are allowed to send right now.
        Respects both congestion window (cwnd) and receiver window (rwnd).
        Minimum 1 so we never get completely stuck.
        """
        return max(1, min(int(self.cwnd), self.rwnd))

    def _print_if_changed(self, phase):
        """
        Only print when cwnd ticks up to a new whole number.
        Congestion Avoidance adds 1/cwnd per ACK, which is a tiny fraction.
        Without this guard, a file with 2000+ packets would produce 2000+ prints
        during Congestion Avoidance — one per ACK. This keeps it clean.
        """
        current = int(self.cwnd)
        if current != self._last_printed_cwnd:
            print(f"[RUDP] {phase:<18} cwnd={current:>4}, ssthresh={int(self.ssthresh):>4}, rwnd={self.rwnd}")
            self._last_printed_cwnd = current

    def send_file(self, data_bytes):
        """
        Split the file into chunks and send using a sliding window.

        Key variables:
          base     = index of the oldest packet that hasn't been ACKed yet
          next_seq = index of the next packet we haven't sent yet

        Each loop iteration:
          1. Fill the window: send packets from next_seq up to base + window size
          2. Wait for one ACK response:
             - New ACK    → slide base forward, grow cwnd
             - Dup ACK x3 → Fast Retransmit: jump back to base and resend
             - Timeout    → Go-Back-N: reset next_seq to base, cwnd back to 1
        """
        chunks   = [data_bytes[i:i + MAX_SEGMENT_SIZE]
                    for i in range(0, len(data_bytes), MAX_SEGMENT_SIZE)]
        total    = len(chunks)
        base     = 0
        next_seq = 0

        print(f"[RUDP] Starting transfer: {total} packets ({len(data_bytes):,} bytes) → {self.dest}")

        while base < total:
            win = self._effective_window()

            # Step 1: send all packets that fit in the current window
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

            # Step 2: wait for an ACK
            try:
                raw, _ = self.sock.recvfrom(HEADER_SIZE + MAX_SEGMENT_SIZE)
                parsed  = parse_packet(raw)
                if not parsed or parsed['type'] != TYPE_ACK:
                    continue

                ack       = parsed['ack']
                self.rwnd = max(1, parsed['window'])   # update rwnd from this ACK

                if ack == self.last_ack:
                    # Same ACK as before — the receiver is re-asking for a missing packet
                    self.dup_ack_count += 1
                    if self.dup_ack_count >= 3 and not self._fr_active:
                        # Fast Retransmit:
                        #   ssthresh = cwnd / 2
                        #   cwnd = ssthresh + 3  (the +3 is from the 3 packets that did get through)
                        print(f"[RUDP] Fast Retransmit - seq={base} (3 dup-ACKs) -> cwnd={int(self.ssthresh + 3)}")
                        self.ssthresh = max(self.cwnd / 2, 2.0)
                        self.cwnd = self.ssthresh + 3
                        next_se = base
                        self.dup_ack_count = 0
                        self._fr_active = True  # to block further FR until we move forward
                        self._last_printed_cwnd = int(self.cwnd)

                elif ack > self.last_ack:
                    # new ACK — real forwarding progress
                    self.last_ack = ack
                    self.dup_ack_count = 0
                    self._fr_active = False  # real progress — FR can fire again if needed
                    base = ack + 1   # slide the window forward

                    if self.cwnd < self.ssthresh:
                        # Slow Start: +1 per ACK means cwnd doubles each full RTT
                        self.cwnd += 1.0
                        self._print_if_changed("Slow Start")
                    else:
                        # Congestion Avoidance: +1/cwnd per ACK means +1 per full RTT
                        self.cwnd += 1.0 / self.cwnd
                        self._print_if_changed("Cong. Avoidance")

            except socket.timeout:
                # Timeout = serious congestion (worse than dup-ACKs)
                # TCP Tahoe response: set ssthresh = cwnd/2, reset cwnd to 1
                print(f"[RUDP] Timeout!          seq={base} → cwnd=1, ssthresh={int(max(self.cwnd / 2, 2))}")
                self.ssthresh           = max(self.cwnd / 2, 2.0)
                self.cwnd               = 1.0
                self.dup_ack_count      = 0
                next_seq                = base   # Go-Back-N: resend from base
                self._last_printed_cwnd = 1

            except ConnectionResetError:
                # for Windows users only: ICMP "port unreachable" — safe to ignore
                pass

        # Done — send FIN to tell the receiver there's nothing more coming
        fin = make_packet(TYPE_FIN, seq_num=0, ack_num=0, window_size=0)
        self.sock.sendto(fin, self.dest)
        print(f"[RUDP] Transfer complete! {total} packets sent.")
        self.sock.close()


# ─────────────────────────────────────────────────────────────
class RUDPReceiver:
    """
    Receiver side of our custom reliable UDP protocol.

    Implements:
    1. Reliability  - Buffers out-of-order packets, sends cumulative ACKs.
                      A cumulative ACK means "I have everything up to seq N in order."
                      If packet 4 arrives before packet 3, we keep ACKing 2 until 3 arrives.
                      This forces the sender to retransmit the missing packet.

    2. Flow Control - Advertises a dynamic rwnd in every ACK.
                      rwnd = RECEIVER_WINDOW - number_of_out_of_order_packets_in_buffer
                      When out-of-order packets pile up (gaps in the sequence), rwnd shrinks,
                      telling the sender to slow down until the gaps are filled.

    Note on rwnd behavior in practice:
    On a clean localhost run with no packet loss, everything arrives in order,
    the buffer never has out-of-order packets, and rwnd stays at 64 the whole time.
    rwnd will visibly drop and recover when you simulate packet loss for Wireshark.
    """

    def __init__(self, listen_port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', listen_port))
        self.rwnd = RECEIVER_WINDOW
        print(f"[RUDP] Receiver listening on port {listen_port}")

        # Clean print tracking
        self._last_print_ack  = -1
        self._last_print_rwnd = -1

    def receive_file(self):
        """
        Receive packets, send cumulative ACKs, return the complete reassembled file.

        received{}  = all packets that arrived, stored by seq number
        expected    = the next seq number needed to advance the in-order frontier
        buffered    = packets in received{} that are ahead of expected
                      (out-of-order arrivals waiting for the gap to be filled)
        advertised  = RECEIVER_WINDOW - buffered  (actual free space in our buffer)
        """
        received = {}   # seq_num → payload bytes
        expected = 0    # next seq needed to extend the in-order run

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

                # Store the packet. If it's a duplicate, the dict just overwrites — no harm.
                if seq not in received:
                    received[seq] = parsed['data']

                # Advance expected as far as we can (only moves when sequence is unbroken)
                # Example: received = {0, 1, 2, 5} → expected stops at 3, ack_num = 2
                while expected in received:
                    expected += 1
                ack_num = expected - 1

                # How many packets are sitting in the buffer ahead of the gap?
                # Those take up space but can't be delivered yet.
                buffered          = len(received) - expected
                advertised_window = max(1, self.rwnd - buffered)
                # If buffered > 0, advertised_window shrinks → sender slows down.
                # Once the gap is filled and expected catches up, buffered drops back to 0.

                ack_packet = make_packet(
                    ptype=TYPE_ACK,
                    seq_num=0,
                    ack_num=ack_num,
                    window_size=advertised_window
                )
                self.sock.sendto(ack_packet, sender_addr)

                # Print every 100 packets, or immediately if rwnd changed
                if ack_num // 100 > self._last_print_ack // 100 or advertised_window != self._last_print_rwnd:
                    print(f"[RUDP] Receiving... ack={ack_num}, rwnd={advertised_window}")
                    self._last_print_ack  = ack_num
                    self._last_print_rwnd = advertised_window

        # Sort by seq number and join into a single bytes object
        result = b''.join(received[i] for i in sorted(received.keys()))
        print(f"[RUDP] Reassembled {len(result):,} bytes successfully.")
        return result