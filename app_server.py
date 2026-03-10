import socket
import os
import json
import threading
import urllib.parse
from agent import handle_request
from rudp import RUDPSender

# ─── Constants ──────────────────────────────────────────────
HTTP_PORT = 5000   # client connects here over TCP
RUDP_PORT = 5001   # client receives the MP3 here over RUDP
HOST      = '0.0.0.0'

# ─── HTTP Helpers ────────────────────────────────────────────

def send_response(conn, body_dict, status=200):
    """Serialize dict to JSON and send as HTTP response"""
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
    Parse raw HTTP request bytes.
    Returns (method, path, query_params)
    Example: GET /search?q=hello  →  ('GET', '/search', {'q': 'hello'})
    """
    # decode and split on first blank line
    text = raw.decode('utf-8', errors='ignore')
    first_line = text.split('\r\n')[0]           # e.g. "GET /search?q=hello HTTP/1.1"
    parts = first_line.split(' ')
    if len(parts) < 2:
        return None, None, {}

    method = parts[0]                            # "GET"
    full_path = parts[1]                         # "/search?q=hello"

    # split path and query string
    if '?' in full_path:
        path, query_string = full_path.split('?', 1)
        params = urllib.parse.parse_qs(query_string)
        # parse_qs returns lists: {'q': ['hello']} → flatten to {'q': 'hello'}
        params = {k: v[0] for k, v in params.items()}
    else:
        path = full_path
        params = {}

    return method, path, params

# ─── Route Handlers ─────────────────────────────────────────

def handle_search(params):
    """Mode 2: search YouTube by song name"""
    query = params.get('q', '')
    if not query:
        return {'status': 'error', 'message': 'No query provided'}
    return handle_request({'action': 'search', 'query': query})

def handle_vibe(params):
    """Mode 3: Gemini suggests songs based on mood"""
    description = params.get('q', '')
    if not description:
        return {'status': 'error', 'message': 'No description provided'}
    return handle_request({'action': 'vibe', 'description': description})

def handle_download(params, client_addr):
    """
    Download song and send to client over RUDP.
    params must include: url, title, client_port
    client_addr is the IP of whoever sent the request.
    """
    url         = params.get('url', '')
    title       = params.get('title', 'song')
    client_port = int(params.get('client_port', RUDP_PORT))
    client_ip   = client_addr[0]   # IP of the client machine

    if not url:
        return {'status': 'error', 'message': 'No URL provided'}

    # Step 1: download MP3 to server's downloads folder
    result = handle_request({'action': 'download_url', 'url': url, 'title': title})
    if result['status'] != 'success':
        return result

    filepath = result['filepath']

    # Step 2: send file over RUDP in background thread
    # (background so we can immediately reply to the HTTP request)
    def send_over_rudp():
        print(f"[APP SERVER] Sending {filepath} over RUDP → {client_ip}:{client_port}")
        try:
            with open(filepath, 'rb') as f:
                file_bytes = f.read()
            sender = RUDPSender(client_ip, client_port)
            sender.send_file(file_bytes)
            print("[APP SERVER] RUDP transfer complete!")
        except Exception as e:
            print(f"[APP SERVER] RUDP transfer failed: {e}")

    threading.Thread(target=send_over_rudp, daemon=True).start()

    return {
        'status': 'success',
        'message': f'Sending {title} over RUDP to port {client_port}',
        'filename': result['filename']
    }

def handle_history(_params):
    """Return list of all previously downloaded songs"""
    return handle_request({'action': 'history'})

# ─── Router ─────────────────────────────────────────────────

def route(method, path, params, client_addr):
    """Map path → handler function"""
    if path == '/search':
        return handle_search(params)
    elif path == '/vibe':
        return handle_vibe(params)
    elif path == '/download':
        return handle_download(params, client_addr)
    elif path == '/history':
        return handle_history(params)
    else:
        return {'status': 'error', 'message': f'Unknown path: {path}'}

# ─── Connection Handler ──────────────────────────────────────

def handle_client(conn, client_addr):
    """
    Called in a new thread for each client connection.
    Reads the HTTP request, routes it, sends back JSON response.
    """
    try:
        # read full request (keep reading until we have the headers at minimum)
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

        print(f"[APP SERVER] {method} {path} params={params}")
        result = route(method, path, params, client_addr)
        send_response(conn, result)

    except Exception as e:
        print(f"[APP SERVER] Error handling client: {e}")
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
    print(f"[APP SERVER] RUDP file transfers on port {RUDP_PORT}")

    try:
        while True:
            conn, client_addr = server_sock.accept()
            print(f"[APP SERVER] New connection from {client_addr}")
            # handle each client in its own thread so server stays responsive
            t = threading.Thread(target=handle_client, args=(conn, client_addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[APP SERVER] Shutting down gracefully...")
    finally:
        server_sock.close()

if __name__ == '__main__':
    start()