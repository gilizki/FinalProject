import yt_dlp
import os
import json
from google import genai
from dotenv import load_dotenv

# ─── Setup ──────────────────────────────────────────────────
load_dotenv()
client_gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

def ensure_downloads_dir():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
# simple in-memory cache for search results and gemini suggestions
_search_cache = {}
_gemini_cache = {}

# ─── Mode 1: Gemini vibe search ─────────────────────────────
def ask_gemini_for_songs(vibe_description):
    """
    Send a mood/vibe description to Gemini.
    Returns a list of 3 specific song suggestions.
    """
    if vibe_description in _gemini_cache:
        print(f"[AGENT] Gemini cache hit for: {vibe_description}")
        return _gemini_cache[vibe_description]

    print(f"[AGENT] Asking Gemini for: '{vibe_description}'")
    prompt = f"""
    The user wants to listen to music matching this description: "{vibe_description}"

    Suggest exactly 3 specific songs that match this vibe.
    Reply ONLY with a JSON array, no other text, like this:
    [
        {{"title": "Song Name", "artist": "Artist Name"}},
        {{"title": "Song Name", "artist": "Artist Name"}},
        {{"title": "Song Name", "artist": "Artist Name"}}
    ]
    """

    try:
        response = client_gemini.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt
        )
        text = response.text.strip()
        text = text.replace('```json', '').replace('```', '').strip()
        songs = json.loads(text)
        print(f"[AGENT] Gemini suggested: {[s['title'] for s in songs]}")
        _gemini_cache[vibe_description] = songs
        return songs
    except Exception as e:
        #print(f"[AGENT] Gemini unavailable ({e}), using mock response")
        print(f"[AGENT] Gemini unavailable, using mock response")
        # mock response so we can keep developing
        return [
            {"title": "Lofi Hip Hop Mix", "artist": "ChilledCow"},
            {"title": "Clair de Lune", "artist": "Debussy"},
            {"title": "Study With Me", "artist": "Thomas Frank"}
        ]


# ─── Mode 2 + 3: YouTube search and download ────────────────
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
    """Download a song as MP3, return the file path."""
    ensure_downloads_dir()
    print(f"[AGENT] Downloading: {title}")
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    output_path = os.path.join(DOWNLOADS_DIR, safe_title)

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'outtmpl': output_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        final_path = output_path + '.mp3'
        if os.path.exists(final_path):
            print(f"[AGENT] Success: {final_path}")
            return final_path
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
    Main entry point. Receives a dict, returns a dict.
    Mode 1: {"action": "download_url", "url": "https://youtube.com/..."}
    Mode 2: {"action": "search", "query": "Bohemian Rhapsody"}
    Mode 3: {"action": "vibe", "description": "something chill to study to"}
    Other:  {"action": "history"}
    """
    action = request.get('action')

    if action == 'download_url':
        # Mode 1: direct URL download
        url = request.get('url')
        title = request.get('title', 'song')
        if not url:
            return {'status': 'error', 'message': 'No URL provided'}
        path = download_song(url, title)
        if path:
            return {'status': 'success', 'filepath': path, 'filename': os.path.basename(path)}
        return {'status': 'error', 'message': 'Download failed'}

    elif action == 'search':
        # Mode 2: search by name
        query = request.get('query')
        if not query:
            return {'status': 'error', 'message': 'No query provided'}
        results = search_songs(query)
        return {'status': 'success', 'results': results}

    elif action == 'vibe':
        # Mode 3: Gemini suggests songs based on mood
        description = request.get('description')
        if not description:
            return {'status': 'error', 'message': 'No description provided'}
        suggestions = ask_gemini_for_songs(description)
        if not suggestions:
            return {'status': 'error', 'message': 'Gemini could not suggest songs'}
        # auto-search the first suggestion on YouTube
        first = suggestions[0]
        search_query = f"{first['title']} {first['artist']}"
        results = search_songs(search_query, max_results=3)
        return {
            'status': 'success',
            'gemini_suggestions': suggestions,
            'search_results': results
        }

    elif action == 'history':
        return {'status': 'success', 'history': get_history()}

    else:
        return {'status': 'error', 'message': f'Unknown action: {action}'}


# ─── Quick test ──────────────────────────────────────────────
if __name__ == '__main__':
    print("=== Test Mode 1: Direct URL ===")
    result = handle_request({
        'action': 'download_url',
        'url': 'https://www.youtube.com/watch?v=RfBHgGR8HDo',
        'title': 'lofi test song'
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