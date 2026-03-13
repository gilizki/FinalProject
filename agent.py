import yt_dlp
import os
import json
import time
from groq import Groq
from dotenv import load_dotenv

# ─── Setup ───────────────────────────────────────────────────
load_dotenv()   # reads GROQ_API_KEY from the .env file
client_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Downloads go to a folder next to this file, not wherever Python is run from.
# os.path.abspath(__file__) gives us the absolute path of agent.py,
# then dirname gives us the folder it lives in.
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

def ensure_downloads_dir():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Simple in-memory caches to avoid repeating the same slow operations.
# _search_cache: YouTube search results (so we don't re-query for the same song name)
# _ai_cache: Groq AI responses (so we don't re-call the API for the same vibe description)
# _download_cache: set of safe_title + video_id strings for songs already downloaded
_search_cache   = {}
_ai_cache       = {}
_download_cache = set()

# ─── Mode 3: AI vibe search ───────────────────────────────────

def ask_AI_for_songs(vibe_description):
    """
    Send a mood description to Groq (Llama 3.3 70B) and get back 3 song suggestions.

    We use a structured prompt that tells the model:
    - detect the language of the description (Hebrew or English)
    - return ONLY a JSON array, no extra text

    The temperature=0.7 controls creativity:
    higher = more varied suggestions, lower = more predictable answers.
    """
    if vibe_description in _ai_cache:
        print(f"[AGENT] AI cache hit for: {vibe_description}")
        return _ai_cache[vibe_description]

    print(f"[AGENT] Asking Groq AI for: '{vibe_description}'")

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
        response = client_groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.7
        )

        text = response.choices[0].message.content.strip()
        # Strip markdown code fences in case the model wraps the JSON anyway
        text = text.replace('```json', '').replace('```', '').strip()
        songs = json.loads(text)

        print(f"[AGENT] Groq suggested: {[s['title'] for s in songs]}")
        _ai_cache[vibe_description] = songs
        return songs

    except Exception as e:
        print(f"[AGENT] Groq error: {e}")
        # Fallback: treat the description itself as a search query
        return [{"title": vibe_description, "artist": ""}]

# ─── Mode 1 + 2: YouTube search and download ──────────────────

