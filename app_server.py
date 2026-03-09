import os
import threading
from flask import Flask, request, jsonify, send_file
from agent import handle_request
from rudp import RUDPSender

# ─── Constants ──────────────────────────────────────────────
HTTP_PORT = 5000       # browser talks to this
RUDP_PORT = 5001       # client.py receives file on this

app = Flask(__name__)

# ─── Routes ─────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the main webpage"""
    return send_file('index.html')

@app.route('/search')
def search():
    """Mode 2: search by song name"""
    query = request.args.get('q', '')
    if not query:
        return jsonify({'status': 'error', 'message': 'No query provided'})
    result = handle_request({'action': 'search', 'query': query})
    return jsonify(result)

@app.route('/vibe')
def vibe():
    """Mode 3: Gemini vibe search"""
    description = request.args.get('q', '')
    if not description:
        return jsonify({'status': 'error', 'message': 'No description provided'})
    result = handle_request({'action': 'vibe', 'description': description})
    return jsonify(result)

@app.route('/download')
def download():
    """
    Download a song and send it to the client over RUDP.
    Browser sends: /download?url=...&title=...&client_port=...
    """
    url        = request.args.get('url', '')
    title      = request.args.get('title', 'song')
    client_port = int(request.args.get('client_port', RUDP_PORT))

    if not url:
        return jsonify({'status': 'error', 'message': 'No URL provided'})

    # Step 1: agent downloads the MP3 to server's downloads folder
    result = handle_request({
        'action': 'download_url',
        'url': url,
        'title': title
    })

    if result['status'] != 'success':
        return jsonify(result)

    filepath = result['filepath']

    # Step 2: send the MP3 to client over RUDP in background thread
    def send_over_rudp():
        print(f"[APP SERVER] Sending {filepath} over RUDP to port {client_port}")
        try:
            with open(filepath, 'rb') as f:
                file_bytes = f.read()
            sender = RUDPSender('127.0.0.1', client_port)
            sender.send_file(file_bytes)
            print(f"[APP SERVER] RUDP transfer complete!")
        except Exception as e:
            print(f"[APP SERVER] RUDP transfer failed: {e}")

    thread = threading.Thread(target=send_over_rudp)
    thread.start()

    return jsonify({
        'status': 'success',
        'message': f'Downloading {title}, transfer starting over RUDP port {client_port}',
        'filename': result['filename']
    })

@app.route('/history')
def history():
    """Return list of previously downloaded songs"""
    result = handle_request({'action': 'history'})
    return jsonify(result)


# ─── Start server ───────────────────────────────────────────
if __name__ == '__main__':
    import signal
    def shutdown(sig, frame):
        print("\n[APP SERVER] Shutting down gracefully...")
        os._exit(0)
    signal.signal(signal.SIGINT, shutdown)
    print(f"[APP SERVER] Starting on http://127.0.0.1:{HTTP_PORT}")
    app.run(host='127.0.0.1', port=HTTP_PORT, debug=False)