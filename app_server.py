import socket
import os
import json
import threading
import urllib.parse
from agent import handle_request
from rudp import RUDPSender

# ─── Constants ───────────────────────────────────────────────
# Three separate ports for three different jobs:
#   5000 = HTTP control channel (client sends search/download commands here)
#   5001 = RUDP file transfer (server pushes MP3 to client over our custom protocol)
#   5002 = plain TCP file transfer (same idea, but using standard TCP)
HTTP_PORT         = 5050
RUDP_PORT         = 5001
TCP_TRANSFER_PORT = 5002
HOST              = '0.0.0.0'   # accept connections from any machine, not just localhost

# ─── HTTP Helpers ─────────────────────────────────────────────

def send_response(conn, body_dict, status=200):
    """
    Serialize a Python dict to JSON and send it back as a valid HTTP/1.1 response.
    We manually build the response string because we're not using Flask —
    just raw sockets — so we have to follow the HTTP format ourselves:
      status line → headers → blank line → body
    """
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
    Parse raw HTTP request bytes into (method, path, params).

    HTTP request first line format:
      GET /search?q=hello%20world HTTP/1.1

    We split on spaces to get method and full path,
    then split the path on '?' to separate query string params.

    Example:
      b'GET /search?q=hello HTTP/1.1\\r\\n...'
      → ('GET', '/search', {'q': 'hello'})

    Returns (None, None, {}) if the request is malformed.
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
        # parse_qs returns lists: {'q': ['hello']} → we flatten to {'q': 'hello'}
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query_string).items()}
    else:
        path   = full_path
        params = {}

    return method, path, params

# ─── File Transfer ────────────────────────────────────────────

def send_over_rudp(filepath, client_ip, client_port):
    """
    Read the MP3 file into memory and send it to the client using our custom
    RUDP protocol (reliable UDP with sliding window, congestion control, etc.).
    The client must already be listening on client_port before we call this.
    """
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
    Read the MP3 file into memory and send it over a plain TCP connection.
    Here the server acts as the TCP *client* (it connects to the client's listening port).
    TCP handles reliability internally, so we just sendall() and close.
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

# ─── Route Handlers ───────────────────────────────────────────

def handle_search(params):
    """
    Mode 1: Search YouTube by song name.
    The 'q' param comes from the URL: /search?q=bohemian+rhapsody
    """
    query = params.get('q', '')
    if not query:
        return {'status': 'error', 'message': 'No query provided'}
    return handle_request({'action': 'search', 'query': query})

def handle_vibe(params):
    """
    Mode 3: AI-powered mood search.
    The client describes a vibe, we ask Groq (Llama 3) for song suggestions,
    then search YouTube for those songs.
    """
    description = params.get('q', '')
    if not description:
        return {'status': 'error', 'message': 'No description provided'}
    return handle_request({'action': 'vibe', 'description': description})

def handle_download_rudp(params, client_addr):
    """
    Mode 2 (RUDP variant): Download a song and send it to the client over RUDP.

    Flow:
      1. agent.py downloads the MP3 from YouTube to the server's downloads/ folder
      2. We send the HTTP response immediately so the client doesn't time out waiting
      3. The actual file transfer runs in a background thread after the HTTP reply
    """
    url         = params.get('url', '')
    title       = params.get('title', 'song')
    client_port = int(params.get('client_port', RUDP_PORT))
    client_ip   = client_addr[0]   # where the HTTP request came from = where to send the file

    if not url:
        return {'status': 'error', 'message': 'No URL provided'}

    # Step 1: download the MP3 (blocking — we need the file before we can send it)
    result = handle_request({'action': 'download_url', 'url': url, 'title': title})
    if result['status'] != 'success':
        return result

    # Step 2: start the RUDP transfer in the background
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
    Mode 2 (TCP variant): Same as handle_download_rudp but uses plain TCP.
    Useful for comparing transfer behavior between TCP and our RUDP implementation.
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
    """Return a list of all MP3 files we've already downloaded on the server."""
    return handle_request({'action': 'history'})

# ─── Router ───────────────────────────────────────────────────

def route(method, path, params, client_addr):
    """
    Map the URL path to the right handler function.
    This is a manual router — the equivalent of Flask's @app.route() decorators,
    but implemented by hand so we fully understand and control the HTTP layer.
    """
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

# ─── Client Connection Handler ────────────────────────────────

def handle_client(conn, client_addr):
    """
    Called in a new thread for each incoming TCP connection.
    Reads bytes until we see the end of HTTP headers (\r\n\r\n),
    parses the request, routes it, and sends back a JSON response.

    Each client gets its own thread so multiple clients can connect simultaneously
    without blocking each other.
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
        conn.close()   # always close the connection when done

# ─── Main Server Loop ─────────────────────────────────────────

def start():
    """
    Create the main TCP socket for the HTTP control channel.
    listen(5) allows up to 5 queued connections waiting to be accepted.
    Each accepted connection gets handed off to handle_client() in its own thread.
    """
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