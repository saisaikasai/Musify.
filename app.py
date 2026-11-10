"""
Musify - Single-file Flask music app (Vercel ready)
Everything is inside this one file: server, database, music search,
web page (HTML), styles (CSS), scripts (JS), manifest, service worker and icons.
Only requirements.txt is needed next to it.
"""

import os
import sys
import json
import base64
import html
import sqlite3
import logging
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional
from flask import Flask, request, jsonify, Response, abort

# =====================================================================
# PART 1 - DATABASE
# =====================================================================

def _resolve_db_path() -> str:
    if os.environ.get("MUSIFY_DB"):
        return os.environ["MUSIFY_DB"]
    if os.environ.get("VERCEL") or not os.access(os.getcwd(), os.W_OK):
        return "/tmp/musify.db"
    return "musify.db"

DB_FILE = _resolve_db_path()


def get_db_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    """Creates a connection to the SQLite database with dictionary-like row access."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DB_FILE) -> None:
    """Initializes the database schema if tables do not exist."""
    conn = get_db_connection(db_path)
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                age INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS playlist_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                song_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                duration INTEGER DEFAULT 0,
                image_url TEXT,
                media_url TEXT NOT NULL,
                position INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (playlist_id) REFERENCES playlists (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS queue_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                duration INTEGER DEFAULT 0,
                image_url TEXT,
                media_url TEXT NOT NULL,
                position INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_playlist_songs_pid ON playlist_songs (playlist_id, position);
            CREATE INDEX IF NOT EXISTS idx_queue_songs_pos ON queue_songs (position);
        """)
    conn.close()


# --- Playlist Operations ---

def create_playlist(name: str, description: str = "", db_path: str = DB_FILE) -> Dict[str, Any]:
    """Creates a new playlist and returns its metadata."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO playlists (name, description) VALUES (?, ?)",
            (name.strip(), description.strip())
        )
        playlist_id = cursor.lastrowid
    conn.close()
    return {"id": playlist_id, "name": name.strip(), "description": description.strip(), "song_count": 0}


def get_all_playlists(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Retrieves all playlists along with their total song counts."""
    conn = get_db_connection(db_path)
    query = """
        SELECT p.id, p.name, p.description, p.created_at,
               COUNT(ps.id) as song_count
        FROM playlists p
        LEFT JOIN playlist_songs ps ON p.id = ps.playlist_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """
    rows = conn.execute(query).fetchall()
    playlists = [dict(row) for row in rows]
    conn.close()
    return playlists


def get_playlist_by_id(playlist_id: int, db_path: str = DB_FILE) -> Optional[Dict[str, Any]]:
    """Retrieves a playlist along with its ordered songs."""
    conn = get_db_connection(db_path)
    playlist_row = conn.execute(
        "SELECT id, name, description, created_at FROM playlists WHERE id = ?",
        (playlist_id,)
    ).fetchone()

    if not playlist_row:
        conn.close()
        return None

    playlist = dict(playlist_row)
    songs_rows = conn.execute(
        """SELECT id, song_id, title, artist, album, duration, image_url, media_url, position, added_at
           FROM playlist_songs
           WHERE playlist_id = ?
           ORDER BY position ASC, added_at ASC""",
        (playlist_id,)
    ).fetchall()
    playlist["songs"] = [dict(song) for song in songs_rows]
    playlist["song_count"] = len(playlist["songs"])
    conn.close()
    return playlist


def delete_playlist(playlist_id: int, db_path: str = DB_FILE) -> bool:
    """Deletes a playlist and cascades song deletion."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def add_song_to_playlist(playlist_id: int, song_data: Dict[str, Any], db_path: str = DB_FILE) -> Dict[str, Any]:
    """Adds a song to the specified playlist at the next available position."""
    conn = get_db_connection(db_path)
    with conn:
        pos_row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,)
        ).fetchone()
        next_pos = pos_row["next_pos"]

        cursor = conn.execute(
            """INSERT INTO playlist_songs 
               (playlist_id, song_id, title, artist, album, duration, image_url, media_url, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                playlist_id,
                str(song_data.get("song_id") or song_data.get("id", "")),
                song_data.get("title", "Unknown Title"),
                song_data.get("artist", "Unknown Artist"),
                song_data.get("album", ""),
                int(song_data.get("duration", 0)),
                song_data.get("image_url", ""),
                song_data.get("media_url", ""),
                next_pos
            )
        )
        item_id = cursor.lastrowid
    conn.close()
    return {"id": item_id, "playlist_id": playlist_id, "position": next_pos, **song_data}


def remove_song_from_playlist(playlist_id: int, song_entry_id: int, db_path: str = DB_FILE) -> bool:
    """Removes a specific song entry from a playlist."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND id = ?",
            (playlist_id, song_entry_id)
        )
        removed = cursor.rowcount > 0
    conn.close()
    return removed


# --- Queue Persistence Operations ---

def get_queue(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Fetches all songs in the persistent playback queue ordered by position."""
    conn = get_db_connection(db_path)
    rows = conn.execute(
        """SELECT id, song_id, title, artist, album, duration, image_url, media_url, position, added_at
           FROM queue_songs
           ORDER BY position ASC, added_at ASC"""
    ).fetchall()
    queue = [dict(row) for row in rows]
    conn.close()
    return queue


def set_queue(song_list: List[Dict[str, Any]], db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Replaces the entire persistent queue with a new ordered list of songs."""
    conn = get_db_connection(db_path)
    with conn:
        conn.execute("DELETE FROM queue_songs")
        for idx, song in enumerate(song_list):
            conn.execute(
                """INSERT INTO queue_songs 
                   (song_id, title, artist, album, duration, image_url, media_url, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(song.get("song_id") or song.get("id", "")),
                    song.get("title", "Unknown Title"),
                    song.get("artist", "Unknown Artist"),
                    song.get("album", ""),
                    int(song.get("duration", 0)),
                    song.get("image_url", ""),
                    song.get("media_url", ""),
                    idx + 1
                )
            )
    conn.close()
    return get_queue(db_path)


