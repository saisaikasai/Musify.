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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.jiosaavn.com/"
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


def format_song(data: Dict[str, Any], lyrics: bool = False) -> Dict[str, Any]:
    """Formats a raw JioSaavn song dictionary and decrypts its 320kbps/160kbps stream URL."""
    is_320 = str(data.get("320kbps", "")).lower() == "true"
    try:
        media_url = decrypt_url(data["encrypted_media_url"])
        if not is_320:
            media_url = media_url.replace("_320.mp4", "_160.mp4")
        preview_url = (
            media_url.replace("_320.mp4", "_96_p.mp4")
            .replace("_160.mp4", "_96_p.mp4")
            .replace("//aac.", "//preview.")
        )
        data["media_url"] = _ensure_https(media_url)
        data["media_preview_url"] = _ensure_https(preview_url)
    except (KeyError, TypeError, Exception):
        url = _ensure_https(data.get("media_preview_url", ""))
        url = url.replace("preview", "aac")
        if is_320:
            url = url.replace("_96_p.mp4", "_320.mp4")
        else:
            url = url.replace("_96_p.mp4", "_160.mp4")
        data["media_url"] = url

    # Build ordered backup stream URLs (320 -> 160 -> 96 -> preview)
    base_u = data.get("media_url", "")
    backup_urls = []
    if base_u:
        backup_urls.append(base_u)
        for br in ["_160.mp4", "_96.mp4", "_320.mp4"]:
            for orig in ["_320.mp4", "_160.mp4", "_96.mp4", "_96_p.mp4"]:
                if orig in base_u:
                    cand = base_u.replace(orig, br).replace("preview.saavncdn.com", "aac.saavncdn.com")
                    if cand not in backup_urls:
                        backup_urls.append(cand)
    if data.get("media_preview_url") and data["media_preview_url"] not in backup_urls:
        backup_urls.append(data["media_preview_url"])

    data["backup_urls"] = backup_urls
    data["song"] = format_str(data.get("song") or data.get("title") or "Unknown Title")
    data["music"] = format_str(data.get("music", ""))
    data["singers"] = format_str(data.get("singers", ""))
    data["starring"] = format_str(data.get("starring", ""))
    data["album"] = format_str(data.get("album", ""))
    data["primary_artists"] = format_str(data.get("primary_artists") or data.get("singers") or data.get("music") or "Various Artists")
    data["image"] = _ensure_https(str(data.get("image", "")).replace("150x150", "500x500"))

    # Also set standard Musify keys so both JioSaavnAPI and Musify UI work seamlessly
    data["title"] = data["song"]
    data["artist"] = data["primary_artists"]
    data["image_url"] = data["image"]
    try:
        data["duration"] = int(data.get("duration") or 210)
    except (ValueError, TypeError):
        data["duration"] = 210

    if lyrics:
        if str(data.get("has_lyrics", "")).lower() == "true":
            data["lyrics"] = get_lyrics(data.get("id", ""))
        else:
            data["lyrics"] = None

    try:
        data["copyright_text"] = str(data.get("copyright_text", "")).replace("&copy;", "©")
    except Exception:
        pass
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


def get_lyrics(song_id: str, title: str = "", artist: str = "") -> Optional[str]:
    """Fetches lyrics from JioSaavnAPI-master endpoint first, then falls back to LRCLIB API."""
    if song_id:
        try:
            url = lyrics_base_url + urllib.parse.quote(str(song_id))
            lyrics_json = requests.get(url, headers=REQ_HEADERS, timeout=5).text
            lyrics_data = json.loads(lyrics_json)
            lyr = lyrics_data.get("lyrics")
            if lyr and len(str(lyr).strip()) > 10:
                return str(lyr)
        except Exception:
            pass

    # Fallback: Query LRCLIB by title & artist for synced/plain lyrics
    if title:
        try:
            q_params = urllib.parse.urlencode({"q": f"{title} {artist}".strip()})
            lrc_resp = requests.get(f"https://lrclib.net/api/search?{q_params}", headers=REQ_HEADERS, timeout=5)
            if lrc_resp.status_code == 200:
                items = lrc_resp.json()
                if isinstance(items, list) and items:
                    best = items[0]
                    plain = best.get("plainLyrics") or best.get("syncedLyrics")
                    if plain:
                        return str(plain).replace("\n", "<br>")
        except Exception:
            pass
    return None


def search_for_song(query: str, lyrics: bool = False, songdata: bool = True, page: int = 1, limit: int = 40) -> Any:
    """
    Searches JioSaavn using both search.getResults (paginated, up to 40 songs per page)
    and autocomplete.get + song.getDetails from JioSaavnAPI-master.
    """
    clean_q = query.strip()
    if clean_q.startswith("http") and "saavn.com" in clean_q:
        sid = get_song_id(clean_q)
        return get_song(sid, lyrics) if sid else []

    collected_ids = []

    # 1. Paginated search.getResults (returns up to 40 song IDs per page!)
    try:
        sr_url = f"{search_results_base_url}&n={limit}&p={page}&q={urllib.parse.quote(clean_q)}"
        sr_resp = requests.get(sr_url, headers=REQ_HEADERS, timeout=6)
        sr_data = _parse_saavn_json(sr_resp.text)
        if isinstance(sr_data, dict):
            for item in sr_data.get("results", []):
                if isinstance(item, dict) and item.get("id"):
                    sid = str(item["id"])
                    if sid not in collected_ids:
                        collected_ids.append(sid)
    except Exception as e:
        logger.debug(f"search.getResults error: {e}")

    # 2. Also query autocomplete.get from JioSaavnAPI-master on page 1
    if page == 1 and len(collected_ids) < 15:
        try:
            ac_url = search_base_url + urllib.parse.quote(clean_q)
            ac_resp = requests.get(ac_url, headers=REQ_HEADERS, timeout=6)
            ac_data = _parse_saavn_json(ac_resp.text)
            song_response = ac_data.get("songs", {}).get("data", [])
            if not songdata:
                return song_response
            for s in song_response:
                sid = str(s.get("id", ""))
                if sid and sid not in collected_ids:
                    collected_ids.append(sid)
        except Exception as e:
            logger.debug(f"autocomplete.get error: {e}")

    if collected_ids:
        songs = get_songs_batch(collected_ids, lyrics=lyrics)
        if songs:
            return songs

    return []


def _search_saavn_mirrors(query: str, page: int = 1, limit: int = 40) -> List[Dict[str, Any]]:
    """Fallback to public JioSaavn mirrors if direct jiosaavn.com is geo-blocked."""
    for mirror in SAAVN_MIRRORS:
        url = f"{mirror}/search/songs?query={urllib.parse.quote(query)}&page={page}&limit={limit}"
        try:
            resp = requests.get(url, headers=REQ_HEADERS, timeout=6)
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
                    urls = []
                    if isinstance(dls, list):
                        for d in reversed(dls):
                            u = d.get("url") or d.get("link") if isinstance(d, dict) else str(d)
                            if u:
                                urls.append(_ensure_https(u))
                    elif isinstance(dls, str):
                        urls.append(_ensure_https(dls))
                    if not urls:
                        continue
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
                    results.append({
                        "id": str(raw.get("id", "")),
                        "title": format_str(raw.get("name") or raw.get("title") or "Unknown Title"),
                        "artist": artist_str,
                        "album": album_str,
                        "duration": int(raw.get("duration") or 210),
                        "image_url": _ensure_https(img_u) or FALLBACK_CATALOG[0]["image_url"],
                        "media_url": urls[0],
                        "backup_urls": urls
                    })
                if results:
                    return results
        except Exception:
            pass
    return []


def search_songs(query: str, page: int = 1, limit: int = 40) -> List[Dict[str, Any]]:
    """
    Main search entrypoint for Musify:
    1. Official JioSaavnAPI-master engine (search.getResults + autocomplete.get + song.getDetails + pyDes)
    2. Public JioSaavn Mirrors (with pagination)
    3. Fallback catalog
    """
    clean_q = query.strip()
    if not clean_q:
        return get_category_songs("home", page=page)

    # Tier 1: JioSaavnAPI-master direct engine
    saavn_songs = search_for_song(clean_q, lyrics=False, songdata=True, page=page, limit=limit)
    if isinstance(saavn_songs, list) and saavn_songs:
        return saavn_songs
    elif isinstance(saavn_songs, dict) and saavn_songs.get("media_url"):
        return [saavn_songs]

    # Tier 2: Public JioSaavn Mirrors
    mirror_songs = _search_saavn_mirrors(clean_q, page=page, limit=limit)
    if mirror_songs:
        return mirror_songs

    return FALLBACK_CATALOG


