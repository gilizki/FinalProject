import yt_dlp
import os
import json
import time
from groq import Groq
from dotenv import load_dotenv

# ─── Setup ──────────────────────────────────────────────────
load_dotenv()
client_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

def ensure_downloads_dir():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
# simple in-memory cache for search results and AI suggestions
_search_cache = {}
_ai_cache = {}
_download_cache = set()

# ─── Mode 3: AI vibe search ─────────────────────────────
def ask_AI_for_songs(vibe_description):
    """
    Send a mood/vibe description to Groq (Llama 3).
    Returns a list of 3 specific song suggestions.
    """
    if vibe_description in _ai_cache:
        print(f"[AGENT] AI cache hit for: {vibe_description}")
        return _ai_cache[vibe_description]

    print(f"[AGENT] Asking Groq AI for: '{vibe_description}'")

    # Prompt instructs the AI to detect the language and reply accordingly
    prompt = f"""
    The user wants to listen to music matching this description: "{vibe_description}"
    Suggest exactly 3 specific, real songs that match this vibe.
    CRITICAL INSTRUCTION: Detect the language of the user's description. 
    - If the description is in Hebrew, you MUST write the song titles and artist names in Hebrew characters (e.g., "אריק איינשטיין").
    - If the description is in English, write them in English.

    Reply ONLY with a valid JSON array, no other text or formatting, like this:
    [
        {{"title": "Song Name", "artist": "Artist Name"}},
        {{"title": "שם השיר", "artist": "שם האמן"}}
    ]
    """

    try:
        # Requesting completion from Groq
        response = client_groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.7 # level of determination
        )

        text = response.choices[0].message.content.strip()
        text = text.replace('```json', '').replace('```', '').strip()
        songs = json.loads(text)

        print(f"[AGENT] Groq suggested: {[s['title'] for s in songs]}")
        _ai_cache[vibe_description] = songs
        return songs

    except Exception as e:
        print(f"[AGENT] Groq error: {e}")
        # Fallback in case of API or network failure
        return [{"title": vibe_description, "artist": ""}]

# ─── Mode 1 + 2: YouTube search and download ────────────────
def search_songs(query, max_results=5):
    """Search YouTube by name, return list of results."""
    # check cache first
    if query in _search_cache:
        print(f"[AGENT] Cache hit for: {query}")
        return _search_cache[query]
    #if not in cache search YouTube
    print(f"[AGENT] Searching YouTube for: {query}")
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            for entry in info['entries']:
                if entry:
                    results.append({
                        'title': entry.get('title', 'Unknown'),
                        'duration': entry.get('duration', 0),
                        'thumbnail': entry.get('thumbnail', ''),
                        'url': f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                        'id': entry.get('id', '')
                    })
        print(f"[AGENT] Found {len(results)} results")
        # save to cache before returning
        _search_cache[query] = results
        return results
    except Exception as e:
        print(f"[AGENT] Search error: {e}")
        return []


def download_song(youtube_url, title="song"):
    """
    Download a single song as MP3.
    Explicitly ignores playlists and sanitizes filenames.
    """
    ensure_downloads_dir()
    # 1. Protection against empty titles (crucial for direct URL mode)
    if not title or title.strip() == "":
        title = "youtube_song"
    # 2. Sanitize filename to prevent OS saving errors
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    # 3. Secondary protection if title contained only invalid characters
    if not safe_title:
        safe_title = f"song_{int(time.time())}"
    final_path = os.path.join(DOWNLOADS_DIR, safe_title + '.mp3')
    output_path = os.path.join(DOWNLOADS_DIR, safe_title)
    cache_key = safe_title + '_' + youtube_url[-11:]  # video ID is last 11 chars of YouTube URL
    print(f"[AGENT] Downloading: '{safe_title}' from URL: {youtube_url}")
    # if already downloaded, skip yt-dlp entirely
    if os.path.exists(final_path) and cache_key in _download_cache:
        print(f"[AGENT] Already downloaded at Agent, using cached file: {final_path}")
        return final_path

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',  # Optimized for faster audio downloads
        'noplaylist': True,  # Prevents downloading entire playlists
        'quiet': False,  # Keep false to see download progress/errors
        'no_warnings': True,
        'outtmpl': f"{output_path}.%(ext)s",
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',  # 128kbps is lighter and faster for RUDP transfer
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        final_path = output_path + '.mp3'

        # 4. Verify file existence before returning path to the server
        if os.path.exists(final_path):
            print(f"[AGENT] Success: Saved to {final_path}")
            _download_cache.add(cache_key)
            return final_path
        else:
            print(f"[AGENT] Error: Could not find the generated MP3 at {final_path}")
            return None

    except Exception as e:
        print(f"[AGENT] Download error: {e}")
        return None


def get_history():
    """Return list of all downloaded MP3s."""
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


# ─── Main handler: routes between the 3 modes ───────────────
def handle_request(request):
    """
    Main entry point. Routes incoming requests to the appropriate logic.
    Supports download by URL, search by query, AI vibe suggestions, and history.
    """
    action = request.get('action')

    if action == 'download_url':
        url = request.get('url')
        title = request.get('title', 'song')
        if not url:
            return {'status': 'error', 'message': 'No URL provided'}

        path = download_song(url, title)
        if path:
            return {'status': 'success', 'filepath': path, 'filename': os.path.basename(path)}
        return {'status': 'error', 'message': 'Download failed'}

    elif action == 'search':
        query = request.get('query')
        if not query:
            return {'status': 'error', 'message': 'No query provided'}

        results = search_songs(query)
        return {'status': 'success', 'results': results}

    elif action == 'vibe':
        description = request.get('description')
        if not description:
            return {'status': 'error', 'message': 'No description provided'}

        suggestions = ask_AI_for_songs(description)
        if not suggestions:
            return {'status': 'error', 'message': 'AI could not suggest songs'}

        # Attempt to find the first suggested song on YouTube
        for song in suggestions:
            search_query = f"{song['title']} {song['artist']}"
            results = search_songs(search_query, max_results=3)

            # Break loop and return as soon as valid results are found
            if results:
                return {
                    'status': 'success',
                    'ai_suggestions': suggestions,
                    'search_results': results
                }

        return {'status': 'error', 'message': 'Could not find the suggested songs on YouTube.'}

    elif action == 'history':
        return {'status': 'success', 'history': get_history()}

    else:
        return {'status': 'error', 'message': f'Unknown action: {action}'}


# ─── Quick test ──────────────────────────────────────────────
if __name__ == '__main__':
    print("=== Test Mode 1: Direct URL ===")
    result = handle_request({
        'action': 'download_url',
        'url': 'https://www.youtube.com/watch?v=XdpLasLSIAw&list=RDXdpLasLSIAw&start_radio=1',
        'title': 'רביד פלוטניק - בדיוק כמו שאני / Ravid Plotnik - As I Am'
    })
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 2: Search by name ===")
    result = handle_request({
        'action': 'search',
        'query': 'Bohemian Rhapsody'
    })
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 3: Vibe (first call) ===")
    result = handle_request({
        'action': 'vibe',
        'description': 'something chill to study to'
    })
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 3: Vibe (second call - should hit cache) ===")
    result = handle_request({
        'action': 'vibe',
        'description': 'something chill to study to'
    })
    print(json.dumps(result, indent=2))

    print("\n=== Test History ===")
    result = handle_request({'action': 'history'})
    print(json.dumps(result, indent=2))