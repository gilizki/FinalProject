import yt_dlp
import os
import json

# ─── Constants ──────────────────────────────────────────────
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


def ensure_downloads_dir():
    """Create downloads folder if it doesn't exist"""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def search_songs(query, max_results=5):
    """
    Search YouTube for songs by name.
    Returns list of results with title, duration, thumbnail, url.
    """
    print(f"[AGENT] Searching for: {query}")

    ydl_opts = {
        'quiet': True,  # don't print yt-dlp's own output
        'no_warnings': True,
        'extract_flat': True,  # don't download, just get info
    }

    results = []
    search_query = f"ytsearch{max_results}:{query}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            for entry in info['entries']:
                if entry:
                    results.append({
                        'title': entry.get('title', 'Unknown'),
                        'duration': entry.get('duration', 0),
                        'thumbnail': entry.get('thumbnail', ''),
                        'url': entry.get('url', ''),
                        'id': entry.get('id', '')
                    })
        print(f"[AGENT] Found {len(results)} results")
        return results
    except Exception as e:
        print(f"[AGENT] Search error: {e}")
        return []


def download_song(youtube_url, title="song"):
    """
    Download a song as MP3 given its YouTube URL.
    Returns the filepath of the downloaded MP3.
    """
    ensure_downloads_dir()
    print(f"[AGENT] Downloading: {title}")

    # clean filename - remove characters that break file paths
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    output_path = os.path.join(DOWNLOADS_DIR, safe_title)

    ydl_opts = {
        'format': 'bestaudio/best',  # get best audio quality
        'quiet': True,
        'no_warnings': True,
        'outtmpl': output_path,  # where to save
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',  # convert to MP3
            'preferredcodec': 'mp3',
            'preferredquality': '192',  # 192kbps quality
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        final_path = output_path + '.mp3'
        if os.path.exists(final_path):
            print(f"[AGENT] Downloaded successfully: {final_path}")
            return final_path
        else:
            print(f"[AGENT] File not found after download: {final_path}")
            return None
    except Exception as e:
        print(f"[AGENT] Download error: {e}")
        return None


def get_history():
    """
    Returns list of all downloaded MP3s in the downloads folder.
    """
    ensure_downloads_dir()
    files = []
    for f in os.listdir(DOWNLOADS_DIR):
        if f.endswith('.mp3'):
            full_path = os.path.join(DOWNLOADS_DIR, f)
            files.append({
                'filename': f,
                'title': f.replace('.mp3', ''),
                'size_kb': round(os.path.getsize(full_path) / 1024, 1),
                'path': full_path
            })
    return files


# ─── Quick test ─────────────────────────────────────────────
if __name__ == '__main__':
    print("=== Testing Search ===")
    results = search_songs("Bohemian Rhapsody")
    for i, r in enumerate(results):
        print(f"{i + 1}. {r['title']} ({r['duration']}s)")

    if results:
        print("\n=== Testing Download ===")
        first = results[0]
        path = download_song(first['url'], first['title'])
        print(f"Saved to: {path}")

    print("\n=== History ===")
    for song in get_history():
        print(f"- {song['title']} ({song['size_kb']} KB)")