def get_category_songs(category: str, page: int = 1) -> List[Dict[str, Any]]:
    """Fetches 40+ songs per page for Home, Telugu, Hindi, English, and Punjabi categories."""
    cat = category.lower().strip()
    category_queries = {
        "home": ["Latest Telugu Hindi Top Hits", "Trending India Songs", "Top Hits 2025"],
        "trending": ["Trending Songs India", "Top Chartbusters", "Viral Hits"],
        "telugu": ["Telugu Top Hits", "Sid Sriram Thaman DSP Telugu Songs", "Latest Telugu Movie Songs"],
        "hindi": ["Hindi Top Hits", "Arijit Singh Pritam Bollywood Hits", "Latest Hindi Songs"],
        "english": ["English Top Hits", "The Weeknd Ed Sheeran Taylor Swift Dua Lipa", "Pop Hits"],
        "punjabi": ["Punjabi Top Hits", "AP Dhillon Diljit Dosanjh Karan Aujla", "Latest Punjabi Songs"]
    }
    queries = category_queries.get(cat, [f"{cat} top songs"])
    selected_q = queries[(page - 1) % len(queries)]
    sub_page = ((page - 1) // len(queries)) + 1

    results = search_songs(selected_q, page=sub_page, limit=40)
    return results if results else FALLBACK_CATALOG


# =====================================================================
# PART 3 - SPOTIFY UI, LYRICS PANEL & FULL AUDIO ENGINE (HTML, CSS, JS)
# =====================================================================

INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
  <title>Musify • Spotify Edition</title>
  <link rel="stylesheet" href="/static/css/style.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%231db954'><circle cx='12' cy='12' r='11'/><polygon points='9.5 7.5 16.5 12 9.5 16.5 9.5 7.5' fill='%23000000'/></svg>">
</head>
<body>

  <!-- Hidden HTML5 Audio Streamer -->
  <audio id="audio" preload="metadata" crossorigin="anonymous"></audio>

  <!-- Spotify 2-Panel Desktop / Full-Screen Mobile Layout -->
  <div class="app-container">

    <!-- Left Sidebar (Spotify Navigation + Your Library) -->
    <aside class="sidebar" id="sidebar">
      <!-- Top Nav Box -->
      <div class="spotify-box nav-box">
        <div class="brand">
          <div class="brand-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="#000">
              <polygon points="6 4 20 12 6 20 6 4"/>
            </svg>
          </div>
          <div class="brand-name">Musify</div>
          <span class="pro-pill">FREE</span>
        </div>

        <nav class="nav-section">
          <a class="nav-link active" id="navHome" onclick="loadCategory('home')">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
            <span>Home</span>
          </a>
          <a class="nav-link" id="navSearch" onclick="focusSearchInput()">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <span>Search</span>
          </a>
          <a class="nav-link" id="navLiked" onclick="openLikedSongs()">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="#1db954" stroke="#1db954" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
            <span>Liked Songs</span>
            <span id="likedCountBadge" class="song-badge">0</span>
          </a>
        </nav>
      </div>

      <!-- Bottom Library Box -->
      <div class="spotify-box library-box">
        <div class="playlist-header">
          <div style="display:flex; align-items:center; gap:8px;">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
            <span>Your Library</span>
          </div>
          <button id="btnNewPlaylist" class="btn-icon-small" title="Create New Playlist">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>

        <!-- Passwordless User Profile Card (Name & Age) -->
        <div id="userProfileCard" class="user-profile-card" onclick="openProfileModal()" title="Tap to edit Name & Age">
          <div id="userAvatar" class="user-avatar">M</div>
          <div class="user-profile-info">
            <div id="userNameDisplay" class="user-name-display">Guest Listener</div>
            <div id="userAgeDisplay" class="user-age-display">Tap to set Name & Age</div>
          </div>
        </div>

        <!-- User Playlists Container -->
        <div id="sidebarPlaylists" class="sidebar-playlists"></div>
      </div>
    </aside>

    <!-- Main Spotify Rounded View -->
    <main class="main-area" id="mainArea">

      <!-- Top Sticky Header -->
      <header class="top-bar">
        <button id="btnMobileMenu" class="mobile-menu-btn" title="Open Library">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>

        <div class="search-box">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" id="query" placeholder="What do you want to play?" autocomplete="off">
        </div>

        <div class="category-pills">
          <button class="pill-btn active" data-category="home">All</button>
          <button class="pill-btn" data-category="telugu">Telugu</button>
          <button class="pill-btn" data-category="hindi">Hindi</button>
          <button class="pill-btn" data-category="english">English</button>
          <button class="pill-btn" data-category="punjabi">Punjabi</button>
          <button class="pill-btn" data-category="trending">Trending</button>
        </div>
      </header>

      <!-- Scrollable Main Content -->
      <section class="content-scroll" id="contentScroll">
        <!-- Quick 6-Tile Spotify Greeting Row -->
        <div id="quickTilesSection" class="quick-tiles-section">
          <h2 id="greetingTitle" class="greeting-title">Good Evening</h2>
          <div class="quick-tiles-grid">
            <div class="quick-tile" onclick="loadCategory('telugu')">
              <img src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80" alt="Telugu">
              <span>Telugu Blockbusters</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
            <div class="quick-tile" onclick="loadCategory('hindi')">
              <img src="https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=200&auto=format&fit=crop&q=80" alt="Hindi">
              <span>Bollywood Top 50</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
            <div class="quick-tile" onclick="loadCategory('english')">
              <img src="https://images.unsplash.com/photo-1508700115892-45ecd05ae2ad?w=200&auto=format&fit=crop&q=80" alt="English">
              <span>Global English Hits</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
            <div class="quick-tile" onclick="loadCategory('punjabi')">
              <img src="https://images.unsplash.com/photo-1487180144351-b8472da7d491?w=200&auto=format&fit=crop&q=80" alt="Punjabi">
              <span>Punjabi Party Anthems</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
            <div class="quick-tile" onclick="openLikedSongs()">
              <div class="liked-tile-icon">♥</div>
              <span>Liked Songs</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
            <div class="quick-tile" onclick="loadCategory('trending')">
              <img src="https://images.unsplash.com/photo-1470225620780-dba8ba36b745?w=200&auto=format&fit=crop&q=80" alt="Trending">
              <span>Viral India Hits</span>
              <span class="tile-play"><svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg></span>
            </div>
          </div>
        </div>

        <div class="view-header">
          <h1 id="heading" class="view-heading">Trending Now</h1>
          <p id="subheading" class="view-subheading">Tap any song to play immediately or use Next/Prev to switch tracks</p>
        </div>

        <!-- Dynamic Song Grid / Playlist View -->
        <div id="content"></div>
      </section>

      <!-- Spotify Full-Screen / Slide-Over Lyrics & Now Playing Panel -->
      <div id="lyricsBox" class="lyrics-panel">
        <div class="lyrics-top-bar">
          <div class="lyrics-song-meta">
            <img id="lyricsThumb" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80" alt="">
            <div>
              <div id="lyricsSongTitle" class="lyrics-song-title">No Song Playing</div>
              <div id="lyricsSongArtist" class="lyrics-song-artist">Pick a track to view lyrics</div>
            </div>
          </div>
          <div style="display:flex; align-items:center; gap:10px;">
            <button class="ctrl-btn" onclick="window.Player.prev()" title="Previous Song">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5" stroke="currentColor" stroke-width="2.2"/></svg>
            </button>
            <button class="ctrl-btn" onclick="window.Player.next()" title="Next Song">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2.2"/></svg>
            </button>
            <button id="btnCloseLyrics" class="btn-icon-small" title="Close Lyrics">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
        </div>
        <div id="lyricsContent" class="lyrics-content">
          Select and play any song to view live lyrics here.
        </div>
      </div>

      <!-- Slide-Over Queue Drawer -->
      <div id="queuePanel" class="queue-panel">
        <div class="queue-header">
          <h3>Queue • Up Next</h3>
          <div style="display:flex; gap:8px;">
            <button id="btnClearQueue" class="btn-icon-small" title="Clear Queue">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
            <button id="btnCloseQueue" class="btn-icon-small" title="Close Queue">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
        </div>
        <div id="queueList" class="queue-list"></div>
      </div>

    </main>
  </div>

  <!-- Spotify Bottom Persistent Player Bar -->
  <footer class="bottom-player">
    <!-- Left: Now Playing Info + Like Heart -->
    <div class="player-left">
      <img id="playerImg" class="player-thumb" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500&auto=format&fit=crop&q=80" alt="Album Cover" onclick="toggleLyricsPanel()">
      <div class="player-track-info" onclick="toggleLyricsPanel()">
        <div id="playerTitle" class="player-title">No song playing</div>
        <div id="playerArtist" class="player-artist">Tap any song to start</div>
      </div>
      <button id="btnLikeCurrent" class="ctrl-btn like-btn" title="Save to Liked Songs">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
      </button>
    </div>

    <!-- Center: Full Song Switching Controls (Shuffle, Prev, -10s, Play/Pause, +10s, Next, Repeat) -->
    <div class="player-center">
      <div class="player-controls">
        <button id="btnShuffle" class="ctrl-btn" title="Shuffle (S)">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>
        </button>

        <button id="btnPrev" class="ctrl-btn" title="Previous Song (P)">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5" stroke="currentColor" stroke-width="2.2"/></svg>
        </button>

        <button id="btnSeekBack" class="ctrl-btn hide-mobile" title="Rewind 10s">
          <span style="font-size:11px;font-weight:700;">-10s</span>
        </button>

        <button id="btnPlay" class="ctrl-btn ctrl-btn-play" title="Play / Pause (Space)">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
        </button>

        <button id="btnSeekFwd" class="ctrl-btn hide-mobile" title="Forward 10s">
          <span style="font-size:11px;font-weight:700;">+10s</span>
        </button>

        <button id="btnNext" class="ctrl-btn" title="Next Song (N)">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2.2"/></svg>
        </button>

        <button id="btnRepeat" class="ctrl-btn" title="Repeat (R)">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
        </button>
      </div>

      <!-- Scrubber Seek Bar -->
      <div class="scrubber-container">
        <span id="currentTime" class="time-label">0:00</span>
        <input type="range" id="progressBar" class="range-slider" min="0" max="100" step="0.1" value="0">
        <span id="totalDuration" class="time-label">0:00</span>
      </div>
    </div>

    <!-- Right: Lyrics Button, Queue Drawer & Volume -->
    <div class="player-right">
      <button id="btnLyricsToggle" class="ctrl-btn lyrics-toggle-btn" title="View Song Lyrics (L)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
        <span class="lyrics-btn-label">Lyrics</span>
      </button>

      <button id="btnQueueToggle" class="ctrl-btn" title="Queue">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
        <span id="queueBadge" class="badge-count">0</span>
      </button>

      <div class="volume-container">
        <button id="btnVolume" class="ctrl-btn" title="Mute / Unmute">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
        </button>
        <input type="range" id="volumeBar" class="range-slider" min="0" max="1" step="0.01" value="0.85">
      </div>
    </div>
  </footer>

  <!-- Spotify Mobile Bottom Tab Bar (Visible on Mobile Screens) -->
  <nav class="mobile-bottom-nav">
    <button class="mob-tab active" onclick="loadCategory('home')">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      <span>Home</span>
    </button>
    <button class="mob-tab" onclick="focusSearchInput()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <span>Search</span>
    </button>
    <button class="mob-tab" onclick="toggleMobileSidebar()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
      <span>Library</span>
    </button>
    <button class="mob-tab" onclick="toggleLyricsPanel()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
      <span>Lyrics</span>
    </button>
  </nav>

  <!-- Modal: Create Playlist -->
  <div id="createPlaylistModal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>Create Playlist</h3>
        <button id="btnCloseCreatePlaylist" class="btn-icon-small">&times;</button>
      </div>
      <form id="formCreatePlaylist">
        <div class="form-group">
          <label for="playlistNameInput">Playlist Name *</label>
          <input type="text" id="playlistNameInput" placeholder="e.g. Telugu Vibes, Gym Workout" required>
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

  <!-- Modal: Add Song to Playlist -->
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

  <!-- Modal: Quick Welcome Onboarding (Name & Age Only - No Login Needed) -->
  <div id="welcomeModal" class="modal-overlay">
    <div class="modal-card welcome-card">
      <div class="welcome-badge">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
      </div>
      <h2 class="welcome-title">Welcome to Musify</h2>
      <p class="welcome-subtitle">No login or password needed! Enter your name and age to unlock unlimited Telugu, Hindi, English & Punjabi streaming with live lyrics.</p>
      <form id="formWelcomeProfile">
        <div class="form-group">
          <label for="userNameInput">Your Name *</label>
          <input type="text" id="userNameInput" placeholder="e.g. Karthik, Priya, Rahul" required autocomplete="name">
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

  <!-- Toast Notification Container -->
  <div id="toastContainer" class="toast-container"></div>

  <!-- Scripts -->
  <script src="/static/js/player.js"></script>
  <script src="/static/js/app.js"></script>
  <script>
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      });
    }
  </script>
