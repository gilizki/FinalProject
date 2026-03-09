import socket
import os
import json
import struct
import threading
from rudp import RUDPReceiver, HEADER_SIZE, MAX_SEGMENT_SIZE
import urllib.parse

# ─── Constants ──────────────────────────────────────────────
DHCP_SERVER = '127.0.0.1'
DHCP_PORT = 6767
DNS_SERVER = '127.0.0.1'
DNS_PORT = 53
APP_SERVER = '127.0.0.1'
APP_PORT = 5000
RUDP_RECEIVE_PORT = 5001
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_downloads")
UTF = 'utf-8'

def ensure_downloads():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ─── Step 1: DHCP ───────────────────────────────────────────
def do_dhcp():
    """Do DHCP handshake, return assigned IP"""
    print("[CLIENT] Starting DHCP handshake...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(b'DISCOVER', (DHCP_SERVER, DHCP_PORT))
        data, _ = sock.recvfrom(1024)
        print(f"[DHCP] Got offer: {data.decode()}")
        sock.sendto(b'REQUEST', (DHCP_SERVER, DHCP_PORT))
        data, _ = sock.recvfrom(1024)
        print(f"[DHCP] Got ACK: {data.decode()}")
        sock.close()
        return '127.0.0.1'  # assigned IP
    except Exception as e:
        print(f"[DHCP] Failed: {e}, using localhost")
        return '127.0.0.1'

# ─── Step 2: DNS ────────────────────────────────────────────
def do_dns(hostname):
    """Query DNS for hostname, return IP"""
    print(f"[CLIENT] DNS query for: {hostname}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(hostname.encode(), (DNS_SERVER, DNS_PORT))
        data, _ = sock.recvfrom(1024)
        ip = data.decode()
        print(f"[DNS] {hostname} → {ip}")
        sock.close()
        return ip
    except Exception as e:
        print(f"[DNS] Failed: {e}, using localhost")
        return '127.0.0.1'

# ─── Step 3: HTTP request to app server ─────────────────────
def http_get(path):
    """Send HTTP GET request, return response body as string"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect((APP_SERVER, APP_PORT))
        request = f"GET {path} HTTP/1.1\r\nHost: {APP_SERVER}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode())
        response = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        sock.close()
        # extract body (after \r\n\r\n)
        body = response.split(b'\r\n\r\n', 1)[1].decode()
        return json.loads(body)
    except Exception as e:
        print(f"[HTTP] Error: {e}")
        return None

# ─── Step 4: Receive file over RUDP ─────────────────────────
def receive_over_rudp(filename):
    """Listen on RUDP port, receive file, save it"""
    ensure_downloads()
    print(f"[RUDP] Waiting for file on port {RUDP_RECEIVE_PORT}...")
    receiver = RUDPReceiver(RUDP_RECEIVE_PORT)
    data = receiver.receive_file()
    filepath = os.path.join(DOWNLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(data)
    print(f"[RUDP] Saved {len(data)} bytes to {filepath}")
    return filepath

# ─── Terminal Menu ───────────────────────────────────────────
def menu():
    print("\n" + "="*40)
    print("      🎵 MUSIC AGENT CLIENT")
    print("="*40)
    print("1. Search by song name")
    print("2. Direct YouTube URL")
    print("3. Vibe mode (AI suggestions)")
    print("4. Show history")
    print("5. Exit")
    print("="*40)
    return input("Choose (1-5): ").strip()

def handle_search(query, path_suffix):
    """Search and let user pick a result to download"""
    print(f"\n[CLIENT] Searching...")
    result = http_get(path_suffix)
    if not result or result.get('status') != 'success':
        print("[CLIENT] Search failed")
        return

    results = result.get('results') or result.get('search_results', [])
    if not results:
        print("[CLIENT] No results found")
        return

    # show results
    print(f"\n{'─'*40}")
    for i, r in enumerate(results):
        duration = r.get('duration', 0)
        mins = int(duration // 60) if duration else 0
        secs = int(duration % 60) if duration else 0
        print(f"{i+1}. {r['title']} ({mins}:{secs:02d})")
    print(f"{'─'*40}")

    # if vibe mode, show Gemini suggestions too
    if 'gemini_suggestions' in result:
        print("✨ Gemini suggested:")
        for s in result['gemini_suggestions']:
            print(f"   - {s['title']} by {s['artist']}")

    choice = input("\nPick a number to download (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) == 0:
        return

    idx = int(choice) - 1
    if idx < 0 or idx >= len(results):
        print("[CLIENT] Invalid choice")
        return

    song = results[idx]
    download_song(song['url'], song['title'])

def download_song(url, title):
    """Start RUDP receiver thread then trigger download"""
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() + '.mp3'

    # start RUDP receiver in background BEFORE telling server to send
    rudp_thread = threading.Thread(
        target=receive_over_rudp,
        args=(safe_title,)
    )
    rudp_thread.start()

    # small delay so receiver is ready
    import time
    time.sleep(0.5)

    import urllib.parse
    encoded_url = urllib.parse.quote(url, safe='')
    encoded_title = urllib.parse.quote(title, safe='')
    path = f"/download?url={encoded_url}&title={encoded_title}&client_port={RUDP_RECEIVE_PORT}"
    # tell app server to download + send over RUDP
    print(f"\n[CLIENT] Requesting download: {title}")
    result = http_get(path)

    if result and result.get('status') == 'success':
        print(f"[CLIENT] Server confirmed, receiving over RUDP...")
        rudp_thread.join()  # wait for transfer to complete
        print(f"\n✅ Done! Saved to client_downloads/{safe_title}")
    else:
        print(f"[CLIENT] Server error: {result}")

def show_history():
    result = http_get('/history')
    if not result or not result.get('history'):
        print("\n[CLIENT] No downloads yet")
        return
    print(f"\n{'─'*40}")
    for song in result['history']:
        print(f"🎵 {song['title']} ({song['size_kb']} KB)")
    print(f"{'─'*40}")

# ─── Main ────────────────────────────────────────────────────
def main():
    print("\n[CLIENT] Starting up...")

    # Step 1: DHCP
    my_ip = do_dhcp()
    print(f"[CLIENT] Got IP: {my_ip}")

    # Step 2: DNS
    server_ip = do_dns('app.local')
    print(f"[CLIENT] App server at: {server_ip}")

    # Step 3: Menu loop
    while True:
        choice = menu()

        if choice == '1':
            query = input("Song name: ").strip()
            handle_search(query, f"/search?q={query.replace(' ', '%20')}")

        elif choice == '2':
            url = input("YouTube URL: ").strip()
            title = input("Title (for filename): ").strip()
            download_song(url, title)

        elif choice == '3':
            vibe = input("Describe the vibe: ").strip()
            handle_search(vibe, f"/vibe?q={vibe.replace(' ', '%20')}")

        elif choice == '4':
            show_history()

        elif choice == '5':
            print("\n[CLIENT] Goodbye! 🎵")
            break

        else:
            print("[CLIENT] Invalid choice, try again")

if __name__ == '__main__':
    main()