def search_songs(query, max_results=5):
    """
    Search YouTube and return a list of result dicts (title, url, duration, etc.).
    Uses yt-dlp in 'flat extract' mode which gets metadata without downloading anything.
    Results are cached by query string to avoid re-querying YouTube for the same thing.
    """
    if query in _search_cache:
        print(f"[AGENT] Cache hit for: {query}")
        return _search_cache[query]

    print(f"[AGENT] Searching YouTube for: {query}")
    ydl_opts = {
        'quiet':        True,
        'no_warnings':  True,
        'extract_flat': True,   # only get metadata, don't download
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            for entry in info['entries']:
                if entry:
                    results.append({
                        'title':     entry.get('title', 'Unknown'),
                        'duration':  entry.get('duration', 0),
                        'thumbnail': entry.get('thumbnail', ''),
                        'url':       f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                        'id':        entry.get('id', '')
                    })
        print(f"[AGENT] Found {len(results)} results")
        _search_cache[query] = results
        return results
    except Exception as e:
        print(f"[AGENT] Search error: {e}")
        return []


def download_song(youtube_url, title="song"):
    """
    Download a single YouTube video as an MP3 file using yt-dlp + FFmpeg.

    yt-dlp downloads the best available audio stream (usually .m4a),
    then the FFmpeg postprocessor converts it to 128kbps MP3.
    128kbps is a good tradeoff between quality and file size for RUDP transfer.

    We sanitize the filename first to remove characters the OS won't accept,
    and check if we've already downloaded this song before calling yt-dlp.
    """
    ensure_downloads_dir()

    # Guard against empty title
    if not title or title.strip() == "":
        title = "youtube_song"

    # Remove any characters that aren't safe in filenames
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()

    # Secondary guard if the title was entirely invalid characters
    if not safe_title:
        safe_title = f"song_{int(time.time())}"

    final_path  = os.path.join(DOWNLOADS_DIR, safe_title + '.mp3')
    output_path = os.path.join(DOWNLOADS_DIR, safe_title)

    # Cache key uses the filename + last 11 chars of URL (YouTube video ID)
    cache_key = safe_title + '_' + youtube_url[-11:]
    print(f"[AGENT] Downloading: '{safe_title}' from URL: {youtube_url}")

    # Skip yt-dlp entirely if we already have the file
    if os.path.exists(final_path) and cache_key in _download_cache:
        print(f"[AGENT] Already downloaded, using cached file: {final_path}")
        return final_path

    ydl_opts = {
        'format':      'bestaudio[ext=m4a]/bestaudio/best',
        'noplaylist':  True,    # never download entire playlists, only the one video
        'quiet':       False,   # keep visible so we can see progress and errors
        'no_warnings': True,
        'outtmpl':     f"{output_path}.%(ext)s",
        'postprocessors': [{
            'key':             'FFmpegExtractAudio',
            'preferredcodec':  'mp3',
            'preferredquality': '128',
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        final_path = output_path + '.mp3'

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
    """
    Scan the downloads folder and return metadata for every MP3 file found.
    This is the server-side history — what we've downloaded before.
    """
    ensure_downloads_dir()
    files = []
    for f in os.listdir(DOWNLOADS_DIR):
        if f.endswith('.mp3'):
            full_path = os.path.join(DOWNLOADS_DIR, f)
            files.append({
                'filename': f,
                'title':    f.replace('.mp3', ''),
                'size_kb':  round(os.path.getsize(full_path) / 1024, 1),
                'path':     full_path
            })
    return files


# ─── Main handler: routes between the 3 modes ─────────────────

def handle_request(request):
    """
    The main entry point that app_server.py calls for every client action.
    Routes to the appropriate function based on the 'action' field:

      download_url → download a specific YouTube URL and return the file path
      search       → search YouTube by name and return a list of results
      vibe         → ask AI for song suggestions matching a mood, then search those
      history      → list all previously downloaded MP3 files
    """
    action = request.get('action')

    if action == 'download_url':
        url   = request.get('url')
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

        # Try each AI suggestion on YouTube until we find one with results
        for song in suggestions:
            search_query = f"{song['title']} {song['artist']}"
            results      = search_songs(search_query, max_results=3)
            if results:
                return {
                    'status':         'success',
                    'ai_suggestions': suggestions,
                    'search_results': results
                }

        return {'status': 'error', 'message': 'Could not find the suggested songs on YouTube.'}

    elif action == 'history':
        return {'status': 'success', 'history': get_history()}

    else:
        return {'status': 'error', 'message': f'Unknown action: {action}'}


# ─── Quick test ───────────────────────────────────────────────
if __name__ == '__main__':
    print("=== Test Mode 1: Direct URL ===")
    result = handle_request({
        'action': 'download_url',
        'url':   'https://www.youtube.com/watch?v=XdpLasLSIAw&list=RDXdpLasLSIAw&start_radio=1',
        'title': 'רביד פלוטניק - בדיוק כמו שאני / Ravid Plotnik - As I Am'
    })
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 2: Search by name ===")
    result = handle_request({'action': 'search', 'query': 'Bohemian Rhapsody'})
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 3: Vibe (first call) ===")
    result = handle_request({'action': 'vibe', 'description': 'something chill to study to'})
    print(json.dumps(result, indent=2))

    print("\n=== Test Mode 3: Vibe (second call - should hit cache) ===")
    result = handle_request({'action': 'vibe', 'description': 'something chill to study to'})
    print(json.dumps(result, indent=2))

    print("\n=== Test History ===")
    result = handle_request({'action': 'history'})
    print(json.dumps(result, indent=2))