</body>
</html>
'''

STYLE_CSS = r'''/* ==========================================================================
   Musify • Authentic Spotify Dark UI Stylesheet (#000000 / #121212 / #1DB954)
   ========================================================================== */

:root {
  --bg-black: #000000;
  --bg-main: #121212;
  --bg-elevated: #181818;
  --bg-card: #181818;
  --bg-card-hover: #282828;
  --bg-tinted: rgba(255, 255, 255, 0.07);
  --bg-tinted-hover: rgba(255, 255, 255, 0.14);
  --border-color: rgba(255, 255, 255, 0.08);
  --text-main: #ffffff;
  --text-muted: #b3b3b3;
  --text-dim: #727272;
  --accent: #1db954;
  --accent-hover: #1ed760;
  --accent-glow: rgba(29, 185, 84, 0.35);
  --danger: #f15e6c;
  --sidebar-width: 280px;
  --player-height: 90px;
  --queue-width: 350px;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --transition: all 0.2s cubic-bezier(0.3, 0, 0.4, 1);
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: "Circular Std", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-color: var(--bg-black);
  color: var(--text-main);
  overflow: hidden;
  height: 100vh;
  display: flex;
  flex-direction: column;
  user-select: none;
}

/* --- Spotify Split Panel Container --- */
.app-container {
  display: flex;
  flex: 1;
  gap: 8px;
  padding: 8px 8px 0 8px;
  height: calc(100vh - var(--player-height));
  position: relative;
  overflow: hidden;
}

/* --- Left Sidebar (2 Stacked Rounded Spotify Boxes) --- */
.sidebar {
  width: var(--sidebar-width);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}

.spotify-box {
  background-color: var(--bg-main);
  border-radius: var(--radius-md);
  padding: 16px;
}

.nav-box {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.library-box {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 2px 6px;
}

.brand-icon {
  width: 34px;
  height: 34px;
  background-color: var(--accent);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 14px var(--accent-glow);
}

.brand-name {
  font-size: 21px;
  font-weight: 800;
  letter-spacing: -0.6px;
  color: #fff;
}

.pro-pill {
  font-size: 9px;
  font-weight: 800;
  background: var(--bg-tinted);
  color: var(--accent);
  padding: 2px 6px;
  border-radius: 99px;
  letter-spacing: 0.6px;
}

.nav-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  text-decoration: none;
  font-size: 14.5px;
  font-weight: 700;
  cursor: pointer;
  transition: var(--transition);
}

.nav-link:hover, .nav-link.active {
  color: var(--text-main);
  background-color: var(--bg-tinted);
}

.nav-link .song-badge {
  margin-left: auto;
  background: var(--bg-tinted);
  color: var(--text-main);
  padding: 2px 8px;
  border-radius: 99px;
  font-size: 11px;
}

.playlist-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 4px 12px 4px;
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 700;
}

.btn-icon-small {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 6px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: var(--transition);
  font-size: 18px;
}

