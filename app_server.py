import socket
import os
import json
import threading
import urllib.parse
from agent import handle_request
from rudp import RUDPSender

# ─── Constants ──────────────────────────────────────────────
HTTP_PORT = 5000   # client sends HTTP requests here (TCP)
RUDP_PORT = 5001   # client receives MP3 over RUDP here
TCP_TRANSFER_PORT = 5002   # client receives MP3 over plain TCP here
HOST  = '0.0.0.0'

# ─── HTTP Helpers ────────────────────────────────────────────

def send_response(conn, body_dict, status=200):
    """Serialize dict to JSON and send as a valid HTTP response"""
    body = json.dumps(body_dict).encode('utf-8')
    response = (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode('utf-8') + body
    conn.sendall(response)

def parse_request(raw):
    """
    Parse raw HTTP bytes into (method, path, params).
    Example: b'GET /search?q=hello HTTP/1.1...'
         ->  ('GET', '/search', {'q': 'hello'})
    """
    text       = raw.decode('utf-8', errors='ignore')
    first_line = text.split('\r\n')[0]
    parts      = first_line.split(' ')
    if len(parts) < 2:
        return None, None, {}

    method    = parts[0]
    full_path = parts[1]

    if '?' in full_path:
        path, query_string = full_path.split('?', 1)
        # parse_qs returns {'q': ['hello']} so flatten to {'q': 'hello'}
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query_string).items()}
    else:
        path   = full_path
        params = {}

    return method, path, params

# ─── File Transfer ───────────────────────────────────────────

def send_over_rudp(filepath, client_ip, client_port):
    """Send a file to the client using our custom RUDP protocol"""
    print(f"[APP SERVER] Sending {filepath} over RUDP → {client_ip}:{client_port}")
    try:
        with open(filepath, 'rb') as f:
            file_bytes = f.read()
        sender = RUDPSender(client_ip, client_port)
        sender.send_file(file_bytes)
        print("[APP SERVER] RUDP transfer complete!")
    except Exception as e:
        print(f"[APP SERVER] RUDP transfer failed: {e}")

def send_over_tcp(filepath, client_ip, client_port):
    """
    Send a file to the client using plain TCP.
    Client listens, server connects and sends all bytes, then closes.
    TCP handles reliability internally — no custom protocol needed.
    """
    print(f"[APP SERVER] Sending {filepath} over TCP → {client_ip}:{client_port}")
    try:
        with open(filepath, 'rb') as f:
            file_bytes = f.read()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect((client_ip, client_port))
        sock.sendall(file_bytes)
        sock.close()
        print("[APP SERVER] TCP transfer complete!")
    except Exception as e:
        print(f"[APP SERVER] TCP transfer failed: {e}")

# ─── Route Handlers ──────────────────────────────────────────

def handle_search(params):
    """Mode 1: search YouTube by song name"""
    query = params.get('q', '')
    if not query:
        return {'status': 'error', 'message': 'No query provided'}
    return handle_request({'action': 'search', 'query': query})

def handle_vibe(params):
    """Mode 3: AI suggests songs based on mood description"""
    description = params.get('q', '')
    if not description:
        return {'status': 'error', 'message': 'No description provided'}
    return handle_request({'action': 'vibe', 'description': description})

def handle_download_rudp(params, client_addr):
    """
    Download song and send to client over RUDP (custom reliable UDP).
    Transfer runs in background thread so HTTP reply is immediate.
    """
    url         = params.get('url', '')
    title       = params.get('title', 'song')
    client_port = int(params.get('client_port', RUDP_PORT))
    client_ip   = client_addr[0]

    if not url:
        return {'status': 'error', 'message': 'No URL provided'}

    # Step 1: agent downloads MP3 to server's downloads/ folder
    result = handle_request({'action': 'download_url', 'url': url, 'title': title})
    if result['status'] != 'success':
        return result

    # Step 2: send file in background (so HTTP response goes back immediately)
    threading.Thread(
        target=send_over_rudp,
        args=(result['filepath'], client_ip, client_port),
        daemon=True
    ).start()

    return {
        'status':   'success',
        'message':  f'Sending {title} over RUDP to port {client_port}',
        'filename': result['filename']
    }

def handle_download_tcp(params, client_addr):
    """
    Download song and send to client over plain TCP.
    Transfer runs in background thread so HTTP reply is immediate.
    """
    url         = params.get('url', '')
    title       = params.get('title', 'song')
    client_port = int(params.get('client_port', TCP_TRANSFER_PORT))
    client_ip   = client_addr[0]

    if not url:
        return {'status': 'error', 'message': 'No URL provided'}

    result = handle_request({'action': 'download_url', 'url': url, 'title': title})
    if result['status'] != 'success':
        return result

    threading.Thread(
        target=send_over_tcp,
        args=(result['filepath'], client_ip, client_port),
        daemon=True
    ).start()

    return {
        'status':   'success',
        'message':  f'Sending {title} over TCP to port {client_port}',
        'filename': result['filename']
    }

def handle_history(_params):
    """Return list of all previously downloaded songs"""
    return handle_request({'action': 'history'})

# ─── Router ──────────────────────────────────────────────────

def route(method, path, params, client_addr):
    """Map URL path to the correct handler function"""
    if path == '/search':
        return handle_search(params)
    elif path == '/vibe':
        return handle_vibe(params)
    elif path == '/download':
        return handle_download_rudp(params, client_addr)
    elif path == '/download_tcp':
        return handle_download_tcp(params, client_addr)
    elif path == '/history':
        return handle_history(params)
    else:
        return {'status': 'error', 'message': f'Unknown path: {path}'}

# ─── Client Connection Handler ───────────────────────────────

def handle_client(conn, client_addr):
    """
    Runs in a new thread for each incoming client connection.
    Reads HTTP request → routes it → sends back JSON response.
    """
    try:
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk

        method, path, params = parse_request(raw)
        if method is None:
            send_response(conn, {'status': 'error', 'message': 'Bad request'}, 400)
            return

        print(f"[APP SERVER] {method} {path} | from={client_addr[0]}")
        result = route(method, path, params, client_addr)
        send_response(conn, result)

    except Exception as e:
        print(f"[APP SERVER] Error: {e}")
        try:
            send_response(conn, {'status': 'error', 'message': str(e)}, 500)
        except:
            pass
    finally:
        conn.close()

# ─── Main Server Loop ────────────────────────────────────────

def start():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, HTTP_PORT))
    server_sock.listen(5)

    print(f"[APP SERVER] Listening on TCP port {HTTP_PORT}")
    print(f"[APP SERVER] RUDP transfers on port {RUDP_PORT}")
    print(f"[APP SERVER] TCP  transfers on port {TCP_TRANSFER_PORT}")

    try:
        while True:
            conn, client_addr = server_sock.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, client_addr),
                daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\n[APP SERVER] Shutting down app server")
    finally:
        server_sock.close()

if __name__ == '__main__':
    start()