# app_server.py
import socket
import json
import threading

APP_PORT_TCP = 5000
APP_PORT_RUDP = 5001


def handle_client(conn, addr):
    """Handles one client connection."""
    print(f"[APP] Client connected: {addr}")
    try:
        while True:
            raw = conn.recv(4096)
            if not raw:
                break
            request = json.loads(raw.decode())
            action = request.get('action')

            if action == 'search':
                # TODO: search for song
                response = {'status': 'ok', 'results': []}
            elif action == 'download':
                # TODO: trigger agent to download
                response = {'status': 'ok', 'message': 'downloading...'}
            else:
                response = {'status': 'error', 'message': 'unknown action'}

            conn.send(json.dumps(response).encode())
    finally:
        conn.close()


def start_tcp_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', APP_PORT_TCP))
        s.listen(5)
        print(f"[APP] TCP listening on port {APP_PORT_TCP}")
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle_client, args=(conn, addr)).start()


if __name__ == '__main__':
    start_tcp_server()