.btn-icon-small:hover {
  color: var(--text-main);
  background-color: var(--bg-tinted);
}

.sidebar-playlists {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.playlist-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
}

.playlist-item:hover, .playlist-item.active {
  color: var(--text-main);
  background-color: var(--bg-card-hover);
}

.playlist-item.active {
  color: var(--accent);
}

/* --- Main Rounded Content Panel --- */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #1f2a24 0%, var(--bg-main) 290px);
  border-radius: var(--radius-md);
  overflow: hidden;
  position: relative;
}

.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 24px;
  background: rgba(18, 18, 18, 0.78);
  backdrop-filter: blur(16px);
  z-index: 10;
}

.search-box {
  display: flex;
  align-items: center;
  background-color: #242424;
  border: 1px solid transparent;
  border-radius: 999px;
  padding: 9px 16px;
  width: 340px;
  transition: var(--transition);
}

.search-box:focus-within {
  background-color: #2a2a2a;
  border-color: #ffffff;
}

.search-box input {
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-main);
  font-size: 14px;
  width: 100%;
  margin-left: 10px;
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
  background-color: var(--bg-tinted);
  border: none;
  color: var(--text-main);
  padding: 7px 14px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
  white-space: nowrap;
}

.pill-btn:hover {
  background-color: var(--bg-tinted-hover);
}

.pill-btn.active {
  background-color: #ffffff;
  color: #000000;
  font-weight: 700;
}

/* --- Spotify Quick-Play 6-Tile Greeting Grid --- */
.content-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px 36px 24px;
}

.quick-tiles-section {
  margin-bottom: 26px;
}

.greeting-title {
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -0.6px;
  margin-bottom: 14px;
}

.quick-tiles-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 10px;
}

.quick-tile {
  display: flex;
  align-items: center;
  background-color: var(--bg-tinted);
  border-radius: var(--radius-sm);
  overflow: hidden;
  cursor: pointer;
  transition: var(--transition);
  height: 56px;
  padding-right: 12px;
}

.quick-tile:hover {
  background-color: var(--bg-tinted-hover);
}

.quick-tile img, .liked-tile-icon {
  width: 56px;
  height: 56px;
  object-fit: cover;
  flex-shrink: 0;
  margin-right: 12px;
}

.liked-tile-icon {
  background: linear-gradient(135deg, #450af5, #c4efd9);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  color: #fff;
}

.quick-tile span {
  font-size: 13.5px;
  font-weight: 700;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tile-play {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background-color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 34px !important;
  opacity: 0;
  transform: scale(0.9);
  box-shadow: 0 6px 14px rgba(0, 0, 0, 0.5);
  transition: var(--transition);
}

.quick-tile:hover .tile-play {
  opacity: 1;
  transform: scale(1);
}

.view-header {
  margin-bottom: 16px;
}

.view-heading {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.4px;
  margin-bottom: 2px;
}

.view-subheading {
  font-size: 13px;
  color: var(--text-muted);
}

/* --- Spotify Song Cards Grid --- */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
  gap: 18px;
}

.song-card {
  background-color: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 14px;
  display: flex;
  flex-direction: column;
  position: relative;
  transition: var(--transition);
  cursor: pointer;
}

.song-card:hover {
  background-color: var(--bg-card-hover);
}

.song-card.playing-now {
  border: 1px solid var(--accent);
  background-color: rgba(29, 185, 84, 0.12);
}

.card-img-wrapper {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 12px;
  background-color: #242424;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
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
  width: 44px;
  height: 44px;
  background-color: var(--accent);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #000;
  border: none;
  box-shadow: 0 8px 16px rgba(0, 0, 0, 0.45);
  opacity: 0;
  transform: translateY(8px);
  transition: var(--transition);
  cursor: pointer;
}

.song-card:hover .card-play-btn, .song-card.playing-now .card-play-btn {
  opacity: 1;
  transform: translateY(0);
}

.card-play-btn:hover {
  background-color: var(--accent-hover);
  transform: scale(1.06);
}

.card-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.card-title {
  font-size: 14.5px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-main);
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
}

.card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-color);
}

.card-actions button {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  font-weight: 600;
  transition: var(--transition);
}

.card-actions button:hover {
  color: #fff;
  background-color: var(--bg-tinted-hover);
}

.card-actions button.liked {
  color: var(--accent);
}

/* --- Spotify Full-Screen / Overlay Lyrics Panel --- */
.lyrics-panel {
  position: absolute;
  inset: 0;
  background: linear-gradient(160deg, #1b4332 0%, #081c15 55%, #000000 100%);
  z-index: 25;
  display: flex;
  flex-direction: column;
  padding: 24px 36px;
  transform: translateY(100%);
  transition: transform 0.32s cubic-bezier(0.16, 1, 0.3, 1);
}

.lyrics-panel.open {
  transform: translateY(0);
}

.lyrics-top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.14);
}

.lyrics-song-meta {
  display: flex;
  align-items: center;
  gap: 14px;
}

.lyrics-song-meta img {
  width: 56px;
  height: 56px;
  border-radius: var(--radius-sm);
  object-fit: cover;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
}

.lyrics-song-title {
  font-size: 18px;
  font-weight: 800;
  color: #ffffff;
}

.lyrics-song-artist {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.75);
}

.lyrics-content {
  flex: 1;
  overflow-y: auto;
  padding: 32px 12px;
  font-size: 24px;
  font-weight: 800;
  line-height: 1.65;
  color: rgba(255, 255, 255, 0.92);
  text-align: center;
  max-width: 780px;
  margin: 0 auto;
  width: 100%;
}

/* --- Playlist Table & Controls --- */
.playlist-controls-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 18px;
}

.btn-primary {
  background-color: var(--accent);
  color: #000000;
  border: none;
  padding: 11px 24px;
  border-radius: 999px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  transition: var(--transition);
}

.btn-primary:hover {
  background-color: var(--accent-hover);
  transform: scale(1.03);
}

.btn-danger {
  background-color: transparent;
  color: var(--danger);
  border: 1px solid var(--danger);
  padding: 9px 18px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: var(--transition);
}

.btn-danger:hover {
  background-color: var(--danger);
  color: #fff;
}

.track-table {
  width: 100%;
  border-collapse: collapse;
}

.track-table th {
  text-align: left;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--border-color);
  text-transform: uppercase;
}

.track-row {
  border-radius: var(--radius-sm);
  transition: var(--transition);
  cursor: pointer;
}

.track-row:hover {
  background-color: var(--bg-tinted);
}

.track-row td {
  padding: 10px 14px;
  font-size: 14px;
  color: var(--text-muted);
  vertical-align: middle;
}

.track-row td.track-title-cell {
  color: var(--text-main);
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 12px;
}

.track-row img {
  width: 42px;
  height: 42px;
  border-radius: 4px;
  object-fit: cover;
}

/* --- Slide-Over Queue Drawer --- */
.queue-panel {
  position: absolute;
  top: 0;
  right: 0;
  width: var(--queue-width);
  height: 100%;
  background-color: #181818;
  border-left: 1px solid var(--border-color);
  z-index: 26;
  display: flex;
  flex-direction: column;
  transform: translateX(100%);
  transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  box-shadow: -8px 0 28px rgba(0, 0, 0, 0.65);
}

.queue-panel.open {
  transform: translateX(0);
}

.queue-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
  border-bottom: 1px solid var(--border-color);
}

.queue-header h3 {
  font-size: 16px;
  font-weight: 800;
}

.queue-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.queue-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background-color: var(--bg-tinted);
  border: 1px solid transparent;
  cursor: pointer;
  transition: var(--transition);
}

.queue-item:hover {
  background-color: var(--bg-tinted-hover);
}

