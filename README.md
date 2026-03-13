# 🎵 Music Agent — Computer Networks Final Project

A client-server music downloader built from scratch using raw sockets.  
The client gets an IP via DHCP, resolves the server hostname via DNS,  
sends HTTP requests to the App Server, and receives MP3 files over our  
custom **RUDP** protocol (Reliable UDP with congestion control).

---

## System Architecture

```
Client
  │
  ├── DHCP handshake   → DHCP Server   (UDP port 6767)
  ├── DNS query        → DNS Server    (UDP port 53)
  ├── HTTP requests    → App Server    (TCP port 5050)
  └── File transfer    → App Server    (RUDP port 5001 / TCP port 5002)
                              │
                         YouTube (yt-dlp)
```

---

## Requirements

- **Python 3.11+** (Python 3.9 is not supported by yt-dlp)
- **FFmpeg** (must be installed separately for MP3 conversion)

### Install FFmpeg
| OS      | Command                     |
|---------|-----------------------------|
| Windows | `winget install ffmpeg`     |
| Mac     | `brew install ffmpeg`       |
| Linux   | `sudo apt install ffmpeg`   |

### Install Python packages
```bash
pip install -r requirements.txt
```

### Groq API Key (for Vibe mode)
Create a `.env` file in the project root:
```
GROQ_API_KEY=your_key_here
```
Get a free key at: https://console.groq.com

---

## How to Run

### Server machine — run these 3 in separate terminals:
```bash
python DHCP.py          # or: sudo python DHCP.py  (if port 6767 needs admin)
python DNS.py           # or: sudo python DNS.py   (port 53 needs admin on Mac/Linux)
python app_server.py
```

### Client machine:
```bash
python client.py
```

> If running across **two computers**, open `client.py` and change:
> ```python
> DHCP_SERVER = '127.0.0.1'   # → replace with server machine's LAN IP
> ```
> Everything else (DNS, App Server IP) is resolved automatically.

---

## Features

| Option | Description |
|--------|-------------|
| 1. Search by name | Search YouTube and download any song |
| 2. Direct URL | Paste a YouTube URL directly |
| 3. Vibe mode | Describe a mood → AI suggests songs (Groq / Llama 3) |
| 4. History | View all previously downloaded songs |

---

## Project Files

| File | Description |
|------|-------------|
| `client.py` | Client: DHCP → DNS → HTTP → RUDP receiver |
| `app_server.py` | HTTP server: routes requests, triggers downloads and transfers |
| `agent.py` | YouTube search/download logic + Groq AI integration |
| `DHCP.py` | DHCP server: full DORA handshake, IP pool management |
| `DNS.py` | DNS server: local table + external forwarding (8.8.8.8) |
| `rudp.py` | Custom RUDP protocol: sliding window, congestion control (TCP Reno) |

---

## RUDP Protocol

Our custom reliable UDP implementation includes:
- **Slow Start** → exponential cwnd growth until ssthresh
- **Congestion Avoidance** → linear cwnd growth (AIMD)
- **Fast Retransmit** → retransmit on 3 duplicate ACKs
- **Go-Back-N timeout** → retransmit window on timeout
- **Flow control** → receiver advertises rwnd dynamically

---

*Course: Computer Networks (7028310) — Ariel University*  
*Instructor: Prof. Amit Dvir*