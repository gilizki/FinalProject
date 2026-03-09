from rudp import RUDPReceiver
import os

r = RUDPReceiver(5001)
data = r.receive_file()

with open('received_song.mp3', 'wb') as f:
    f.write(data)
print(f"Saved {len(data)} bytes!")