.queue-item.active {
  border-color: var(--accent);
  background-color: rgba(29, 185, 84, 0.16);
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
  font-size: 13.5px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.queue-item-artist {
  font-size: 11.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* --- Spotify Bottom Persistent Player --- */
.bottom-player {
  height: var(--player-height);
  background-color: var(--bg-black);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  z-index: 30;
  flex-shrink: 0;
}

.player-left {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 300px;
  min-width: 180px;
  cursor: pointer;
}

.player-thumb {
  width: 56px;
  height: 56px;
  border-radius: var(--radius-sm);
  background-color: var(--bg-card);
  object-fit: cover;
  flex-shrink: 0;
}

.player-track-info {
  display: flex;
  flex-direction: column;
  gap: 3px;
  overflow: hidden;
  flex: 1;
}

.player-title {
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.player-artist {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.like-btn.liked svg {
  fill: var(--accent);
  stroke: var(--accent);
}

.player-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  flex: 1;
  max-width: 620px;
}

.player-controls {
  display: flex;
  align-items: center;
  gap: 14px;
}

.ctrl-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  transition: var(--transition);
  padding: 6px;
  border-radius: 999px;
}

.ctrl-btn:hover {
  color: var(--text-main);
  transform: scale(1.08);
}

.ctrl-btn.active {
  color: var(--accent);
}

.ctrl-btn-play {
  width: 36px;
  height: 36px;
  background-color: #ffffff;
  color: #000000;
  border-radius: 50%;
}

.ctrl-btn-play:hover {
  background-color: var(--accent);
  color: #000000;
  transform: scale(1.08);
}

.lyrics-toggle-btn {
  background-color: var(--bg-tinted);
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}

.lyrics-toggle-btn.active {
  background-color: var(--accent);
  color: #000;
}

.scrubber-container {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.time-label {
  font-size: 11px;
  color: var(--text-muted);
  width: 35px;
  text-align: center;
}

.range-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 4px;
  border-radius: 2px;
  background: #4d4d4d;
  outline: none;
  cursor: pointer;
}

.range-slider:hover {
  background: var(--accent);
}

.range-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #ffffff;
  cursor: pointer;
}

.player-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  width: 300px;
  min-width: 160px;
}

.volume-container {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 110px;
}

.badge-count {
  background-color: var(--accent);
  color: #000000;
  font-size: 10px;
  font-weight: 800;
  padding: 1px 6px;
  border-radius: 999px;
}

/* --- Modals & Profile Card --- */
.user-profile-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background-color: var(--bg-tinted);
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  margin-bottom: 12px;
  cursor: pointer;
  transition: var(--transition);
}

.user-profile-card:hover {
  background-color: var(--bg-tinted-hover);
}

.user-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-color: var(--accent);
  color: #000;
  font-weight: 800;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.user-name-display {
  font-size: 13px;
  font-weight: 700;
  color: #fff;
}

.user-age-display {
  font-size: 11px;
  color: var(--text-muted);
}

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.78);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}

.modal-overlay.open {
  opacity: 1;
  pointer-events: auto;
}

.modal-card {
  background-color: #242424;
  border-radius: var(--radius-lg);
  padding: 24px;
  width: 90%;
  max-width: 420px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.7);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: left;
}

.form-group label {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-muted);
}

.form-group input, .form-group textarea, .form-group select {
  background-color: #121212;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  color: #fff;
  font-size: 14px;
  outline: none;
}