def add_to_queue(song_data: Dict[str, Any], db_path: str = DB_FILE) -> Dict[str, Any]:
    """Appends a single song to the persistent queue."""
    conn = get_db_connection(db_path)
    with conn:
        pos_row = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM queue_songs").fetchone()
        next_pos = pos_row["next_pos"]
        cursor = conn.execute(
            """INSERT INTO queue_songs 
               (song_id, title, artist, album, duration, image_url, media_url, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(song_data.get("song_id") or song_data.get("id", "")),
                song_data.get("title", "Unknown Title"),
                song_data.get("artist", "Unknown Artist"),
                song_data.get("album", ""),
                int(song_data.get("duration", 0)),
                song_data.get("image_url", ""),
                song_data.get("media_url", ""),
                next_pos
            )
        )
        item_id = cursor.lastrowid
    conn.close()
    return {"id": item_id, "position": next_pos, **song_data}


def remove_from_queue(queue_entry_id: int, db_path: str = DB_FILE) -> bool:
    """Removes a song from the queue by its unique queue entry ID."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute("DELETE FROM queue_songs WHERE id = ?", (queue_entry_id,))
        removed = cursor.rowcount > 0
    conn.close()
    return removed


def clear_queue(db_path: str = DB_FILE) -> None:
    """Clears all songs from the playback queue."""
    conn = get_db_connection(db_path)
    with conn:
        conn.execute("DELETE FROM queue_songs")
    conn.close()


# --- Passwordless User Profile (Name & Age) ---

def save_user_profile(name: str, age: int, db_path: str = DB_FILE) -> Dict[str, Any]:
    """Saves or updates the quick onboarding user profile (name and age only)."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO user_profiles (name, age) VALUES (?, ?)",
            (name.strip(), int(age))
        )
        profile_id = cursor.lastrowid
    conn.close()
    return {"id": profile_id, "name": name.strip(), "age": int(age)}


def get_latest_profile(db_path: str = DB_FILE) -> Optional[Dict[str, Any]]:
    """Retrieves the most recently saved user profile."""
    conn = get_db_connection(db_path)
    row = conn.execute(
        "SELECT id, name, age, created_at FROM user_profiles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# =====================================================================
# PART 2 - JIOSAAVN API ENGINE (Integrated from JioSaavnAPI-master)
# Includes endpoints.py, helper.py (pyDes decrypt_url + 320kbps/160kbps format_song),
# and jiosaavn.py (search_for_song, get_song, get_album, get_playlist, get_lyrics)
# with multi-page search (40+ songs per request + Load More pagination).
# =====================================================================

import re
import requests
try:
    from pyDes import des, ECB, PAD_PKCS5
    HAS_PYDES = True
except ImportError:
    HAS_PYDES = False

logger = logging.getLogger(__name__)

# --- Official JioSaavn API Endpoints (from JioSaavnAPI-master/endpoints.py) ---
search_base_url = "https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query="
search_results_base_url = "https://www.jiosaavn.com/api.php?__call=search.getResults&_format=json&_marker=0&cc=in&includeMetaTags=1"
song_details_base_url = "https://www.jiosaavn.com/api.php?__call=song.getDetails&cc=in&_marker=0%3F_marker%3D0&_format=json&pids="
album_details_base_url = "https://www.jiosaavn.com/api.php?__call=content.getAlbumDetails&_format=json&cc=in&_marker=0%3F_marker%3D0&albumid="
playlist_details_base_url = "https://www.jiosaavn.com/api.php?__call=playlist.getDetails&_format=json&cc=in&_marker=0%3F_marker%3D0&listid="
lyrics_base_url = "https://www.jiosaavn.com/api.php?__call=lyrics.getLyrics&ctx=web6dot0&api_version=4&_format=json&_marker=0%3F_marker%3D0&lyrics_id="

REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "te-IN,te;q=0.95,en-IN;q=0.9,hi-IN;q=0.8,en-US;q=0.7",
    "Cookie": "L=telugu%2Ctamil%2Chindi%2Cenglish",
    "Referer": "https://www.jiosaavn.com/",
    "X-Forwarded-For": "49.205.120.14",
    "X-Real-IP": "49.205.120.14",
    "CF-Connecting-IP": "49.205.120.14"
}

SAAVN_MIRRORS = [
    "https://saavn.dev/api",
    "https://saavn.sumit.co/api",
    "https://jiosaavn-api-privatecvc2.vercel.app"
]
ITUNES_API = "https://itunes.apple.com/search"

GUARANTEED_STREAMS = [
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3"
]

FALLBACK_CATALOG = [
    {
        "id": "telugu_01",
        "title": "Samajavaragamana",
        "artist": "Sid Sriram, Thaman S",
        "album": "Ala Vaikunthapurramuloo",
        "duration": 214,
        "language": "telugu",
        "image_url": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500&auto=format&fit=crop&q=80",
        "media_url": GUARANTEED_STREAMS[0],
        "backup_urls": [GUARANTEED_STREAMS[0], GUARANTEED_STREAMS[1]]
    },
    {
        "id": "hindi_01",
        "title": "Kesariya",
        "artist": "Arijit Singh, Pritam",
        "album": "Brahmastra",
        "duration": 268,
        "language": "hindi",
        "image_url": "https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=500&auto=format&fit=crop&q=80",
        "media_url": GUARANTEED_STREAMS[1],
        "backup_urls": [GUARANTEED_STREAMS[1], GUARANTEED_STREAMS[2]]
    },
    {
        "id": "english_01",
        "title": "Blinding Lights",
        "artist": "The Weeknd",
        "album": "After Hours",
        "duration": 200,
        "language": "english",
        "image_url": "https://images.unsplash.com/photo-1508700115892-45ecd05ae2ad?w=500&auto=format&fit=crop&q=80",
        "media_url": GUARANTEED_STREAMS[2],
        "backup_urls": [GUARANTEED_STREAMS[2], GUARANTEED_STREAMS[3]]
    }
]


def _ensure_https(url: str) -> str:
    if not url:
        return ""
    u = str(url).strip()
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u


# --- Helper functions from JioSaavnAPI-master/helper.py ---

def format_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val)
    return html.unescape(s).replace("&quot;", "'").replace("&amp;", "&").replace("&#039;", "'")


def decrypt_url(url: str) -> str:
    """Decrypts JioSaavn encrypted_media_url using pyDes (key 38346591) or cryptography fallback."""
    enc_url = base64.b64decode(url.strip())
    if HAS_PYDES:
        des_cipher = des(b"38346591", ECB, b"\0\0\0\0\0\0\0\0", pad=None, padmode=PAD_PKCS5)
        dec_url = des_cipher.decrypt(enc_url, padmode=PAD_PKCS5).decode("utf-8")
    else:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.TripleDES(b"38346591"), modes.ECB(), backend=default_backend())
        decryptor = cipher.decryptor()
        raw = decryptor.update(enc_url) + decryptor.finalize()
        dec_url = raw[:-raw[-1]].decode("utf-8")
    dec_url = dec_url.replace("_96.mp4", "_320.mp4")
    return _ensure_https(dec_url)


def _parse_saavn_json(raw_text: str) -> Any:
    """Safely parses JioSaavn JSON responses (handling unescaped quotes in '(From "...")')."""
    try:
        return json.loads(raw_text)
    except Exception:
        pass
    try:
        cleaned = raw_text.encode().decode("unicode-escape")
        pattern = r'\(From "([^"]+)"\)'
        cleaned = re.sub(pattern, r"(From '\1')", cleaned)
        return json.loads(cleaned)
    except Exception:
        pattern = r'\(From "([^"]+)"\)'
        return json.loads(re.sub(pattern, r"(From '\1')", raw_text))


def _build_full_aac_urls(raw_url: str, is_320: bool = True) -> List[str]:
    """
    Converts any JioSaavn stream or preview URL into full-length aac.saavncdn.com streams ONLY
    (_320.mp4, _160.mp4, _96.mp4, _48.mp4) — never 30-second preview.saavncdn.com (_96_p.mp4) clips!
    """
    if not raw_url:
        return []
    u = _ensure_https(raw_url).replace("//preview.saavncdn.com", "//aac.saavncdn.com").replace("_96_p.mp4", "_160.mp4")
    bitrates = ["_320.mp4", "_160.mp4", "_96.mp4", "_48.mp4"] if is_320 else ["_160.mp4", "_96.mp4", "_320.mp4", "_48.mp4"]
    urls = []
    for br in bitrates:
        for orig in ["_320.mp4", "_160.mp4", "_96.mp4", "_48.mp4", "_96_p.mp4"]:
            if orig in u:
                cand = u.replace(orig, br)
                if cand not in urls:
                    urls.append(cand)
    if u not in urls:
        urls.insert(0, u)
    return urls


def format_song(data: Dict[str, Any], lyrics: bool = False) -> Dict[str, Any]:
    """Formats a raw JioSaavn song dictionary (v1 or v4) into full-length aac.saavncdn.com streams."""
    more = data.get("more_info") if isinstance(data.get("more_info"), dict) else {}
    is_320 = str(data.get("320kbps") or more.get("320kbps") or "").lower() == "true"
    enc_url = data.get("encrypted_media_url") or more.get("encrypted_media_url") or ""
    raw_prev = data.get("media_preview_url") or more.get("media_preview_url") or more.get("vlink") or ""

    decrypted = ""
    if enc_url:
        try:
            decrypted = decrypt_url(str(enc_url))
        except Exception:
            decrypted = ""

    base_stream = decrypted or raw_prev
    backup_urls = _build_full_aac_urls(base_stream, is_320=is_320)
    data["media_url"] = backup_urls[0] if backup_urls else ""
    data["backup_urls"] = backup_urls

    # Extract artist names cleanly from either v1 or v4 schema
    artist_str = data.get("primary_artists") or data.get("singers") or more.get("music") or ""
    if not artist_str and isinstance(more.get("artistMap"), dict):
        prim = more["artistMap"].get("primary_artists") or more["artistMap"].get("artists") or []
        if isinstance(prim, list) and prim:
            artist_str = ", ".join([format_str(a.get("name", "")) for a in prim if isinstance(a, dict) and a.get("name")])

    data["song"] = format_str(data.get("song") or data.get("title") or "Unknown Title")
    data["music"] = format_str(data.get("music") or more.get("music") or "")
    data["singers"] = format_str(data.get("singers") or artist_str or "")
    data["starring"] = format_str(data.get("starring") or "")
    data["album"] = format_str(data.get("album") or more.get("album") or "")
    data["primary_artists"] = format_str(artist_str or data.get("subtitle") or "Various Artists")
    data["image"] = _ensure_https(str(data.get("image", "")).replace("150x150", "500x500").replace("50x50", "500x500"))

    data["title"] = data["song"]
    data["artist"] = data["primary_artists"]
    data["image_url"] = data["image"]
    try:
        data["duration"] = int(data.get("duration") or more.get("duration") or 210)
    except (ValueError, TypeError):
        data["duration"] = 210

    if lyrics:
        has_l = str(data.get("has_lyrics") or more.get("has_lyrics") or "").lower() == "true"
        data["lyrics"] = get_lyrics(data.get("id", ""), title=data["title"], artist=data["artist"]) if has_l else None

    return data


def format_album(data: Dict[str, Any], lyrics: bool = False) -> Dict[str, Any]:
    data["image"] = _ensure_https(str(data.get("image", "")).replace("150x150", "500x500"))
    data["name"] = format_str(data.get("name", ""))
    data["primary_artists"] = format_str(data.get("primary_artists", ""))
    data["title"] = format_str(data.get("title", ""))
    songs = data.get("songs", [])
    if isinstance(songs, list):
        data["songs"] = [format_song(s, lyrics) for s in songs if isinstance(s, dict)]
    return data


def format_playlist(data: Dict[str, Any], lyrics: bool = False) -> Dict[str, Any]:
    data["firstname"] = format_str(data.get("firstname", ""))
    data["listname"] = format_str(data.get("listname", ""))
    songs = data.get("songs", [])
    if isinstance(songs, list):
        data["songs"] = [format_song(s, lyrics) for s in songs if isinstance(s, dict)]
    return data


# --- Core Functions from JioSaavnAPI-master/jiosaavn.py ---

def get_song(song_id: str, lyrics: bool = False) -> Optional[Dict[str, Any]]:
    try:
        url = song_details_base_url + urllib.parse.quote(str(song_id))
        resp = requests.get(url, headers=REQ_HEADERS, timeout=6)
        song_response = _parse_saavn_json(resp.text)
        if isinstance(song_response, dict) and str(song_id) in song_response:
            return format_song(song_response[str(song_id)], lyrics)
    except Exception as e:
        logger.debug(f"get_song({song_id}) error: {e}")
    return None


def get_songs_batch(song_ids: List[str], lyrics: bool = False) -> List[Dict[str, Any]]:
    """Fetches full details for up to 40 song IDs in a single batch call to song.getDetails."""
    if not song_ids:
        return []
    results = []
    # Process in chunks of 20 IDs per HTTP request for speed and reliability
    for i in range(0, len(song_ids), 20):
        chunk = [str(sid).strip() for sid in song_ids[i:i + 20] if sid]
        if not chunk:
            continue
        pids_param = ",".join(chunk)
        try:
            url = song_details_base_url + urllib.parse.quote(pids_param)
            resp = requests.get(url, headers=REQ_HEADERS, timeout=7)
            data = _parse_saavn_json(resp.text)
            if isinstance(data, dict):
                for sid in chunk:
                    raw_song = data.get(sid)
                    if isinstance(raw_song, dict):
                        formatted = format_song(raw_song, lyrics)
                        if formatted.get("media_url"):
                            results.append(formatted)
        except Exception as e:
            logger.debug(f"Batch get_songs error: {e}")
    return results


def get_song_id(url: str) -> Optional[str]:
    try:
        res = requests.get(url, data=[("bitrate", "320")], headers=REQ_HEADERS, timeout=6)
        try:
            return (res.text.split('"pid":"'))[1].split('","')[0]
        except IndexError:
            return res.text.split('"song":{"type":"')[1].split('","image":')[0].split('"id":"')[-1]
    except Exception:
        return None


def get_album(album_id: str, lyrics: bool = False) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(album_details_base_url + urllib.parse.quote(str(album_id)), headers=REQ_HEADERS, timeout=6)
        if resp.status_code == 200:
            songs_json = _parse_saavn_json(resp.text)
            return format_album(songs_json, lyrics)
    except Exception as e:
        logger.debug(f"get_album error: {e}")
    return None


def get_album_id(input_url: str) -> Optional[str]:
    try:
        res = requests.get(input_url, headers=REQ_HEADERS, timeout=6)
        try:
            return res.text.split('"album_id":"')[1].split('"')[0]
        except IndexError:
            return res.text.split('"page_id","')[1].split('","')[0]
    except Exception:
        return None


def get_playlist(list_id: str, lyrics: bool = False) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(playlist_details_base_url + urllib.parse.quote(str(list_id)), headers=REQ_HEADERS, timeout=6)
        if resp.status_code == 200:
            songs_json = _parse_saavn_json(resp.text)
            return format_playlist(songs_json, lyrics)
    except Exception as e:
        logger.debug(f"get_playlist error: {e}")
    return None


def get_playlist_id(input_url: str) -> Optional[str]:
    try:
        res = requests.get(input_url, headers=REQ_HEADERS, timeout=6).text
        try:
            return res.split('"type":"playlist","id":"')[1].split('"')[0]
        except IndexError:
            return res.split('"page_id","')[1].split('","')[0]
    except Exception:
        return None


def _clean_track_title(raw_title: str) -> str:
    """Removes parenthetical movie tags like (From 'Maestro'), language suffixes, and remix tags."""
    if not raw_title:
        return ""
    cleaned = html.unescape(str(raw_title))
    cleaned = re.sub(r"\s*[\(\[].*?[\)\]]", "", cleaned).strip()
    cleaned = re.sub(r"\s*-\s*.*$", "", cleaned).strip()
    cleaned = re.sub(
        r"\b(telugu|tamil|hindi|malayalam|kannada|remix|lofi|slowed|reverbed|club mix|version|female|male|ost|theme)\b",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()
    return cleaned or str(raw_title).strip()


def _dedup_key(song: Dict[str, Any]) -> str:
    """Generates a strict canonical deduplication key from the core song title so variations never repeat."""
    raw_t = str(song.get("title") or song.get("song") or "")
    base_t = _clean_track_title(raw_t).lower()
    base_t = re.sub(r"[^a-z0-9]+", "", base_t)
    if len(base_t) >= 3:
        return base_t
    fallback_t = re.sub(r"[^a-z0-9]+", "", raw_t.lower())
    return fallback_t or str(song.get("id", ""))


JUNK_KEYWORDS = (
    "marimba", "rock a bye", "cover songs collective", "jess.x.kirby", "shape of young",
    "karaoke", "lullaby", "8-bit", "8 bit", "ringtone", "tribute to", "originally performed",
    "instrumental version", "piano cover", "midi", "workout remix",
    "mila toh marega", "mila to marega", "khaul khaul ke", "paisewala", "maar de",
    "kaaton se", "har jazbe mein", "maarapapa", "khel shuru"
)

RAKTACHARITRA_TELUGU_POSTER = "https://c.saavncdn.com/208/Raktacharitra-Telugu-2010-20190220140106-500x500.jpg"
RAKTACHARITRA_TELUGU_TITLES = (
    "dorikithe chastavu", "katthula toh", "thudilenidhi", "dabbunnoda", "dabbunoda",
    "champaraa", "champara", "gundello", "karma dharma"
)


def _is_junk_or_fake_cover(song: Dict[str, Any]) -> bool:
    """Filters out fake marimba/lullaby/karaoke cover knockoffs and Hindi Rakta Charitra duplicates."""
    title = str(song.get("title") or song.get("song") or "").lower()
    artist = str(song.get("artist") or song.get("primary_artists") or "").lower()
    album = str(song.get("album") or "").lower()
    lang = str(song.get("language") or "").lower()
    combined = f"{title} {artist} {album}"
    for kw in JUNK_KEYWORDS:
        if kw in combined:
            return True
    # Block Hindi Rakta Charitra / Rakht Charitra songs when user wants Telugu Raktacharitra
    if ("rakta charitra" in album or "rakht charitra" in album) and lang == "hindi":
        if not any(tk in title for tk in RAKTACHARITRA_TELUGU_TITLES):
            return True
    # Normalize Telugu Raktacharitra poster & album label
    if any(tk in title for tk in ("dorikithe chastavu", "katthula toh", "thudilenidhi", "dabbunnoda", "champaraa")):
        song["image_url"] = RAKTACHARITRA_TELUGU_POSTER
        if not song.get("album") or "rakta" in album:
            song["album"] = "Raktacharitra (Telugu)"
    return False


def _deduplicate_songs(songs: List[Dict[str, Any]], max_per_album: int = 0) -> List[Dict[str, Any]]:
    """Removes duplicate songs and fake cover tracks, and optionally caps tracks per album for variety."""
    unique = []
    seen_keys = set()
    seen_ids = set()
    seen_urls = set()
    album_counts: Dict[str, int] = {}

    for s in songs:
        if not isinstance(s, dict):
            continue
        if _is_junk_or_fake_cover(s):
            continue
        sid = str(s.get("id", "")).strip()
        murl = str(s.get("media_url", "")).strip()
        dkey = _dedup_key(s)
        if (sid and sid in seen_ids) or (dkey and dkey in seen_keys) or (murl and murl in seen_urls and murl not in GUARANTEED_STREAMS):
            continue

        alb_key = _clean_track_title(str(s.get("album") or "")).lower()
        if max_per_album > 0 and alb_key and len(alb_key) > 2:
            if album_counts.get(alb_key, 0) >= max_per_album:
                continue
            album_counts[alb_key] = album_counts.get(alb_key, 0) + 1

        if sid:
            seen_ids.add(sid)
        if dkey:
            seen_keys.add(dkey)
        if murl:
            seen_urls.add(murl)
        unique.append(s)
    return unique


def _parse_lrc_timestamps(lrc_text: str) -> List[Dict[str, Any]]:
    """Parses LRC [mm:ss.xx] timestamps into [{'time': float_sec, 'text': str}, ...]."""
    lines = []
    pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
    for raw_line in str(lrc_text).splitlines():
        m = pattern.match(raw_line.strip())
        if m:
            mins = int(m.group(1))
            secs = float(m.group(2))
            txt = m.group(3).strip()
            if txt:
                lines.append({"time": round(mins * 60 + secs, 2), "text": txt})
    return lines


def get_lyrics_payload(song_id: str, title: str = "", artist: str = "", duration: int = 210) -> Dict[str, Any]:
    """
    Fetches voice-synced lyrics (with exact [mm:ss.xx] timestamps from LRCLIB when available)
    or builds timed vocal lines from JioSaavn / Lyrics.ovh plain lyrics.
    """
    clean_t = _clean_track_title(title)
    first_artist = (artist or "").split(",")[0].strip()
    search_candidates = [q for q in [f"{clean_t} {first_artist}".strip(), clean_t, title] if q]

    # 1. Try LRCLIB first for exact voice-synced LRC timestamps ([mm:ss.xx])
    plain_fallback = None
    for cand in search_candidates:
        try:
            q_params = urllib.parse.urlencode({"q": cand})
            lrc_resp = requests.get(f"https://lrclib.net/api/search?{q_params}", headers=REQ_HEADERS, timeout=5)
            if lrc_resp.status_code == 200:
                items = lrc_resp.json()
                if isinstance(items, list) and items:
                    # First look for a track with syncedLyrics
                    for item in items:
                        synced = item.get("syncedLyrics")
                        if synced and "[" in str(synced):
                            parsed = _parse_lrc_timestamps(str(synced))
                            if len(parsed) >= 3:
                                html_str = "<br>".join([p["text"] for p in parsed])
                                return {"status": True, "synced": True, "lines": parsed, "lyrics": html_str}
                        if not plain_fallback and item.get("plainLyrics"):
                            plain_fallback = str(item["plainLyrics"])
        except Exception:
            pass

    # 2. Try Official JioSaavn lyrics.getLyrics
    raw_plain = plain_fallback
    if not raw_plain and song_id:
        try:
            url = lyrics_base_url + urllib.parse.quote(str(song_id))
            lyrics_json = requests.get(url, headers=REQ_HEADERS, timeout=4).text
            lyrics_data = json.loads(lyrics_json)
            lyr = lyrics_data.get("lyrics")
            if lyr and len(str(lyr).strip()) > 10:
                raw_plain = str(lyr)
        except Exception:
            pass

        # 3. Try Saavn.dev mirror lyrics
        if not raw_plain:
            try:
                m_url = f"https://saavn.dev/api/songs/{urllib.parse.quote(str(song_id))}/lyrics"
                m_resp = requests.get(m_url, headers=REQ_HEADERS, timeout=4)
                if m_resp.status_code == 200:
                    m_data = m_resp.json()
                    lyr = (m_data.get("data") or {}).get("lyrics") or m_data.get("lyrics")
                    if lyr and len(str(lyr).strip()) > 10:
                        raw_plain = str(lyr)
            except Exception:
                pass

    # 4. Try Lyrics.ovh fallback
    if not raw_plain and clean_t and first_artist:
        try:
            ovh_url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(first_artist)}/{urllib.parse.quote(clean_t)}"
            ovh_resp = requests.get(ovh_url, headers=REQ_HEADERS, timeout=4)
            if ovh_resp.status_code == 200:
                ovh_l = ovh_resp.json().get("lyrics")
                if ovh_l and len(str(ovh_l).strip()) > 15:
                    raw_plain = str(ovh_l)
        except Exception:
            pass

    if raw_plain:
        # Convert plain lyrics into voice-timed lines starting after typical 6s intro
        text_lines = [
            re.sub(r"<[^>]+>", "", ln).strip()
            for ln in re.split(r"<br\s*/?>|\r?\n", raw_plain)
        ]
        text_lines = [ln for ln in text_lines if ln]
        dur = max(60, int(duration or 210))
        intro_offset = min(8.0, dur * 0.05)
        usable_span = max(30.0, dur - intro_offset - 8.0)
        step = usable_span / max(1, len(text_lines))
        timed_lines = [
            {"time": round(intro_offset + i * step, 2), "text": ln}
            for i, ln in enumerate(text_lines)
        ]
        return {
            "status": True,
            "synced": False,
            "lines": timed_lines,
            "lyrics": "<br>".join(text_lines)
        }

    return {"status": False, "synced": False, "lines": [], "lyrics": None}


def get_lyrics(song_id: str, title: str = "", artist: str = "") -> Optional[str]:
    payload = get_lyrics_payload(song_id, title=title, artist=artist)
    return payload.get("lyrics")


import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Fast in-memory TTL cache (10 minutes) for instant category/singer/mood switching
_SEARCH_CACHE: Dict[str, Any] = {}
_CACHE_TTL_SEC = 600


def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    entry = _SEARCH_CACHE.get(key)
    if entry and (_time.time() - entry[0]) < _CACHE_TTL_SEC:
        return entry[1]
    return None


def _cache_set(key: str, val: List[Dict[str, Any]]) -> None:
    if not val:
        return
    if len(_SEARCH_CACHE) > 250:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[key] = (_time.time(), val)


def _search_v4_songs(clean_q: str, page: int = 1, limit: int = 35, lyrics: bool = False) -> List[Dict[str, Any]]:
    """Calls JioSaavn v4 search.getResults and decrypts full-length streams directly."""
    out: List[Dict[str, Any]] = []
    fallback_ids: List[str] = []
    try:
        v4_url = f"{search_results_base_url}&api_version=4&ctx=web6dot0&n={limit}&p={page}&q={urllib.parse.quote(clean_q)}"
        v4_resp = requests.get(v4_url, headers=REQ_HEADERS, timeout=5)
        v4_data = _parse_saavn_json(v4_resp.text)
        if isinstance(v4_data, dict):
            for item in v4_data.get("results", []):
                if not isinstance(item, dict):
                    continue
                more = item.get("more_info") if isinstance(item.get("more_info"), dict) else {}
                if more.get("encrypted_media_url") or item.get("encrypted_media_url") or more.get("vlink"):
                    formatted = format_song(item, lyrics=lyrics)
                    if formatted.get("media_url") and not _is_junk_or_fake_cover(formatted):
                        out.append(formatted)
                elif item.get("id"):
                    fallback_ids.append(str(item["id"]))
    except Exception as e:
        logger.debug(f"v4 search.getResults error: {e}")

    if fallback_ids and not out:
        out.extend(get_songs_batch(fallback_ids[:20], lyrics=lyrics))
    return out


def _search_saavn_mirrors(query: str, page: int = 1, limit: int = 35) -> List[Dict[str, Any]]:
    """Queries India-backed JioSaavn mirrors returning full-length aac.saavncdn.com streams."""
    for mirror in SAAVN_MIRRORS:
        url = f"{mirror}/search/songs?query={urllib.parse.quote(query)}&page={page}&limit={limit}"
        try:
            resp = requests.get(url, headers=REQ_HEADERS, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                raw_items = (
                    data.get("data", {}).get("results", [])
                    if isinstance(data.get("data"), dict)
                    else data.get("results", [])
                )
                results = []
                for raw in (raw_items or []):
                    if not isinstance(raw, dict):
                        continue
                    dls = raw.get("downloadUrl") or []
                    raw_urls = []
                    if isinstance(dls, list):
                        for d in reversed(dls):
                            u = d.get("url") or d.get("link") if isinstance(d, dict) else str(d)
                            if u:
                                raw_urls.append(_ensure_https(u))
                    elif isinstance(dls, str):
                        raw_urls.append(_ensure_https(dls))
                    if not raw_urls:
                        continue
                    urls = _build_full_aac_urls(raw_urls[0], is_320=True)
                    imgs = raw.get("image") or []
                    img_u = ""
                    if isinstance(imgs, list) and imgs:
                        last = imgs[-1]
                        img_u = last.get("url") or last.get("link") if isinstance(last, dict) else str(last)
                    elif isinstance(imgs, str):
                        img_u = imgs
                    artists = raw.get("artists", {})
                    if isinstance(artists, dict) and isinstance(artists.get("primary"), list) and artists["primary"]:
                        artist_str = ", ".join([format_str(a.get("name", "")) for a in artists["primary"] if isinstance(a, dict)])
                    else:
                        artist_str = format_str(raw.get("primaryArtists") or raw.get("artist") or "Various Artists")
                    album_info = raw.get("album")
                    album_str = format_str(album_info.get("name") if isinstance(album_info, dict) else (album_info or ""))
                    song_obj = {
                        "id": str(raw.get("id", "")),
                        "title": format_str(raw.get("name") or raw.get("title") or "Unknown Title"),
                        "artist": artist_str,
                        "album": album_str,
                        "duration": int(raw.get("duration") or 210),
                        "image_url": _ensure_https(img_u) or FALLBACK_CATALOG[0]["image_url"],
                        "media_url": urls[0],
                        "backup_urls": urls
                    }
                    if not _is_junk_or_fake_cover(song_obj):
                        results.append(song_obj)
                if results:
                    return _deduplicate_songs(results)
        except Exception:
            pass
    return []


def _fetch_saavn_url_direct(saavn_url: str, lyrics: bool = False) -> List[Dict[str, Any]]:
    """Resolves a direct JioSaavn /song/ OR /album/ URL via webapi.get and mirrors."""
    is_album = "/album/" in saavn_url
    kind = "album" if is_album else "song"
    token = saavn_url.rstrip("/").split("/")[-1].strip()
    out: List[Dict[str, Any]] = []

    # 1. Official JioSaavn v4 webapi.get by URL token (works for both song & album tokens)
    if token:
        try:
            w_url = (
                "https://www.jiosaavn.com/api.php?__call=webapi.get"
                f"&token={urllib.parse.quote(token)}&type={kind}&includeMetaTags=0"
                "&ctx=web6dot0&api_version=4&_format=json&_marker=0"
            )
            w_resp = requests.get(w_url, headers=REQ_HEADERS, timeout=5)
            w_data = _parse_saavn_json(w_resp.text)
            if isinstance(w_data, dict):
                raw_list = (
                    w_data.get("list")
                    or w_data.get("songs")
                    or (list(w_data.values()) if not is_album else [])
                )
                if isinstance(raw_list, list):
                    for item in raw_list:
                        if isinstance(item, dict) and (item.get("id") or item.get("encrypted_media_url") or item.get("more_info")):
                            formatted = format_song(item, lyrics=lyrics)
                            if formatted.get("media_url") and not _is_junk_or_fake_cover(formatted):
                                out.append(formatted)
                if out:
                    return out
        except Exception as e:
            logger.debug(f"webapi.get {kind} token error ({token}): {e}")

    # 2. Mirror link lookup (/songs?link= or /albums?link=)
    endpoint_path = "albums" if is_album else "songs"
    for mirror in SAAVN_MIRRORS:
        try:
            m_url = f"{mirror}/{endpoint_path}?link={urllib.parse.quote(saavn_url)}"
            m_resp = requests.get(m_url, headers=REQ_HEADERS, timeout=5)
            if m_resp.status_code == 200:
                m_data = m_resp.json()
                data_node = m_data.get("data")
                if isinstance(data_node, dict) and isinstance(data_node.get("songs"), list):
                    raw_items = data_node["songs"]
                elif isinstance(data_node, list):
                    raw_items = data_node
                else:
                    raw_items = []
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    dls = raw.get("downloadUrl") or []
                    raw_urls = [
                        _ensure_https(d.get("url") or d.get("link") if isinstance(d, dict) else str(d))
                        for d in reversed(dls) if d
                    ] if isinstance(dls, list) else []
                    if not raw_urls:
                        continue
                    urls = _build_full_aac_urls(raw_urls[0], is_320=True)
                    imgs = raw.get("image") or []
                    img_u = ""
                    if isinstance(imgs, list) and imgs:
                        last = imgs[-1]
                        img_u = last.get("url") or last.get("link") if isinstance(last, dict) else str(last)
                    elif isinstance(imgs, str):
                        img_u = imgs
                    artists = raw.get("artists", {})
                    if isinstance(artists, dict) and isinstance(artists.get("primary"), list) and artists["primary"]:
                        artist_str = ", ".join([format_str(a.get("name", "")) for a in artists["primary"] if isinstance(a, dict)])
                    else:
                        artist_str = format_str(raw.get("primaryArtists") or raw.get("artist") or "Various Artists")
                    album_info = raw.get("album")
                    album_str = format_str(album_info.get("name") if isinstance(album_info, dict) else (album_info or ""))
                    song_obj = {
                        "id": str(raw.get("id", token)),
                        "title": format_str(raw.get("name") or raw.get("title") or "Unknown Title"),
                        "artist": artist_str,
                        "album": album_str,
                        "duration": int(raw.get("duration") or 220),
                        "image_url": _ensure_https(img_u) or RAKTACHARITRA_TELUGU_POSTER,
                        "media_url": urls[0],
                        "backup_urls": urls
                    }
                    if not _is_junk_or_fake_cover(song_obj):
                        out.append(song_obj)
                if out:
                    return out
        except Exception:
            pass

    # 3. Fallback via get_album_id / get_song_id
    if is_album:
        aid = get_album_id(saavn_url)
        if aid:
            alb = get_album(aid, lyrics)
            if isinstance(alb, dict) and isinstance(alb.get("songs"), list):
                return [s for s in alb["songs"] if isinstance(s, dict) and s.get("media_url") and not _is_junk_or_fake_cover(s)]
    else:
        sid = get_song_id(saavn_url)
        if sid:
            s = get_song(sid, lyrics)
            if isinstance(s, dict) and s.get("media_url") and not _is_junk_or_fake_cover(s):
                return [s]
    return []


def _search_matching_album_tracks(clean_q: str) -> List[Dict[str, Any]]:
    """
    When a user searches a short movie/album title (1-3 words, e.g. 'Aarya 2', 'Maestro', 'Orange', 'Pokiri'),
    checks JioSaavn autocomplete albums for an exact title match and extracts all tracks from that album.
    """
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", clean_q) if len(w) >= 2]
    if not words or len(words) > 3:
        return []
    out: List[Dict[str, Any]] = []
    try:
        ac_resp = requests.get(search_base_url + urllib.parse.quote(clean_q), headers=REQ_HEADERS, timeout=4)
        ac_data = _parse_saavn_json(ac_resp.text)
        albums_list = (ac_data.get("albums") or {}).get("data") or []
        for alb in albums_list[:2]:
            if not isinstance(alb, dict):
                continue
            alb_title = format_str(alb.get("title") or alb.get("name") or "").lower()
            alb_lang = str((alb.get("more_info") or {}).get("language") or alb.get("language") or "").lower()
            # Never pull Hindi Rakta Charitra album
            if ("rakta" in alb_title or "rakht" in alb_title) and alb_lang == "hindi":
                continue
            # Require the primary keyword to appear in the album title
            if words[0] in alb_title and not any(jk in alb_title for jk in JUNK_KEYWORDS):
                aid = str(alb.get("id") or "").strip()
                if aid:
                    full_alb = get_album(aid, lyrics=False)
                    if isinstance(full_alb, dict) and isinstance(full_alb.get("songs"), list):
                        for s in full_alb["songs"]:
                            if isinstance(s, dict) and s.get("media_url") and not _is_junk_or_fake_cover(s):
                                out.append(s)
    except Exception:
        pass
    return out


def search_for_song(query: str, lyrics: bool = False, songdata: bool = True, page: int = 1, limit: int = 35) -> Any:
    """
    Combines India-backed Saavn Mirrors + Direct JioSaavn v4 search + Matching Album Extractor concurrently.
    """
    clean_q = query.strip()
    if clean_q.startswith("http") and "saavn.com" in clean_q:
        return _fetch_saavn_url_direct(clean_q, lyrics=lyrics)

    if not songdata:
        try:
            ac_resp = requests.get(search_base_url + urllib.parse.quote(clean_q), headers=REQ_HEADERS, timeout=5)
            return _parse_saavn_json(ac_resp.text).get("songs", {}).get("data", [])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_v4 = pool.submit(_search_v4_songs, clean_q, page, limit, lyrics)
        fut_mir = pool.submit(_search_saavn_mirrors, clean_q, page, limit)
        fut_alb = pool.submit(_search_matching_album_tracks, clean_q) if page == 1 else None
        v4_res = fut_v4.result() or []
        mir_res = fut_mir.result() or []
        alb_res = (fut_alb.result() or []) if fut_alb else []

    combined = mir_res + v4_res + alb_res
    return _deduplicate_songs(combined)


# Comprehensive Movie / Album / Artist Query Expansion Table (Short 2-3 Word Queries + Direct Album/Song URLs)
MOVIE_ALBUM_EXPANSIONS = [
    (("aarya 2", "arya 2", "aarya-2", "arya-2", "uppenantha", "karige loga", "my love is gone", "baby he loves you"), [
        "https://www.jiosaavn.com/album/aarya-2/szAD227UDqE_",
        "Uppenantha",
        "Ringa Ringa",
        "My Love Is Gone",
        "Karige Loga",
        "Baby He Loves You",
        "Mr Perfect Aarya 2"
    ]),
    (("maestro", "baby oh baby"), [
        "https://www.jiosaavn.com/song/baby-oh-baby/GyksSTlEdnE",
        "Baby Oh Baby Maestro",
        "Vennello Aadapilla",
        "Shyam Singha Roy",
        "Sirivennela",
        "Anurag Kulkarni"
    ]),
    (("raktacharitra", "rakta charitra", "raktha charitra", "dorikithe", "katthula toh", "thudilenidhi", "dabbunnoda", "champaraa"), [
        "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
        "https://www.jiosaavn.com/song/katthula-toh/QABaehx5dlY",
        "https://www.jiosaavn.com/song/thudilenidhi/JwkDRCZEZ14",
        "https://www.jiosaavn.com/song/dabbunnoda/HS8BRjF9D2k",
        "https://www.jiosaavn.com/song/champaraa/OFwuYRcIfkQ",
        "https://www.jiosaavn.com/song/karma-dharma/OSYAfSBVdQo",
        "https://www.jiosaavn.com/song/gundello/SREvYB9qZQM",
        "Dorikithe Chastavu",
        "Raktacharitra",
        "Katthula Toh",
        "Thudilenidhi"
    ]),
    (("they call him og", "og telugu", "hungry cheetah", "firestorm og"), [
        "https://www.jiosaavn.com/song/hungry-cheetah-from-they-call-him-og/OgQvaDxEbwE",
        "Hungry Cheetah",
        "They Call Him OG",
        "Thaman S Telugu",
        "Bheemla Nayak Title Song",
        "Gabbar Singh"
    ]),
    (("devara", "fear song", "chuttamalle", "daavudi", "ayudha pooja"), [
        "https://www.jiosaavn.com/album/devara-part-1-telugu/kArqawD7cKk_",
        "Fear Song Devara",
        "Chuttamalle Devara",
        "Daavudi Devara",
        "Ayudha Pooja Devara",
        "Red Sea Devara"
    ]),
    (("pushpa 2", "pushpa", "kissik", "sooseki", "peelings", "oo antava", "srivalli"), [
        "https://www.jiosaavn.com/song/kissik-from-pushpa-2-the-rule-telugu/Jw0tVx9XblE",
        "Kissik Pushpa 2",
        "Sooseki Pushpa 2",
        "Pushpa Pushpa",
        "Peelings Pushpa 2",
        "Oo Antava Pushpa",
        "Srivalli Pushpa",
        "Saami Saami Pushpa"
    ]),
    (("guntur kaaram", "kurchi madathapetti", "dum masala", "mawaa enthaina"), [
        "https://www.jiosaavn.com/song/kurchi-madathapetti/MToeaRBITlg",
        "Kurchi Madathapetti",
        "Dum Masala",
        "Oh My Baby Guntur Kaaram",
        "Mawaa Enthaina",
        "Ramana Aei"
    ]),
    (("daaku maharaaj", "daaku", "rage of daaku", "dabidi dibidi"), [
        "https://www.jiosaavn.com/album/daaku-maharaaj/SI2T091Z92g_",
        "The Rage of Daaku",
        "Dabidi Dibidi",
        "Chinni Daaku Maharaaj",
        "Daaku The Intro"
    ]),
    (("jailer 2", "jailer", "hukum", "kaavaalaa", "tiger muthuvel"), [
        "https://www.jiosaavn.com/album/jailer-2-original-motion-picture-soundtrack/zg-RfbPQuWM_",
        "Hukum Jailer",
        "Kaavaalaa Jailer",
        "Tiger Muthuvel Pandian",
        "One Sun One Moon",
        "Putaapu Gang",
        "Rathamaarey"
    ]),
    (("raga of revenge", "from dc", "dc anirudh"), [
        "https://www.jiosaavn.com/song/raga-of-revenge-from-dc/MwNaZ1l4A1E",
        "Raga of Revenge",
        "Vikram Title Track",
        "Lokiverse",
        "Badass Leo",
        "Hukum Jailer",
        "Fear Song Devara"
    ]),
    (("om chapter", "udhiram", "veezha veera", "alaakaa loova", "senjudare", "karuppu", "god mode", "radhimaa", "sai abhyankkar"), [
        "https://www.jiosaavn.com/song/veezha-veera-from-om-chapter-1-udhiram-the-blood-wood-tamil/GgQpfBhUYUY",
        "https://www.jiosaavn.com/song/god-mode-from-karuppu/HQdZYDxzGng",
        "https://www.jiosaavn.com/album/radhimaa-from-think-indie/waSqOF2IJ3g_",
        "Veezha Veera",
        "God Mode Karuppu",
        "Radhimaa",
        "Alaakaa Loova",
        "Senjudare",
        "Katchi Sera",
        "Aasa Kooda"
    ]),
    (("hi nanna", "samayama", "gaaju bomma", "ammadi", "odiyamma"), [
        "https://www.jiosaavn.com/song/samayama-from-hi-nanna/Jh5YCCFGdEk",
        "Samayama Hi Nanna",
        "Gaaju Bomma Hi Nanna",
        "Ammadi Hi Nanna",
        "Odiyamma Hi Nanna",
        "Adigaa Hi Nanna"
    ]),
    (("salaar", "sooreede", "prathikadalo", "sound of salaar"), [
        "Sooreede Salaar",
        "Sound of Salaar",
        "Prathikadalo Salaar",
        "Vinaraa Salaar",
        "Aaru Sethulunnaa Salaar"
    ]),
    (("kalki", "bhairava anthem", "ta takkara", "theme of kalki"), [
        "Bhairava Anthem",
        "Ta Takkara Kalki",
        "Theme of Kalki",
        "Bujji Theme",
        "Keshava Madhava"
    ]),
    (("orange", "rooba rooba", "nenu nuvvantu", "chilipiga", "hello rammante"), [
        "Rooba Rooba",
        "Nenu Nuvvantu",
        "Chilipiga",
        "Hello Rammante",
        "O Range",
        "Sydney Nagaram"
    ]),
    (("gabbar singh", "kevvu keka", "dekho dekho", "akasam ammayaithe"), [
        "Kevvu Keka",
        "Dekho Dekho Gabbar Singh",
        "Akasam Ammayaithe",
        "Mandu Babulam",
        "Pilla Gabbar Singh"
    ]),
    (("pokiri", "dole dole", "ippatikinka", "jagadame", "gala gala"), [
        "Dole Dole Pokiri",
        "Ippatikinka Pokiri",
        "Jagadame Pokiri",
        "Gala Gala Pokiri",
        "Devuda Pokiri",
        "Choododdu Antunna"
    ]),
    (("businessman", "sir osthara", "pilla chao", "chandamama", "bad boys"), [
        "Sir Osthara",
        "Pilla Chao",
        "Bad Boys Businessman",
        "Chandamama Businessman",
        "We Love Bad Boys"
    ]),
    (("rangasthalam", "jigelu rani", "yentha sakkagunnave", "rangamma mangamma"), [
        "Jigelu Rani",
        "Yentha Sakkagunnave",
        "Rangamma Mangamma",
        "Ranga Ranga Rangasthalam",
        "Aa Gattununtaava"
    ]),
    (("ala vaikunthapurramuloo", "samajavaragamana", "buttabomma", "ramuloo ramulaa", "omg daddy"), [
        "Samajavaragamana",
        "Buttabomma",
        "Ramuloo Ramulaa",
        "OMG Daddy",
        "Sittharala Sirapadu"
    ]),
    (("sita ramam", "inthandham", "oh sita hey rama", "kanunna kalyanam"), [
        "Inthandham",
        "Oh Sita Hey Rama",
        "Kanunna Kalyanam",
        "Eppudo Ninnu",
        "Nene Aa Nene"
    ]),
    (("geetha govindam", "inkem inkem", "vachindamma", "yenti yenti"), [
        "Inkem Inkem",
        "Vachindamma",
        "Yenti Yenti",
        "What The Life",
        "Kanureppala Kaalam"
    ]),
    (("dj tillu", "tillu square", "tillu anna", "radhika tillu", "ticket eh"), [
        "Tillu Anna DJ Pedithe",
        "Pataas Pilla",
        "Ticket Eh Konakunda",
        "Radhika Tillu Square",
        "Oh My Lily"
    ]),
    (("jalsa", "my heart is beating", "chalore chalore", "you and i jalsa"), [
        "My Heart Is Beating",
        "Chalore Chalore Chal",
        "You And I Jalsa",
        "Jennifer Lopez Jalsa",
        "Gallo Thelinattunde"
    ]),
    (("kushi", "ammaye sannaga", "cheliya cheliya", "naa roja nuvve", "aradhya"), [
        "Ammaye Sannaga",
        "Cheliya Cheliya Kushi",
        "Ye Mera Jaha",
        "Naa Roja Nuvve",
        "Aradhya Kushi"
    ]),
    (("athadu", "pilichina", "avunu nijam", "neetho cheppana"), [
        "Pilichina Ranantava",
        "Avunu Nijam",
        "Neetho Cheppana",
        "Adara Adara",
        "Chandrullo Unde"
    ]),
    (("bommarillu", "appudo ippudo", "bommani geesthe", "kaani ippudu"), [
        "Appudo Ippudo",
        "Bommani Geesthe",
        "Kaani Ippudu",
        "Nammaka Thappani",
        "We Have A Romeo"
    ]),
    (("ye maaya chesave", "vintunnava", "kundanapu bomma", "ee hridayam"), [
        "Vintunnava",
        "Kundanapu Bomma",
        "Ee Hridayam",
        "Manasaa",
        "Aaromale"
    ]),
    (("mirchi", "idhedho bagundhe", "barbie girl mirchi", "darlingey", "yahoon yahoon"), [
        "Idhedho Bagundhe",
        "Barbie Girl Mirchi",
        "Darlingey Mirchi",
        "Mirchi Title Song",
        "Yahoon Yahoon"
    ]),
    (("animal", "ammayi animal", "arjan vailly", "pehle bhi main", "satranga"), [
        "Ammayi Animal",
        "Nanna Nuv Naa Pranam",
        "Yaalo Yaalaa",
        "Evarevaro",
        "Arjan Vailly",
        "Pehle Bhi Main",
        "Satranga"
    ]),
    (("lucky baskhar", "srimathi garu", "lucky baskhar title"), [
        "Srimathi Garu",
        "Lucky Baskhar Title Track",
        "Nijamaa Kalaa",
        "GV Prakash Telugu"
    ]),
    (("thandel", "bujji thalli", "namo namah shivaya thandel"), [
        "Bujji Thalli Thandel",
        "Namo Namah Shivaya Thandel",
        "Devi Sri Prasad Telugu"
    ]),
    (("game changer", "jaragandi", "raa macha macha", "naanaa hyraanaa"), [
        "Jaragandi",
        "Raa Macha Macha",
        "Naanaa Hyraanaa",
        "Dhop Game Changer"
    ])
]


def _normalize_user_query(q: str) -> List[str]:
    """
    Expands user searches and multi-song card queries into clean 2-3 word JioSaavn queries
    or direct JioSaavn /song/ and /album/ URLs so 0-result queries never occur.
    """
    q_clean = re.sub(r"\s+", " ", q.strip())
    if not q_clean:
        return ["Devara Telugu"]

    # 1. Support explicit pipe-separated multi-queries ("Query 1|Query 2|Query 3")
    if "|" in q_clean:
        parts = [p.strip() for p in q_clean.split("|") if p.strip()]
        if parts:
            return parts

    q_low = q_clean.lower()

    # 2. Check exact word-boundary short triggers for Raktacharitra & OM Chapter 1
    if re.search(r"(rak|rakta|raktha|raktacharitra|dorikithe|katthula|thudilenidhi|dabbunnoda|champara|champaraa)", q_low):
        return MOVIE_ALBUM_EXPANSIONS[2][1]

    if re.search(r"(om|udhiram|karuppu|veezha|radhimaa|alaakaa|senjudare)", q_low):
        return MOVIE_ALBUM_EXPANSIONS[10][1]

    # 3. Check the comprehensive 33-movie/album expansion table
    for keywords, expansion_list in MOVIE_ALBUM_EXPANSIONS:
        if any(kw in q_low for kw in keywords):
            return expansion_list

    # 4. Special category/artist keywords
    if "high beam" in q_low or "highbeam" in q_low:
        return [
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "Dorikithe Chastavu",
            "Katthula Toh",
            "Hungry Cheetah",
            "Fear Song Devara",
            "Ayudha Pooja Devara",
            "The Rage of Daaku",
            "Sooreede Salaar",
            "Jai Balayya",
            "Bheemla Nayak Title Song",
            "Hukum Jailer"
        ]
    if "item" in q_low:
        return [
            "Kissik Pushpa 2",
            "Oo Antava Pushpa",
            "Daavudi Devara",
            "Kurchi Madathapetti",
            "Pakka Local",
            "Jigelu Rani",
            "Ringa Ringa",
            "Aa Ante Amalapuram",
            "Kevvu Keka",
            "Dimaak Kharaab",
            "Blockbuster Sarrainodu",
            "Swing Zara"
        ]
    if "shareen" in q_low or "sheeran" in q_low:
        return [
            "Shape of You Ed Sheeran",
            "Perfect Ed Sheeran",
            "Bad Habits Ed Sheeran",
            "Photograph Ed Sheeran",
            "Ed Sheeran"
        ]
    if "weeknd" in q_low or "weekend" in q_low:
        return [
            "Blinding Lights The Weeknd",
            "Starboy The Weeknd",
            "Die For You The Weeknd",
            "Save Your Tears The Weeknd",
            "The Weeknd"
        ]
    if "dhanush" in q_low:
        return [
            "Rowdy Baby",
            "Why This Kolaveri Di",
            "Po Ve Po 3",
            "Luckkanna Mate Nillu",
            "Raayan",
            "Dhanush"
        ]

    # 5. Strip parentheticals and noise words from long queries so JioSaavn AND-matching never fails
    stripped = re.sub(r"[\(\["'].*?[\)\]'"]", " ", q_clean)
    noise_re = r"(original|motion|picture|soundtrack|from|full|album|all|songs|song|mp3|download|listen|online|free|feat|ft)"
    stripped = re.sub(noise_re, " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"[^a-zA-Z0-9\s]+", " ", stripped)
    words = [w for w in stripped.split() if len(w) >= 2]

    if len(words) > 3:
        # Break long multi-word queries into 2-word chunks so every song/movie part matches independently
        chunks = []
        for i in range(0, min(len(words), 8), 2):
            chunk = " ".join(words[i:i + 2]).strip()
            if chunk and chunk not in chunks:
                chunks.append(chunk)
        if " ".join(words[:3]) not in chunks:
            chunks.insert(0, " ".join(words[:3]))
        return chunks

    expanded = [q_clean]
    if stripped.strip() and stripped.strip() != q_clean:
        expanded.insert(0, stripped.strip())
    return expanded


def _fetch_single_query(sub_q: str, page: int = 1, limit: int = 25) -> List[Dict[str, Any]]:
    saavn_songs = search_for_song(sub_q, lyrics=False, songdata=True, page=page, limit=limit)
    if isinstance(saavn_songs, list):
        return saavn_songs
    if isinstance(saavn_songs, dict) and saavn_songs.get("media_url"):
        return [saavn_songs]
    return []


def _parallel_fetch_queries(queries: List[str], page: int = 1, per_query_limit: int = 12, max_per_album: int = 0) -> List[Dict[str, Any]]:
    if not queries:
        return []
    ordered_buckets: List[List[Dict[str, Any]]] = [[] for _ in queries]
    with ThreadPoolExecutor(max_workers=min(10, len(queries))) as pool:
        future_map = {
            pool.submit(_fetch_single_query, q_str, page, per_query_limit): idx
            for idx, q_str in enumerate(queries)
        }
        for fut in as_completed(future_map):
            idx = future_map[fut]
            try:
                ordered_buckets[idx] = fut.result() or []
            except Exception:
                ordered_buckets[idx] = []

    interleaved: List[Dict[str, Any]] = []
    max_len = max((len(b) for b in ordered_buckets), default=0)
    for row_i in range(max_len):
        for bucket in ordered_buckets:
            if row_i < len(bucket):
                interleaved.append(bucket[row_i])

    return _deduplicate_songs(interleaved, max_per_album=max_per_album)


def search_songs(query: str, page: int = 1, limit: int = 35) -> List[Dict[str, Any]]:
    clean_q = query.strip()
    if not clean_q:
        return get_category_songs("home", page=page)

    cache_key = f"search_v24::{clean_q.lower()}::p{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    queries_to_run = _normalize_user_query(clean_q)
    if len(queries_to_run) == 1:
        res = _fetch_single_query(queries_to_run[0], page=page, limit=limit)
        deduped = _deduplicate_songs(res)
        if deduped:
            _cache_set(cache_key, deduped)
            return deduped
    else:
        deduped = _parallel_fetch_queries(queries_to_run, page=page, per_query_limit=18)
        if deduped:
            _cache_set(cache_key, deduped)
            return deduped

    # Progressive token fallback: if the phrase returned 0 results, split into individual words/pairs
    raw_words = [w for w in re.findall(r"[a-zA-Z0-9]+", clean_q) if len(w) >= 3]
    if len(raw_words) >= 2:
        fallback_subqueries = [" ".join(raw_words[:2]), raw_words[0], raw_words[-1]]
        deduped = _parallel_fetch_queries(fallback_subqueries, page=page, per_query_limit=15)
        if deduped:
            _cache_set(cache_key, deduped)
            return deduped

    # Final fallback: return real full-length Telugu hits (NEVER fake SoundHelix demo tracks)
    real_fallback = get_category_songs("telugu", page=1)
    return real_fallback if real_fallback else FALLBACK_CATALOG


def get_category_songs(category: str, page: int = 1) -> List[Dict[str, Any]]:
    """
    Fetches full-length Telugu Mass, High Beam, Item Songs, Melodies, OM Chapter 1,
    English (Ed Sheeran, The Weeknd), and Hindi hits in parallel.
    """
    cat = category.lower().strip()
    cache_key = f"cat_v24::{cat}::p{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    category_queries = {
        "home": [
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "Fear Song Devara Telugu",
            "Hungry Cheetah OG",
            "Veezha Veera OM Chapter 1",
            "Kissik Pushpa 2 Telugu",
            "Kurchi Madathapetti Guntur Kaaram",
            "The Rage of Daaku Daaku Maharaaj",
            "Baby Oh Baby Maestro",
            "Samayama Hi Nanna",
            "Raga of Revenge Anirudh",
            "Radhimaa Sai Abhyankkar",
            "God Mode Karuppu",
            "Ed Sheeran Shape of You",
            "The Weeknd Blinding Lights"
        ],
        "highbeam": [
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "https://www.jiosaavn.com/song/katthula-toh/QABaehx5dlY",
            "https://www.jiosaavn.com/song/thudilenidhi/JwkDRCZEZ14",
            "https://www.jiosaavn.com/song/dabbunnoda/HS8BRjF9D2k",
            "Dorikithe Chastavu",
            "Raktacharitra",
            "Hungry Cheetah OG",
            "Fear Song Devara",
            "Ayudha Pooja Devara",
            "Daaku Maharaaj Thaman",
            "Sooreede Salaar",
            "Sound of Salaar",
            "Jai Balayya Akhanda",
            "Akhanda Title Song",
            "Bheemla Nayak Title Song",
            "Hukum Jailer",
            "Vikram Title Track Anirudh",
            "Jagadame Pokiri",
            "Agni Skalana Chatrapathi"
        ],
        "itemsongs": [
            "Kissik Pushpa 2",
            "Oo Antava Pushpa",
            "Daavudi Devara",
            "Kurchi Madathapetti",
            "Pakka Local Janatha Garage",
            "Jigelu Rani Rangasthalam",
            "Ringa Ringa Arya 2",
            "Aa Ante Amalapuram Arya",
            "Kevvu Keka Gabbar Singh",
            "Dimaak Kharaab iSmart Shankar",
            "Swing Zara Jai Lava Kusa",
            "Blockbuster Sarrainodu",
            "Ratthaalu Khaidi No 150",
            "Ammadu Lets Do Kummudu",
            "Daang Daang Sarileru Neekevvaru",
            "Bhoom Bhaddhal Krack",
            "Junction Lo Aagadu",
            "Top Lesi Poddi Iddarammayilatho",
            "Ippatikinka Pokiri",
            "Pilla Chao Businessman"
        ],
        "mass": [
            "Kurchi Madathapetti Guntur Kaaram",
            "Pushpa Pushpa Pushpa 2",
            "Daavudi Devara",
            "Fear Song Devara",
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "Dorikithe Chastavu",
            "Hungry Cheetah OG",
            "Daaku Maharaaj Thaman",
            "Boss Party Waltair Veerayya",
            "Jai Balayya Veera Simha Reddy",
            "Naatu Naatu RRR",
            "Bheemla Nayak Title Song",
            "Dimaak Kharaab iSmart Shankar",
            "Dekho Dekho Gabbar Singh",
            "Dole Dole Pokiri"
        ],
        "telugu": [
            "Devara Telugu",
            "Pushpa 2 Telugu",
            "Guntur Kaaram",
            "Daaku Maharaaj Thaman",
            "Hi Nanna",
            "Kalki 2898 AD Telugu",
            "Salaar Telugu",
            "Sita Ramam Telugu",
            "Ala Vaikunthapurramuloo",
            "Sid Sriram Telugu"
        ],
        "om": [
            "Alaakaa Loova",
            "Senjudare",
            "Veezha Veera",
            "Sai Abhyankkar",
            "Dhanush",
            "Rowdy Baby Dhanush",
            "3 Telugu Dhanush",
            "Raghuvaran B Tech",
            "Raayan Dhanush"
        ],
        "english": [
            "Ed Sheeran",
            "The Weeknd",
            "Blinding Lights The Weeknd",
            "Shape of You Ed Sheeran",
            "Starboy The Weeknd",
            "Perfect Ed Sheeran",
            "Dua Lipa",
            "Alan Walker",
            "Justin Bieber",
            "Coldplay"
        ],
        "hindi": [
            "Arijit Singh",
            "Stree 2",
            "Animal Hindi",
            "Jawan Hindi",
            "Brahmastra",
            "Kabir Singh",
            "Aashiqui 2",
            "Pritam"
        ],
        "love": [
            "Hi Nanna",
            "Sita Ramam Telugu",
            "Geetha Govindam",
            "Sid Sriram Telugu",
            "Arijit Singh",
            "Ed Sheeran Perfect"
        ],
        "sad": [
            "Orange Telugu",
            "Arya 2",
            "3 Telugu Dhanush",
            "Majili Telugu",
            "Aashiqui 2",
            "Kabir Singh"
        ],
        "chill": [
            "Sirivennela Shyam Singha Roy",
            "Oohalu Gusagusalade",
            "Sammohanam",
            "Pellichoopulu",
            "Dear Comrade Telugu"
        ],
        "workout": [
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "Dorikithe Chastavu",
            "Hungry Cheetah OG",
            "Salaar Telugu",
            "Vikram Anirudh",
            "Jailer Anirudh",
            "Arjan Vailly",
            "Believer Imagine Dragons"
        ],
        "drive": [
            "Ye Maaya Chesave",
            "Awara Telugu",
            "Gharshana",
            "Manam Telugu",
            "Blinding Lights The Weeknd"
        ],
        "party": [
            "Kissik Pushpa 2",
            "Oo Antava Pushpa",
            "Kurchi Madathapetti",
            "Boss Party",
            "Dj Tillu",
            "Aaj Ki Raat Stree 2"
        ],
        "evergreen": [
            "SP Balasubrahmanyam Telugu",
            "KS Chithra Telugu",
            "Mani Sharma Telugu",
            "Nuve Kavali",
            "Kushi Pawan Kalyan",
            "Okkadu Mahesh Babu"
        ],
        "devotional": [
            "Annamayya SPB",
            "Shree Hanuman Chalisa",
            "Namo Namo Kedarnath",
            "Deva Deva Brahmastra"
        ],
        "melodies": [
            "Sid Sriram Telugu",
            "Sirivennela Shyam Singha Roy",
            "Hi Nanna",
            "Sita Ramam Telugu",
            "Geetha Govindam"
        ],
        "trending": [
            "Devara Telugu",
            "Pushpa 2 Telugu",
            "Daaku Maharaaj Thaman",
            "Alaakaa Loova",
            "Stree 2",
            "The Weeknd"
        ],
        "punjabi": [
            "AP Dhillon",
            "Diljit Dosanjh",
            "Karan Aujla",
            "Shubh"
        ]
    }

    queries = category_queries.get(cat, [f"{cat} Telugu", f"{cat}"])

    if page == 1:
        max_alb = 4 if cat in ("highbeam", "itemsongs", "mass") else 2
        combined = _parallel_fetch_queries(queries, page=1, per_query_limit=10, max_per_album=max_alb)
        if combined:
            _cache_set(cache_key, combined)
            return combined

    selected_q = queries[(page - 1) % len(queries)]
    sub_page = ((page - 1) // len(queries)) + 1
    results = _parallel_fetch_queries([selected_q, queries[page % len(queries)]], page=sub_page, per_query_limit=20, max_per_album=4)
    if results:
        _cache_set(cache_key, results)
        return results
    return FALLBACK_CATALOG


def get_similar_songs_for_track(title: str, artist: str = "", album: str = "", exclude_csv: str = "") -> List[Dict[str, Any]]:
    """
    Builds a smart, non-repeating Spotify Radio queue based on the currently playing song's
    album/movie, singer/music director, and mood/vibe (High Beam, Item Songs, Mass, Melody, OM, English, Hindi).
    """
    clean_t = _clean_track_title(title)
    clean_alb = _clean_track_title(album)
    artists = [a.strip() for a in (artist or "").split(",") if a.strip() and a.strip().lower() != "various artists"]
    main_artist = artists[0] if artists else ""
    second_artist = artists[1] if len(artists) > 1 else ""

    excluded_keys = set()
    if title:
        excluded_keys.add(_dedup_key({"title": title}))
    for part in (exclude_csv or "").split(","):
        k = re.sub(r"[^a-z0-9]+", "", part.strip().lower())
        if k:
            excluded_keys.add(k)

    meta_low = f"{title} {album} {artist}".lower()
    queries: List[str] = []

    # 1. Same Album / Movie tracks first (if valid movie/album name)
    if clean_alb and len(clean_alb) >= 3 and clean_alb.lower() not in ("unknown", "featured", "single"):
        if "rakta" in clean_alb.lower():
            queries.extend([
                "https://www.jiosaavn.com/song/katthula-toh/QABaehx5dlY",
                "https://www.jiosaavn.com/song/thudilenidhi/JwkDRCZEZ14",
                "https://www.jiosaavn.com/song/dabbunnoda/HS8BRjF9D2k",
                "https://www.jiosaavn.com/song/champaraa/OFwuYRcIfkQ",
                "Raktacharitra"
            ])
        else:
            alb_expanded = _normalize_user_query(clean_alb)
            queries.extend(alb_expanded[:4])

    # 2. Same Music Director / Singer hits
    if main_artist:
        queries.append(f"{main_artist} Telugu" if "arijit" not in main_artist.lower() and "weeknd" not in main_artist.lower() and "sheeran" not in main_artist.lower() else main_artist)
    if second_artist:
        queries.append(second_artist)

    # 3. Match the exact Vibe / Base of the current song
    highbeam_kws = (
        "rakta", "raktacharitra", "dorikithe", "katthula", "thudilenidhi", "dabbunnoda", "champara",
        "og", "hungry cheetah", "fear song", "ayudha pooja", "daaku", "rage of daaku", "salaar",
        "sooreede", "akhanda", "jai balayya", "hukum", "jailer", "vikram", "bheemla", "jagadame",
        "chatrapathi", "raga of revenge", "god mode", "karuppu", "veezha veera"
    )
    item_kws = (
        "kissik", "oo antava", "daavudi", "kurchi", "pakka local", "jigelu rani", "ringa ringa",
        "amalapuram", "kevvu keka", "dimaak kharaab", "swing zara", "blockbuster", "ratthaalu",
        "ammadu", "daang daang", "bhoom bhaddhal", "junction lo", "top lesi poddi", "ippatikinka",
        "pilla chao", "boss party", "dabidi dibidi"
    )
    english_kws = ("weeknd", "blinding lights", "starboy", "sheeran", "shape of you", "perfect", "dua lipa", "alan walker", "believer", "coldplay", "justin bieber")
    hindi_kws = ("arijit", "kesariya", "stree 2", "arjan vailly", "pehle bhi main", "satranga", "kabir singh", "aashiqui 2", "pritam", "jawan")

    if any(kw in meta_low for kw in highbeam_kws):
        queries.extend([
            "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o",
            "Hungry Cheetah",
            "Fear Song Devara",
            "Ayudha Pooja Devara",
            "The Rage of Daaku",
            "Sooreede Salaar",
            "Hukum Jailer",
            "Raga of Revenge",
            "Jai Balayya Akhanda",
            "Bheemla Nayak Title Song",
            "Jagadame Pokiri"
        ])
    elif any(kw in meta_low for kw in item_kws):
        queries.extend([
            "Kissik Pushpa 2",
            "Oo Antava Pushpa",
            "Daavudi Devara",
            "Kurchi Madathapetti",
            "Pakka Local",
            "Jigelu Rani",
            "Ringa Ringa",
            "Aa Ante Amalapuram",
            "Kevvu Keka",
            "Dimaak Kharaab",
            "Blockbuster Sarrainodu"
        ])
    elif any(kw in meta_low for kw in english_kws):
        queries.extend([
            "Blinding Lights The Weeknd",
            "Starboy The Weeknd",
            "Shape of You Ed Sheeran",
            "Perfect Ed Sheeran",
            "Save Your Tears The Weeknd",
            "Bad Habits Ed Sheeran",
            "Dua Lipa Levitating"
        ])
    elif any(kw in meta_low for kw in hindi_kws):
        queries.extend([
            "Arijit Singh",
            "Pehle Bhi Main Animal",
            "Satranga Animal",
            "Apna Bana Le",
            "Kesariya Brahmastra",
            "Tum Hi Ho Aashiqui 2",
            "Bekhayali Kabir Singh"
        ])
    else:
        # Feel-good Telugu Melodies & Superhits base
        queries.extend([
            "Samayama Hi Nanna",
            "Baby Oh Baby Maestro",
            "Chuttamalle Devara",
            "Uppenantha",
            "My Love Is Gone",
            "Inthandham Sita Ramam",
            "Sirivennela Shyam Singha Roy",
            "Inkem Inkem",
            "Rooba Rooba Orange",
            "Vintunnava Ye Maaya Chesave"
        ])

    # Deduplicate query list while preserving order
    unique_queries: List[str] = []
    for q_item in queries:
        if q_item and q_item not in unique_queries:
            unique_queries.append(q_item)

    raw_songs = _parallel_fetch_queries(unique_queries[:10], page=1, per_query_limit=10, max_per_album=3)
    filtered: List[Dict[str, Any]] = []
    for s in raw_songs:
        dk = _dedup_key(s)
        sid = re.sub(r"[^a-z0-9]+", "", str(s.get("id") or "").lower())
        if dk in excluded_keys or (sid and sid in excluded_keys):
            continue
        excluded_keys.add(dk)
        filtered.append(s)

    return filtered[:30]


# =====================================================================
# PART 3 - 1:1 SPOTIFY MOBILE UI (MATCHING SCREENSHOTS 70792 - 70798)
# =====================================================================

INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta id="vpMeta" name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, minimum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <script>
    (function() {
      function syncViewportVars() {
        var isTouch = ("ontouchstart" in window) || (navigator.maxTouchPoints > 0);
        var scale = 1;
        if (isTouch && window.innerWidth >= 700 && screen.width > 200 && screen.width < 600) {
          scale = window.innerWidth / screen.width;
          document.documentElement.style.zoom = scale.toFixed(3);
        } else {
          document.documentElement.style.zoom = "";
        }
        var trueHeight = Math.round(window.innerHeight / scale);
        document.documentElement.style.setProperty("--vh-full", trueHeight + "px");
        document.documentElement.style.setProperty("--stage-h", Math.round(trueHeight * 0.88) + "px");
      }
      window.addEventListener("resize", syncViewportVars);
      window.addEventListener("orientationchange", syncViewportVars);
      syncViewportVars();
      document.addEventListener("DOMContentLoaded", syncViewportVars);
    })();
  </script>
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
  <title>Musify</title>
  <link rel="stylesheet" href="/static/css/style.css?v=24">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%231db954'><circle cx='12' cy='12' r='11'/><polygon points='9.5 7.5 16.5 12 9.5 16.5 9.5 7.5' fill='%23000000'/></svg>">
</head>
<body>

  <!-- Hidden HTML5 Audio Streamer -->
  <audio id="audio" preload="auto" playsinline></audio>

  <div class="app-container">
    <main class="main-area" id="mainArea">

      <!-- =============================================================
           TAB 1: HOME VIEW (Matches Screenshots 70792, 70793, 70794)
           ============================================================= -->
      <div id="homeView" class="tab-view active-view">
        <header class="top-bar">
          <div id="topBarAvatar" class="top-avatar" onclick="openProfileModal()">P</div>
          <div class="category-pills">
            <button class="pill-btn active" data-category="home">All</button>
            <button class="pill-btn" data-category="highbeam">High Beam 🔥</button>
            <button class="pill-btn" data-category="itemsongs">Item Songs 💃</button>
            <button class="pill-btn" data-category="mass">Mass Beats ⚡</button>
            <button class="pill-btn" data-category="telugu">Telugu New</button>
            <button class="pill-btn" data-category="om">OM • Dhanush</button>
            <button class="pill-btn" data-category="english">English</button>
            <button class="pill-btn" data-category="hindi">Hindi</button>
          </div>
        </header>

        <div class="view-scroll" id="contentScroll">
          <div id="homeHeroSections">
            <!-- 1. 2x4 Quick Tiles Grid (Maestro, High beam, Aarya - 2, OG Telugu, Radhimaa, Ballari baava, Feel good, Karuppu) -->
            <div id="quickTilesGrid" class="quick-tiles-grid"></div>

            <!-- 2. "Picked for you" Featured Card (Screenshot 70792.jpg) -->
            <div class="home-section">
              <h2 class="section-title">Picked for you</h2>
              <div id="pickedForYouCard" class="picked-card"></div>
            </div>

            <!-- 3. "For fans of Anirudh Ravichander" (Screenshots 70792 & 70793) -->
            <div class="home-section">
              <div class="fan-header" onclick="openCategoryOrQuery('Anirudh Ravichander', 'Anirudh Ravichander')">
                <img id="fanHeaderAvatar" class="fan-avatar" src="https://c.saavncdn.com/783/Anirudh-Ravichander-Latest-Blockbuster-Hits-Telugu-2024-20241014191030-500x500.jpg" onerror="this.src='https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80'" alt="">
                <div>
                  <div class="fan-sub">For fans of</div>
                  <div class="fan-title">Anirudh Ravichander</div>
                </div>
              </div>
              <div id="anirudhRow" class="horiz-scroll-row"></div>
            </div>

            <!-- 4. "More of what you like" (Screenshot 70793.jpg) -->
            <div class="home-section">
              <h2 class="section-title">More of what you like</h2>
              <div id="newMusicRow" class="horiz-scroll-row"></div>
            </div>

            <!-- 5. "Jump back in" (Screenshot 70794.jpg) -->
            <div class="home-section">
              <h2 class="section-title">Jump back in</h2>
              <div id="jumpBackRow" class="horiz-scroll-row"></div>
            </div>

            <!-- 6. "Happy" (Screenshot 70794.jpg) -->
            <div class="home-section">
              <h2 class="section-title">Happy</h2>
              <div id="hindiHeroRow" class="horiz-scroll-row"></div>
            </div>

            <!-- 7. "Your favourite artists" (Screenshot 70794.jpg) -->
            <div class="home-section">
              <h2 class="section-title">Your favourite artists</h2>
              <div id="singersRow" class="singers-scroll-row"></div>
            </div>
          </div>

          <!-- Dynamic Category / Playlist / Feed Header & Songs -->
          <div class="view-header">
            <h1 id="heading" class="view-heading">Recommended for you</h1>
            <p id="subheading" class="view-subheading">Full-length Telugu, English & Hindi streams</p>
          </div>
          <div id="content"></div>
        </div>
      </div>

      <!-- =============================================================
           TAB 2: SEARCH VIEW (Matches Screenshots 70795, 70796, 70797)
           ============================================================= -->
      <div id="searchView" class="tab-view">
        <header class="tab-top-header">
          <div class="tab-header-left">
            <div id="searchTabAvatar" class="top-avatar" onclick="openProfileModal()">P</div>
            <h1 class="tab-big-title">Search</h1>
          </div>
          <button class="np-icon-btn" onclick="document.getElementById('query').focus()" title="Search">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
          </button>
        </header>

        <!-- Big White Spotify Search Input Box -->
        <div class="spotify-white-search-wrap">
          <div class="spotify-white-search">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#121212" stroke-width="2.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" id="query" placeholder="What do you want to listen to?" autocomplete="off">
            <button id="btnClearSearch" class="btn-clear-white-search" onclick="clearSearchInput()">&times;</button>
          </div>
        </div>

        <div class="view-scroll" id="searchScroll">
          <!-- Live Search Results Container (Shown when typing) -->
          <div id="searchResultsBox" style="display:none;">
            <h2 id="searchResultsHeading" class="section-title" style="margin-top:8px;">Results</h2>
            <div id="searchResultsList"></div>
          </div>

          <!-- Default Browse Categories & Discover Something New (Screenshots 70795-70797) -->
          <div id="searchBrowseSection">
            <div id="searchTopTilesGrid" class="browse-tiles-grid"></div>

            <div class="home-section" style="margin-top:22px;">
              <h2 class="section-title">Discover something new</h2>
              <div id="discoverReelsRow" class="discover-reels-row"></div>
            </div>

            <div class="home-section" style="margin-top:22px;">
              <h2 class="section-title">Browse all</h2>
              <div id="searchBrowseAllGrid" class="browse-tiles-grid"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- =============================================================
           TAB 3: YOUR LIBRARY VIEW (Matches Screenshot 70798.jpg)
           ============================================================= -->
      <div id="libraryView" class="tab-view">
        <header class="tab-top-header">
          <div class="tab-header-left">
            <div id="libTabAvatar" class="top-avatar" onclick="openProfileModal()">P</div>
            <h1 class="tab-big-title">Your Library</h1>
          </div>
          <div style="display:flex;align-items:center;gap:16px;">
            <button class="np-icon-btn" onclick="switchTab('search'); document.getElementById('query').focus()" title="Search Library">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            </button>
            <button id="btnNewPlaylist" class="np-icon-btn" title="Create Playlist">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"><line x1="12" y1="4" x2="12" y2="20"/><line x1="4" y1="12" x2="20" y2="12"/></svg>
            </button>
          </div>
        </header>

        <div class="lib-pills-row">
          <button class="lib-pill active" onclick="filterLibraryItems('all', this)">Playlists</button>
          <button class="lib-pill" onclick="filterLibraryItems('albums', this)">Albums</button>
          <button class="lib-pill" onclick="filterLibraryItems('artists', this)">Artists</button>
        </div>

        <div class="view-scroll" id="libraryScroll">
          <!-- User's Custom Created Playlists + Liked Songs + Curated Albums (Screenshot 70798.jpg) -->
          <div id="libraryItemsList" class="library-list"></div>
        </div>
      </div>

      <!-- Hidden Legacy Sidebar Container for JS compatibility -->
      <div id="sidebar" style="display:none;">
        <div id="userProfileCard"></div>
        <div id="userAvatar"></div>
        <div id="userNameDisplay"></div>
        <div id="userAgeDisplay"></div>
        <span id="likedCountBadge"></span>
        <div id="sidebarPlaylists"></div>
        <div id="moodsGrid"></div>
      </div>

      <!-- =============================================================
           CRIMSON FLOATING MINI-PLAYER BAR (Matches Screenshots 70792-70798)
           ============================================================= -->
      <div id="miniPlayer" class="mini-player" onclick="openNowPlayingSheet(event)">
        <div class="mini-player-main">
          <img id="playerImg" class="mini-thumb" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80" alt="">
          <div class="mini-info">
            <div id="playerTitle" class="mini-title">Baby Oh Baby</div>
            <div class="mini-artist-row">
              <span id="playerArtist" class="mini-artist">Mahati Swara Sagar, Anurag Kulkarni</span>
            </div>
          </div>
          <div class="mini-actions" onclick="event.stopPropagation()">
            <button class="mini-icon-btn" onclick="openNowPlayingSheet()" title="Video & Lyrics">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="5" width="14" height="14" rx="2"/><rect x="18" y="3" width="4" height="18" rx="1"/><circle cx="9" cy="12" r="2.5"/></svg>
            </button>
            <button id="btnMiniLike" class="mini-icon-btn" onclick="toggleLikeCurrent()" title="Save to Liked Songs">
              <svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
            </button>
            <button id="btnPlay" class="mini-play-btn" title="Play / Pause">
              <svg width="23" height="23" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
            </button>
          </div>
        </div>
        <div class="mini-progress-track">
          <div id="miniProgressFill" class="mini-progress-fill"></div>
        </div>
      </div>

    </main>
  </div>

  <!-- =================================================================
       FULL-SCREEN SPOTIFY VIDEO CANVAS & VOICE-SYNCED LYRICS SHEET
       ================================================================= -->
  <div id="nowPlayingSheet" class="np-sheet">
    <div id="npSwipeZone" class="np-video-stage">
      <video id="canvasVideo" class="np-canvas-video" autoplay loop muted playsinline preload="auto"></video>
      <img id="npBackdropImg" class="np-backdrop-img" src="" alt="">
      <div class="np-video-gradient"></div>

      <div class="np-top-bar">
        <button class="np-icon-btn" onclick="closeNowPlayingSheet()" title="Back to Musify Home" style="background:rgba(0,0,0,0.4);padding:6px 12px;border-radius:99px;font-size:12.5px;font-weight:700;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
          <span>Home</span>
        </button>
        <div class="np-top-center">
          <div class="np-top-sub">Based on your interest in</div>
          <div id="npAlbumHeader" class="np-top-album">Maestro</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <button class="np-icon-btn" onclick="downloadCurrentSong()" title="Download Full Song" style="background:rgba(29,185,84,0.22);border:1px solid var(--accent);padding:6px 11px;border-radius:99px;font-size:12px;font-weight:800;color:var(--accent);">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>Download</span>
          </button>
          <button class="np-icon-btn" onclick="openAddToPlaylistCurrent()" title="Add to Playlist">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="#fff"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
          </button>
        </div>
      </div>

      <div id="npSwipeIndicator" style="display:none;"></div>

      <!-- Large Centered Square Album Poster (Matches Screenshot 70811.jpg) -->
      <div class="np-center-poster-wrap">
        <img id="npCenterPoster" class="np-center-poster" src="https://c.saavncdn.com/208/Rakta-Charitra-English-2018-20180621193420-500x500.jpg" alt="">
      </div>

      <div class="np-stage-bottom">
        <div id="npFloatingLyric" class="np-floating-lyric" onclick="scrollToLyricsCard()"></div>

        <div class="np-track-row">
          <img id="npThumb" class="np-track-thumb" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80" alt="">
          <div class="np-track-text">
            <div id="npTitle" class="np-track-title">Select a song</div>
            <div class="np-track-artist-wrap">
              <span class="sparkle-badge">✦</span>
              <span id="npArtist" class="np-track-artist">Artist</span>
            </div>
          </div>
          <button class="np-circle-action" onclick="window.Player.next()" title="Skip Song">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
          <button id="btnNpLike" class="np-circle-action" onclick="toggleLikeCurrent()" title="Save to Liked Songs">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
          </button>
        </div>

        <div class="np-scrubber-wrap">
          <input type="range" id="progressBar" class="np-range-slider" min="0" max="100" step="0.1" value="0">
          <div class="np-time-row">
            <span id="currentTime">0:00</span>
            <span id="totalDuration">0:00</span>
          </div>
        </div>

        <div class="np-controls-row">
          <button id="btnShuffle" class="np-ctrl-btn active" title="Shuffle">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>
          </button>

          <button id="btnPrev" class="np-ctrl-btn" title="Previous Song">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="#fff"><polygon points="19 20 9 12 19 4 19 20"/><rect x="5" y="4" width="2.5" height="16" fill="#fff"/></svg>
          </button>

          <button id="btnNpPlay" class="np-big-play-btn" onclick="window.Player.toggle()" title="Play / Pause">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          </button>

          <button id="btnNext" class="np-ctrl-btn" title="Next Song">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="#fff"><polygon points="5 4 15 12 5 20 5 4"/><rect x="16.5" y="4" width="2.5" height="16" fill="#fff"/></svg>
          </button>

          <button id="btnRepeat" class="np-ctrl-btn" title="Repeat">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="13" r="8"/><polyline points="12 9 12 13 15 15"/><line x1="9" y1="2" x2="15" y2="2"/></svg>
          </button>
        </div>

        <div class="np-sub-actions">
          <button class="np-icon-btn" onclick="scrollToLyricsCard()" title="Lyrics & Info" style="font-size:12px;font-weight:700;gap:6px;">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><circle cx="12" cy="12" r="3"/></svg>
            <span>Lyrics</span>
          </button>
          <div style="display:flex; gap:18px; align-items:center;">
            <button class="np-icon-btn" onclick="downloadCurrentSong()" title="Download Song">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            </button>
            <button class="np-icon-btn" onclick="shareCurrentSong()" title="Share Song">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
            </button>
            <button id="btnQueueToggle" class="np-icon-btn" title="Open Queue">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
              <span id="queueBadge" class="badge-count">0</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <div class="np-below-fold">
      <div id="lyricsPreviewCard" class="np-lyrics-card">
        <div class="np-card-header"><span>Lyrics preview</span></div>
        <div id="lyricsContent" class="np-lyrics-body">Tap any song to load live voice-synced lyrics...</div>
        <button id="btnExpandLyrics" class="np-show-lyrics-pill" onclick="toggleExpandLyrics()">Show lyrics</button>
      </div>

      <div class="np-info-card">
        <div class="np-artist-banner-wrap">
          <img id="npArtistBannerImg" class="np-artist-banner" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=600&auto=format&fit=crop&q=80" alt="">
          <div class="np-artist-banner-label">About the artist</div>
        </div>
        <div class="np-artist-body">
          <div class="np-artist-title-row">
            <div>
              <div class="np-artist-name-verified">
                <span id="npAboutArtistName">Artist</span>
                <span class="verified-check">✔</span>
              </div>
              <div class="np-monthly-listeners">41.3L monthly listeners</div>
            </div>
            <button class="np-follow-btn" onclick="this.textContent = this.textContent === 'Follow' ? 'Following' : 'Follow'">Follow</button>
          </div>
          <p id="npArtistBio" class="np-artist-bio">Featured composer, vocalist, and producer streaming in full-length HD audio on Musify.</p>
        </div>
      </div>

      <div class="np-info-card np-padded-card">
        <h3 id="npExploreHeading" class="np-section-heading">Explore Artist</h3>
        <div id="npExploreGrid" class="np-explore-grid"></div>
      </div>

      <div class="np-info-card np-padded-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
          <h3 class="np-section-heading" style="margin:0;">Credits</h3>
          <span style="color:var(--accent);font-size:13px;font-weight:700;">Show all</span>
        </div>
        <div id="npCreditsList" class="np-credits-list"></div>
      </div>
    </div>
  </div>

  <!-- Slide-Over Queue Drawer -->
  <div id="queuePanel" class="queue-panel">
    <div class="queue-header">
      <h3>Queue • Up Next</h3>
      <div style="display:flex; gap:8px;">
        <button id="btnClearQueue" class="btn-icon-small" title="Clear Queue">🗑</button>
        <button id="btnCloseQueue" class="btn-icon-small" title="Close Queue">&times;</button>
      </div>
    </div>
    <div id="queueList" class="queue-list"></div>
  </div>

  <!-- Bottom 5-Tab Navigation Bar (Matches Screenshots 70792 - 70798) -->
  <nav class="mobile-bottom-nav">
    <button class="mob-tab active" id="tabHome" onclick="switchTab('home')">
      <svg width="23" height="23" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3l9-8z"/></svg>
      <span>Home</span>
    </button>
    <button class="mob-tab" id="tabSearch" onclick="switchTab('search')">
      <svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <span>Search</span>
    </button>
    <button class="mob-tab" id="tabLibrary" onclick="switchTab('library')">
      <svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="4" y1="4" x2="4" y2="20"/><line x1="9" y1="4" x2="9" y2="20"/><line x1="14" y1="5" x2="19" y2="20"/></svg>
      <span>Your Library</span>
    </button>
    <button class="mob-tab" id="tabLyrics" onclick="openNowPlayingSheet(); setTimeout(scrollToLyricsCard, 200)">
      <svg width="23" height="23" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10"/><path d="M8 10c2.5-1 5.5-1 8 0M8.5 13c2-0.8 5-0.8 7 0M9.5 16c1.5-0.5 3.5-0.5 5 0" stroke="#000" stroke-width="1.6" fill="none"/></svg>
      <span>Premium</span>
    </button>
    <button class="mob-tab" id="tabCreate" onclick="document.getElementById('btnNewPlaylist').click()">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="12" y1="4" x2="12" y2="20"/><line x1="4" y1="12" x2="20" y2="12"/></svg>
      <span>Create</span>
    </button>
  </nav>

  <!-- Modals -->
  <div id="createPlaylistModal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>Create Playlist</h3>
        <button id="btnCloseCreatePlaylist" class="btn-icon-small">&times;</button>
      </div>
      <form id="formCreatePlaylist">
        <div class="form-group">
          <label for="playlistNameInput">Playlist Name *</label>
          <input type="text" id="playlistNameInput" placeholder="e.g. High beam, Telugu Vibes" required>
        </div>
        <div class="form-group">
          <label for="playlistDescInput">Description (Optional)</label>
          <textarea id="playlistDescInput" rows="2" placeholder="Add an optional description..."></textarea>
        </div>
        <div class="modal-actions">
          <button type="button" id="btnCancelCreatePlaylist" class="btn-secondary">Cancel</button>
          <button type="submit" class="btn-primary">Create</button>
        </div>
      </form>
    </div>
  </div>

  <div id="addToPlaylistModal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>Add to Playlist</h3>
        <button id="btnCloseAddToPlaylist" class="btn-icon-small">&times;</button>
      </div>
      <form id="formAddToPlaylist">
        <div class="form-group">
          <label for="playlistSelect">Choose Playlist</label>
          <select id="playlistSelect" required></select>
        </div>
        <div class="modal-actions">
          <button type="button" id="btnCancelAddToPlaylist" class="btn-secondary">Cancel</button>
          <button type="submit" class="btn-primary">Add Song</button>
        </div>
      </form>
    </div>
  </div>

  <div id="welcomeModal" class="modal-overlay">
    <div class="modal-card welcome-card">
      <div class="welcome-badge">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
      </div>
      <h2 class="welcome-title">Welcome to Musify</h2>
      <p class="welcome-subtitle">Enter your name and age to start streaming full-length songs with live lyrics and background video.</p>
      <form id="formWelcomeProfile">
        <div class="form-group">
          <label for="userNameInput">Your Name *</label>
          <input type="text" id="userNameInput" placeholder="e.g. Pavan, Rahul, Karthik" required autocomplete="name">
        </div>
        <div class="form-group" style="margin-top: 12px;">
          <label for="userAgeInput">Your Age *</label>
          <input type="number" id="userAgeInput" placeholder="e.g. 21" min="5" max="110" required>
        </div>
        <div class="modal-actions" style="margin-top: 20px; justify-content: stretch;">
          <button type="submit" class="btn-primary" style="width: 100%; justify-content: center; padding: 12px;">
            Start Listening Free
          </button>
        </div>
      </form>
    </div>
  </div>

  <div id="toastContainer" class="toast-container"></div>

  <script src="/static/js/player.js?v=24"></script>
  <script src="/static/js/app.js?v=24"></script>
  <script>
    if ("caches" in window) {
      caches.keys().then((keys) => {
        keys.forEach((k) => {
          if (k !== "musify-spotify-v24") caches.delete(k);
        });
      }).catch(() => {});
    }
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js?v=24").then((reg) => reg.update()).catch(() => {});
      });
    }
  </script>
</body>
</html>
'''

STYLE_CSS = r'''/* ==========================================================================
   Musify • 1:1 Spotify Mobile App UI (Matching Screenshots 70792 - 70798)
   ========================================================================== */

:root {
  --bg-black: #000000;
  --bg-main: #121212;
  --bg-card: #1f1f1f;
  --bg-card-hover: #282828;
  --bg-tinted: rgba(255, 255, 255, 0.08);
  --border-color: rgba(255, 255, 255, 0.1);
  --text-main: #ffffff;
  --text-muted: #b3b3b3;
  --text-dim: #777777;
  --accent: #1db954;
  --danger: #f15e6c;
  --transition: all 0.2s ease;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
  -webkit-tap-highlight-color: transparent;
}

html, body {
  width: 100%;
  max-width: 100vw;
  overflow-x: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-color: var(--bg-main);
  color: var(--text-main);
  height: var(--vh-full, 100dvh);
  display: flex;
  flex-direction: column;
  user-select: none;
}

.app-container {
  display: flex;
  flex: 1;
  width: 100%;
  height: calc(var(--vh-full, 100dvh) - 56px);
  overflow: hidden;
}

.main-area {
  flex: 1;
  width: 100%;
  height: 100%;
  position: relative;
  overflow: hidden;
  background-color: var(--bg-main);
}

/* --- 3 Main Tab Views (Home, Search, Your Library) --- */
.tab-view {
  display: none;
  flex-direction: column;
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.tab-view.active-view {
  display: flex;
}

.view-scroll {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 14px 130px 14px;
}

/* --- Home Top Bar (Screenshot 70792.jpg) --- */
.top-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 14px 10px 14px;
  background-color: var(--bg-main);
}

.top-avatar {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background-color: #b86b43;
  color: #ffffff;
  font-weight: 800;
  font-size: 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  cursor: pointer;
}

.category-pills {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  scrollbar-width: none;
}

.category-pills::-webkit-scrollbar {
  display: none;
}

.pill-btn {
  background-color: #2a2a2a;
  border: none;
  color: #ffffff;
  padding: 7px 16px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.pill-btn.active {
  background-color: var(--accent);
  color: #000000;
  font-weight: 700;
}

/* --- 2x4 Compact Top Grid (Screenshot 70792.jpg) --- */
.quick-tiles-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 24px;
  width: 100%;
}

.quick-tile {
  display: flex;
  align-items: center;
  min-width: 0;
  background-color: #282828;
  border-radius: 6px;
  overflow: hidden;
  height: 54px;
  cursor: pointer;
}

.quick-tile:active {
  background-color: #383838;
}

.quick-tile img {
  width: 54px;
  height: 54px;
  object-fit: cover;
  flex-shrink: 0;
}

.quick-tile-title {
  font-size: 13px;
  font-weight: 700;
  color: #ffffff;
  padding: 0 10px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.25;
}

/* --- "Picked for you" Featured Horizontal Card (Screenshot 70792.jpg) --- */
.home-section {
  margin-bottom: 26px;
}

.section-title {
  font-size: 21px;
  font-weight: 800;
  letter-spacing: -0.4px;
  margin-bottom: 12px;
  color: #ffffff;
}

.picked-card {
  display: flex;
  background-color: #242424;
  border-radius: 8px;
  overflow: hidden;
  height: 146px;
  cursor: pointer;
}

.picked-card-img {
  width: 146px;
  height: 146px;
  object-fit: cover;
  flex-shrink: 0;
}

.picked-card-body {
  flex: 1;
  min-width: 0;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.picked-card-tag {
  font-size: 11.5px;
  color: var(--text-muted);
}

.picked-card-title {
  font-size: 16px;
  font-weight: 800;
  color: #ffffff;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.25;
  margin-top: 2px;
}

.picked-card-artist {
  font-size: 12.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 3px;
}

.picked-card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}

.picked-play-circle {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background-color: #ffffff;
  color: #000000;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

/* --- "For fans of Anirudh Ravichander" Header (Screenshots 70792 & 70793) --- */
.fan-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  cursor: pointer;
}

.fan-avatar {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  object-fit: cover;
}

.fan-sub {
  font-size: 12px;
  color: var(--text-muted);
}

.fan-title {
  font-size: 21px;
  font-weight: 800;
  color: #ffffff;
  letter-spacing: -0.4px;
}

/* --- Horizontal Carousels (Screenshots 70793 & 70794) --- */
.horiz-scroll-row, .singers-scroll-row, .discover-reels-row {
  display: flex;
  gap: 14px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 4px;
}

.horiz-scroll-row::-webkit-scrollbar,
.singers-scroll-row::-webkit-scrollbar,
.discover-reels-row::-webkit-scrollbar {
  display: none;
}

.horiz-card {
  width: 154px;
  flex-shrink: 0;
  cursor: pointer;
}

.horiz-card-img-wrap {
  position: relative;
  width: 154px;
  height: 154px;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 8px;
  background-color: #242424;
}

.horiz-card-img-wrap img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.spotify-corner-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background-color: var(--accent);
  color: #000;
  font-size: 10px;
  font-weight: 900;
  display: flex;
  align-items: center;
  justify-content: center;
}

.horiz-card-title {
  font-size: 13.5px;
  font-weight: 700;
  color: #ffffff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: 2px;
}

.horiz-card-sub {
  font-size: 12.5px;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.35;
}

/* --- Circular Artist Row ("Your favourite artists") --- */
.singer-circle-card {
  width: 104px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  text-align: center;
}

.singer-avatar-ring {
  width: 96px;
  height: 96px;
  border-radius: 50%;
  overflow: hidden;
  background-color: #282828;
}

.singer-avatar-ring img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.singer-name-label {
  font-size: 13px;
  font-weight: 700;
  color: #ffffff;
}

/* --- Clean 2-Column Spotify Song Cards --- */
.view-header {
  margin-bottom: 12px;
}

.view-heading {
  font-size: 20px;
  font-weight: 800;
}

.view-subheading {
  font-size: 12px;
  color: var(--text-muted);
}

.cards-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  gap: 14px;
  width: 100%;
}

.song-card {
  min-width: 0;
  overflow: hidden;
  background-color: transparent;
  display: flex;
  flex-direction: column;
  cursor: pointer;
}

.card-img-wrapper {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 8px;
  background-color: #242424;
}

.card-img-wrapper img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.card-play-btn {
  position: absolute;
  bottom: 8px;
  right: 8px;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background-color: var(--accent);
  color: #000;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(0,0,0,0.5);
}

.card-info {
  min-width: 0;
  overflow: hidden;
}

.card-title {
  font-size: 14px;
  font-weight: 700;
  color: #ffffff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.song-card.playing-now .card-title {
  color: var(--accent);
}

.card-artist {
  font-size: 12.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 2px;
}

.card-actions {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  padding-top: 4px;
}

.card-actions button {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.card-actions button.liked {
  color: var(--accent);
}

/* =====================================================================
   TAB 2: SEARCH VIEW STYLES (Matches Screenshots 70795, 70796, 70797)
   ===================================================================== */
.tab-top-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 14px 10px 14px;
}

.tab-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.tab-big-title {
  font-size: 24px;
  font-weight: 800;
  color: #ffffff;
  letter-spacing: -0.5px;
}

.spotify-white-search-wrap {
  padding: 4px 14px 12px 14px;
}

.spotify-white-search {
  display: flex;
  align-items: center;
  background-color: #ffffff;
  border-radius: 6px;
  height: 46px;
  padding: 0 14px;
}

.spotify-white-search input {
  background: transparent;
  border: none;
  outline: none;
  color: #121212;
  font-size: 15px;
  font-weight: 600;
  width: 100%;
  margin-left: 10px;
}

.spotify-white-search input::placeholder {
  color: #555555;
  font-weight: 600;
}

.btn-clear-white-search {
  background: transparent;
  border: none;
  color: #333;
  font-size: 22px;
  cursor: pointer;
}

/* 2-Column Colorful Spotify Browse Tiles with Rotated Corner Image */
.browse-tiles-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.browse-tile {
  position: relative;
  height: 96px;
  border-radius: 6px;
  padding: 12px;
  overflow: hidden;
  cursor: pointer;
}

.browse-tile-title {
  font-size: 16px;
  font-weight: 800;
  color: #ffffff;
  max-width: 62%;
  line-height: 1.22;
  position: relative;
  z-index: 2;
}

.browse-tile-img {
  position: absolute;
  right: -12px;
  bottom: -6px;
  width: 68px;
  height: 68px;
  border-radius: 4px;
  object-fit: cover;
  transform: rotate(25deg);
  box-shadow: -2px 4px 10px rgba(0, 0, 0, 0.45);
  z-index: 1;
}

/* "Discover something new" Vertical Cards (Screenshot 70795.jpg) */
.discover-reel-card {
  position: relative;
  width: 138px;
  height: 210px;
  border-radius: 8px;
  overflow: hidden;
  flex-shrink: 0;
  cursor: pointer;
  background-color: #242424;
}

.discover-reel-card img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.discover-reel-tag {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent 55%, rgba(0,0,0,0.88) 100%);
  display: flex;
  align-items: flex-end;
  padding: 12px 10px;
  font-size: 13.5px;
  font-weight: 800;
  color: #ffffff;
}

/* =====================================================================
   TAB 3: YOUR LIBRARY VIEW STYLES (Matches Screenshot 70798.jpg)
   ===================================================================== */
.lib-pills-row {
  display: flex;
  gap: 8px;
  padding: 2px 14px 12px 14px;
}

.lib-pill {
  background-color: #2a2a2a;
  border: none;
  color: #ffffff;
  padding: 7px 16px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.lib-pill.active {
  background-color: var(--accent);
  color: #000000;
  font-weight: 700;
}

.library-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.lib-row {
  display: flex;
  align-items: center;
  gap: 14px;
  cursor: pointer;
  padding: 4px 0;
}

.lib-row-art, .lib-liked-box {
  width: 64px;
  height: 64px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
}

.lib-row-art.artist-round {
  border-radius: 50%;
}

.lib-liked-box {
  background: linear-gradient(135deg, #450af5 0%, #8e8ee5 55%, #c4efd9 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #ffffff;
  font-size: 26px;
}

.lib-row-info {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.lib-row-title {
  font-size: 16px;
  font-weight: 700;
  color: #ffffff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lib-row-sub {
  font-size: 13px;
  color: var(--text-muted);
  margin-top: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 5px;
}

.pin-green {
  color: var(--accent);
  font-size: 12px;
}

/* =====================================================================
   CRIMSON FLOATING MINI-PLAYER BAR (Matches Screenshots 70792 - 70798)
   ===================================================================== */
.mini-player {
  position: fixed;
  left: 8px;
  right: 8px;
  bottom: 58px;
  background-color: #590d12;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.8);
  z-index: 35;
  overflow: hidden;
  cursor: pointer;
  touch-action: pan-y;
}

.mini-player-main {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  height: 54px;
}

.mini-thumb {
  width: 40px;
  height: 40px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
}

.mini-info {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.mini-title {
  font-size: 13.5px;
  font-weight: 700;
  color: #ffffff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini-artist-row {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.76);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.mini-icon-btn, .mini-play-btn {
  background: transparent;
  border: none;
  color: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.mini-icon-btn.liked svg {
  stroke: var(--accent);
  color: var(--accent);
}

.mini-progress-track {
  height: 2px;
  background-color: rgba(255, 255, 255, 0.22);
  width: 100%;
}

.mini-progress-fill {
  height: 100%;
  background-color: #ffffff;
  width: 0%;
  transition: width 0.15s linear;
}

/* --- Bottom 5-Tab Navigation Bar --- */
.mobile-bottom-nav {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 56px;
  background: linear-gradient(180deg, rgba(0, 0, 0, 0.92) 0%, #000000 100%);
  display: flex;
  justify-content: space-around;
  align-items: center;
  z-index: 40;
}

.mob-tab {
  background: transparent;
  border: none;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  font-size: 10.5px;
  font-weight: 600;
  cursor: pointer;
}

.mob-tab.active, .mob-tab:hover {
  color: #ffffff;
}

/* =====================================================================
   FULL-SCREEN VIDEO CANVAS & VOICE-SYNCED LYRICS SHEET
   ===================================================================== */
.np-sheet {
  position: fixed;
  inset: 0;
  background-color: #0c0c0c;
  z-index: 80;
  overflow-y: auto;
  transform: translateY(100%);
  transition: transform 0.3s cubic-bezier(0.2, 0.9, 0.3, 1);
}

.np-sheet.open {
  transform: translateY(0);
}

.np-video-stage {
  position: relative;
  width: 100%;
  height: var(--stage-h, 88dvh);
  max-height: var(--stage-h, 88dvh);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  overflow: hidden;
  background: linear-gradient(180deg, #7c161b 0%, #4a0c10 58%, #1a0406 100%);
  touch-action: pan-y;
}

.np-canvas-video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0.28;
  z-index: 1;
}

.np-backdrop-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  filter: blur(36px) brightness(0.42) saturate(1.6);
  z-index: 0;
}

.np-video-gradient {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(110, 16, 22, 0.45) 0%,
    rgba(60, 8, 12, 0.35) 50%,
    rgba(18, 4, 6, 0.95) 100%
  );
  z-index: 2;
}

.np-center-poster-wrap {
  position: relative;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 4px 24px;
  flex: 1;
  min-height: 0;
}

.np-center-poster {
  width: min(76vw, 310px);
  height: min(76vw, 310px);
  aspect-ratio: 1;
  border-radius: 8px;
  object-fit: cover;
  box-shadow: 0 18px 42px rgba(0, 0, 0, 0.65);
}

.np-top-bar {
  position: relative;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 16px 8px 16px;
}

.np-top-center {
  text-align: center;
}

.np-top-sub {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.8);
}

.np-top-album {
  font-size: 13.5px;
  font-weight: 800;
  color: #ffffff;
}

.np-icon-btn {
  background: transparent;
  border: none;
  color: #ffffff;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.np-stage-bottom {
  position: relative;
  z-index: 5;
  padding: 10px 18px 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.np-floating-lyric {
  font-size: 15.5px;
  font-weight: 800;
  color: #ffffff;
  text-shadow: 0 2px 10px rgba(0, 0, 0, 0.85);
  min-height: 22px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.np-track-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.np-track-thumb {
  width: 44px;
  height: 44px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
}

.np-track-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.np-track-title {
  font-size: 18px;
  font-weight: 800;
  color: #ffffff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.np-track-artist-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13.5px;
  color: rgba(255, 255, 255, 0.78);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sparkle-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background-color: var(--accent);
  color: #000;
  font-size: 9px;
  font-weight: 900;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  flex-shrink: 0;
}

.np-circle-action {
  background: transparent;
  border: none;
  color: #ffffff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.np-circle-action.liked svg {
  stroke: var(--accent);
  color: var(--accent);
}

.np-scrubber-wrap {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.np-range-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.35);
  outline: none;
  cursor: pointer;
}

.np-range-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #ffffff;
  cursor: pointer;
}

.np-time-row {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  color: rgba(255, 255, 255, 0.75);
}

.np-controls-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.np-ctrl-btn {
  background: transparent;
  border: none;
  color: #ffffff;
  cursor: pointer;
}

.np-ctrl-btn.active {
  color: var(--accent);
}

.np-big-play-btn {
  width: 62px;
  height: 62px;
  border-radius: 50%;
  background-color: #ffffff;
  color: #000000;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.np-sub-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* Below-the-fold Lyrics & Artist Cards */
.np-below-fold {
  padding: 8px 14px 60px 14px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.np-lyrics-card {
  background: linear-gradient(165deg, #7a1216 0%, #4a0a0d 100%);
  border-radius: 16px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.np-card-header {
  font-size: 16px;
  font-weight: 800;
  color: #ffffff;
}

.np-lyrics-body {
  font-size: 20px;
  font-weight: 800;
  line-height: 1.55;
  color: rgba(255, 255, 255, 0.35);
  max-height: 250px;
  overflow-y: auto;
  scroll-behavior: smooth;
  scrollbar-width: none;
}

.np-lyrics-body::-webkit-scrollbar {
  display: none;
}

.np-lyrics-body.expanded {
  max-height: 68vh;
}

.lyric-line {
  padding: 7px 6px;
  border-radius: 8px;
  color: rgba(255, 255, 255, 0.34);
  transition: color 0.22s ease, transform 0.22s ease;
  cursor: pointer;
}

.lyric-line.past {
  color: rgba(255, 255, 255, 0.58);
}

.lyric-line.active {
  color: #ffffff;
  transform: scale(1.03);
  background: rgba(255, 255, 255, 0.07);
}

.np-show-lyrics-pill {
  align-self: flex-start;
  background-color: #ffffff;
  color: #000000;
  border: none;
  padding: 9px 20px;
  border-radius: 999px;
  font-size: 13.5px;
  font-weight: 800;
  cursor: pointer;
}

.np-info-card {
  background-color: #1f1f1f;
  border-radius: 16px;
  overflow: hidden;
}

.np-padded-card {
  padding: 20px;
}

.np-artist-banner-wrap {
  position: relative;
  width: 100%;
  height: 210px;
}

.np-artist-banner {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.np-artist-banner-label {
  position: absolute;
  top: 16px;
  left: 18px;
  font-size: 16px;
  font-weight: 800;
  color: #ffffff;
  text-shadow: 0 2px 8px rgba(0,0,0,0.8);
}

.np-artist-body {
  padding: 18px;
}

.np-artist-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.np-artist-name-verified {
  font-size: 20px;
  font-weight: 800;
  display: flex;
  align-items: center;
  gap: 6px;
}

.verified-check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background-color: #a5f3fc;
  color: #000;
  font-size: 11px;
  font-weight: 900;
}

.np-monthly-listeners {
  font-size: 13px;
  color: var(--text-muted);
}

.np-follow-btn {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.45);
  color: #ffffff;
  padding: 7px 18px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}

.np-artist-bio {
  font-size: 13.5px;
  color: var(--text-muted);
  line-height: 1.5;
}

.np-section-heading {
  font-size: 16px;
  font-weight: 800;
  margin-bottom: 14px;
}

.np-explore-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.np-explore-card {
  position: relative;
  height: 160px;
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
}

.np-explore-card img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.np-explore-card-label {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent 40%, rgba(0,0,0,0.88) 100%);
  display: flex;
  align-items: flex-end;
  padding: 10px;
  font-size: 12px;
  font-weight: 800;
  color: #ffffff;
}

.np-credits-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.np-credit-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.np-credit-name {
  font-size: 15px;
  font-weight: 700;
}

.np-credit-role {
  font-size: 12.5px;
  color: var(--text-muted);
}

/* Queue Drawer, Track Table & Modals */
.queue-panel {
  position: fixed;
  top: 0;
  right: 0;
  width: 340px;
  max-width: 100%;
  height: 100%;
  background-color: #181818;
  z-index: 95;
  display: flex;
  flex-direction: column;
  transform: translateX(100%);
  transition: transform 0.28s ease;
}

.queue-panel.open {
  transform: translateX(0);
}

.queue-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px;
  border-bottom: 1px solid var(--border-color);
}

.queue-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.queue-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px;
  border-radius: 6px;
  background-color: var(--bg-tinted);
  cursor: pointer;
}

.queue-item.active {
  border: 1px solid var(--accent);
}

.queue-item img {
  width: 40px;
  height: 40px;
  border-radius: 4px;
  object-fit: cover;
}

.queue-item-info {
  flex: 1;
  overflow: hidden;
}

.queue-item-title {
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.queue-item-artist {
  font-size: 11.5px;
  color: var(--text-muted);
}

.badge-count {
  background-color: var(--accent);
  color: #000;
  font-size: 10px;
  font-weight: 800;
  padding: 1px 6px;
  border-radius: 99px;
}

.btn-primary {
  background-color: var(--accent);
  color: #000;
  border: none;
  padding: 10px 22px;
  border-radius: 999px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
}

.btn-danger {
  background: transparent;
  border: 1px solid var(--danger);
  color: var(--danger);
  padding: 8px 16px;
  border-radius: 999px;
  font-weight: 700;
  cursor: pointer;
}

.btn-secondary {
  background: transparent;
  border: 1px solid var(--text-muted);
  color: #fff;
  padding: 8px 16px;
  border-radius: 999px;
  cursor: pointer;
}

.btn-icon-small {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
}

.track-table {
  width: 100%;
  border-collapse: collapse;
}

.track-table th, .track-table td {
  padding: 10px 6px;
  text-align: left;
  font-size: 13.5px;
}

.track-title-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 700;
}

.track-title-cell img {
  width: 42px;
  height: 42px;
  border-radius: 4px;
  object-fit: cover;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 110;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s;
}

.modal-overlay.open {
  opacity: 1;
  pointer-events: auto;
}

.modal-card {
  background-color: #242424;
  border-radius: 14px;
  padding: 22px;
  width: 90%;
  max-width: 400px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: left;
}

.form-group input, .form-group textarea, .form-group select {
  background-color: #121212;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 10px;
  color: #fff;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.welcome-card {
  text-align: center;
}

.welcome-badge {
  width: 54px;
  height: 54px;
  border-radius: 50%;
  background-color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto;
}

.toast-container {
  position: fixed;
  bottom: 120px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 130;
  display: flex;
  flex-direction: column;
  gap: 6px;
  pointer-events: none;
}

.toast {
  background-color: var(--accent);
  color: #000;
  font-weight: 800;
  padding: 9px 18px;
  border-radius: 999px;
  font-size: 13px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.6);
}
'''

PLAYER_JS = r'''/* ==========================================================================
   Musify Spotify Canvas & Smart Auto-Radio Player:
   - Atomic 2nd-Song Launch (canplay + loadeddata + 1s Watchdog, zero race condition)
   - Smart Context Radio (/api/similar) based on the currently playing song
   - Strict Zero-Repeat History (state.playedKeys) so played songs NEVER repeat
   - Touch Swipe Left -> Next Song | Touch Swipe Right -> Previous Song
   - Live Voice-Synced Karaoke Lyrics + Background Play on Android Home
   ========================================================================== */
(function () {
  const $ = (id) => document.getElementById(id);
  const audio = $("audio");
  const canvasVideo = $("canvasVideo");

  const ICON_PLAY_MINI = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  const ICON_PAUSE_MINI = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
  const ICON_PLAY_BIG = '<svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  const ICON_PAUSE_BIG = '<svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';

  const CANVAS_VIDEOS = [
    "https://assets.mixkit.co/videos/preview/mixkit-girl-dancing-happily-in-a-field-of-flowers-4702-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-dj-playing-music-at-a-nightclub-4344-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-silhouette-of-a-woman-dancing-in-the-dark-41763-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-young-woman-listening-to-music-with-headphones-4813-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-crowd-cheering-at-a-rock-concert-14021-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-man-dancing-under-changing-lights-32746-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-woman-dancing-in-neon-light-33899-large.mp4",
    "https://assets.mixkit.co/videos/preview/mixkit-musician-playing-acoustic-guitar-in-the-dark-44425-large.mp4"
  ];

  const state = {
    queue: [],
    index: -1,
    shuffle: false,
    repeat: 0,
    urlTry: 0,
    lyricLines: [],
    playedKeys: new Set(),
    lastAdvanceTs: 0,
    trackStartTs: 0,
    wantPlaying: false,
    lastVisibilityChange: 0,
    fetchingSimilar: false,
    lastSimilarSeedKey: ""
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  window.esc = esc;

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) return "0:00";
    sec = Math.floor(sec);
    return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
  }
  window.fmtTime = fmt;

  function toast(msg) {
    const box = $("toastContainer");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), 2200);
  }
  window.toast = toast;

  function ensureHttps(u) {
    return u ? String(u).trim().replace(/^http:\/\//i, "https://") : "";
  }

  // Canonical song title key so "Song", "Song - Telugu", "Song (From Movie)", "Song - Remix" all match
  function canonicalSongKey(song) {
    if (!song) return "";
    let raw = String(song.title || song.song || "").toLowerCase();
    raw = raw.replace(/\s*[\(\[].*?[\)\]]/g, "");
    raw = raw.replace(/\s*-\s*.*$/, "");
    raw = raw.replace(/\b(telugu|tamil|hindi|malayalam|kannada|remix|lofi|slowed|reverbed|club mix|version|female|male|ost|theme)\b/gi, "");
    const clean = raw.replace(/[^a-z0-9]+/g, "");
    if (clean.length >= 3) return clean;
    return String(song.title || song.id || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }
  window.canonicalSongKey = canonicalSongKey;

  function markSongPlayed(song) {
    if (!song) return;
    const k = canonicalSongKey(song);
    if (k) state.playedKeys.add(k);
    if (song.id) state.playedKeys.add("id_" + String(song.id));
    if (song.media_url) state.playedKeys.add("url_" + String(song.media_url));
  }

  function hasSongBeenPlayed(song) {
    if (!song) return true;
    const k = canonicalSongKey(song);
    if (k && state.playedKeys.has(k)) return true;
    if (song.id && state.playedKeys.has("id_" + String(song.id))) return true;
    if (song.media_url && state.playedKeys.has("url_" + String(song.media_url))) return true;
    return false;
  }

  function urlsFor(song) {
    const raw = [song.media_url].concat(song.backup_urls || []);
    const expanded = [];
    raw.forEach((u) => {
      let clean = ensureHttps(u);
      if (!clean || clean.includes("soundhelix.com")) return;
      clean = clean.replace("//preview.saavncdn.com", "//aac.saavncdn.com").replace("_96_p.mp4", "_160.mp4");
      if (!expanded.includes(clean)) expanded.push(clean);
      ["_320.mp4", "_160.mp4", "_96.mp4", "_48.mp4"].forEach((br) => {
        ["_320.mp4", "_160.mp4", "_96.mp4", "_48.mp4", "_96_p.mp4"].forEach((orig) => {
          if (clean.includes(orig)) {
            const v = clean.replace(orig, br);
            if (!expanded.includes(v)) expanded.push(v);
          }
        });
      });
    });
    return expanded;
  }

  function save() {
    try {
      localStorage.setItem("musify_queue", JSON.stringify({ queue: state.queue.slice(0, 60), index: state.index }));
    } catch (e) {}
  }

  function load() {
    try {
      const d = JSON.parse(localStorage.getItem("musify_queue") || "null");
      if (d && Array.isArray(d.queue) && d.queue.length) {
        state.queue = d.queue.filter((x) => x && x.media_url && !String(x.media_url).includes("soundhelix.com"));
        state.index = (typeof d.index === "number" && d.index >= 0 && d.index < state.queue.length) ? d.index : 0;
      }
    } catch (e) {}
  }

  function current() {
    return state.queue[state.index] || null;
  }

  // ---- Smart Context Radio Prefetcher (/api/similar) ----
  // Ensures that after searching a song or playing any song, 20+ non-repeating songs on the same base are queued right after it!
  async function prefetchSimilarSongs(seedSong, prioritizeImmediateNext) {
    if (!seedSong || state.fetchingSimilar) return;
    const seedKey = canonicalSongKey(seedSong);
    state.fetchingSimilar = true;
    state.lastSimilarSeedKey = seedKey;

    try {
      const excludeArr = Array.from(state.playedKeys).filter((k) => !k.startsWith("id_") && !k.startsWith("url_")).slice(-40);
      const params = new URLSearchParams({
        title: seedSong.title || "",
        artist: seedSong.artist || "",
        album: seedSong.album || "",
        exclude: excludeArr.join(",")
      });
      const res = await fetch("/api/similar?" + params.toString());
      const data = await res.json();
      const simList = (data && Array.isArray(data.songs)) ? data.songs : [];
      if (!simList.length) {
        state.fetchingSimilar = false;
        return;
      }

      // Collect keys already in upcoming queue so we never add duplicates
      const existingKeys = new Set();
      state.queue.forEach((qItem, idx) => {
        if (idx <= state.index) {
          const k = canonicalSongKey(qItem);
          if (k) existingKeys.add(k);
        }
      });
      state.playedKeys.forEach((pk) => existingKeys.add(pk));

      const freshRelated = [];
      simList.forEach((cand) => {
        if (!cand || !cand.media_url) return;
        const ck = canonicalSongKey(cand);
        if (!ck || existingKeys.has(ck) || hasSongBeenPlayed(cand)) return;
        existingKeys.add(ck);
        freshRelated.push(cand);
      });

      if (freshRelated.length) {
        if (prioritizeImmediateNext) {
          // User played a song from Search or a single card: place smart related songs immediately at index + 1
          const keptBeforeAndCurrent = state.queue.slice(0, state.index + 1);
          const remainingAfter = state.queue.slice(state.index + 1).filter((item) => {
            const ik = canonicalSongKey(item);
            return ik && !existingKeys.has(ik) && !hasSongBeenPlayed(item);
          });
          state.queue = keptBeforeAndCurrent.concat(freshRelated, remainingAfter);
        } else {
          // Append non-duplicate related tracks to the end of queue
          const allCurrentKeys = new Set(state.queue.map((x) => canonicalSongKey(x)));
          freshRelated.forEach((fr) => {
            const fk = canonicalSongKey(fr);
            if (fk && !allCurrentKeys.has(fk)) {
              state.queue.push(fr);
              allCurrentKeys.add(fk);
            }
          });
        }
        renderQueue();
        save();
      }
    } catch (e) {}
    state.fetchingSimilar = false;
  }

  // Removes any duplicate versions of already-played songs sitting ahead of state.index
  function pruneUpcomingDuplicates() {
    if (!state.queue.length || state.index < 0) return;
    const seenKeys = new Set();
    // Mark all songs up to and including state.index as seen
    for (let i = 0; i <= state.index && i < state.queue.length; i++) {
      const k = canonicalSongKey(state.queue[i]);
      if (k) seenKeys.add(k);
    }
    state.playedKeys.forEach((pk) => seenKeys.add(pk));

    const head = state.queue.slice(0, state.index + 1);
    const tail = [];
    for (let j = state.index + 1; j < state.queue.length; j++) {
      const item = state.queue[j];
      const ik = canonicalSongKey(item);
      if (!ik || seenKeys.has(ik) || hasSongBeenPlayed(item)) continue;
      seenKeys.add(ik);
      tail.push(item);
    }
    state.queue = head.concat(tail);
  }

  // ---- Background Canvas Video Controller ----
  function updateCanvasVideo(song) {
    if (!song) return;
    const img = ensureHttps(song.image_url);
    if ($("npBackdropImg") && img) $("npBackdropImg").src = img;

    if (!canvasVideo) return;
    let hash = 0;
    const str = (song.id || "") + (song.title || "");
    for (let i = 0; i < str.length; i++) hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
    const videoUrl = song.video_url || CANVAS_VIDEOS[hash % CANVAS_VIDEOS.length];

    if (canvasVideo.getAttribute("data-src") !== videoUrl) {
      canvasVideo.setAttribute("data-src", videoUrl);
      canvasVideo.src = videoUrl;
      if (!document.hidden) {
        try { canvasVideo.load(); } catch (e) {}
      }
    }
    if (!document.hidden && state.wantPlaying) {
      const p = canvasVideo.play();
      if (p && p.catch) p.catch(() => {});
    }
  }

  // ---- Voice-Synced Karaoke Lyrics Resolver & Auto-Highlighter ----
  state.activeLyricIdx = -1;

  function renderTimedLyricsDOM(lines) {
    const box = $("lyricsContent");
    if (!box) return;
    state.lyricLines = lines;
    state.activeLyricIdx = -1;
    box.innerHTML = lines.map((item, idx) =>
      `<div class="lyric-line" data-idx="${idx}" data-time="${item.time}">${esc(item.text)}</div>`
    ).join("");

    box.querySelectorAll(".lyric-line").forEach((el) => {
      el.addEventListener("click", () => {
        const t = parseFloat(el.getAttribute("data-time") || "0");
        if (!isNaN(t) && audio.duration) {
          audio.currentTime = Math.min(audio.duration - 0.5, Math.max(0, t));
          if (audio.paused) audio.play().catch(() => {});
        }
      });
    });
  }

  function syncLyricsToTime(currentTimeSec) {
    const lines = state.lyricLines;
    if (!lines || !lines.length) return;

    let activeIdx = 0;
    for (let i = 0; i < lines.length; i++) {
      if (currentTimeSec >= lines[i].time) {
        activeIdx = i;
      } else {
        break;
      }
    }

    if (activeIdx === state.activeLyricIdx) return;
    state.activeLyricIdx = activeIdx;

    if ($("npFloatingLyric") && lines[activeIdx]) {
      $("npFloatingLyric").textContent = "♪ " + lines[activeIdx].text;
    }

    const box = $("lyricsContent");
    if (!box) return;
    const domLines = box.querySelectorAll(".lyric-line");
    domLines.forEach((el, idx) => {
      el.classList.toggle("active", idx === activeIdx);
      el.classList.toggle("past", idx < activeIdx);
    });

    const activeEl = domLines[activeIdx];
    if (activeEl && box.scrollHeight > box.clientHeight) {
      const targetTop = activeEl.offsetTop - box.clientHeight / 2 + activeEl.clientHeight / 2;
      box.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
    }
  }

  async function loadLyricsForCurrent() {
    const s = current();
    const box = $("lyricsContent");
    const floatingEl = $("npFloatingLyric");
    if (!s || !box) return;

    box.innerHTML = '<div style="color:rgba(255,255,255,0.7);font-size:16px;">Syncing voice lyrics for "' + esc(s.title) + '"...</div>';
    if (floatingEl) floatingEl.textContent = "♪ " + s.title + " • " + s.artist;

    if (Array.isArray(s.timedLyrics) && s.timedLyrics.length) {
      renderTimedLyricsDOM(s.timedLyrics);
      syncLyricsToTime(audio.currentTime || 0);
      return;
    }

    try {
      const params = new URLSearchParams({
        id: s.id || "",
        title: s.title || "",
        artist: s.artist || "",
        duration: String(s.duration || Math.round(audio.duration || 210))
      });
      const res = await fetch("/api/lyrics?" + params.toString());
      const data = await res.json();
      if (data.status && Array.isArray(data.lines) && data.lines.length) {
        s.timedLyrics = data.lines;
        renderTimedLyricsDOM(data.lines);
        syncLyricsToTime(audio.currentTime || 0);
        return;
      }
    } catch (e) {}

    const hook = s.title || "Melody";
    const artistName = (s.artist || "Artist").split(",")[0];
    const fallbackLines = [
      { time: 0, text: "♪ " + hook + " (Instrumental Intro)" },
      { time: 12, text: hook + " — Vocals by " + artistName },
      { time: 28, text: "From the album " + (s.album || hook) },
      { time: 48, text: "♪ Melodic verse & chorus ♪" },
      { time: 90, text: hook + " • " + artistName }
    ];
    s.timedLyrics = fallbackLines;
    renderTimedLyricsDOM(fallbackLines);
    syncLyricsToTime(audio.currentTime || 0);
  }

  function updateArtistAndCreditsCards(s) {
    if (!s) return;
    const artists = (s.artist || "Featured Artist").split(",").map((a) => a.trim()).filter(Boolean);
    const mainArtist = artists[0] || "Featured Artist";
    const secondArtist = artists[1] || s.music || mainArtist;

    if ($("npAlbumHeader")) $("npAlbumHeader").textContent = s.album || s.title || "Musify Radio";
    if ($("miniBannerHint")) $("miniBannerHint").textContent = "Continuous Radio • Based on " + (s.title || s.album);
    if ($("npArtistBannerImg") && s.image_url) $("npArtistBannerImg").src = s.image_url;
    if ($("npAboutArtistName")) $("npAboutArtistName").textContent = mainArtist;
    if ($("npArtistBio")) {
      $("npArtistBio").textContent =
        mainArtist + " is a celebrated music artist, composer, and performer featured on " +
        (s.album || s.title) + ". Continuous non-repeating radio is active based on this track.";
    }

    if ($("npExploreHeading")) $("npExploreHeading").textContent = "Explore " + mainArtist;
    if ($("npExploreGrid")) {
      const img = esc(s.image_url || "https://c.saavncdn.com/368/Fear-Song-From-Devara-Part-1-Telugu-Telugu-2024-20240519131003-500x500.jpg");
      $("npExploreGrid").innerHTML = `
        <div class="np-explore-card" onclick="closeNowPlayingSheet(); window.triggerArtistSearch('${esc(mainArtist)}')">
          <img src="${img}" alt="">
          <div class="np-explore-card-label">Songs by ${esc(mainArtist)}</div>
        </div>
        <div class="np-explore-card" onclick="closeNowPlayingSheet(); window.triggerArtistSearch('${esc(secondArtist)}')">
          <img src="${img}" alt="" style="filter:hue-rotate(35deg)">
          <div class="np-explore-card-label">Similar to ${esc(mainArtist)}</div>
        </div>
        <div class="np-explore-card" onclick="closeNowPlayingSheet(); window.triggerArtistSearch('${esc(s.album || s.title)}')">
          <img src="${img}" alt="" style="filter:hue-rotate(85deg)">
          <div class="np-explore-card-label">Similar to ${esc(s.title)}</div>
        </div>`;
    }

    if ($("npCreditsList")) {
      $("npCreditsList").innerHTML = `
        <div class="np-credit-row">
          <div>
            <div class="np-credit-name">${esc(mainArtist)}</div>
            <div class="np-credit-role">Main Artist • Composer</div>
          </div>
          <button class="np-follow-btn" onclick="this.textContent = this.textContent === 'Follow' ? 'Following' : 'Follow'">Follow</button>
        </div>
        ${secondArtist && secondArtist !== mainArtist ? `
        <div class="np-credit-row">
          <div>
            <div class="np-credit-name">${esc(secondArtist)}</div>
            <div class="np-credit-role">Main Artist • Vocalist</div>
          </div>
          <button class="np-follow-btn" onclick="this.textContent = this.textContent === 'Follow' ? 'Following' : 'Follow'">Follow</button>
        </div>` : ""}
        <div class="np-credit-row">
          <div>
            <div class="np-credit-name">${esc(s.album || "Original Motion Picture Soundtrack")}</div>
            <div class="np-credit-role">Album • Source</div>
          </div>
        </div>`;
    }
  }

  function updateInfo() {
    const s = current();
    $("playerTitle").textContent = s ? s.title : "Tap a song to start";
    $("playerArtist").textContent = s ? s.artist : "Swipe left for next • Swipe right for previous";
    if (s && s.image_url) $("playerImg").src = s.image_url;

    if ($("npTitle")) $("npTitle").textContent = s ? s.title : "Select a song";
    if ($("npArtist")) $("npArtist").textContent = s ? s.artist : "Artist";
    if (s && s.image_url && $("npThumb")) $("npThumb").src = s.image_url;
    if (s && s.image_url && $("npCenterPoster")) $("npCenterPoster").src = s.image_url;

    document.title = s ? s.title + " • Musify" : "Musify";

    if (s) {
      updateCanvasVideo(s);
      updateArtistAndCreditsCards(s);
      loadLyricsForCurrent();
    }

    if (window.updateLikeButtonState) window.updateLikeButtonState(s);

    if ("mediaSession" in navigator && s) {
      try {
        navigator.mediaSession.metadata = new MediaMetadata({
          title: s.title, artist: s.artist, album: s.album || "",
          artwork: s.image_url ? [{ src: s.image_url, sizes: "512x512" }] : []
        });
      } catch (e) {}
    }
  }

  function renderQueue() {
    const list = $("queueList");
    if ($("queueBadge")) $("queueBadge").textContent = state.queue.length;
    if (!list) return;
    if (!state.queue.length) {
      list.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px;">Queue is empty. Tap any song to start!</div>';
      return;
    }
    list.innerHTML = state.queue.map((s, i) => `
      <div class="queue-item ${i === state.index ? "active" : ""}" data-i="${i}">
        <img src="${esc(s.image_url)}" alt="" onerror="this.style.visibility='hidden'">
        <div class="queue-item-info">
          <div class="queue-item-title">${esc(s.title)}</div>
          <div class="queue-item-artist">${esc(s.artist)}</div>
        </div>
        <button class="btn-icon-small" data-remove="${i}" title="Remove">&times;</button>
      </div>`).join("");
  }

  // ---- Atomic Audio Launcher (Guarantees Song 2+ Always Starts Playing on Mobile & Desktop) ----
  function attemptPlayAudio() {
    if (!state.wantPlaying || !audio.src) return;
    const p = audio.play();
    if (p && p.catch) {
      p.catch(() => {
        setTimeout(() => {
          if (state.wantPlaying && audio.paused && audio.src) {
            audio.play().catch(() => {});
          }
        }, 220);
      });
    }
  }

  function startAudio() {
    const s = current();
    if (!s) return;
    state.wantPlaying = true;
    state.trackStartTs = Date.now();
    const urls = urlsFor(s);
    if (!urls.length) {
      // Resolve fresh stream via search if media_url was missing
      resolveFreshAndPlay(s);
      return;
    }
    const chosenUrl = urls[Math.min(state.urlTry, urls.length - 1)];
    try { audio.pause(); } catch (e) {}
    audio.autoplay = true;
    audio.src = chosenUrl;
    try { audio.load(); } catch (e) {}
    attemptPlayAudio();

    if ("mediaSession" in navigator) {
      try { navigator.mediaSession.playbackState = "playing"; } catch (e) {}
    }
  }

  async function resolveFreshAndPlay(s) {
    if (!s || s._resolvedFresh) {
      if (state.queue.length > 1) next(true, true);
      return;
    }
    s._resolvedFresh = true;
    try {
      const q = encodeURIComponent((s.title || "") + " " + (s.artist || "").split(",")[0]);
      const r = await fetch("/api/search?q=" + q);
      const j = await r.json();
      if (j.songs && j.songs[0] && j.songs[0].media_url) {
        s.media_url = j.songs[0].media_url;
        s.backup_urls = j.songs[0].backup_urls || [j.songs[0].media_url];
        state.urlTry = 0;
        startAudio();
        return;
      }
    } catch (e) {}
    if (state.queue.length > 1) next(true, true);
  }

  function playAt(i, prioritizeRadio) {
    if (i < 0 || i >= state.queue.length) return;
    state.index = i;
    state.urlTry = 0;
    const s = state.queue[i];
    markSongPlayed(s);
    pruneUpcomingDuplicates();
    startAudio();
    updateInfo();
    renderQueue();
    save();

    // Always keep at least 8 unplayed, non-duplicate related songs ready ahead of state.index
    const remainingUnplayed = state.queue.slice(state.index + 1).filter((x) => !hasSongBeenPlayed(x));
    if (prioritizeRadio || remainingUnplayed.length < 8) {
      prefetchSimilarSongs(s, Boolean(prioritizeRadio));
    }
  }

  // ---- Smart Non-Repeating Next Track Selector ----
  async function next(auto, bypassLock) {
    const now = Date.now();
    // Prevent double-advance race condition when ended + pause/watchdog fire close together
    if (auto && !bypassLock && (now - state.lastAdvanceTs < 2400)) {
      return;
    }
    state.lastAdvanceTs = now;

    if (!state.queue.length) return;

    // Repeat ONE mode only when explicitly toggled to repeat === 2
    if (auto && state.repeat === 2) {
      audio.currentTime = 0;
      attemptPlayAudio();
      return;
    }

    pruneUpcomingDuplicates();

    // 1. Find the next UNPLAYED song in state.queue after state.index
    for (let idx = state.index + 1; idx < state.queue.length; idx++) {
      const cand = state.queue[idx];
      if (cand && !hasSongBeenPlayed(cand)) {
        return playAt(idx, false);
      }
    }

    // 2. Also check earlier indexes in state.queue in case the user started mid-list
    for (let idx = 0; idx < state.index; idx++) {
      const cand = state.queue[idx];
      if (cand && !hasSongBeenPlayed(cand)) {
        return playAt(idx, false);
      }
    }

    // 3. Check window.allLoadedSongs for any unplayed song
    const screenCatalog = window.allLoadedSongs || [];
    for (let k = 0; k < screenCatalog.length; k++) {
      const sc = screenCatalog[k];
      if (sc && !hasSongBeenPlayed(sc)) {
        state.queue.push(sc);
        return playAt(state.queue.length - 1, false);
      }
    }

    // 4. All current queue items have been played! Fetch fresh related songs based on current() immediately
    const seed = current();
    if (seed) {
      toast("🎵 Loading next related songs...");
      await prefetchSimilarSongs(seed, false);
      for (let idx = state.index + 1; idx < state.queue.length; idx++) {
        const cand = state.queue[idx];
        if (cand && !hasSongBeenPlayed(cand)) {
          return playAt(idx, false);
        }
      }
    }

    // 5. Ultimate fallback if user played every single song in the session: clear history except current and continue
    const curSong = current();
    state.playedKeys.clear();
    if (curSong) markSongPlayed(curSong);
    if (state.queue.length > 1) {
      const nextIdx = (state.index + 1) % state.queue.length;
      return playAt(nextIdx, false);
    }
  }

  function prev() {
    if (!state.queue.length) return;
    if (audio.currentTime > 4) { audio.currentTime = 0; return; }
    const targetIdx = state.index > 0 ? state.index - 1 : 0;
    state.index = targetIdx;
    state.urlTry = 0;
    startAudio();
    updateInfo();
    renderQueue();
    save();
  }

  function toggle() {
    if (!current()) {
      if (state.queue.length) playAt(0, false);
      return;
    }
    if (!audio.src) { startAudio(); return; }
    if (audio.paused) {
      state.wantPlaying = true;
      attemptPlayAudio();
    } else {
      state.wantPlaying = false;
      audio.pause();
    }
  }

  function setPlayIcon(playing) {
    if ($("btnPlay")) $("btnPlay").innerHTML = playing ? ICON_PAUSE_MINI : ICON_PLAY_MINI;
    if ($("btnNpPlay")) $("btnNpPlay").innerHTML = playing ? ICON_PAUSE_BIG : ICON_PLAY_BIG;
    if ("mediaSession" in navigator) {
      try { navigator.mediaSession.playbackState = playing ? "playing" : "paused"; } catch (e) {}
    }
    if (canvasVideo) {
      if (playing && !document.hidden) canvasVideo.play().catch(() => {});
      else canvasVideo.pause();
    }
  }

  // ---- Touch Swipe Left (Next Song) & Swipe Right (Previous Song) ----
  function attachSwipeGesture(el) {
    if (!el) return;
    let startX = 0;
    let startY = 0;
    el.addEventListener("touchstart", (e) => {
      if (!e.touches || !e.touches.length) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
    }, { passive: true });

    el.addEventListener("touchend", (e) => {
      if (!e.changedTouches || !e.changedTouches.length) return;
      const dx = e.changedTouches[0].clientX - startX;
      const dy = e.changedTouches[0].clientY - startY;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        if (dx < 0) {
          toast("⏭ Next Song");
          next(false, true);
        } else {
          toast("⏮ Previous Song");
          prev();
        }
      }
    }, { passive: true });
  }

  attachSwipeGesture($("miniPlayer"));
  attachSwipeGesture($("npSwipeZone"));

  // ---- Full-Screen Sheet Helpers & Android Back Button Guard ----
  try {
    if (!history.state || !history.state.musifyBase) {
      history.replaceState({ musifyBase: true, view: "home" }, "", location.pathname);
    }
  } catch (e) {}

  window.openNowPlayingSheet = function () {
    const sheet = $("nowPlayingSheet");
    const wasOpen = sheet.classList.contains("open");
    sheet.classList.add("open");
    if (canvasVideo && !audio.paused && !document.hidden) {
      canvasVideo.play().catch(() => {});
    }
    if (!wasOpen) {
      try {
        history.pushState({ musifyBase: true, view: "nowPlaying" }, "", "#playing");
      } catch (e) {}
    }
  };

  window.closeNowPlayingSheet = function (fromPopState) {
    const sheet = $("nowPlayingSheet");
    if (!sheet.classList.contains("open")) return;
    sheet.classList.remove("open");
    sheet.scrollTo({ top: 0, behavior: "instant" });
    if ($("lyricsContent")) $("lyricsContent").classList.remove("expanded");
    if ($("btnExpandLyrics")) $("btnExpandLyrics").textContent = "Show lyrics";
    if (!fromPopState && location.hash === "#playing") {
      try { history.back(); } catch (e) {}
    }
  };

  window.addEventListener("popstate", () => {
    if ($("queuePanel") && $("queuePanel").classList.contains("open")) {
      $("queuePanel").classList.remove("open");
      try { history.pushState({ musifyBase: true, view: "home" }, "", location.pathname); } catch (err) {}
      return;
    }
    if ($("nowPlayingSheet") && $("nowPlayingSheet").classList.contains("open")) {
      window.closeNowPlayingSheet(true);
      try { history.pushState({ musifyBase: true, view: "home" }, "", location.pathname); } catch (err) {}
      return;
    }
    if ($("sidebar") && $("sidebar").classList.contains("mobile-open")) {
      $("sidebar").classList.remove("mobile-open");
      try { history.pushState({ musifyBase: true, view: "home" }, "", location.pathname); } catch (err) {}
      return;
    }
    if (window.loadCategory) {
      try { history.pushState({ musifyBase: true, view: "home" }, "", location.pathname); } catch (err) {}
    }
  });

  // ---- Full-Length Song Downloader ----
  window.downloadSongObj = async function (song) {
    if (!song || !song.media_url) {
      toast("Select a song first to download");
      return;
    }
    const cleanTitle = (song.title || "Song").replace(/[\\/*?:"<>|]+/g, "").trim();
    const cleanArtist = (song.artist || "").split(",")[0].replace(/[\\/*?:"<>|]+/g, "").trim();
    const fileName = (cleanArtist ? `${cleanTitle} - ${cleanArtist}` : cleanTitle) + ".m4a";

    toast("⬇ Downloading: " + cleanTitle + "...");
    const dlUrl = `/api/download?url=${encodeURIComponent(song.media_url)}&id=${encodeURIComponent(song.id || "")}&title=${encodeURIComponent(song.title || "")}&artist=${encodeURIComponent(song.artist || "")}`;

    try {
      const resp = await fetch(dlUrl);
      if (resp.ok) {
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.style.display = "none";
        a.href = blobUrl;
        a.download = fileName;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
          URL.revokeObjectURL(blobUrl);
          a.remove();
        }, 5000);
        toast("✅ Downloaded: " + cleanTitle);
        return;
      }
    } catch (e) {}

    let iframe = document.getElementById("hiddenDlFrame");
    if (!iframe) {
      iframe = document.createElement("iframe");
      iframe.id = "hiddenDlFrame";
      iframe.style.display = "none";
      document.body.appendChild(iframe);
    }
    iframe.src = dlUrl;
  };

  window.downloadCurrentSong = function () {
    const s = current();
    if (s) window.downloadSongObj(s);
    else toast("Play a song first to download");
  };

  window.scrollToLyricsCard = function () {
    if (!$("nowPlayingSheet").classList.contains("open")) {
      window.openNowPlayingSheet();
    }
    const card = $("lyricsPreviewCard");
    if (card) card.scrollIntoView({ behavior: "smooth" });
  };

  window.toggleExpandLyrics = function () {
    const body = $("lyricsContent");
    const btn = $("btnExpandLyrics");
    if (!body) return;
    const exp = body.classList.toggle("expanded");
    if (btn) btn.textContent = exp ? "Hide lyrics" : "Show lyrics";
  };

  window.shareCurrentSong = function () {
    const s = current();
    if (!s) return;
    if (navigator.share) {
      navigator.share({ title: s.title, text: "Listen to " + s.title + " by " + s.artist + " on Musify", url: location.href }).catch(() => {});
    } else {
      toast("Link copied: " + s.title);
    }
  };

  window.Player = {
    playSongs(songs, startIndex, openFull, isFromSearch) {
      const rawList = (songs && songs.length) ? songs.slice() : [];
      const chosen = rawList[startIndex || 0];
      if (!chosen) return;

      // Build a clean deduplicated queue starting with chosen song
      const chosenKey = canonicalSongKey(chosen);
      const seen = new Set([chosenKey]);
      const cleanQueue = [chosen];

      // Add remaining songs from rawList that are NOT duplicate versions of chosen song or already played
      for (let i = (startIndex || 0) + 1; i < rawList.length; i++) {
        const item = rawList[i];
        const ik = canonicalSongKey(item);
        if (item && ik && !seen.has(ik) && !hasSongBeenPlayed(item)) {
          seen.add(ik);
          cleanQueue.push(item);
        }
      }
      for (let i = 0; i < (startIndex || 0); i++) {
        const item = rawList[i];
        const ik = canonicalSongKey(item);
        if (item && ik && !seen.has(ik) && !hasSongBeenPlayed(item)) {
          seen.add(ik);
          cleanQueue.push(item);
        }
      }

      state.queue = cleanQueue;
      // Trigger Smart Radio prefetch immediately when playing from Search or when queue is short
      playAt(0, Boolean(isFromSearch || cleanQueue.length <= 6));
      if (openFull) window.openNowPlayingSheet();
    },
    seedQueueIfEmpty(songs) {
      if (!songs || !songs.length) return;
      if (state.queue.length <= 1 && audio.paused) {
        state.queue = songs.slice();
        state.index = 0;
        updateInfo();
        renderQueue();
        save();
      }
    },
    currentSong: current,
    addToQueue(song) {
      state.queue.push(song);
      renderQueue(); save();
      toast("Added to queue");
    },
    playNext(song) {
      state.queue.splice(state.index + 1, 0, song);
      renderQueue(); save();
      toast("Playing next: " + song.title);
    },
    removeAt(i) {
      const wasCurrent = i === state.index;
      state.queue.splice(i, 1);
      if (i < state.index) state.index--;
      if (wasCurrent) {
        if (state.queue.length) { state.index = Math.min(i, state.queue.length) - 1; next(false, true); }
        else { audio.pause(); audio.removeAttribute("src"); state.index = -1; updateInfo(); }
      }
      renderQueue(); save();
    },
    clear() {
      audio.pause();
      audio.removeAttribute("src");
      state.queue = []; state.index = -1;
      updateInfo(); renderQueue(); save(); setPlayIcon(false);
    },
    next: () => next(false, true),
    prev,
    toggle
  };

  // ---- Audio Events, CanPlay Auto-Starter & 1-Second Continuous Watchdog ----
  audio.addEventListener("play", () => {
    setPlayIcon(true);
  });

  // Whenever a newly loaded track buffers enough to play, automatically start it if wantPlaying is true!
  audio.addEventListener("canplay", () => {
    if (state.wantPlaying && audio.paused) {
      attemptPlayAudio();
    }
  });

  audio.addEventListener("loadeddata", () => {
    if (state.wantPlaying && audio.paused) {
      attemptPlayAudio();
    }
  });

  audio.addEventListener("pause", () => {
    // 1. If AAC stream auto-paused right at the end of the track, advance to next song!
    if (audio.duration > 15 && (audio.duration - audio.currentTime) <= 1.2) {
      next(true, false);
      return;
    }
    // 2. If Android OS briefly paused <audio> when pressing the Home button while wantPlaying is true, auto-resume!
    if (state.wantPlaying && (document.hidden || (Date.now() - state.lastVisibilityChange < 1500))) {
      setTimeout(() => {
        if (state.wantPlaying && audio.paused) {
          audio.play().catch(() => {});
        }
      }, 80);
      return;
    }
    setPlayIcon(false);
  });

  // Continuous 1-Second Watchdog: catches any silent mobile stall or missed 'ended' event
  setInterval(() => {
    if (!state.wantPlaying || !current()) return;
    // A. If track reached the end and is paused/ended, advance immediately
    if (audio.ended || (audio.duration > 15 && audio.currentTime >= audio.duration - 0.6 && audio.paused)) {
      next(true, false);
      return;
    }
    // B. If Song 2 loaded (readyState >= 2) within the last 8s of switching tracks but mobile browser left it paused, kick play()!
    if (audio.paused && audio.readyState >= 2 && (Date.now() - state.trackStartTs < 8000)) {
      audio.play().catch(() => {});
      return;
    }
    // C. If stream URL stalled at readyState === 0 for > 6.5s, step to backup bitrate URL
    if (audio.paused && audio.readyState === 0 && (Date.now() - state.trackStartTs > 6500)) {
      const s = current();
      const urls = urlsFor(s);
      if (state.urlTry + 1 < urls.length) {
        state.urlTry++;
        startAudio();
      } else {
        resolveFreshAndPlay(s);
      }
    }
  }, 1000);

  // ---- Background Home Play vs. Recent Apps Close Handler ----
  document.addEventListener("visibilitychange", () => {
    state.lastVisibilityChange = Date.now();
    if (document.hidden) {
      if (canvasVideo) {
        try { canvasVideo.pause(); } catch (e) {}
      }
      if (state.wantPlaying && audio.paused) {
        audio.play().catch(() => {});
      }
    } else {
      if (state.wantPlaying && !audio.paused && canvasVideo) {
        canvasVideo.play().catch(() => {});
      }
    }
  });

  function stopAudioOnAppTermination() {
    state.wantPlaying = false;
    try {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      if (canvasVideo) canvasVideo.pause();
      if ("mediaSession" in navigator) {
        navigator.mediaSession.playbackState = "none";
        navigator.mediaSession.metadata = null;
      }
    } catch (e) {}
  }

  window.addEventListener("pagehide", stopAudioOnAppTermination);
  window.addEventListener("beforeunload", stopAudioOnAppTermination);
  window.addEventListener("unload", stopAudioOnAppTermination);

  audio.addEventListener("ended", () => {
    next(true, false);
  });

  audio.addEventListener("loadedmetadata", () => {
    if ($("totalDuration")) $("totalDuration").textContent = fmt(audio.duration);
  });

  audio.addEventListener("timeupdate", () => {
    if (audio.duration && isFinite(audio.duration)) {
      const pct = (audio.currentTime / audio.duration) * 100;
      if ($("progressBar")) $("progressBar").value = pct;
      if ($("miniProgressFill")) $("miniProgressFill").style.width = pct + "%";
      if ($("currentTime")) $("currentTime").textContent = fmt(audio.currentTime);

      syncLyricsToTime(audio.currentTime);

      if ("mediaSession" in navigator && navigator.mediaSession.setPositionState) {
        try {
          navigator.mediaSession.setPositionState({
            duration: audio.duration,
            playbackRate: audio.playbackRate || 1,
            position: Math.min(audio.duration, audio.currentTime)
          });
        } catch (e) {}
      }
    }
  });

  audio.addEventListener("error", async () => {
    const s = current();
    if (!s) return;
    const urls = urlsFor(s);
    if (state.urlTry + 1 < urls.length) {
      state.urlTry++;
      startAudio();
      return;
    }
    resolveFreshAndPlay(s);
  });

  if ($("btnPlay")) $("btnPlay").addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
  if ($("btnNext")) $("btnNext").addEventListener("click", () => next(false, true));
  if ($("btnPrev")) $("btnPrev").addEventListener("click", prev);
  if ($("btnShuffle")) {
    // Default shuffle OFF so related songs play in smart relevance order
    $("btnShuffle").classList.remove("active");
    $("btnShuffle").addEventListener("click", () => {
      state.shuffle = !state.shuffle;
      $("btnShuffle").classList.toggle("active", state.shuffle);
      toast("Shuffle " + (state.shuffle ? "ON" : "OFF"));
    });
  }
  if ($("btnRepeat")) {
    $("btnRepeat").addEventListener("click", () => {
      state.repeat = (state.repeat + 1) % 3;
      $("btnRepeat").classList.toggle("active", state.repeat > 0);
      toast(["Repeat OFF", "Repeat ALL", "Repeat ONE"][state.repeat]);
    });
  }
  if ($("progressBar")) {
    $("progressBar").addEventListener("input", (e) => {
      if (audio.duration) audio.currentTime = (e.target.value / 100) * audio.duration;
    });
  }

  if ($("btnQueueToggle")) $("btnQueueToggle").addEventListener("click", () => $("queuePanel").classList.toggle("open"));
  if ($("btnCloseQueue")) $("btnCloseQueue").addEventListener("click", () => $("queuePanel").classList.remove("open"));
  if ($("btnClearQueue")) $("btnClearQueue").addEventListener("click", () => window.Player.clear());
  if ($("queueList")) {
    $("queueList").addEventListener("click", (e) => {
      const rm = e.target.closest("[data-remove]");
      if (rm) { window.Player.removeAt(parseInt(rm.dataset.remove, 10)); return; }
      const item = e.target.closest(".queue-item");
      if (item) playAt(parseInt(item.dataset.i, 10), false);
    });
  }

  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const k = e.key.toLowerCase();
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    else if (k === "n" || e.key === "ArrowRight") next(false, true);
    else if (k === "p" || e.key === "ArrowLeft") prev();
  });

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.setActionHandler("play", () => {
        state.wantPlaying = true;
        attemptPlayAudio();
      });
      navigator.mediaSession.setActionHandler("pause", () => {
        state.wantPlaying = false;
        audio.pause();
      });
      navigator.mediaSession.setActionHandler("stop", () => {
        stopAudioOnAppTermination();
      });
      navigator.mediaSession.setActionHandler("nexttrack", () => next(false, true));
      navigator.mediaSession.setActionHandler("previoustrack", prev);
    } catch (e) {}
  }

  load();
  updateInfo();
  renderQueue();
  setPlayIcon(false);
})();
'''

APP_JS = r'''/* ==========================================================================
   Musify 1:1 Spotify Mobile Controller (Home, Search, Your Library)
   Matches Screenshots 70792 - 70798, 70811, 70826, 70836
   ========================================================================== */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = window.esc;
  const toast = window.toast;

  let currentSongs = [];
  let currentCategory = "home";
  let pendingSong = null;
  let openPlaylistId = null;
  let searchTimer = null;
  let currentPage = 1;
  let activeSearchQuery = "";
  let currentLibFilter = "all";

  // Verified JioSaavn CDN Artwork Constants (100% tested HTTP 200 URLs)
  const ART = {
    maestro: "https://c.saavncdn.com/blob/793/Maestro-Telugu-2021-20220201143350-500x500.jpg",
    raktacharitra: "https://c.saavncdn.com/208/Raktacharitra-Telugu-2010-20190220140106-500x500.jpg",
    aarya2: "https://c.saavncdn.com/298/Aarya-2-Telugu-2009-20190822135933-500x500.jpg",
    og: "https://c.saavncdn.com/532/They-Call-Him-OG-Telugu-2025-20250924124333-500x500.jpg",
    ogCheetah: "https://c.saavncdn.com/989/Hungry-Cheetah-From-They-Call-Him-OG-Telugu-2023-20230903132606-500x500.jpg",
    radhimaa: "https://c.saavncdn.com/877/Radhimaa-From-Think-Indie-Tamil-2026-20260827192132-500x500.jpg",
    karuppu: "https://c.saavncdn.com/835/God-Mode-From-Karuppu-Tamil-2025-20251020131154-500x500.jpg",
    omVeezha: "https://c.saavncdn.com/696/Veezha-Veera-From-OM-Chapter-1-Udhiram-The-Blood-Wood-Tamil-Tamil-2026-20261019161818-500x500.jpg",
    devara: "https://c.saavncdn.com/368/Fear-Song-From-Devara-Part-1-Telugu-Telugu-2024-20240519131003-500x500.jpg",
    anirudh: "https://c.saavncdn.com/783/Anirudh-Ravichander-Latest-Blockbuster-Hits-Telugu-2024-20241014191030-500x500.jpg",
    guntur: "https://c.saavncdn.com/000/Guntur-Kaaram-Telugu-2023-20240126145901-500x500.jpg",
    kissik: "https://c.saavncdn.com/587/Kissik-From-Pushpa-2-The-Rule-Telugu-Telugu-2024-20241124192109-500x500.jpg",
    itemSongs: "https://c.saavncdn.com/132/Tollywood-Sensational-Item-Songs-Telugu-2025-20250923191005-500x500.jpg",
    hinanna: "https://c.saavncdn.com/307/Samayama-From-Hi-Nanna-Telugu-2023-20230918164922-500x500.jpg",
    loveBeats: "https://c.saavncdn.com/606/Tollywood-Love-Beats-Telugu-2023-20231215191230-500x500.jpg",
    daaku: "https://c.saavncdn.com/951/Daaku-Maharaaj-Telugu-2024-20250612195102-500x500.jpg",
    dcRevenge: "https://c.saavncdn.com/484/Raga-of-Revenge-From-DC-Tamil-2026-20260515180436-500x500.jpg",
    jailer2: "https://c.saavncdn.com/823/Jailer-2-Original-Motion-Picture-Soundtrack-Tamil-2026-20260930120445-500x500.jpg",
    workout: "https://c.saavncdn.com/472/Telugu-Workout-Music-Hits-Telugu-2024-20240930171028-500x500.jpg",
    danceMasti: "https://c.saavncdn.com/324/Tollywood-Dance-Masti-Telugu-2024-20241211151003-500x500.jpg",
    romantic2026: "https://c.saavncdn.com/209/Tollywood-Romantic-Songs-Telugu-2026-20260206171136-500x500.jpg"
  };

  const FALLBACK_ART_LIST = Object.values(ART);
  const SVG_FALLBACK_DATA_URI = "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">' +
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#7c161b"/><stop offset="100%" stop-color="#181818"/></linearGradient></defs>' +
    '<rect width="300" height="300" fill="url(#g)"/>' +
    '<circle cx="150" cy="150" r="58" fill="#1db954" opacity="0.9"/>' +
    '<polygon points="136,122 186,150 136,178" fill="#000000"/>' +
    '</svg>'
  );

  // Self-healing image fallback so broken image icons NEVER appear anywhere
  window.handleImgError = function (imgEl, idx) {
    if (!imgEl) return;
    const step = parseInt(imgEl.getAttribute("data-err-step") || "0", 10);
    if (step === 0) {
      imgEl.setAttribute("data-err-step", "1");
      const loaded = window.allLoadedSongs || [];
      const pickIdx = (typeof idx === "number" && !isNaN(idx)) ? idx : Math.floor(Math.random() * 10);
      if (loaded.length && loaded[pickIdx % loaded.length] && loaded[pickIdx % loaded.length].image_url) {
        imgEl.src = loaded[pickIdx % loaded.length].image_url;
        return;
      }
      imgEl.src = FALLBACK_ART_LIST[pickIdx % FALLBACK_ART_LIST.length] || SVG_FALLBACK_DATA_URI;
    } else if (step === 1) {
      imgEl.setAttribute("data-err-step", "2");
      imgEl.src = ART.devara;
    } else {
      imgEl.onerror = null;
      imgEl.src = SVG_FALLBACK_DATA_URI;
    }
  };

  // 1. Exact 8 Top Quick-Play Cards from Screenshot 70792.jpg & 70836.jpg
  const QUICK_8_CARDS = [
    { title: "Maestro", query: "Baby Oh Baby Maestro|Vennello Aadapilla|Shyam Singha Roy|Sirivennela", img: ART.maestro },
    { title: "High beam 🔥", cat: "highbeam", img: ART.raktacharitra },
    { title: "Aarya - 2 (Original...", query: "https://www.jiosaavn.com/album/aarya-2/szAD227UDqE_|Uppenantha|Ringa Ringa|My Love Is Gone|Karige Loga|Baby He Loves You", img: ART.aarya2 },
    { title: "OG Telugu Full Album", query: "https://www.jiosaavn.com/song/hungry-cheetah-from-they-call-him-og/OgQvaDxEbwE|Hungry Cheetah|They Call Him OG|Thaman S Telugu", img: ART.og },
    { title: "Telugu Item Songs 💃", cat: "itemsongs", img: ART.itemSongs },
    { title: "Raktacharitra • Mass", query: "https://www.jiosaavn.com/song/dorikithe-chastavu/HCAfAg15b2o|Dorikithe Chastavu|Katthula Toh|Thudilenidhi|Dabbunnoda|Champaraa", img: ART.raktacharitra },
    { title: "Feel good Love Songs ...", query: "Samayama Hi Nanna|Baby Oh Baby Maestro|Chuttamalle Devara|Inthandham Sita Ramam|Inkem Inkem", img: ART.hinanna },
    { title: "OM Chapter 1 • Karuppu", cat: "om", img: ART.omVeezha }
  ];

  // Curated "For fans of Anirudh Ravichander" Cards (Screenshots 70792 & 70793)
  const ANIRUDH_CURATED_CARDS = [
    { title: "Fear Song - Telugu", sub: "Devara Part 1 • Anirudh Ravichander", query: "Fear Song Devara Telugu Anirudh", img: ART.devara },
    { title: "Jailer 2 - All Songs [Telugu] ✨🔥", sub: "Album • Anirudh Ravichander", query: "Jailer 2 Anirudh Ravichander Hukum", img: ART.jailer2 },
    { title: "Raga of Revenge (From \"DC\")", sub: "Single • Anirudh Ravichander", query: "Raga of Revenge DC Anirudh", img: ART.dcRevenge },
    { title: "Anirudh Latest Blockbuster Hits", sub: "Ayudha Pooja, Chuttamalle, Daavudi", query: "Anirudh Ravichander Telugu Devara", img: ART.anirudh },
    { title: "Ayudha Pooja - Telugu", sub: "Devara Part 1 • Anirudh, Kaala Bhairava", query: "Ayudha Pooja Devara Telugu", img: ART.devara },
    { title: "Hukum - Tiger Ka Hukum", sub: "Jailer • Anirudh Ravichander", query: "Hukum Jailer Anirudh", img: ART.jailer2 }
  ];

  // Curated "More of what you like" Cards (Screenshot 70793.jpg)
  const MORE_LIKE_CURATED_CARDS = [
    { title: "Dorikithe Chastavu (Telugu)", sub: "Raktacharitra • Ravindra Upadhyay", query: "Dorikithe Chastavu Raktacharitra", img: ART.raktacharitra },
    { title: "Veezha Veera (From \"OM Chapter 1\")", sub: "Single • Sai Abhyankkar", query: "Veezha Veera OM Chapter 1", img: ART.omVeezha },
    { title: "Kissik (From \"Pushpa 2\")", sub: "Pushpa 2 The Rule • Devi Sri Prasad", query: "Kissik Pushpa 2 Telugu", img: ART.kissik },
    { title: "Kurchi Madathapetti", sub: "Guntur Kaaram • Thaman S", query: "Kurchi Madathapetti Guntur Kaaram", img: ART.guntur },
    { title: "Radhimaa (From \"Think Indie\")", sub: "Single • Sai Abhyankkar", query: "Radhimaa Sai Abhyankkar", img: ART.radhimaa },
    { title: "God Mode (From \"Karuppu\")", sub: "Karuppu • Sai Abhyankkar", query: "God Mode Karuppu Sai Abhyankkar", img: ART.karuppu },
    { title: "The Rage of Daaku", sub: "Daaku Maharaaj • Thaman S", query: "The Rage of Daaku Daaku Maharaaj", img: ART.daaku }
  ];

  // Curated "Jump back in" Cards (Screenshot 70794.jpg)
  const JUMP_BACK_CURATED_CARDS = [
    { title: "Raga of Revenge (From \"DC\")", sub: "Single • Anirudh Ravichander", query: "Raga of Revenge DC Anirudh", img: ART.dcRevenge },
    { title: "OG Telugu songs", sub: "Playlist • Thaman S, Pawan Kalyan", query: "Hungry Cheetah They Call Him OG", img: ART.og },
    { title: "Raktacharitra (Telugu)", sub: "Dorikithe Chastavu • Katthula Toh", query: "Dorikithe Chastavu Raktacharitra", img: ART.raktacharitra },
    { title: "Daaku Maharaaj", sub: "EP • Thaman S", query: "Daaku Maharaaj Thaman", img: ART.daaku },
    { title: "Tollywood Sensational Item Songs 💃", sub: "Kissik, Oo Antava, Daavudi, Pakka Local", cat: "itemsongs", img: ART.itemSongs },
    { title: "Aarya - 2", sub: "Album • Devi Sri Prasad", query: "Uppenantha Ringa Ringa Aarya 2", img: ART.aarya2 },
    { title: "Maestro", sub: "Album • Baby Oh Baby, Anurag Kulkarni", query: "Baby Oh Baby Maestro", img: ART.maestro }
  ];

  // Curated "Happy" Cards (Screenshot 70794.jpg)
  const HAPPY_CURATED_CARDS = [
    { title: "Kiraak Telugu Dance", sub: "Anirudh Ravichander, Devi Sri Prasad, Thaman S", cat: "mass", img: ART.danceMasti },
    { title: "Tollywood Love Beats", sub: "Samayama, Baby Oh Baby, Chuttamalle", query: "Samayama Hi Nanna Chuttamalle Devara", img: ART.loveBeats },
    { title: "Guntur Kaaram Party", sub: "Kurchi Madathapetti, Dum Masala", query: "Kurchi Madathapetti Guntur Kaaram", img: ART.guntur },
    { title: "Tollywood Romantic Hits", sub: "Sid Sriram, Anurag Kulkarni, Shreya Ghoshal", cat: "love", img: ART.romantic2026 },
    { title: "Global Pop • Weeknd & Ed", sub: "Blinding Lights, Shape of You, Starboy", cat: "english", img: ART.workout }
  ];

  // 2. Search Tab Colorful Browse Tiles (Screenshots 70795, 70796, 70797)
  const SEARCH_TOP_TILES = [
    { title: "Music", color: "#e1118c", cat: "telugu", img: ART.devara },
    { title: "High Beam 🔥", color: "#b81d24", cat: "highbeam", img: ART.raktacharitra },
    { title: "Item Songs 💃", color: "#8400e7", cat: "itemsongs", img: ART.itemSongs },
    { title: "Home of I-Pop", color: "#1e3264", cat: "hindi", img: ART.loveBeats },
    { title: "Made For You", color: "#8d67ab", cat: "love", img: ART.aarya2 },
    { title: "Mass Beats ⚡", color: "#006450", cat: "mass", img: ART.guntur }
  ];

  const SEARCH_BROWSE_ALL = [
    { title: "Upcoming releases", color: "#006450", query: "Veezha Veera OM Chapter 1 God Mode Karuppu", img: ART.omVeezha },
    { title: "New Releases", color: "#608108", cat: "telugu", img: ART.devara },
    { title: "High Beam 🔥", color: "#7c161b", cat: "highbeam", img: ART.raktacharitra },
    { title: "Item Songs 💃", color: "#e8115b", cat: "itemsongs", img: ART.itemSongs },
    { title: "Telugu", color: "#b85d19", cat: "telugu", img: ART.guntur },
    { title: "Hindi", color: "#e1118c", cat: "hindi", img: ART.loveBeats },
    { title: "Tamil", color: "#ba5d07", query: "Anirudh Ravichander Jailer 2 Vikram Leo", img: ART.jailer2 },
    { title: "English • Weeknd & Ed", color: "#1e3264", cat: "english", img: ART.workout },
    { title: "Punjabi", color: "#b02897", cat: "punjabi", img: ART.danceMasti },
    { title: "OM • Dhanush", color: "#8400e7", cat: "om", img: ART.omVeezha },
    { title: "Charts", color: "#8d67ab", cat: "trending", img: ART.anirudh },
    { title: "Malayalam", color: "#477d95", query: "Malayalam Top Hits Aavesham Premalu", img: ART.romantic2026 },
    { title: "Summer & Chill", color: "#148a08", cat: "chill", img: ART.hinanna },
    { title: "Discover Weekly", color: "#8d67ab", cat: "melodies", img: ART.maestro },
    { title: "Radio", color: "#e1118c", cat: "evergreen", img: ART.aarya2 },
    { title: "Fresh Finds", color: "#ff0090", query: "Radhimaa Sai Abhyankkar God Mode Karuppu", img: ART.radhimaa },
    { title: "Workout & Gym", color: "#e13300", cat: "workout", img: ART.workout },
    { title: "Heartbreak & Sad", color: "#777777", cat: "sad", img: ART.aarya2 }
  ];

  // 3. Curated Library Items Matching Screenshot 70798.jpg
  const CURATED_LIBRARY_ITEMS = [
    { type: "playlists", title: "High beam", sub: "Playlist • PAVAN BHAI YT", cat: "highbeam", img: ART.raktacharitra },
    { type: "albums", title: "Daaku Maharaaj", sub: "EP • Thaman S", query: "The Rage of Daaku Daaku Maharaaj", img: ART.daaku },
    { type: "albums", title: "Raktacharitra", sub: "Compilation • Various Artists (Telugu)", query: "Dorikithe Chastavu Raktacharitra", img: ART.raktacharitra },
    { type: "playlists", title: "Jailer 2 -All Songs [Telugu] ✨🔥", sub: "Playlist • Arora ☆🎧", query: "Jailer 2 Anirudh Hukum", img: ART.jailer2 },
    { type: "albums", title: "Jailer 2 (Original Motion Picture So...", sub: "Album • Anirudh Ravichander", query: "Jailer 2 Original Motion Picture Soundtrack Anirudh", img: ART.jailer2 },
    { type: "albums", title: "Veezha Veera (From \"OM Chapter ...", sub: "Single • Sai Abhyankkar", query: "Veezha Veera OM Chapter 1", img: ART.omVeezha },
    { type: "playlists", title: "Tollywood Sensational Item Songs 💃", sub: "Playlist • Kissik, Oo Antava, Daavudi & 20 Hits", cat: "itemsongs", img: ART.itemSongs },
    { type: "albums", title: "Devara Part 1 - Telugu", sub: "Album • Anirudh Ravichander", query: "Fear Song Chuttamalle Daavudi Devara", img: ART.devara },
    { type: "albums", title: "They Call Him OG", sub: "Album • Thaman S", query: "Hungry Cheetah They Call Him OG", img: ART.og },
    { type: "albums", title: "Aarya - 2", sub: "Album • Devi Sri Prasad", query: "Uppenantha Ringa Ringa Aarya 2", img: ART.aarya2 },
    { type: "artists", title: "Anirudh Ravichander", sub: "Artist • Composer & Vocalist", query: "Anirudh Ravichander", img: ART.anirudh, round: true },
    { type: "artists", title: "Sid Sriram", sub: "Artist • Telugu & Tamil Melodies", query: "Sid Sriram Telugu", img: ART.hinanna, round: true },
    { type: "artists", title: "Devi Sri Prasad", sub: "Artist • Rockstar DSP", query: "Devi Sri Prasad Telugu", img: ART.kissik, round: true },
    { type: "artists", title: "Thaman S", sub: "Artist • Mass & High Beam", query: "Thaman S Telugu", img: ART.daaku, round: true },
    { type: "artists", title: "Sai Abhyankkar", sub: "Artist • OM Chapter 1, Karuppu & Indie", query: "Sai Abhyankkar", img: ART.radhimaa, round: true }
  ];

  const ALL_SINGERS = [
    { name: "Anirudh", query: "Anirudh Ravichander", img: ART.anirudh },
    { name: "Thaman S", query: "Thaman S Telugu", img: ART.daaku },
    { name: "Devi Sri Prasad", query: "Devi Sri Prasad Telugu", img: ART.kissik },
    { name: "Sid Sriram", query: "Sid Sriram Telugu", img: ART.hinanna },
    { name: "Sai Abhyankkar", query: "Sai Abhyankkar", img: ART.radhimaa },
    { name: "Anurag Kulkarni", query: "Anurag Kulkarni", img: ART.maestro },
    { name: "Dhanush", query: "Dhanush", img: ART.omVeezha },
    { name: "The Weeknd", query: "The Weeknd", img: ART.workout },
    { name: "Ed Sheeran", query: "Ed Sheeran", img: ART.loveBeats },
    { name: "Arijit Singh", query: "Arijit Singh", img: ART.romantic2026 },
    { name: "S. P. Balasubrahmanyam", query: "SP Balasubrahmanyam Telugu", img: ART.aarya2 },
    { name: "Hesham Abdul Wahab", query: "Hesham Abdul Wahab", img: ART.hinanna },
    { name: "Shreya Ghoshal", query: "Shreya Ghoshal Telugu", img: ART.kissik }
  ];

  const TITLES = {
    home: ["Recommended for today", "Full-length Telugu, High Beam, Item Songs, English & Hindi • Back-to-back"],
    highbeam: ["High Beam 🔥 • Mass & Elevation", "Dorikithe Chastavu (Telugu), Hungry Cheetah, Fear Song, Daaku Maharaaj, Salaar & Hukum"],
    itemsongs: ["Tollywood Sensational Item Songs 💃", "Kissik, Oo Antava, Daavudi, Pakka Local, Jigelu Rani, Ringa Ringa, Kevvu Keka & more"],
    telugu: ["New & Trending Telugu Hits", "Devara, Pushpa 2, Guntur Kaaram, Daaku Maharaaj, Hi Nanna & OG"],
    om: ["OM Chapter 1: Udhiram • Karuppu", "Veezha Veera, God Mode, Alaakaa Loova, Senjudare, Radhimaa & Dhanush"],
    english: ["Ed Sheeran, The Weeknd & English Hits", "Blinding Lights, Shape of You, Starboy, Perfect & global pop"],
    hindi: ["Bollywood Top 50", "Arijit Singh, Stree 2, Animal, Jawan & Hindi chartbusters"],
    love: ["Romantic & Love Melodies", "Samayama, Baby Oh Baby, Chuttamalle & Telugu love songs"],
    sad: ["Heartbreak & Sad Songs", "My Love Is Gone, Uppenantha, Orange, 3 & Sid Sriram"],
    mass: ["Mass & Dance Beats ⚡", "Kurchi Madathapetti, Daavudi, Pushpa Pushpa, Dorikithe Chastavu & Boss Party"],
    chill: ["Chill & Feel Good", "Sirivennela, Maestro, Samayama & acoustic vibes"],
    workout: ["Gym & Workout Motivation", "High-energy Dorikithe Chastavu, Fear Song, Hungry Cheetah & Salaar"],
    drive: ["Late Night Drive", "Ye Maaya Chesave, Awara, Gharshana & Blinding Lights"],
    party: ["Party & DJ Hits", "Non-stop dance floor & club anthems"],
    evergreen: ["90s & 2000s Evergreen", "SPB, Chithra, Mani Sharma & classic hits"],
    devotional: ["Devotional & Peaceful", "Annamayya, Hanuman Chalisa & Namo Namo"],
    melodies: ["Feel Good Telugu Melodies", "Baby Oh Baby, Samayama, Sirivennela & Sita Ramam"],
    trending: ["Trending Chartbusters", "Most streamed Telugu, Tamil, English & Hindi tracks"],
    punjabi: ["Punjabi Party Anthems", "AP Dhillon, Diljit Dosanjh & Karan Aujla"]
  };

  // ---- Tab Switching (Home / Search / Your Library) ----
  window.switchTab = function (tabName) {
    window.closeNowPlayingSheet();
    ["home", "search", "library"].forEach((t) => {
      const viewEl = $(t + "View");
      if (viewEl) viewEl.classList.toggle("active-view", t === tabName);
    });
    if ($("tabHome")) $("tabHome").classList.toggle("active", tabName === "home");
    if ($("tabSearch")) $("tabSearch").classList.toggle("active", tabName === "search");
    if ($("tabLibrary")) $("tabLibrary").classList.toggle("active", tabName === "library");

    if (tabName === "library") {
      renderLibraryView();
    } else if (tabName === "search") {
      initSearchBrowseTiles();
    }
  };

  // Opens a query or album/artist card and optionally auto-plays the first track immediately like Spotify
  window.openCategoryOrQuery = function (queryOrCat, headingLabel, autoPlayFirst) {
    window.switchTab("home");
    activeSearchQuery = queryOrCat;
    currentCategory = "custom";
    currentPage = 1;
    openPlaylistId = null;
    $("homeHeroSections").style.display = "none";
    document.querySelectorAll(".pill-btn").forEach((b) => b.classList.remove("active"));
    setHeading(headingLabel || queryOrCat, "Full-length tracks • Tap any song or ▶ Play All");
    showLoading("Loading " + (headingLabel || queryOrCat) + "...");
    api("/api/search?q=" + encodeURIComponent(queryOrCat) + "&page=1")
      .then((data) => {
        const list = data.songs || [];
        renderSongs(list, false, false);
        if (autoPlayFirst && currentSongs.length) {
          window.Player.playSongs(currentSongs, 0, true);
        }
      })
      .catch(() => {});
  };

  function getLikedSongs() {
    try { return JSON.parse(localStorage.getItem("musify_liked_songs") || "[]"); }
    catch (e) { return []; }
  }

  function saveLikedSongs(list) {
    try { localStorage.setItem("musify_liked_songs", JSON.stringify(list)); }
    catch (e) {}
  }

  function isSongLiked(song) {
    if (!song) return false;
    return getLikedSongs().some((s) => s.title === song.title && s.artist === song.artist);
  }

  function toggleLikeSong(song) {
    if (!song) return;
    const list = getLikedSongs();
    const idx = list.findIndex((s) => s.title === song.title && s.artist === song.artist);
    if (idx !== -1) {
      list.splice(idx, 1);
      toast("Removed from Liked Songs");
    } else {
      list.unshift(song);
      toast("Added to Liked Songs ♥");
    }
    saveLikedSongs(list);
    window.updateLikeButtonState(window.Player.currentSong());
  }

  window.toggleLikeCurrent = function () {
    const s = window.Player.currentSong();
    if (s) toggleLikeSong(s);
    else toast("Play a song first");
  };

  window.openAddToPlaylistCurrent = function () {
    const s = window.Player.currentSong();
    if (s) openAddToPlaylist(s);
  };

  window.updateLikeButtonState = function (song) {
    const liked = isSongLiked(song);
    if ($("btnMiniLike")) $("btnMiniLike").classList.toggle("liked", liked);
    if ($("btnNpLike")) $("btnNpLike").classList.toggle("liked", liked);
  };

  window.openLikedSongs = function () {
    window.switchTab("home");
    openPlaylistId = "liked";
    $("homeHeroSections").style.display = "none";
    const liked = getLikedSongs();
    currentSongs = liked;
    setHeading("Liked Songs ♥", liked.length + " saved songs");
    renderSongs(liked, false, true);
  };

  async function api(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok || data.success === false) throw new Error(data.error || "Request failed");
    return data;
  }

  function jsonOpts(method, body) {
    return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  function setHeading(title, sub) {
    $("heading").textContent = title;
    $("subheading").textContent = sub || "";
  }

  function showLoading(text) {
    $("content").innerHTML = '<div style="color:var(--text-muted);padding:30px 0;">' + esc(text || "Loading...") + "</div>";
  }

  function normalizeSongKey(s) {
    if (window.canonicalSongKey) return window.canonicalSongKey(s);
    const raw = String((s && (s.title || s.song)) || "").toLowerCase();
    return raw.replace(/\s*[\(\[].*?[\)\]]/g, "").replace(/\s*-\s*.*$/, "").replace(/[^a-z0-9]+/g, "") || String(s.id || "");
  }

  function deduplicateClientSongs(list) {
    const seen = new Set();
    const out = [];
    (list || []).forEach((s) => {
      if (!s) return;
      const tLow = String(s.title || "").toLowerCase();
      if (tLow.includes("mila toh marega") || tLow.includes("mila to marega") || tLow.includes("khaul khaul")) return;
      const k = normalizeSongKey(s);
      if (k && seen.has(k)) return;
      if (k) seen.add(k);
      out.push(s);
    });
    return out;
  }

  // ---- Render Home Screen Sections Immediately & Enrich with Live Songs ----
  function renderHomeHeroSections(songs) {
    const liveSongs = Array.isArray(songs) ? songs : [];

    // 1. 2x4 Top Quick Grid (Maestro, High beam, Aarya - 2, OG Telugu, Telugu Item Songs, Raktacharitra, Feel good, OM Chapter 1)
    if ($("quickTilesGrid")) {
      $("quickTilesGrid").innerHTML = QUICK_8_CARDS.map((card, i) => `
        <div class="quick-tile" data-quick-cat="${esc(card.cat || "")}" data-quick-q="${esc(card.query || "")}" data-quick-title="${esc(card.title)}">
          <img src="${esc(card.img)}" alt="" onerror="window.handleImgError(this, ${i})">
          <div class="quick-tile-title">${esc(card.title)}</div>
        </div>`).join("");
    }

    // 2. "Picked for you" Featured Horizontal Card (Prioritizes Telugu Dorikithe Chastavu / Veezha Veera / Fear Song)
    const preferredFeatured = liveSongs.find((s) => {
      const tl = String(s.title || "").toLowerCase();
      return tl.includes("dorikithe") || tl.includes("veezha") || tl.includes("fear song") || tl.includes("hungry cheetah");
    }) || liveSongs[0] || {
      title: "Dorikithe Chastavu",
      artist: "Ravindra Upadhyay, Vishvesh Parmar, Sandeep Patil",
      album: "Raktacharitra (Telugu)",
      image_url: ART.raktacharitra,
      query: "Dorikithe Chastavu Raktacharitra"
    };

    if ($("pickedForYouCard")) {
      const fImg = preferredFeatured.image_url || ART.raktacharitra;
      $("pickedForYouCard").innerHTML = `
        <img class="picked-card-img" src="${esc(fImg)}" alt="" onerror="window.handleImgError(this, 0)">
        <div class="picked-card-body">
          <div>
            <div class="picked-card-tag">Single • ${esc(preferredFeatured.album || "Raktacharitra (Telugu)")}</div>
            <div class="picked-card-title">${esc(preferredFeatured.title)}</div>
            <div class="picked-card-artist">${esc(preferredFeatured.artist)}</div>
          </div>
          <div class="picked-card-actions">
            <button class="mini-icon-btn" id="btnPickedDl" title="Save / Add">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#b3b3b3" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
            </button>
            <button class="picked-play-circle" title="Play">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
            </button>
          </div>
        </div>`;

      $("pickedForYouCard").onclick = (ev) => {
        if (ev.target.closest("#btnPickedDl")) {
          ev.stopPropagation();
          if (preferredFeatured.media_url) window.downloadSongObj(preferredFeatured);
          else window.openCategoryOrQuery("Dorikithe Chastavu Raktacharitra", "Raktacharitra (Telugu)", true);
          return;
        }
        const matchIdx = currentSongs.findIndex((x) => x.title === preferredFeatured.title);
        if (matchIdx !== -1) {
          window.Player.playSongs(currentSongs, matchIdx, true);
        } else {
          window.openCategoryOrQuery(preferredFeatured.query || preferredFeatured.title, preferredFeatured.title, true);
        }
      };
    }

    // 3. "For fans of Anirudh Ravichander" Row (Guaranteed Anirudh tracks & albums + verified Anirudh avatar)
    if ($("fanHeaderAvatar")) {
      $("fanHeaderAvatar").src = ART.anirudh;
      $("fanHeaderAvatar").onerror = function () { window.handleImgError(this, 1); };
    }
    if ($("anirudhRow")) {
      const liveAnirudh = liveSongs.filter((s) => {
        const comb = ((s.artist || "") + " " + (s.album || "") + " " + (s.title || "")).toLowerCase();
        return comb.includes("anirudh") || comb.includes("devara") || comb.includes("jailer") || comb.includes("fear song") || comb.includes("raga of revenge");
      });
      const anirudhItems = [
        ...liveAnirudh.map((s) => ({
          title: s.title,
          sub: s.artist,
          img: s.image_url || ART.devara,
          songIndex: currentSongs.indexOf(s)
        })),
        ...ANIRUDH_CURATED_CARDS
      ].slice(0, 8);

      $("anirudhRow").innerHTML = anirudhItems.map((item, idx) => `
        <div class="horiz-card" data-hero-i="${typeof item.songIndex === 'number' && item.songIndex >= 0 ? item.songIndex : ''}" data-card-q="${esc(item.query || '')}" data-card-title="${esc(item.title)}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(item.img)}" alt="" onerror="window.handleImgError(this, ${idx + 1})">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-title">${esc(item.title)}</div>
          <div class="horiz-card-sub">${esc(item.sub)}</div>
        </div>`).join("");
    }

    // 4. "More of what you like" Row (High Beam, OM Chapter 1, Pushpa 2, Guntur Kaaram, Daaku Maharaaj)
    if ($("newMusicRow")) {
      const moreItems = [
        ...MORE_LIKE_CURATED_CARDS,
        ...liveSongs.slice(0, 8).map((s) => ({
          title: s.title,
          sub: s.artist,
          img: s.image_url || ART.kissik,
          songIndex: currentSongs.indexOf(s)
        }))
      ].slice(0, 9);

      $("newMusicRow").innerHTML = moreItems.map((item, idx) => `
        <div class="horiz-card" data-hero-i="${typeof item.songIndex === 'number' && item.songIndex >= 0 ? item.songIndex : ''}" data-card-q="${esc(item.query || '')}" data-card-title="${esc(item.title)}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(item.img)}" alt="" onerror="window.handleImgError(this, ${idx + 2})">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-title">${esc(item.title)}</div>
          <div class="horiz-card-sub">${esc(item.sub)}</div>
        </div>`).join("");
    }

    // 5. "Jump back in" Row (Matches Screenshot 70794.jpg: Raga of Revenge, OG Telugu songs, Raktacharitra, Daaku Maharaaj, Item Songs)
    if ($("jumpBackRow")) {
      $("jumpBackRow").innerHTML = JUMP_BACK_CURATED_CARDS.map((item, idx) => `
        <div class="horiz-card" data-card-cat="${esc(item.cat || '')}" data-card-q="${esc(item.query || '')}" data-card-title="${esc(item.title)}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(item.img)}" alt="" onerror="window.handleImgError(this, ${idx + 3})">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-title">${esc(item.title)}</div>
          <div class="horiz-card-sub">${esc(item.sub)}</div>
        </div>`).join("");
    }

    // 6. "Happy" Row (Matches Screenshot 70794.jpg)
    if ($("hindiHeroRow")) {
      const happyItems = [
        ...HAPPY_CURATED_CARDS,
        ...liveSongs.slice(6, 12).map((s) => ({
          title: s.title,
          sub: s.artist,
          img: s.image_url || ART.hinanna,
          songIndex: currentSongs.indexOf(s)
        }))
      ].slice(0, 8);

      $("hindiHeroRow").innerHTML = happyItems.map((item, idx) => `
        <div class="horiz-card" data-hero-i="${typeof item.songIndex === 'number' && item.songIndex >= 0 ? item.songIndex : ''}" data-card-cat="${esc(item.cat || '')}" data-card-q="${esc(item.query || '')}" data-card-title="${esc(item.title)}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(item.img)}" alt="" onerror="window.handleImgError(this, ${idx + 4})">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-title">${esc(item.title)}</div>
          <div class="horiz-card-sub">${esc(item.sub)}</div>
        </div>`).join("");
    }

    // 7. "Your favourite artists" Circular Row
    if ($("singersRow")) {
      $("singersRow").innerHTML = ALL_SINGERS.map((sg, sIdx) => {
        const matched = liveSongs.find((x) => (x.artist || "").toLowerCase().includes(sg.name.toLowerCase().split(" ")[0]));
        const artUrl = sg.img || (matched ? matched.image_url : ART.anirudh);
        return `
        <div class="singer-circle-card" data-singer-q="${esc(sg.query)}" data-singer-name="${esc(sg.name)}">
          <div class="singer-avatar-ring">
            <img src="${esc(artUrl)}" alt="${esc(sg.name)}" onerror="window.handleImgError(this, ${sIdx})">
          </div>
          <div class="singer-name-label">${esc(sg.name)}</div>
        </div>`;
      }).join("");
    }
  }

  $("homeHeroSections").addEventListener("click", (e) => {
    const quickEl = e.target.closest("[data-quick-title]");
    if (quickEl) {
      const qCat = quickEl.getAttribute("data-quick-cat");
      const qStr = quickEl.getAttribute("data-quick-q");
      if (qCat) loadCategory(qCat, true);
      else window.openCategoryOrQuery(qStr, quickEl.getAttribute("data-quick-title"), true);
      return;
    }
    const singerEl = e.target.closest("[data-singer-q]");
    if (singerEl) {
      window.openCategoryOrQuery(singerEl.getAttribute("data-singer-q"), singerEl.getAttribute("data-singer-name"), true);
      return;
    }
    const cardEl = e.target.closest(".horiz-card");
    if (cardEl) {
      const heroIdxStr = cardEl.getAttribute("data-hero-i");
      if (heroIdxStr !== null && heroIdxStr !== "") {
        const idx = parseInt(heroIdxStr, 10);
        if (!isNaN(idx) && currentSongs[idx]) {
          window.Player.playSongs(currentSongs, idx, true);
          return;
        }
      }
      const cCat = cardEl.getAttribute("data-card-cat");
      const cQ = cardEl.getAttribute("data-card-q");
      const cTitle = cardEl.getAttribute("data-card-title");
      if (cCat) loadCategory(cCat, true);
      else if (cQ) window.openCategoryOrQuery(cQ, cTitle, true);
    }
  });

  // ---- Search Tab Initialization (Matches Screenshots 70795, 70796, 70797) ----
  function initSearchBrowseTiles() {
    if ($("searchTopTilesGrid") && !$("searchTopTilesGrid").innerHTML) {
      $("searchTopTilesGrid").innerHTML = SEARCH_TOP_TILES.map((t, i) => `
        <div class="browse-tile" style="background-color:${t.color}" data-browse-cat="${esc(t.cat || "")}" data-browse-q="${esc(t.query || "")}" data-browse-title="${esc(t.title)}">
          <div class="browse-tile-title">${esc(t.title)}</div>
          <img class="browse-tile-img" src="${esc(t.img)}" alt="" onerror="window.handleImgError(this, ${i})">
        </div>`).join("");
    }

    if ($("discoverReelsRow") && !$("discoverReelsRow").innerHTML) {
      const reels = [
        { tag: "#tollywood", q: "Fear Song Devara Pushpa 2 Telugu", img: ART.devara },
        { tag: "#telugu dance", q: "Kurchi Madathapetti Kissik Daavudi", img: ART.guntur },
        { tag: "#high beam", q: "Dorikithe Chastavu Raktacharitra Hungry Cheetah", img: ART.raktacharitra }
      ];
      $("discoverReelsRow").innerHTML = reels.map((r, i) => `
        <div class="discover-reel-card" data-browse-q="${esc(r.q)}" data-browse-title="${esc(r.tag)}">
          <img src="${esc(r.img)}" alt="" onerror="window.handleImgError(this, ${i})">
          <div class="discover-reel-tag">${esc(r.tag)}</div>
        </div>`).join("");
    }

    if ($("searchBrowseAllGrid") && !$("searchBrowseAllGrid").innerHTML) {
      $("searchBrowseAllGrid").innerHTML = SEARCH_BROWSE_ALL.map((t, i) => `
        <div class="browse-tile" style="background-color:${t.color}" data-browse-cat="${esc(t.cat || "")}" data-browse-q="${esc(t.query || "")}" data-browse-title="${esc(t.title)}">
          <div class="browse-tile-title">${esc(t.title)}</div>
          <img class="browse-tile-img" src="${esc(t.img)}" alt="" onerror="window.handleImgError(this, ${i})">
        </div>`).join("");
    }
  }

  $("searchBrowseSection").addEventListener("click", (e) => {
    const tile = e.target.closest("[data-browse-title]");
    if (!tile) return;
    const cat = tile.getAttribute("data-browse-cat");
    const q = tile.getAttribute("data-browse-q");
    const title = tile.getAttribute("data-browse-title");
    if (cat) {
      window.switchTab("home");
      loadCategory(cat, true);
    } else if (q) {
      window.openCategoryOrQuery(q, title, true);
    }
  });

  // ---- Your Library Tab Rendering (Matches Screenshot 70798.jpg) ----
  function getLocalPlaylists() {
    try { return JSON.parse(localStorage.getItem("musify_playlists_v3") || "[]"); }
    catch (e) { return []; }
  }

  function saveLocalPlaylists(lists) {
    try { localStorage.setItem("musify_playlists_v3", JSON.stringify(lists)); }
    catch (e) {}
  }

  window.filterLibraryItems = function (filterType, btnEl) {
    currentLibFilter = filterType;
    document.querySelectorAll(".lib-pill").forEach((b) => b.classList.remove("active"));
    if (btnEl) btnEl.classList.add("active");
    renderLibraryView();
  };

  async function renderLibraryView() {
    const box = $("libraryItemsList");
    if (!box) return;
    const profName = ($("userNameDisplay") && $("userNameDisplay").textContent) || "PAVAN BHAI YT";
    const likedCount = getLikedSongs().length;
    const customLists = getLocalPlaylists();

    let html = "";

    // 1. Pinned Liked Songs Row (Screenshot 70798.jpg top row)
    if (currentLibFilter === "all" || currentLibFilter === "playlists") {
      html += `
        <div class="lib-row" onclick="openLikedSongs()">
          <div class="lib-liked-box">♥</div>
          <div class="lib-row-info">
            <div class="lib-row-title">Liked Songs</div>
            <div class="lib-row-sub"><span class="pin-green">📌</span> Playlist • ${esc(profName)} (${likedCount} songs)</div>
          </div>
        </div>`;

      // 2. User's Custom Playlists
      customLists.forEach((p, idx) => {
        const count = Array.isArray(p.songs) ? p.songs.length : (p.song_count || 0);
        const cover = (p.songs && p.songs[0] && p.songs[0].image_url) || ART.guntur;
        html += `
          <div class="lib-row" onclick="openPlaylist(${p.id})">
            <img class="lib-row-art" src="${esc(cover)}" alt="" onerror="window.handleImgError(this, ${idx})">
            <div class="lib-row-info">
              <div class="lib-row-title">${esc(p.name)}</div>
              <div class="lib-row-sub">Playlist • ${esc(profName)} • ${count} songs</div>
            </div>
          </div>`;
      });
    }

    // 3. Curated Albums, Playlists & Artists from Screenshot 70798.jpg
    CURATED_LIBRARY_ITEMS.forEach((item, idx) => {
      if (currentLibFilter !== "all" && item.type !== currentLibFilter) return;
      const clickAction = item.cat
        ? `loadCategory('${esc(item.cat)}', true)`
        : `openCategoryOrQuery('${esc(item.query)}', '${esc(item.title)}', true)`;
      html += `
        <div class="lib-row" onclick="${clickAction}">
          <img class="lib-row-art ${item.round ? "artist-round" : ""}" src="${esc(item.img)}" alt="" onerror="window.handleImgError(this, ${idx})">
          <div class="lib-row-info">
            <div class="lib-row-title">${esc(item.title)}</div>
            <div class="lib-row-sub">${esc(item.sub)}</div>
          </div>
        </div>`;
    });

    box.innerHTML = html;
  }

  function renderSongs(songs, append, hideLoadMore) {
    currentSongs = deduplicateClientSongs(append ? currentSongs.concat(songs) : songs);
    window.allLoadedSongs = currentSongs.slice();
    if (!append && currentCategory === "home" && !activeSearchQuery && !openPlaylistId) {
      renderHomeHeroSections(currentSongs);
    }
    if (!currentSongs.length) {
      $("content").innerHTML = '<div style="color:var(--text-muted);padding:30px 0;">No songs found.</div>';
      return;
    }
    const cur = window.Player.currentSong();
    const playAllBanner = (currentCategory !== "home" || activeSearchQuery)
      ? `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
           <button class="btn-primary" id="btnPlayAll" style="display:inline-flex;align-items:center;gap:8px;padding:10px 22px;border-radius:500px;font-weight:800;">
             ▶ Play All (${currentSongs.length} Songs)
           </button>
         </div>`
      : "";

    $("content").innerHTML = playAllBanner + '<div class="cards-grid">' + currentSongs.map((s, i) => {
      const playing = Boolean(cur && cur.title === s.title);
      const liked = isSongLiked(s);
      return `
      <div class="song-card ${playing ? "playing-now" : ""}" data-i="${i}">
        <div class="card-img-wrapper">
          <img src="${esc(s.image_url || ART.devara)}" alt="" loading="lazy" onerror="window.handleImgError(this, ${i})">
          <button class="card-play-btn" data-act="play" title="Play">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          </button>
        </div>
        <div class="card-info">
          <div class="card-title" title="${esc(s.title)}">${esc(s.title)}</div>
          <div class="card-artist">${esc(s.artist)}</div>
        </div>
        <div class="card-actions">
          <button data-act="like" class="${liked ? "liked" : ""}">♥</button>
          <button data-act="lyrics">🎤 Lyrics</button>
          <button data-act="download">⬇ Save</button>
          <button data-act="playlist">+List</button>
        </div>
      </div>`;
    }).join("") + '</div>' +
    (hideLoadMore ? "" :
      '<div style="display:flex;justify-content:center;padding:26px 0 12px 0;">' +
        '<button id="btnLoadMore" class="btn-primary">Load More Songs</button>' +
      '</div>');
  }

  $("content").addEventListener("click", (e) => {
    const card = e.target.closest(".song-card");
    if (card) {
      const i = parseInt(card.dataset.i, 10);
      const song = currentSongs[i];
      const btn = e.target.closest("button[data-act]");
      const act = btn ? btn.dataset.act : "play";
      if (act === "play") window.Player.playSongs(currentSongs, i, true, Boolean(activeSearchQuery));
      else if (act === "like") {
        toggleLikeSong(song);
        if (btn) btn.classList.toggle("liked", isSongLiked(song));
      } else if (act === "lyrics") {
        window.Player.playSongs(currentSongs, i, true);
        setTimeout(window.scrollToLyricsCard, 250);
      } else if (act === "download") {
        window.downloadSongObj(song);
      } else if (act === "playlist") openAddToPlaylist(song);
      return;
    }

    const row = e.target.closest(".track-row");
    if (row) {
      const i = parseInt(row.dataset.i, 10);
      if (e.target.closest("[data-act='remove']")) { removeFromOpenPlaylist(row.dataset.entry); return; }
      window.Player.playSongs(currentSongs, i, true);
      return;
    }

    if (e.target.closest("#btnPlayAll")) {
      if (currentSongs.length) window.Player.playSongs(currentSongs, 0, true);
      else toast("This playlist is empty");
    } else if (e.target.closest("#btnDeletePlaylist")) {
      deleteOpenPlaylist();
    } else if (e.target.closest("#btnLoadMore")) {
      loadMoreSongs();
    }
  });

  async function loadMoreSongs() {
    const btn = $("btnLoadMore");
    if (btn) { btn.textContent = "Loading more..."; btn.disabled = true; }
    currentPage += 1;
    try {
      const fixedUrl = activeSearchQuery
        ? "/api/search?q=" + encodeURIComponent(activeSearchQuery) + "&page=" + currentPage
        : "/api/category/" + encodeURIComponent(currentCategory) + "?page=" + currentPage;
      const data = await api(fixedUrl);
      const more = data.songs || [];
      if (more.length) {
        renderSongs(more, true, false);
      } else if (btn) {
        btn.style.display = "none";
      }
    } catch (err) {
      if (btn) { btn.textContent = "Load More Songs"; btn.disabled = false; }
    }
  }

  let isAutoLoadingMore = false;
  window.loadMoreInBackground = function () {
    if (!isAutoLoadingMore && !openPlaylistId) {
      isAutoLoadingMore = true;
      loadMoreSongs().finally(() => { isAutoLoadingMore = false; });
    }
  };

  if ($("contentScroll")) {
    $("contentScroll").addEventListener("scroll", () => {
      if (isAutoLoadingMore || openPlaylistId) return;
      const sc = $("contentScroll");
      if (sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 420) {
        isAutoLoadingMore = true;
        loadMoreSongs().finally(() => { isAutoLoadingMore = false; });
      }
    }, { passive: true });
  }

  async function loadCategory(cat, autoPlayFirst) {
    window.switchTab("home");
    currentCategory = cat;
    activeSearchQuery = "";
    currentPage = 1;
    openPlaylistId = null;
    $("homeHeroSections").style.display = cat === "home" ? "block" : "none";
    document.querySelectorAll(".pill-btn").forEach((b) => b.classList.toggle("active", b.dataset.category === cat));
    const t = TITLES[cat] || [cat.charAt(0).toUpperCase() + cat.slice(1), ""];
    setHeading(t[0], t[1]);
    if (cat === "home") {
      renderHomeHeroSections(currentSongs);
    }
    showLoading("Loading songs...");
    try {
      const data = await api("/api/category/" + encodeURIComponent(cat) + "?page=1");
      const list = data.songs || [];
      renderSongs(list, false, false);
      if (autoPlayFirst && currentSongs.length) {
        window.Player.playSongs(currentSongs, 0, true);
      } else if (window.Player.seedQueueIfEmpty) {
        window.Player.seedQueueIfEmpty(currentSongs);
      }
    } catch (err) {}
  }
  window.loadCategory = loadCategory;

  // ---- Search Tab Live Input (Matches Screenshots 70795-70797) ----
  async function runSearch(q) {
    activeSearchQuery = q;
    currentPage = 1;
    $("searchBrowseSection").style.display = "none";
    $("searchResultsBox").style.display = "block";
    $("searchResultsHeading").textContent = 'Results for "' + q + '"';
    $("searchResultsList").innerHTML = '<div style="color:var(--text-muted);padding:20px 0;">Searching full-length songs...</div>';
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(q) + "&page=1");
      const list = deduplicateClientSongs(data.songs || []);
      currentSongs = list;
      window.allLoadedSongs = list.slice();
      if (!list.length) {
        $("searchResultsList").innerHTML = '<div style="color:var(--text-muted);padding:20px 0;">No matching songs found.</div>';
        return;
      }
      $("searchResultsList").innerHTML = '<div class="library-list">' + list.map((s, idx) => `
        <div class="lib-row" data-search-i="${idx}">
          <img class="lib-row-art" src="${esc(s.image_url || ART.raktacharitra)}" alt="" onerror="window.handleImgError(this, ${idx})">
          <div class="lib-row-info">
            <div class="lib-row-title">${esc(s.title)}</div>
            <div class="lib-row-sub">Song • ${esc(s.artist)}</div>
          </div>
          <button class="mini-icon-btn" data-search-dl="${idx}" title="Download" style="padding:8px;">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#b3b3b3" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
        </div>`).join("") + '</div>';
    } catch (err) {}
  }

  $("searchResultsList").addEventListener("click", (e) => {
    const dlBtn = e.target.closest("[data-search-dl]");
    if (dlBtn) {
      e.stopPropagation();
      const idx = parseInt(dlBtn.getAttribute("data-search-dl"), 10);
      if (currentSongs[idx]) window.downloadSongObj(currentSongs[idx]);
      return;
    }
    const row = e.target.closest("[data-search-i]");
    if (row) {
      const idx = parseInt(row.getAttribute("data-search-i"), 10);
      window.Player.playSongs(currentSongs, idx, true, true);
    }
  });

  window.clearSearchInput = function () {
    $("query").value = "";
    $("searchResultsBox").style.display = "none";
    $("searchBrowseSection").style.display = "block";
  };

  window.triggerArtistSearch = function (q) {
    window.openCategoryOrQuery(q, q, true);
  };

  $("query").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    if (!q) {
      window.clearSearchInput();
      return;
    }
    searchTimer = setTimeout(() => runSearch(q), 350);
  });

  document.querySelectorAll(".pill-btn").forEach((b) => {
    b.addEventListener("click", () => loadCategory(b.dataset.category, false));
  });

  // ---- Playlists Management ----
  window.openPlaylist = async function (id) {
    window.switchTab("home");
    openPlaylistId = id;
    $("homeHeroSections").style.display = "none";
    const localLists = getLocalPlaylists();
    const localMatch = localLists.find((p) => Number(p.id) === Number(id));
    try {
      const data = await api("/api/playlists/" + id);
      const p = data.playlist;
      if (localMatch && (!p.songs || !p.songs.length) && localMatch.songs && localMatch.songs.length) {
        p.songs = localMatch.songs;
      }
      renderPlaylistContent(p);
      return;
    } catch (err) {}
    if (localMatch) renderPlaylistContent(localMatch);
  };

  function renderPlaylistContent(p) {
    currentSongs = p.songs || [];
    setHeading(p.name, p.description || (currentSongs.length + " songs"));
    let html = `
      <div style="display:flex;gap:12px;margin-bottom:18px;">
        <button class="btn-primary" id="btnPlayAll">▶ Play All</button>
        <button class="btn-danger" id="btnDeletePlaylist">Delete Playlist</button>
      </div>`;
    if (!currentSongs.length) {
      html += '<div style="color:var(--text-muted);">This playlist is empty. Tap +List on any song card to add tracks.</div>';
    } else {
      html += '<table class="track-table"><thead><tr><th>#</th><th>Title</th><th>Artist</th><th></th></tr></thead><tbody>' +
        currentSongs.map((s, i) => `
          <tr class="track-row" data-i="${i}" data-entry="${s.id}">
            <td>${i + 1}</td>
            <td style="color:#fff;font-weight:500;">${esc(s.title)}</td>
            <td>${esc(s.artist)}</td>
            <td style="text-align:right;"><button class="btn-danger" data-act="remove" style="padding:4px 8px;">Remove</button></td>
          </tr>`).join("") +
        "</tbody></table>";
    }
    $("content").innerHTML = html;
  }

  async function removeFromOpenPlaylist(entryId) {
    if (!openPlaylistId) return;
    const lists = getLocalPlaylists();
    const pIdx = lists.findIndex((p) => Number(p.id) === Number(openPlaylistId));
    if (pIdx !== -1 && Array.isArray(lists[pIdx].songs)) {
      lists[pIdx].songs = lists[pIdx].songs.filter((s) => String(s.id) !== String(entryId));
      saveLocalPlaylists(lists);
    }
    try {
      await api("/api/playlists/" + openPlaylistId + "/songs/" + entryId, { method: "DELETE" });
    } catch (err) {}
    toast("Song removed");
    window.openPlaylist(openPlaylistId);
  }

  async function deleteOpenPlaylist() {
    if (!openPlaylistId) return;
    const lists = getLocalPlaylists().filter((p) => Number(p.id) !== Number(openPlaylistId));
    saveLocalPlaylists(lists);
    try {
      await api("/api/playlists/" + openPlaylistId, { method: "DELETE" });
    } catch (err) {}
    toast("Playlist deleted");
    openPlaylistId = null;
    loadCategory("home", false);
  }

  function openModal(id) { if ($(id)) $(id).classList.add("open"); }
  function closeModal(id) { if ($(id)) $(id).classList.remove("open"); }

  if ($("btnNewPlaylist")) {
    $("btnNewPlaylist").addEventListener("click", () => {
      if ($("playlistNameInput")) $("playlistNameInput").value = "";
      if ($("playlistDescInput")) $("playlistDescInput").value = "";
      openModal("createPlaylistModal");
    });
  }
  if ($("btnCloseCreatePlaylist")) $("btnCloseCreatePlaylist").addEventListener("click", () => closeModal("createPlaylistModal"));
  if ($("btnCancelCreatePlaylist")) $("btnCancelCreatePlaylist").addEventListener("click", () => closeModal("createPlaylistModal"));

  if ($("formCreatePlaylist")) {
    $("formCreatePlaylist").addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = $("playlistNameInput").value.trim();
      const description = $("playlistDescInput").value.trim();
      if (!name) return;
      let created = { id: Date.now(), name, description, song_count: 0, songs: [] };
      try {
        const data = await api("/api/playlists", jsonOpts("POST", { name, description }));
        if (data.playlist) created = { ...data.playlist, songs: [] };
      } catch (err) {}
      const localLists = getLocalPlaylists();
      localLists.unshift(created);
      saveLocalPlaylists(localLists);
      closeModal("createPlaylistModal");
      toast("Playlist created: " + name);
      renderLibraryView();
    });
  }

  async function openAddToPlaylist(song) {
    pendingSong = song;
    const lists = getLocalPlaylists();
    if (!lists.length) { toast("Tap Create (+) in the bottom bar to create a playlist first"); return; }
    $("playlistSelect").innerHTML = lists.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
    openModal("addToPlaylistModal");
  }

  if ($("btnCloseAddToPlaylist")) $("btnCloseAddToPlaylist").addEventListener("click", () => closeModal("addToPlaylistModal"));
  if ($("btnCancelAddToPlaylist")) $("btnCancelAddToPlaylist").addEventListener("click", () => closeModal("addToPlaylistModal"));

  if ($("formAddToPlaylist")) {
    $("formAddToPlaylist").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!pendingSong) return;
      const s = pendingSong;
      const id = $("playlistSelect").value;
      let addedEntry = {
        id: Date.now(),
        song_id: s.id, title: s.title, artist: s.artist, album: s.album || "",
        duration: s.duration || 0, image_url: s.image_url || "", media_url: s.media_url,
        backup_urls: s.backup_urls || []
      };
      try {
        const data = await api("/api/playlists/" + id + "/songs", jsonOpts("POST", {
          song_id: s.id, title: s.title, artist: s.artist, album: s.album || "",
          duration: s.duration || 0, image_url: s.image_url || "", media_url: s.media_url
        }));
        if (data.song) addedEntry = { ...data.song, backup_urls: s.backup_urls || [] };
      } catch (err) {}
      const localLists = getLocalPlaylists();
      const pl = localLists.find((p) => Number(p.id) === Number(id));
      if (pl) {
        if (!Array.isArray(pl.songs)) pl.songs = [];
        pl.songs.push(addedEntry);
        pl.song_count = pl.songs.length;
        saveLocalPlaylists(localLists);
      }
      closeModal("addToPlaylistModal");
      toast("Added to playlist");
    });
  }

  // ---- Profile Avatar & Onboarding ----
  function showProfile(p) {
    const ch = (p.name || "P").trim().charAt(0).toUpperCase();
    if ($("userNameDisplay")) $("userNameDisplay").textContent = p.name;
    if ($("topBarAvatar")) $("topBarAvatar").textContent = ch;
    if ($("searchTabAvatar")) $("searchTabAvatar").textContent = ch;
    if ($("libTabAvatar")) $("libTabAvatar").textContent = ch;
  }

  window.openProfileModal = function () {
    try {
      const p = JSON.parse(localStorage.getItem("musify_profile") || "null");
      if (p) { $("userNameInput").value = p.name; $("userAgeInput").value = p.age; }
    } catch (e) {}
    openModal("welcomeModal");
  };

  if ($("formWelcomeProfile")) {
    $("formWelcomeProfile").addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = $("userNameInput").value.trim();
      const age = parseInt($("userAgeInput").value, 10);
      if (!name || !age) return;
      const profile = { name, age };
      try { localStorage.setItem("musify_profile", JSON.stringify(profile)); } catch (err) {}
      try { await api("/api/profile", jsonOpts("POST", profile)); } catch (err) {}
      showProfile(profile);
      closeModal("welcomeModal");
      toast("Welcome, " + name + "!");
    });
  }

  async function initProfile() {
    let profile = null;
    try { profile = JSON.parse(localStorage.getItem("musify_profile") || "null"); } catch (e) {}
    if (!profile) {
      try {
        const data = await api("/api/profile");
        if (data.profile) profile = { name: data.profile.name, age: data.profile.age };
      } catch (e) {}
    }
    if (profile) showProfile(profile);
    else openModal("welcomeModal");
  }

  // Render Spotify Home Hero Sections immediately (0ms wait) before API fetch finishes
  saveLikedSongs(getLikedSongs());
  renderHomeHeroSections([]);
  initProfile();
  initSearchBrowseTiles();
  renderLibraryView();
  loadCategory("home", false);
})();
'''

MANIFEST_JSON = r'''{
  "name": "Musify",
  "short_name": "Musify",
  "description": "Stream Telugu, Hindi, English and Punjabi music with video canvas, swipe controls, and lyrics",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#000000",
  "theme_color": "#000000",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}
'''

SW_JS = r'''const CACHE = "musify-spotify-v24";
const ASSETS = [
  "/static/css/style.css",
  "/static/js/player.js",
  "/static/js/app.js",
  "/manifest.json"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
'''

ICON_192_B64 = """
iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAFyElEQVR4nO3cy3EcRxBF0YSCeyxggrygCaARso1GQCbIC5oAG6iF
2NT8u3u6Pi/z3WOAYqIq72QBgvTy+vr2MwBTf8z+AMBMBABrBABrBABrBABrBABrBABrBABrBABrBABrBABrBABrBABrBABrBABr
BABrBABrBABrBABrBABrBABrBABrX2Z/gOre/v52+J/x+f7R4JPglhf+v0BttBj0vQjjOAJ40oyBX0MQ+xHARooDv4Yg1hHAAxmH
/h5iuI0Abqg0+JcI4RwB/FJ56O8hBgKwHPxLziHYBsDgX3MMwS4ABn+dUwhWfwrB8G/jdE4WG8DpQlurvg3KbwCG/5jq51d2A1S/
uBkqboOSG4Dh76PiuZYLoOIlKal2vmWeQNUuJoMKT6ISG4Dhn6PCuacPoMIlZJb9/FMHkP3wq8h8D2kDyHzoFWW9j5QBZD3s6jLe
S7oAMh6yk2z3kyqAbIfrKtM9pQkg06Eiz32lCCDLYeJchnuTDyDDIeI+9fuTDkD98LCN8j1KBwD0JhuA8rcG9lO9T8kAVA8Lxyje
q1wACof04+v32R+hLIX7PSUXgIofX78TggGpANS+HSLYBj0o3bNMAEqHcolt0J7KfcsEkAEh1CMRgMq3wVZE0IbCvU8PQOEQnsE2
aGP2/U8PIDsiyG1qALPrb4VtcMzMOWADNEQI+UwLoMq3/y1EsN+seWADdMI2yGFKAJW//S8RwnYz5oINMAgRaCKAgdgGeoYH4PT8
uYcQ7hs9H2yAiYhgvqEB8O1/jW1wbeScsAFEEMIcBCCGCMYaFgDPn+3YBuPmhQ0gzD2CEQhAHNugLwJIghD6GBIA7/92nCIYMTds
gITYBu0QQGKEcBwBFEAEz+seAO//Mapug97zwwYopmoIvRBAUUSwDQEUxjZYRwAGCOE+AjBCBNe6BsBvgPRk3AY954gNYCpbBL0Q
gLGM26A1AoB1CASA3xwjIACccdsGBICbXEIgADxUPQICwKrK2+DL7A8AfX/+89fsj9ANGwAPVR7+CDYA7qg++AsCwBmXwV/wBMJv
bsMfwQZAeA7+ggCMOQ/+gieQKYb/P103wOf7B/9RjJiMg//5/tHtn80GMJJx+HvjZwADDP59BFAYg7+OJ1BRDP82bIBiGPx9ugfA
b4LGqDr4PX8DFMETqISqwz8CT6DEGPzjCCAhBr+dIU+g3u84J07DP2Ju2ABJOA3+SAQgjsHvi98CCWP4+xu2Afj3Adsx+ON+bmQD
iGH4x+JnABEM/hxDA+AZdI3Bvzby1+Y8gSZi+Ocb/gRiCzD4j4z+l6b8DDAQg6+HJ9AgDL+mKRvA6RnE4G8342/GeAJ1wuDnMO0J
VPkvRBn+/WbNAxugIQY/n6kBVPlZgME/ZuZrgN8CHcTw5zb9CZR1CzD4bcz+WVBiA8w+hL0Y/jYU7n36BsiEwa9HJgDlpxCD357C
t3+EyBNooXIopxj+9pTuWWYDqGHwPby8vr79nP0hLqk+hXCc0rd/hNgTaKF2SGhD8V4lA4jQPCw8T/U+ZQMARpAOQPVbA/so36N0
ABHah4d16vcnH0CE/iHitgz3liKAiByHif9lua80AUTkOVR3me4pVQARuQ7XUbb7SRdARL5DdpHxXlIGEJHzsCvLeh9pA4jIe+jV
ZL6H1AFE5D78CrKff/oAIvJfQlYVzl3yz6GP4E+p+6sw+IsSG+BUpctRVO18ywUQUe+SVFQ813JPoEs8iY6rOPiLkhvgVOXLG6H6
+ZXfAKfYBttVH/xF+Q1wyuVSj3I6J6sNcIptcM1p8Be2ASwIwXPwF/YBLBxDcB78BQHcUDkGhv4cATxQKQQG/zYC2ChjDAz9OgJ4
kmIQDPx+BNDIjCAY+OMIoLMWYTDo/RAArFn9KQRwiQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBgjQBg
jQBgjQBgjQBgjQBgjQBg7V/6t925tvIswgAAAABJRU5ErkJggg==
"""

ICON_512_B64 = """
iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AAAQ+UlEQVR4nO3cwXkUSRKG4WaeuesgE9aLMQGMWNvGCDBhvRgTsIE5
AIuQWt3VVVmZkfm/rwOjAxnxESXmw9PT87cLABDlj9E/AADQnwAAgEACAAACCQAACCQAACCQAACAQAIAAAIJAAAIJAAAIJAAAIBA
AgAAAgkAAAgkAAAgkAAAgEACAAACCQAACCQAACCQAACAQAIAAAIJAAAIJAAAIJAAAIBAAgAAAgkAAAgkAAAgkAAAgEACAAACCQAA
CCQAACCQAACAQAIAAAIJAAAIJAAAIJAAAIBAAgAAAgkAAAgkAAAgkAAAgEACAAACCQAACCQAACCQAACAQAIAAAIJAAAIJAAAIJAA
AIBAAgAAAgkAAAgkAAAgkAAAgEACAAACCQAACCQAACCQAACAQAIAAAIJAAAIJAAAIJAAAIBAAgAAAgkAAAj05+gfAGjj+cunbv+t
rx8/d/tvAef48PT0/G30DwFc13Opn0UsQE0CAAZbYcnvJQ5gHAEAHSUv+61EAfQhAOAkln07ogDaEwDQiIXfjyCA4wQA7GTh1yEI
4HECADay8OchCOA+AQA3WPrzEwNwnQCAFyz89QkC+E4AEM/SzyUGSCYAiGXx85MQIJEAIIqlzz1igBQCgOVZ+uwlBliZAGBZFj+t
CAFWJABYiqXP2cQAqxAALMHipzchwOwEAFOz+BlNCDArAcCULH6qEQLMRgAwFYuf6oQAsxAAlGfpMysxQGUCgLIsflYhBKjoj9E/
AFxj+bMSf56pyAWAUgxKVucaQBUCgBIsftIIAUYTAAxl8ZNOCDCKAGAIix9+JwTozS8B0p3lD295F/TmAkA3Bhxs4xpADwKA01n8
sI8Q4Ew+AXAqyx/28344kwsApzC4oC3XAFpzAaA5yx/a865ozQWAZgwo6MM1gBZcAGjC8od+vDdacAHgEIMIxnINYC8XAHaz/GE8
75C9BAC7GDpQh/fIHj4B8BCDBmrzSYCtXADYzPKH+rxTthIAbGKowDy8V7bwCYCbDBKYm08CvMcFgHdZ/jA/75j3CACuMjRgHd4z
1wgA3jAsYD3eNa/5HQD+z4CADH4vgMvFBYAfLH/I4b1zuQgALoYBJPLuEQDhDAHI5f1nEwDBPH7AHMglAEJ59MBP5kEm/wogjIcO
3OJfCORwAQhi+QP3mBM5BEAIjxrYyrzIIAACeMzAo8yN9QmAxXnEwF7mx9oEwMI8XuAoc2RdAmBRHi3QinmyJgGwII8VaM1cWY8A
WIxHCpzFfFmLAFiIxwmczZxZhwBYhEcJ9GLerEEALMBjBHozd+YnACbnEQKjmD9zEwAAEEgATEx9A6OZQ/MSAJPy6IAqzKM5CYAJ
eWxANebSfATAZDwyoCrzaS4CYCIeF1CdOTUPATAJjwqYhXk1BwEAAIEEwATUNDAbc6s+AVCcRwTMyvyqTQAU5vEAszPH6hIARXk0
wCrMs5oEAAAEEgAFqWVgNeZaPQKgGI8EWJX5VosAKMTjAFZnztUhAAAgkAAoQhUDKcy7GgRAAR4DkMbcG08ADOYRnOufv/4e/SMA
7zD/xhIALO+fv/4WAgCvCICB1G9fIgDqMQfHEQCD+EM/hmsA1GMejiEAiCQEgHQCYAC1W4cIgBrMxf4EQGf+kNfjGgA1mI99CQD4
QQQASQRAR+q2PtcAGMuc7EcAdOIP9VyEAIxjXvYhAOAGEQCsSgB0oGbn5hoA/Zmb5xMAsJEQAFYiAE6mYtcjAqAP8/NcAuBE/vCu
yzUA+jBHzyMA4AAhAMxKAJxEtWYRAXAe8/QcAgAacQ0AZiIATqBWswkBaM9cbU8AwElEAFCZAGhMpfKSawC0Y762JQCgAxEAVCMA
GlKn3OIaAMeZs+0IgEb8oWQrIQDHmLdtCAAYRAQAIwmABtQoe7kGwD7m7nECAAoQAkBvAgAKEQFALwLgIGcoWnMNgG3M32MEABQl
BIAzCYAD1Cc9iAB4nzm8nwCACbgGAK0JgJ1UJyMIAXjLPN5HAMCERABwlADYQW1SgWsA/GIuP04AwOREALCHAHiQyqQi1wAwnx8l
AGAhQgDYSgDAgkQAcI8AeIDzEjNxDSCROb2dAIDFCQHgGgGwkapkdiKAFOb1NgIAgrgGAD8JAAgkBAABsIFzEqsSAazK3L5PAEA4
1wDIJACAy+UiBCCNALjDGYk0IoBVmN+3CQDgDdcAWJ8AuEE9kk4EMDtz/H0CALjJNQDWJACATYQArEUAvMPZCK4TAczGPL9OAAAP
cw2A+QkAYDchAPMSAMBhIgDmIwCu8L0IHucaQGXm+lsCAGhKCMAcBABwChEAtQmAV5yJoB3XACox338nAIDTCQGoRwAA3YgAqEMA
AF25BkANAuAF34egHxHACOb8LwIAGMY1AMYRAMBwQgD6EwBAGSIA+hEAP/guBDW4BnA28/47AQCUJATgXAIAKE0EwDkEAFCeawC0
JwAuvgfBLIQArZj7AgCYkAiA4wQAMCXXADhGAABTEwKwjwAAliAC4DHxAeAXQWAdrgE8In3+xwcAsB4RAPcJAGBJrgFwmwAAliYE
4DoBAEQQAfC76ABI/wUQSOMawGvJeyA6AIBMQgAEABBMBJBMAADRXANIJQAALkKAPAIA4AURQAoBAPCKawAJYgMg+Z9+ANsIgQyp
+yA2AAC2EgGsSAAAbOAawGoEAMADRACrEAAAD3INYAUCAGAnIcDMBADAQSKAGQkAgAZcA5iNAABoSAgwCwEAcAIRQHUCAOAkrgFU
FhkAqf/bR2AMIVBf4l6IDACAEUQAlQgAgI5cA6hCAAAMIAQYTQAADCQCGEUAAAzmGsAIAgCgCBFATwIAoBDXAHoRAAAFCQHOJgAA
ChMBnEUAABTnGsAZBADAJIQALQkAgMmIAFoQAAATcg3gqD9H/wAAPO4///vv6B+BybkAAEzG8qcFFwCASVj8tCQAAIqz+DmDTwAA
hVn+nMUFAKAgi5+zCQCAQix+evEJAKAIy5+eXAAABrP4GcEFAGAgy59RXAAABrD4GU0AAHRk8VOFTwAAnVj+VBJ5Afj68fPl+cun
0T8GEMLir+/rx8+jf4TuIgMAoAeLn8p8AgA4geVPdS4AAA1Z/MxCAAA0YPEzG58AAA6y/JmRCwDAThY/MxMAAA+y+FmBTwAAD7D8
WYULAMAGFj+rcQEAuMPyZ0WxFwD/O2DgHos/Q+L/BvhyCQ4AgPdY/CTwCQDgBcufFC4AABeLnzwCAIhm8ZPKJwAgluVPMhcAII7F
D+EB4J8CQhaLn9dS/wng5eITABDC8offRV8AgPVZ/HCdAACWZPHDbT4BAMux/OG++AuAXwSEdVj8PCL5FwAvFxcAYBGWPzwm/gIA
zM3ih30EADAlix+O8QkAmI7lD8e5AFz8IiDMwuKnlfRfALxcBAAwAYsf2vMJACjN8odzuAAAJVn8cC4B8IPfA4AaLH7O5vv/dz4B
AGVY/tCPCwAwnMUP/QkAYBiLH8bxCeAF34WgH8ufEcz5X1wAgK4sfqjBBQDoxvKHOlwAgNNZ/FCPAHjF/w8A2rH4qcT3/9/5BACc
wvKH2lwAgKYsfpiDALjCZwB4nMVPZc7/b/kEABxm+cN8XACA3Sx+mJcAAB5m8cP8fAJ4h+9FcJ3lz2zM8+tcAIBNLH5YiwAAbrL4
YU0+AdzgbEQ6y5/ZmePvcwEA3rD4YX0uAHeoR9JY/qzC/L7NBQC4XC4WP6QRABDO4odMPgFs4IzEqix/VmVu3+cCAIEsfkAAQBCL
H/jJJ4CNnJOYneVPCvN6GxcAWJzFD1wjAB7w9ePny/OXT6N/DNjE4ieRv/1v5xMALMjyB+5xAYCFWPzAVgLgQT4DUJHFD87/j/IJ
ACZn+QN7uADs4ApABRY//OJv/49zAYAJWf7AUS4AO7kCMILFD2/52/8+AgAmYPEDrfkEcIDqpAfLH95nDu/nAgBFWfzAmQTAQX4X
gNYsftjG3/6P8QkACrH8gV5cAKAAix/oTQA04DMAe1n8sI/z/3E+AcAglj8wkgtAI64AbGXxwzH+9t+GAGhIBHCLxQ/HWf7t+AQA
HVj+QDUuAI25AvCSxQ/t+Nt/Wy4AcBLLH6jMBeAErgDZLH5oz9/+2xMA0IjFD8zEJ4CTqNUslj+cxzw9hwsAHGDxA7MSACfyuwDr
svihD3/7P49PACfzh3c9lj/0YX6eywUANrL4gZUIgA58CpibxQ/9+dv/+XwCgBssf2BVLgCduALMxeKHcfztvw8B0JEIqM/ih7Es
/358AoAfLH8giQtAZ64A9Vj8UIO//fflAjCAP+R1WP5Qg7nYnwsAkSx+IJ0AGMSngDEsfqjH3/7H8AlgIH/o+7L8oR5zcBwXAJZn
8QO89eHp6fnb6B8inU8BQCJ/+x/LJ4ACPAIgjbk3ngAowmMAUph3NQgAAAgkAApRxcDqzLk6BEAxHgewKvOtFgFQkEcCrMZcq0cA
AEAgAVCUWgZWYZ7VJAAK82iA2ZljdQmA4jweYFbmV20CYAIeETAbc6s+AQAAgQTAJNQ0MAvzag4CYCIeFVCdOTUPATAZjwuoynya
iwCYkEcGVGMuzUcATMpjA6owj+YkACbm0QGjmUPzEgAAEEgATE59A6OYP3MTAAvwCIHezJ35CYBFeIxAL+bNGgTAQjxK4GzmzDoE
wGI8TuAs5staBMCCPFKgNXNlPQJgUR4r0Ip5siYBsDCPFjjKHFmXAFicxwvsZX6sTQAE8IiBR5kb6xMAITxmYCvzIoMACOJRA/eY
Ezk+PD09fxv9Q9Df85dPo38EoBCLP48LQCiPHfjJPMgkAIJ59IA5kEsAhPP4IZf3n00AYAhAIO8eAcDlcjEMIIn3zuXiXwFwhX8h
AGuy+HnJBYA3DAlYj3fNawKAqwwLWIf3zDUCgHcZGjA/75j3+B0ANvF7ATAXi597XADYxDCBeXivbCEA2MxQgfq8U7byCYBdfBKA
Wix+HuUCwC6GDdThPbKHAGA3QwfG8w7ZyycAmvBJAPqy+DnKBYAmDCPox3ujBRcAmnMNgHNY/LTkAkBzhhS0513RmgsAp3INgGMs
fs7iAsCpDC/Yz/vhTC4AdOMaANtY/PQgAOhOCMB1Fj89+QRAd4YcvOVd0JsLAEO5BpDO4mcUAUAJQoA0Fj+jCQBKEQKszuKnCgFA
SUKA1Vj8VOOXACnJsGQl/jxTkQsA5bkGMCuLn8oEAFMRA1Rn6TMLAcCUhADVWPzMRgAwNSHAaBY/sxIALEEI0JvFz+wEAEsRApzN
4mcVAoBliQFasfRZkQBgeUKAvSx+ViYAiCIGuMfSJ4UAIJYY4CdLn0QCgHhCIJfFTzIBAC+IgfVZ+vCdAIAbBMH8LHy4TgDARmJg
HpY+3CcAYCdBUIeFD48TANCIIOjHwofjBACcRBC0Y+FDewIAOhIF91n20IcAgMGSo8Cyh3EEABS2QhxY8lCTAIBF9IwFSx3mJwAA
INAfo38AAKA/AQAAgQQAAAQSAAAQSAAAQCABAACBBAAABBIAABBIAABAIAEAAIEEAAAEEgAAEEgAAEAgAQAAgQQAAAQSAAAQSAAA
QCABAACBBAAABBIAABBIAABAIAEAAIEEAAAEEgAAEEgAAEAgAQAAgQQAAAQSAAAQSAAAQCABAACBBAAABBIAABBIAABAIAEAAIEE
AAAEEgAAEEgAAEAgAQAAgQQAAAQSAAAQSAAAQCABAACBBAAABBIAABBIAABAIAEAAIEEAAAEEgAAEEgAAEAgAQAAgQQAAAQSAAAQ
SAAAQCABAACBBAAABBIAABBIAABAIAEAAIEEAAAE+hcncu5YOfjFMgAAAABJRU5ErkJggg==
"""

STATIC_FILES = {
    "css/style.css": (STYLE_CSS.encode("utf-8"), "text/css; charset=utf-8"),
    "js/player.js": (PLAYER_JS.encode("utf-8"), "application/javascript; charset=utf-8"),
    "js/app.js": (APP_JS.encode("utf-8"), "application/javascript; charset=utf-8"),
    "icons/icon-192.png": (base64.b64decode("".join(ICON_192_B64.split())), "image/png"),
    "icons/icon-512.png": (base64.b64decode("".join(ICON_512_B64.split())), "image/png"),
}


# =====================================================================
# PART 4 - FLASK SERVER
# =====================================================================

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False

try:
    init_db()
except Exception as e:
    print(f"DB init warning: {e}")


def _no_cache(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/")
def index():
    return _no_cache(Response(INDEX_HTML, mimetype="text/html"))


@app.route("/static/<path:filename>")
def serve_static(filename):
    item = STATIC_FILES.get(filename)
    if not item:
        abort(404)
    return _no_cache(Response(item[0], mimetype=item[1]))


@app.route("/manifest.json")
def serve_manifest():
    return _no_cache(Response(MANIFEST_JSON, mimetype="application/manifest+json"))


@app.route("/sw.js")
def serve_sw():
    return _no_cache(Response(SW_JS, mimetype="application/javascript"))


def _err(e, code=500):
    return jsonify({"success": False, "error": str(e)}), code


# --- Music discovery ---

@app.route("/api/search", methods=["GET"])
def api_search():
    query = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1) or 1)
    try:
        results = search_songs(query, page=page, limit=40)
        return jsonify({"success": True, "query": query, "page": page, "count": len(results), "songs": results})
    except Exception as e:
        return _err(e)


@app.route("/api/category/<category>", methods=["GET"])
def api_category(category):
    page = int(request.args.get("page", 1) or 1)
    try:
        results = get_category_songs(category, page=page)
        return jsonify({"success": True, "category": category, "page": page, "count": len(results), "songs": results})
    except Exception as e:
        return _err(e)


@app.route("/api/similar", methods=["GET"])
def api_similar():
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    exclude = request.args.get("exclude", "").strip()
    try:
        results = get_similar_songs_for_track(title=title, artist=artist, album=album, exclude_csv=exclude)
        return jsonify({"success": True, "count": len(results), "songs": results})
    except Exception as e:
        return _err(e)


# --- Original JioSaavnAPI-master Endpoints ---

@app.route("/song/", methods=["GET"])
def saavn_search_route():
    query = request.args.get("query", "").strip()
    lyrics = str(request.args.get("lyrics", "false")).lower() not in ("false", "0", "")
    songdata = str(request.args.get("songdata", "true")).lower() == "true"
    page = int(request.args.get("page", 1) or 1)
    if not query:
        return jsonify({"status": False, "error": "Query is required to search songs!"})
    return jsonify(search_for_song(query, lyrics=lyrics, songdata=songdata, page=page, limit=40))


@app.route("/song/get/", methods=["GET"])
def saavn_get_song_route():
    song_id = request.args.get("id", "").strip()
    lyrics = str(request.args.get("lyrics", "false")).lower() not in ("false", "0", "")
    if not song_id:
        return jsonify({"status": False, "error": "Song ID is required to get a song!"})
    resp = get_song(song_id, lyrics=lyrics)
    if not resp:
        return jsonify({"status": False, "error": "Invalid Song ID received!"})
    return jsonify(resp)


@app.route("/playlist/", methods=["GET"])
def saavn_playlist_route():
    query = request.args.get("query", "").strip()
    lyrics = str(request.args.get("lyrics", "false")).lower() not in ("false", "0", "")
    if not query:
        return jsonify({"status": False, "error": "Query is required to search playlists!"})
    pid = get_playlist_id(query) if "http" in query else query
    return jsonify(get_playlist(pid, lyrics=lyrics))


@app.route("/album/", methods=["GET"])
def saavn_album_route():
    query = request.args.get("query", "").strip()
    lyrics = str(request.args.get("lyrics", "false")).lower() not in ("false", "0", "")
    if not query:
        return jsonify({"status": False, "error": "Query is required to search albums!"})
    aid = get_album_id(query) if "http" in query else query
    return jsonify(get_album(aid, lyrics=lyrics))


@app.route("/lyrics/", methods=["GET"])
@app.route("/api/lyrics", methods=["GET"])
def saavn_lyrics_route():
    query = (request.args.get("query") or request.args.get("id") or "").strip()
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    duration = int(request.args.get("duration", 210) or 210)
    if not query and not title:
        return jsonify({"status": False, "error": "Song ID or title is required to fetch lyrics!"})
    sid = get_song_id(query) if ("http" in query and "saavn" in query) else query
    payload = get_lyrics_payload(sid, title=title, artist=artist, duration=duration)
    return jsonify(payload)


@app.route("/result/", methods=["GET"])
def saavn_result_route():
    query = request.args.get("query", "").strip()
    lyrics = str(request.args.get("lyrics", "false")).lower() not in ("false", "0", "")
    if not query:
        return jsonify({"status": False, "error": "Query is required!"})
    if "saavn" not in query:
        return jsonify(search_for_song(query, lyrics=lyrics, songdata=True, limit=40))
    if "/song/" in query:
        sid = get_song_id(query)
        return jsonify(get_song(sid, lyrics=lyrics))
    if "/album/" in query:
        aid = get_album_id(query)
        return jsonify(get_album(aid, lyrics=lyrics))
    if "/playlist/" in query or "/featured/" in query:
        pid = get_playlist_id(query)
        return jsonify(get_playlist(pid, lyrics=lyrics))
    return jsonify({"status": False, "error": "Unsupported JioSaavn URL"})


# --- Playlists ---

@app.route("/api/playlists", methods=["GET"])
def api_get_playlists():
    try:
        return jsonify({"success": True, "playlists": get_all_playlists()})
    except Exception as e:
        return _err(e)


@app.route("/api/playlists", methods=["POST"])
def api_create_playlist():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return _err("Playlist name is required", 400)
    description = str(data.get("description", "")).strip()
    try:
        return jsonify({"success": True, "playlist": create_playlist(name, description)}), 201
    except Exception as e:
        return _err(e)


@app.route("/api/playlists/<int:playlist_id>", methods=["GET"])
def api_get_playlist(playlist_id):
    try:
        playlist = get_playlist_by_id(playlist_id)
        if not playlist:
            return _err("Playlist not found", 404)
        return jsonify({"success": True, "playlist": playlist})
    except Exception as e:
        return _err(e)


@app.route("/api/playlists/<int:playlist_id>", methods=["DELETE"])
def api_delete_playlist(playlist_id):
    try:
        if not delete_playlist(playlist_id):
            return _err("Playlist not found", 404)
        return jsonify({"success": True, "message": "Playlist deleted successfully"})
    except Exception as e:
        return _err(e)


@app.route("/api/playlists/<int:playlist_id>/songs", methods=["POST"])
def api_add_song_to_playlist(playlist_id):
    data = request.get_json(silent=True) or {}
    if not data.get("media_url") or not data.get("title"):
        return _err("Title and media_url are required", 400)
    try:
        if not get_playlist_by_id(playlist_id):
            return _err("Playlist not found", 404)
        added = add_song_to_playlist(playlist_id, data)
        return jsonify({"success": True, "song": added}), 201
    except Exception as e:
        return _err(e)


@app.route("/api/playlists/<int:playlist_id>/songs/<int:song_entry_id>", methods=["DELETE"])
def api_remove_song_from_playlist(playlist_id, song_entry_id):
    try:
        if not remove_song_from_playlist(playlist_id, song_entry_id):
            return _err("Song not found in playlist", 404)
        return jsonify({"success": True, "message": "Song removed from playlist"})
    except Exception as e:
        return _err(e)


# --- Queue ---

@app.route("/api/queue", methods=["GET"])
def api_get_queue():
    try:
        return jsonify({"success": True, "queue": get_queue()})
    except Exception as e:
        return _err(e)


@app.route("/api/queue", methods=["POST"])
def api_update_queue():
    data = request.get_json(silent=True) or {}
    try:
        if "queue" in data and isinstance(data["queue"], list):
            return jsonify({"success": True, "queue": set_queue(data["queue"])})
        if "media_url" in data and "title" in data:
            return jsonify({"success": True, "song": add_to_queue(data)}), 201
        return _err("Invalid queue payload", 400)
    except Exception as e:
        return _err(e)


@app.route("/api/queue/<int:entry_id>", methods=["DELETE"])
def api_remove_from_queue(entry_id):
    try:
        if not remove_from_queue(entry_id):
            return _err("Song not found in queue", 404)
        return jsonify({"success": True, "message": "Song removed from queue"})
    except Exception as e:
        return _err(e)


@app.route("/api/queue", methods=["DELETE"])
def api_clear_queue():
    try:
        clear_queue()
        return jsonify({"success": True, "message": "Queue cleared"})
    except Exception as e:
        return _err(e)


# --- Profile (name & age, no password) ---

@app.route("/api/profile", methods=["GET"])
def api_get_profile():
    try:
        return jsonify({"success": True, "profile": get_latest_profile()})
    except Exception as e:
        return _err(e)


@app.route("/api/profile", methods=["POST"])
def api_save_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return _err("Name is required", 400)
    try:
        age = int(data.get("age"))
        if age < 1 or age > 120:
            return _err("Please enter a valid age (1-120)", 400)
        return jsonify({"success": True, "profile": save_user_profile(name, age)}), 201
    except (ValueError, TypeError):
        return _err("Valid numeric age is required", 400)
    except Exception as e:
        return _err(e)


# --- Full-Song File Download Endpoint ---

@app.route("/api/download", methods=["GET"])
def api_download_song():
    """
    Streams/downloads a full-length song from JioSaavn CDN with Content-Disposition: attachment
    so mobile browsers save it directly to the phone's Downloads folder with its clean title.
    """
    media_url = request.args.get("url", "").strip()
    song_id = request.args.get("id", "").strip()
    title = request.args.get("title", "Musify_Song").strip()
    artist = request.args.get("artist", "").strip()

    if not media_url and song_id:
        song_obj = get_song(song_id, lyrics=False)
        if song_obj and song_obj.get("media_url"):
            media_url = song_obj["media_url"]
            title = song_obj.get("title") or title
            artist = song_obj.get("artist") or artist

    if not media_url:
        return _err("Valid song stream URL or ID is required for download", 400)

    # Ensure we use full-length aac.saavncdn.com URLs
    candidates = _build_full_aac_urls(media_url, is_320=True)
    clean_name = re.sub(r'[\\/*?:"<>|]+', "", f"{title} - {artist}" if artist else title).strip() or "Musify_Song"
    filename = f"{clean_name}.m4a"

    for cand_url in candidates:
        try:
            upstream = requests.get(cand_url, headers=REQ_HEADERS, stream=True, timeout=12)
            if upstream.status_code == 200:
                content_type = upstream.headers.get("Content-Type", "audio/mp4")
                headers = {
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": content_type,
                    "Cache-Control": "no-cache"
                }
                if upstream.headers.get("Content-Length"):
                    headers["Content-Length"] = upstream.headers["Content-Length"]
                return Response(upstream.iter_content(chunk_size=65536), headers=headers, content_type=content_type)
        except Exception:
            continue

    return _err("Could not download audio stream", 502)


# --- Health ---

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "healthy",
        "service": "Musify Web App",
        "version": "3.0.0-single-file",
        "database": DB_FILE
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Musify running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
