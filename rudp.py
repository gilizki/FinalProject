# rudp.py
import socket
import struct
import threading
import time

# Packet types
TYPE_DATA = 0
TYPE_ACK = 1
TYPE_SYN = 2
TYPE_FIN = 3

HEADER_FORMAT = '!BBHII'  # type, flags, window_size, seq_num, ack_num
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAX_SEGMENT_SIZE = 1400  # bytes (stay under 64KB UDP limit)
TIMEOUT = 0.5  # retransmission timeout seconds


def make_packet(ptype, seq_num, ack_num, window_size, data=b''):
    header = struct.pack(HEADER_FORMAT, ptype, 0, window_size, seq_num, ack_num)
    return header + data


def parse_packet(raw):
    if len(raw) < HEADER_SIZE:
        return None
    header = struct.unpack(HEADER_FORMAT, raw[:HEADER_SIZE])
    data = raw[HEADER_SIZE:]
    return {'type': header[0], 'flags': header[1],
            'window': header[2], 'seq': header[3],
            'ack': header[4], 'data': data}


class RUDPSocket:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.window_size = 4  # start small
        self.seq_num = 0
        self.ack_num = 0
        # TODO: add send buffer, receive buffer, timer

    def bind(self, addr):
        self.sock.bind(addr)

    def send_data(self, data, addr):
        # TODO: implement sliding window send
        pass

    def recv_data(self):
        # TODO: implement ACK + reordering
        pass

    def close(self):
        self.sock.close()