.form-group input:focus {
  border-color: var(--accent);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.btn-secondary {
  background: transparent;
  border: 1px solid var(--text-muted);
  color: #fff;
  padding: 8px 18px;
  border-radius: 999px;
  font-weight: 700;
  cursor: pointer;
}

.welcome-card {
  text-align: center;
}

.welcome-badge {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background-color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 6px auto;
}

/* --- Toast Notifications --- */
.toast-container {
  position: fixed;
  bottom: calc(var(--player-height) + 18px);
  right: 24px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 120;
  pointer-events: none;
}

.toast {
  background-color: var(--accent);
  color: #000000;
  font-weight: 800;
  padding: 10px 18px;
  border-radius: 999px;
  font-size: 13px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}

.mobile-menu-btn, .mobile-bottom-nav {
  display: none;
}

/* --- Mobile Spotify App Responsive Layout --- */
@media (max-width: 768px) {
  .app-container {
    padding: 0;
    height: calc(100vh - var(--player-height) - 56px);
  }

  .main-area {
    border-radius: 0;
  }

  .mobile-menu-btn {
    display: inline-flex;
    background: var(--bg-tinted);
    border: none;
    color: #fff;
    padding: 8px;
    border-radius: var(--radius-sm);
  }

  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: calc(var(--player-height) + 56px);
    z-index: 60;
    background: #000;
    padding: 8px;
    transform: translateX(-100%);
    transition: transform 0.28s ease;
  }

  .sidebar.mobile-open {
    transform: translateX(0);
  }

  .top-bar {
    flex-wrap: wrap;
    padding: 10px 14px;
  }

  .search-box {
    flex: 1;
    width: auto;
  }

  .category-pills {
    width: 100%;
  }

  .quick-tiles-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }

  .cards-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 12px;
  }

  .lyrics-content {
    font-size: 18px;
    padding: 20px 6px;
  }

  .hide-mobile, .volume-container, .lyrics-btn-label {
    display: none !important;
  }

  .bottom-player {
    height: 76px;
    padding: 0 10px;
  }

  .player-left {
    width: auto;
    min-width: 110px;
    max-width: 145px;
  }

  .player-thumb {
    width: 42px;
    height: 42px;
  }

  .player-right {
    width: auto;
    min-width: auto;
  }

  .mobile-bottom-nav {
    display: flex;
    height: 56px;
    background-color: #000000;
    border-top: 1px solid var(--border-color);
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
    font-size: 10px;
    font-weight: 700;
    cursor: pointer;
  }

  .mob-tab.active, .mob-tab:hover {
    color: #ffffff;
  }
}
'''

PLAYER_JS = r'''/* Musify Spotify Edition - Audio Engine, Song Switcher & Live Lyrics Controller */
(function () {
  const $ = (id) => document.getElementById(id);
  const audio = $("audio");

  const ICON_PLAY = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  const ICON_PAUSE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';

  const state = { queue: [], index: -1, shuffle: false, repeat: 0, urlTry: 0 };

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
    setTimeout(() => t.remove(), 2600);
  }
  window.toast = toast;

  function ensureHttps(u) {
    return u ? String(u).trim().replace(/^http:\/\//i, "https://") : "";
  }

  function urlsFor(song) {
    const raw = [song.media_url].concat(song.backup_urls || []);
    const expanded = [];
    raw.forEach((u) => {
      const clean = ensureHttps(u);
      if (!clean) return;
      if (!expanded.includes(clean)) expanded.push(clean);
      ["_320.mp4", "_160.mp4", "_96.mp4"].forEach((br) => {
        ["_320.mp4", "_160.mp4", "_96.mp4", "_96_p.mp4"].forEach((orig) => {
          if (clean.includes(orig)) {
            const v = clean.replace(orig, br).replace("preview.saavncdn.com", "aac.saavncdn.com");
            if (!expanded.includes(v)) expanded.push(v);
          }
        });
      });
    });
    expanded.push("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3");
    return expanded;
  }

  function save() {
    try {
      localStorage.setItem("musify_queue", JSON.stringify({ queue: state.queue, index: state.index }));
    } catch (e) {}
  }

  function load() {
    try {
      const d = JSON.parse(localStorage.getItem("musify_queue") || "null");
      if (d && Array.isArray(d.queue)) {
        state.queue = d.queue;
        state.index = typeof d.index === "number" ? d.index : -1;
      }
    } catch (e) {}
  }

  function current() {
    return state.queue[state.index] || null;
  }

  // ---- Lyrics Fetching & Display ----
  async function loadLyricsForCurrent() {
    const s = current();
    const box = $("lyricsContent");
    if (!s) {
      box.innerHTML = "Select and play any song to view live lyrics here.";
      return;
    }
    $("lyricsSongTitle").textContent = s.title || "Unknown Title";
    $("lyricsSongArtist").textContent = s.artist || "";
    if (s.image_url) $("lyricsThumb").src = s.image_url;

    if (s.lyrics) {
      box.innerHTML = s.lyrics;
      return;
    }

    box.innerHTML = '<div style="opacity:0.6;font-size:16px;">Loading lyrics for "' + esc(s.title) + '"...</div>';
    try {
      const params = new URLSearchParams({
        id: s.id || "",
        title: s.title || "",
        artist: s.artist || ""
      });
      const res = await fetch("/api/lyrics?" + params.toString());
      const data = await res.json();
      if (data.status && data.lyrics) {
        s.lyrics = data.lyrics;
        box.innerHTML = data.lyrics;
      } else {
        box.innerHTML =
          '<div style="opacity:0.75;font-size:18px;line-height:1.8;">' +
          "🎵 Now Streaming: <strong>" + esc(s.title) + "</strong><br>" +
          "Artist: " + esc(s.artist) + "<br>" +
          (s.album ? "Album: " + esc(s.album) + "<br><br>" : "<br>") +
          '<span style="font-size:14px;opacity:0.7;">(Synced studio lyrics are not yet published for this track)</span></div>';
      }
    } catch (e) {
      box.innerHTML = "🎵 Streaming " + esc(s.title) + " — " + esc(s.artist);
    }
  }

  window.toggleLyricsPanel = function () {
    const panel = $("lyricsBox");
    const btn = $("btnLyricsToggle");
    const isOpen = panel.classList.toggle("open");
    if (btn) btn.classList.toggle("active", isOpen);
    if (isOpen) loadLyricsForCurrent();
  };

  $("btnLyricsToggle").addEventListener("click", window.toggleLyricsPanel);
  $("btnCloseLyrics").addEventListener("click", () => {
    $("lyricsBox").classList.remove("open");
    $("btnLyricsToggle").classList.remove("active");
  });

  function updateInfo() {
    const s = current();
    $("playerTitle").textContent = s ? s.title : "No song playing";
    $("playerArtist").textContent = s ? s.artist : "Tap any song to start";
    if (s && s.image_url) $("playerImg").src = s.image_url;
    document.title = s ? s.title + " • Musify" : "Musify • Spotify Edition";

    // Highlight active card in grid
    document.querySelectorAll(".song-card").forEach((card) => {
      const cardTitle = card.querySelector(".card-title");
      card.classList.toggle("playing-now", Boolean(s && cardTitle && cardTitle.textContent === s.title));
    });

    // Update heart icon if liked
    if (window.updateLikeButtonState) window.updateLikeButtonState(s);

    // Update lyrics panel if open
    if ($("lyricsBox").classList.contains("open")) {
      loadLyricsForCurrent();
    }

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
    $("queueBadge").textContent = state.queue.length;
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

  function startAudio() {
    const s = current();
    if (!s) return;
    const urls = urlsFor(s);
    audio.src = urls[Math.min(state.urlTry, urls.length - 1)];
    const p = audio.play();
    if (p && p.catch) p.catch(() => {});
  }

  function playAt(i) {
    if (i < 0 || i >= state.queue.length) return;
    state.index = i;
    state.urlTry = 0;
    startAudio();
    updateInfo();
    renderQueue();
    save();
  }

  function next(auto) {
    if (!state.queue.length) return;
    if (auto && state.repeat === 2) { audio.currentTime = 0; audio.play().catch(() => {}); return; }
    if (state.shuffle && state.queue.length > 1) {
      let r;
      do { r = Math.floor(Math.random() * state.queue.length); } while (r === state.index);
      return playAt(r);
    }
    if (state.index + 1 < state.queue.length) return playAt(state.index + 1);
    return playAt(0);
  }

  function prev() {
    if (!state.queue.length) return;
    if (audio.currentTime > 3) { audio.currentTime = 0; return; }
    playAt(state.index > 0 ? state.index - 1 : state.queue.length - 1);
  }

  function toggle() {
    if (!current()) {
      if (state.queue.length) playAt(0);
      return;
    }
    if (!audio.src) { startAudio(); return; }
    if (audio.paused) audio.play().catch(() => {}); else audio.pause();
  }

  function setPlayIcon(playing) {
    $("btnPlay").innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  }

  window.Player = {
    playSongs(songs, startIndex) {
      state.queue = songs.slice();
      playAt(startIndex || 0);
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
        if (state.queue.length) { state.index = Math.min(i, state.queue.length) - 1; next(false); }
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
    next: () => next(false),
    prev,
    toggle
  };

  // ---- Audio events ----
  audio.addEventListener("play", () => setPlayIcon(true));
  audio.addEventListener("pause", () => setPlayIcon(false));
  audio.addEventListener("ended", () => next(true));
  audio.addEventListener("loadedmetadata", () => { $("totalDuration").textContent = fmt(audio.duration); });
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) {
      $("progressBar").value = (audio.currentTime / audio.duration) * 100;
      $("currentTime").textContent = fmt(audio.currentTime);
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
    if (!s._resolvedFresh) {
      s._resolvedFresh = true;
      try {
        const q = encodeURIComponent((s.title || "") + " " + (s.artist || ""));
        const r = await fetch("https://itunes.apple.com/search?term=" + q + "&country=IN&media=music&entity=song&limit=3");
        const j = await r.json();
        if (j.results && j.results[0] && j.results[0].previewUrl) {
          audio.src = ensureHttps(j.results[0].previewUrl);
          audio.play().catch(() => {});
          return;
        }
      } catch (e) {}
    }
    if (state.queue.length > 1) next(true);
  });

  // ---- Controls ----
  $("btnPlay").addEventListener("click", toggle);
  $("btnNext").addEventListener("click", () => next(false));
  $("btnPrev").addEventListener("click", prev);
  if ($("btnSeekBack")) {
    $("btnSeekBack").addEventListener("click", () => {
      audio.currentTime = Math.max(0, (audio.currentTime || 0) - 10);
    });
  }
  if ($("btnSeekFwd")) {
    $("btnSeekFwd").addEventListener("click", () => {
      if (audio.duration) audio.currentTime = Math.min(audio.duration, (audio.currentTime || 0) + 10);
    });
  }
  $("btnShuffle").addEventListener("click", () => {
    state.shuffle = !state.shuffle;
    $("btnShuffle").classList.toggle("active", state.shuffle);
    toast("Shuffle " + (state.shuffle ? "ON" : "OFF"));
  });
  $("btnRepeat").addEventListener("click", () => {
    state.repeat = (state.repeat + 1) % 3;
    $("btnRepeat").classList.toggle("active", state.repeat > 0);
    toast(["Repeat OFF", "Repeat ALL", "Repeat ONE"][state.repeat]);
  });
  $("progressBar").addEventListener("input", (e) => {
    if (audio.duration) audio.currentTime = (e.target.value / 100) * audio.duration;
  });
  $("volumeBar").addEventListener("input", (e) => { audio.volume = parseFloat(e.target.value); });
  audio.volume = 0.85;
  $("btnVolume").addEventListener("click", () => {
    audio.muted = !audio.muted;
    $("btnVolume").style.opacity = audio.muted ? "0.4" : "1";
  });

  // ---- Queue panel ----
  $("btnQueueToggle").addEventListener("click", () => $("queuePanel").classList.toggle("open"));
  $("btnCloseQueue").addEventListener("click", () => $("queuePanel").classList.remove("open"));
  $("btnClearQueue").addEventListener("click", () => window.Player.clear());
  $("queueList").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-remove]");
    if (rm) { window.Player.removeAt(parseInt(rm.dataset.remove, 10)); return; }
    const item = e.target.closest(".queue-item");
    if (item) playAt(parseInt(item.dataset.i, 10));
  });

  // ---- Keyboard shortcuts ----
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const k = e.key.toLowerCase();
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    else if (k === "n" || e.key === "ArrowRight") next(false);
    else if (k === "p" || e.key === "ArrowLeft") prev();
    else if (k === "s") $("btnShuffle").click();
    else if (k === "r") $("btnRepeat").click();
    else if (k === "l") window.toggleLyricsPanel();
  });

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.setActionHandler("play", toggle);
      navigator.mediaSession.setActionHandler("pause", toggle);
      navigator.mediaSession.setActionHandler("nexttrack", () => next(false));
      navigator.mediaSession.setActionHandler("previoustrack", prev);
    } catch (e) {}
  }

  load();
  updateInfo();
  renderQueue();
  setPlayIcon(false);
})();
'''

APP_JS = r'''/* Musify Spotify Edition - Discovery, Liked Songs, Playlists & Profile Controller */
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

  const TITLES = {
    home: ["Made For You • Top Hits", "Non-stop Telugu, Hindi, English & Punjabi streams"],
    trending: ["Viral & Trending India", "What everyone is streaming right now"],
    telugu: ["Telugu Blockbusters", "Sid Sriram, Thaman S, DSP, Anirudh & Tollywood hits"],
    hindi: ["Bollywood Top 50", "Arijit Singh, Pritam, Shreya Ghoshal & Hindi chartbusters"],
    english: ["Global English Pop", "The Weeknd, Ed Sheeran, Taylor Swift & Dua Lipa"],
    punjabi: ["Punjabi Party Anthems", "AP Dhillon, Diljit Dosanjh & Karan Aujla"]
  };

  // ---- Liked Songs Storage ----
  function getLikedSongs() {
    try { return JSON.parse(localStorage.getItem("musify_liked_songs") || "[]"); }
    catch (e) { return []; }
  }

  function saveLikedSongs(list) {
    try { localStorage.setItem("musify_liked_songs", JSON.stringify(list)); }
    catch (e) {}
    if ($("likedCountBadge")) $("likedCountBadge").textContent = list.length;
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

  window.updateLikeButtonState = function (song) {
    const btn = $("btnLikeCurrent");
    if (!btn) return;
    btn.classList.toggle("liked", isSongLiked(song));
  };

  if ($("btnLikeCurrent")) {
    $("btnLikeCurrent").addEventListener("click", () => {
      const s = window.Player.currentSong();
      if (s) toggleLikeSong(s);
      else toast("Play a song first to like it");
    });
  }

  window.openLikedSongs = function () {
    closeMobileMenu();
    openPlaylistId = "liked";
    $("quickTilesSection").style.display = "none";
    const liked = getLikedSongs();
    currentSongs = liked;
    setHeading("Liked Songs ♥", liked.length + " saved tracks on your device");
    if (!liked.length) {
      $("content").innerHTML = '<div style="color:var(--text-muted);padding:30px 0;">You haven't liked any songs yet. Tap the ♥ heart button on any song or in the bottom player!</div>';
      return;
    }
    renderSongs(liked, false, true);
  };

  async function api(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok || data.success === false) throw new Error(data.error || "Request failed (" + res.status + ")");
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

  function showError(msg) {
    $("content").innerHTML = '<div style="color:var(--danger);padding:30px 0;">' + esc(msg) + "</div>";
  }

  function closeMobileMenu() {
    $("sidebar").classList.remove("mobile-open");
  }

  window.toggleMobileSidebar = function () {
    $("sidebar").classList.toggle("mobile-open");
  };

  window.focusSearchInput = function () {
    closeMobileMenu();
    $("lyricsBox").classList.remove("open");
    $("query").focus();
  };

  // ---------- Song grid ----------
  function renderSongs(songs, append, hideLoadMore) {
    currentSongs = append ? currentSongs.concat(songs) : songs;
    if (!currentSongs.length) {
      $("content").innerHTML = '<div style="color:var(--text-muted);padding:30px 0;">No songs found.</div>';
      return;
    }
    const cur = window.Player.currentSong();
    $("content").innerHTML = '<div class="cards-grid">' + currentSongs.map((s, i) => {
      const playing = Boolean(cur && cur.title === s.title);
      const liked = isSongLiked(s);
      return `
      <div class="song-card ${playing ? "playing-now" : ""}" data-i="${i}">
        <div class="card-img-wrapper">
          <img src="${esc(s.image_url)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
          <button class="card-play-btn" data-act="play" title="Play Song">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          </button>
        </div>
        <div class="card-info">
          <div class="card-title" title="${esc(s.title)}">${esc(s.title)}</div>
          <div class="card-artist">${esc(s.artist)}</div>
        </div>
        <div class="card-actions">
          <button data-act="like" class="${liked ? "liked" : ""}" title="Like Song">♥</button>
          <button data-act="lyrics" title="View Lyrics">🎤 Lyrics</button>
          <button data-act="queue" title="Add to Queue">+Queue</button>
          <button data-act="playlist" title="Add to Playlist">+List</button>
        </div>
      </div>`;
    }).join("") + '</div>' +
    (hideLoadMore ? "" :
      '<div style="display:flex;justify-content:center;padding:28px 0 12px 0;">' +
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
      if (act === "play") window.Player.playSongs(currentSongs, i);
      else if (act === "like") {
        toggleLikeSong(song);
        if (btn) btn.classList.toggle("liked", isSongLiked(song));
      } else if (act === "lyrics") {
        window.Player.playSongs(currentSongs, i);
        if (!$("lyricsBox").classList.contains("open")) window.toggleLyricsPanel();
      } else if (act === "queue") window.Player.addToQueue(song);
      else if (act === "playlist") openAddToPlaylist(song);
      return;
    }

    const row = e.target.closest(".track-row");
    if (row) {
      const i = parseInt(row.dataset.i, 10);
      if (e.target.closest("[data-act='remove']")) { removeFromOpenPlaylist(row.dataset.entry); return; }
      window.Player.playSongs(currentSongs, i);
      return;
    }

    if (e.target.closest("#btnPlayAll")) {
      if (currentSongs.length) window.Player.playSongs(currentSongs, 0);
      else toast("This playlist is empty");
    } else if (e.target.closest("#btnDeletePlaylist")) {
      deleteOpenPlaylist();
    } else if (e.target.closest("#btnLoadMore")) {
      loadMoreSongs();
    }
  });

  async function loadMoreSongs() {
    const btn = $("btnLoadMore");
    if (btn) { btn.textContent = "Loading more songs..."; btn.disabled = true; }
    currentPage += 1;
    try {
      const fixedUrl = activeSearchQuery
        ? "/api/search?q=" + encodeURIComponent(activeSearchQuery) + "&page=" + currentPage
        : "/api/category/" + encodeURIComponent(currentCategory) + "?page=" + currentPage;
      const data = await api(fixedUrl);
      const more = data.songs || [];
      if (more.length) {
        renderSongs(more, true, false);
        toast("Loaded " + more.length + " more songs");
      } else {
        toast("No more songs found");
        if (btn) btn.style.display = "none";
      }
    } catch (err) {
      toast("Could not load more songs");
      if (btn) { btn.textContent = "Load More Songs"; btn.disabled = false; }
    }
  }

  async function loadCategory(cat) {
    currentCategory = cat;
    activeSearchQuery = "";
    currentPage = 1;
    openPlaylistId = null;
    $("lyricsBox").classList.remove("open");
    $("quickTilesSection").style.display = cat === "home" ? "block" : "none";
    document.querySelectorAll(".pill-btn").forEach((b) => b.classList.toggle("active", b.dataset.category === cat));
    document.querySelectorAll(".sidebar-playlists .playlist-item").forEach((p) => p.classList.remove("active"));
    const t = TITLES[cat] || [cat.charAt(0).toUpperCase() + cat.slice(1), ""];
    setHeading(t[0], t[1]);
    showLoading("Loading songs from JioSaavn...");
    closeMobileMenu();
    try {
      const data = await api("/api/category/" + encodeURIComponent(cat) + "?page=1");
      renderSongs(data.songs || [], false, false);
    } catch (err) {
      showError("Could not load songs: " + err.message);
    }
  }
  window.loadCategory = loadCategory;

  async function runSearch(q) {
    openPlaylistId = null;
    activeSearchQuery = q;
    currentPage = 1;
    $("lyricsBox").classList.remove("open");
    $("quickTilesSection").style.display = "none";
    setHeading('Results for "' + q + '"', "Searching JioSaavn catalog...");
    showLoading("Searching JioSaavn...");
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(q) + "&page=1");
      setHeading('Results for "' + q + '"', (data.count || 0) + " songs loaded (tap Load More for more)");
      renderSongs(data.songs || [], false, false);
    } catch (err) {
      showError("Search failed: " + err.message);
    }
  }

  $("query").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(() => {
      if (q) runSearch(q); else loadCategory(currentCategory);
    }, 450);
  });

  document.querySelectorAll(".pill-btn").forEach((b) => {
    b.addEventListener("click", () => { $("query").value = ""; loadCategory(b.dataset.category); });
  });

  // ---------- Playlists (Dual-Synced: SQLite + Mobile localStorage) ----------
  function getLocalPlaylists() {
    try { return JSON.parse(localStorage.getItem("musify_playlists_v3") || "[]"); }
    catch (e) { return []; }
  }

  function saveLocalPlaylists(lists) {
    try { localStorage.setItem("musify_playlists_v3", JSON.stringify(lists)); }
    catch (e) {}
  }

  async function loadPlaylists() {
    const box = $("sidebarPlaylists");
    let localLists = getLocalPlaylists();
    try {
      const data = await api("/api/playlists");
      const serverLists = data.playlists || [];
      if (serverLists.length > 0) {
        localLists = serverLists.map((sp) => {
          const match = localLists.find((lp) => Number(lp.id) === Number(sp.id) || lp.name === sp.name);
          const songs = match && Array.isArray(match.songs) ? match.songs : [];
          return { ...sp, songs, song_count: Math.max(sp.song_count || 0, songs.length) };
        });
        saveLocalPlaylists(localLists);
      }
    } catch (err) {}

    if (!localLists.length) {
      box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px 12px;">No custom playlists yet. Tap + above to create one.</div>';
      return;
    }
    box.innerHTML = localLists.map((p) => {
      const count = Array.isArray(p.songs) ? Math.max(p.songs.length, p.song_count || 0) : (p.song_count || 0);
      return `
        <div class="playlist-item ${Number(p.id) === Number(openPlaylistId) ? "active" : ""}" data-id="${p.id}">
          <span>🎵 ${esc(p.name)}</span>
          <span class="song-badge">${count}</span>
        </div>`;
    }).join("");
  }

  $("sidebarPlaylists").addEventListener("click", (e) => {
    const item = e.target.closest(".playlist-item");
    if (item) openPlaylist(parseInt(item.dataset.id, 10));
  });

  function renderPlaylistContent(p) {
    $("quickTilesSection").style.display = "none";
    currentSongs = p.songs || [];
    setHeading(p.name, p.description || (currentSongs.length + " songs"));
    let html = `
      <div class="playlist-controls-row" style="margin-bottom:18px;">
        <button class="btn-primary" id="btnPlayAll">▶ Play All</button>
        <button class="btn-danger" id="btnDeletePlaylist">Delete Playlist</button>
      </div>`;
    if (!currentSongs.length) {
      html += '<div style="color:var(--text-muted);">This playlist is empty. Add songs using the "+List" button on any song card.</div>';
    } else {
      html += '<table class="track-table"><thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Time</th><th></th></tr></thead><tbody>' +
        currentSongs.map((s, i) => `
          <tr class="track-row" data-i="${i}" data-entry="${s.id}">
            <td style="width:36px;">${i + 1}</td>
            <td><div class="track-title-cell"><img src="${esc(s.image_url)}" alt="" onerror="this.style.visibility='hidden'"><span>${esc(s.title)}</span></div></td>
            <td>${esc(s.artist)}</td>
            <td>${window.fmtTime(s.duration)}</td>
            <td><button class="btn-icon-small" data-act="remove" title="Remove">&times;</button></td>
          </tr>`).join("") + "</tbody></table>";
    }
    $("content").innerHTML = html;
  }

  async function openPlaylist(id) {
    openPlaylistId = id;
    closeMobileMenu();
    $("lyricsBox").classList.remove("open");
    document.querySelectorAll(".pill-btn").forEach((b) => b.classList.remove("active"));
    showLoading("Loading playlist...");
    const localLists = getLocalPlaylists();
    const localMatch = localLists.find((p) => Number(p.id) === Number(id));
    try {
      const data = await api("/api/playlists/" + id);
      const p = data.playlist;
      if (localMatch && (!p.songs || !p.songs.length) && localMatch.songs && localMatch.songs.length) {
        p.songs = localMatch.songs;
      }
      if (localMatch) {
        localMatch.songs = p.songs || [];
        localMatch.song_count = localMatch.songs.length;
        saveLocalPlaylists(localLists);
      }
      renderPlaylistContent(p);
      loadPlaylists();
      return;
    } catch (err) {}

    if (localMatch) {
      renderPlaylistContent(localMatch);
      loadPlaylists();
    } else {
      showError("Could not open playlist.");
    }
  }

  async function removeFromOpenPlaylist(entryId) {
    const localLists = getLocalPlaylists();
    const pl = localLists.find((p) => Number(p.id) === Number(openPlaylistId));
    if (pl && Array.isArray(pl.songs)) {
      pl.songs = pl.songs.filter((s) => String(s.id) !== String(entryId));
      pl.song_count = pl.songs.length;
      saveLocalPlaylists(localLists);
    }
    try {
      await api("/api/playlists/" + openPlaylistId + "/songs/" + entryId, { method: "DELETE" });
    } catch (err) {}
    toast("Song removed");
    openPlaylist(openPlaylistId);
  }

  async function deleteOpenPlaylist() {
    if (!confirm("Delete this playlist?")) return;
    const localLists = getLocalPlaylists().filter((p) => Number(p.id) !== Number(openPlaylistId));
    saveLocalPlaylists(localLists);
    try {
      await api("/api/playlists/" + openPlaylistId, { method: "DELETE" });
    } catch (err) {}
    toast("Playlist deleted");
    await loadPlaylists();
    loadCategory("home");
  }

  // ---------- Modals ----------
  function openModal(id) { $(id).classList.add("open"); }
  function closeModal(id) { $(id).classList.remove("open"); }

  $("btnNewPlaylist").addEventListener("click", () => {
    $("playlistNameInput").value = "";
    $("playlistDescInput").value = "";
    openModal("createPlaylistModal");
  });
  $("btnCloseCreatePlaylist").addEventListener("click", () => closeModal("createPlaylistModal"));
  $("btnCancelCreatePlaylist").addEventListener("click", () => closeModal("createPlaylistModal"));

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
    toast("Playlist created");
    loadPlaylists();
  });

  async function openAddToPlaylist(song) {
    pendingSong = song;
    await loadPlaylists();
    const lists = getLocalPlaylists();
    if (!lists.length) { toast("Create a playlist first (tap + in Your Library)"); return; }
    $("playlistSelect").innerHTML = lists.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
    openModal("addToPlaylistModal");
  }

  $("btnCloseAddToPlaylist").addEventListener("click", () => closeModal("addToPlaylistModal"));
  $("btnCancelAddToPlaylist").addEventListener("click", () => closeModal("addToPlaylistModal"));

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
    loadPlaylists();
  });

  // ---------- Profile & Spotify Dynamic Time Greeting ----------
  function showProfile(p) {
    $("userNameDisplay").textContent = p.name;
    $("userAgeDisplay").textContent = "Age " + p.age + " • Spotify Free";
    $("userAvatar").textContent = p.name.trim().charAt(0).toUpperCase() || "M";
    const hr = new Date().getHours();
    const greet = hr < 12 ? "Good morning" : (hr < 17 ? "Good afternoon" : "Good evening");
    if ($("greetingTitle")) $("greetingTitle").textContent = greet + ", " + p.name;
  }

  window.openProfileModal = function () {
    try {
      const p = JSON.parse(localStorage.getItem("musify_profile") || "null");
      if (p) { $("userNameInput").value = p.name; $("userAgeInput").value = p.age; }
    } catch (e) {}
    openModal("welcomeModal");
  };

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

  $("btnMobileMenu").addEventListener("click", window.toggleMobileSidebar);

  saveLikedSongs(getLikedSongs());
  initProfile();
  loadPlaylists();
  loadCategory("home");
})();
'''

MANIFEST_JSON = r'''{
  "name": "Musify • Spotify Edition",
  "short_name": "Musify",
  "description": "Stream Telugu, Hindi, English and Punjabi music with live lyrics and playlists",
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

SW_JS = r'''const CACHE = "musify-spotify-v4";
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
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
"""

ICON_512_B64 = """
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
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


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    item = STATIC_FILES.get(filename)
    if not item:
        abort(404)
    return Response(item[0], mimetype=item[1])


@app.route("/manifest.json")
def serve_manifest():
    return Response(MANIFEST_JSON, mimetype="application/manifest+json")


@app.route("/sw.js")
def serve_sw():
    return Response(SW_JS, mimetype="application/javascript")


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
    if not query and not title:
        return jsonify({"status": False, "error": "Song ID or title is required to fetch lyrics!"})
    sid = get_song_id(query) if ("http" in query and "saavn" in query) else query
    lyr = get_lyrics(sid, title=title, artist=artist)
    return jsonify({"status": bool(lyr), "lyrics": lyr})


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
