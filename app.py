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
    "Referer": "https://www.jiosaavn.com/",
    "Cookie": "L=telugu"
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


def _clean_track_title(raw_title: str) -> str:
    """Removes parenthetical movie tags like (From 'Maestro') and normalizes title."""
    if not raw_title:
        return ""
    cleaned = html.unescape(str(raw_title))
    cleaned = re.sub(r"\s*[\(\[].*?[\)\]]", "", cleaned).strip()
    cleaned = re.sub(r"\s*-\s*.*$", "", cleaned).strip()
    return cleaned or str(raw_title).strip()


def _dedup_key(song: Dict[str, Any]) -> str:
    """Generates a canonical deduplication key from the cleaned song title."""
    raw_t = song.get("title") or song.get("song") or ""
    base_t = _clean_track_title(raw_t).lower()
    base_t = re.sub(r"[^a-z0-9]+", "", base_t)
    return base_t or str(song.get("id", ""))


def _deduplicate_songs(songs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Removes duplicate/repeated songs that share the same ID, media_url, or normalized base title."""
    unique = []
    seen_keys = set()
    seen_ids = set()
    seen_urls = set()
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id", "")).strip()
        murl = str(s.get("media_url", "")).strip()
        dkey = _dedup_key(s)
        if (sid and sid in seen_ids) or (dkey and dkey in seen_keys) or (murl and murl in seen_urls and murl not in GUARANTEED_STREAMS):
            continue
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


def search_for_song(query: str, lyrics: bool = False, songdata: bool = True, page: int = 1, limit: int = 40) -> Any:
    """
    Searches JioSaavn using both search.getResults (paginated, up to 40 songs per page)
    and autocomplete.get + song.getDetails from JioSaavnAPI-master, with strict deduplication.
    """
    clean_q = query.strip()
    if clean_q.startswith("http") and "saavn.com" in clean_q:
        sid = get_song_id(clean_q)
        return get_song(sid, lyrics) if sid else []

    collected_ids = []

    # 1. Paginated search.getResults
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

    # 2. Autocomplete fallback on page 1
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
            return _deduplicate_songs(songs)

    return []


def _search_saavn_mirrors(query: str, page: int = 1, limit: int = 40) -> List[Dict[str, Any]]:
    """Fallback to public JioSaavn mirrors with strict deduplication."""
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
                    return _deduplicate_songs(results)
        except Exception:
            pass
    return []


def search_songs(query: str, page: int = 1, limit: int = 40) -> List[Dict[str, Any]]:
    clean_q = query.strip()
    if not clean_q:
        return get_category_songs("home", page=page)

    saavn_songs = search_for_song(clean_q, lyrics=False, songdata=True, page=page, limit=limit)
    if isinstance(saavn_songs, list) and saavn_songs:
        return _deduplicate_songs(saavn_songs)
    elif isinstance(saavn_songs, dict) and saavn_songs.get("media_url"):
        return [saavn_songs]

    mirror_songs = _search_saavn_mirrors(clean_q, page=page, limit=limit)
    if mirror_songs:
        return _deduplicate_songs(mirror_songs)

    return FALLBACK_CATALOG


def get_category_songs(category: str, page: int = 1) -> List[Dict[str, Any]]:
    """
    Prioritizes Telugu songs first on Home/All, Telugu, Melodies, and Trending tabs,
    with strict deduplication so no song appears twice.
    """
    cat = category.lower().strip()
    category_queries = {
        "home": [
            "Latest Telugu Hit Songs",
            "Telugu Top Songs Sid Sriram Thaman DSP Mickey J Meyer",
            "Telugu Blockbuster Movie Songs Anirudh",
            "Feel Good Love Songs in Telugu",
            "Telugu Evergreen Melodies"
        ],
        "telugu": [
            "Telugu Top Hits Sid Sriram Thaman DSP",
            "Latest Telugu Movie Songs Devara Pushpa Guntur Kaaram",
            "Mickey J Meyer Anirudh Vivek Sagar Telugu Songs",
            "Allu Arjun Prabhas Mahesh Babu Nani Telugu Hits",
            "SP Balasubrahmanyam Chitra Telugu Melodies"
        ],
        "melodies": [
            "Telugu Feel Good Love Songs Sid Sriram",
            "Mickey J Meyer Sirivennela Telugu Melodies",
            "AR Rahman Ilaiyaraaja Keeravani Telugu Songs",
            "Anurag Kulkarni Shreya Ghoshal Telugu Songs"
        ],
        "trending": [
            "Trending Telugu Songs",
            "Telugu Viral Hits Mass Songs",
            "Top Telugu Chartbusters"
        ],
        "hindi": [
            "Hindi Top Hits",
            "Arijit Singh Pritam Bollywood Hits",
            "Latest Hindi Songs"
        ],
        "english": [
            "English Top Hits",
            "The Weeknd Ed Sheeran Taylor Swift Dua Lipa",
            "Pop Hits"
        ],
        "punjabi": [
            "Punjabi Top Hits",
            "AP Dhillon Diljit Dosanjh Karan Aujla",
            "Latest Punjabi Songs"
        ]
    }
    queries = category_queries.get(cat, [f"Telugu {cat} songs", f"{cat} top songs"])
    selected_q = queries[(page - 1) % len(queries)]
    sub_page = ((page - 1) // len(queries)) + 1

    if cat in ("home", "all", "") and page == 1:
        part1 = search_songs(queries[0], page=1, limit=30)
        part2 = search_songs(queries[1], page=1, limit=30)
        combined = _deduplicate_songs(part1 + part2)
        if combined:
            return combined

    results = search_songs(selected_q, page=sub_page, limit=40)
    return _deduplicate_songs(results) if results else FALLBACK_CATALOG


# =====================================================================
# PART 3 - SPOTIFY MOBILE & DESKTOP UI WITH VIDEO CANVAS, SWIPE & LYRICS
# Matches Screenshots 1-5:
#  - Home with Avatar + All/Music/Podcasts/Language pills + 2x4 Quick Grid
#  - "It's New Music Friday!" & "Jump back in" horizontal carousels
#  - Floating Mini-Player with Swipe Left (Next) / Swipe Right (Previous)
#  - Full-Screen Video Canvas Player (<video> background + swipe left/right)
#  - Scrollable Lyrics Preview Card + Show Lyrics + About the Artist + Explore + Credits
# =====================================================================

INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta id="vpMeta" name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, minimum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <script>
    // Accurately size both width and height (--vh-full) whether Mobile or "Desktop site" mode is active
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
  <link rel="stylesheet" href="/static/css/style.css?v=10">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%231db954'><circle cx='12' cy='12' r='11'/><polygon points='9.5 7.5 16.5 12 9.5 16.5 9.5 7.5' fill='%23000000'/></svg>">
</head>
<body>

  <!-- Hidden HTML5 Audio Streamer -->
  <audio id="audio" preload="metadata" crossorigin="anonymous"></audio>

  <div class="app-container">

    <!-- Desktop Sidebar / Mobile Library Drawer -->
    <aside class="sidebar" id="sidebar">
      <div class="spotify-box nav-box">
        <div class="brand">
          <div class="brand-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          </div>
          <div class="brand-name">Musify</div>
        </div>
        <nav class="nav-section">
          <a class="nav-link active" onclick="loadCategory('home')">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3l9-8z"/></svg>
            <span>Home</span>
          </a>
          <a class="nav-link" onclick="openSearchView()">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <span>Search</span>
          </a>
          <a class="nav-link" onclick="openLikedSongs()">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="#1db954"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
            <span>Liked Songs</span>
            <span id="likedCountBadge" class="song-badge">0</span>
          </a>
        </nav>
      </div>

      <div class="spotify-box library-box">
        <div class="playlist-header">
          <span>Your Library</span>
          <button id="btnNewPlaylist" class="btn-icon-small" title="Create Playlist">+</button>
        </div>
        <div id="userProfileCard" class="user-profile-card" onclick="openProfileModal()">
          <div id="userAvatar" class="user-avatar">P</div>
          <div class="user-profile-info">
            <div id="userNameDisplay" class="user-name-display">Listener</div>
            <div id="userAgeDisplay" class="user-age-display">Tap to edit profile</div>
          </div>
        </div>
        <div id="sidebarPlaylists" class="sidebar-playlists"></div>
      </div>
    </aside>

    <!-- Main View (Matches Screenshot 70501.jpg) -->
    <main class="main-area" id="mainArea">

      <!-- Top Bar: Avatar + All / Music / Podcasts / Languages Pills -->
      <header class="top-bar">
        <div id="topBarAvatar" class="top-avatar" onclick="openProfileModal()" title="Profile">P</div>
        <div class="category-pills">
          <button class="pill-btn active" data-category="home">All (Telugu)</button>
          <button class="pill-btn" data-category="telugu">Telugu Hits</button>
          <button class="pill-btn" data-category="melodies">Telugu Melodies</button>
          <button class="pill-btn" data-category="trending">Telugu Trending</button>
          <button class="pill-btn" data-category="hindi">Hindi</button>
          <button class="pill-btn" data-category="english">English</button>
          <button class="pill-btn" data-category="punjabi">Punjabi</button>
        </div>
      </header>

      <!-- Collapsible Search Bar -->
      <div id="searchBarWrap" class="search-bar-wrap">
        <div class="search-box">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#b3b3b3" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" id="query" placeholder="What do you want to listen to?" autocomplete="off">
          <button id="btnClearSearch" class="btn-clear-search" onclick="clearSearchInput()">&times;</button>
        </div>
      </div>

      <!-- Scrollable Home & Discovery Content -->
      <section class="content-scroll" id="contentScroll">

        <!-- Home Exclusive Sections (2x4 Grid + Carousels from Screenshot 70501.jpg) -->
        <div id="homeHeroSections">
          <!-- 8 Compact 2-Column Rectangular Tiles -->
          <div id="quickTilesGrid" class="quick-tiles-grid"></div>

          <!-- Horizontal Carousel 1: It's New Music Friday! -->
          <div class="home-section">
            <h2 class="section-title">It's New Music Friday!</h2>
            <div id="newMusicRow" class="horiz-scroll-row"></div>
          </div>

          <!-- Horizontal Carousel 2: Jump back in -->
          <div class="home-section">
            <h2 class="section-title">Jump back in</h2>
            <div id="jumpBackRow" class="horiz-scroll-row"></div>
          </div>
        </div>

        <div class="view-header">
          <h1 id="heading" class="view-heading">Recommended for today</h1>
          <p id="subheading" class="view-subheading">Swipe left/right on the player to change songs</p>
        </div>

        <!-- Main Song Grid / Playlist View -->
        <div id="content"></div>
      </section>

      <!-- Floating Spotify Mini-Player Bar (Screenshot 70501.jpg bottom) -->
      <div id="miniPlayer" class="mini-player" onclick="openNowPlayingSheet(event)">
        <div class="mini-banner-hint" id="miniBannerHint">Similar to the album you chose • Swipe left/right to change song</div>
        <div class="mini-player-main">
          <img id="playerImg" class="mini-thumb" src="https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=200&auto=format&fit=crop&q=80" alt="">
          <div class="mini-info">
            <div id="playerTitle" class="mini-title">Tap a song to start</div>
            <div class="mini-artist-row">
              <span class="sparkle-badge">✦</span>
              <span id="playerArtist" class="mini-artist">Swipe left for next • Swipe right for previous</span>
            </div>
          </div>
          <div class="mini-actions" onclick="event.stopPropagation()">
            <button class="mini-icon-btn" onclick="openNowPlayingSheet()" title="Video & Lyrics">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
            <button id="btnMiniLike" class="mini-icon-btn" onclick="toggleLikeCurrent()" title="Add to Liked Songs">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
            </button>
            <button id="btnPlay" class="mini-play-btn" title="Play / Pause">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
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
       FULL-SCREEN SPOTIFY CANVAS VIDEO & LYRICS SHEET (Screenshots 2-5)
       Swipe Left = Next Song | Swipe Right = Previous Song
       Scroll Down = Lyrics Preview + About the Artist + Explore + Credits
       ================================================================= -->
  <div id="nowPlayingSheet" class="np-sheet">
    <!-- Full-Bleed Looping Canvas Video Background -->
    <div id="npSwipeZone" class="np-video-stage">
      <video id="canvasVideo" class="np-canvas-video" autoplay loop muted playsinline preload="auto"></video>
      <img id="npBackdropImg" class="np-backdrop-img" src="" alt="">
      <div class="np-video-gradient"></div>

      <!-- Top Header inside Full-Screen Player -->
      <div class="np-top-bar">
        <button class="np-icon-btn" onclick="closeNowPlayingSheet()" title="Minimize">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="np-top-center">
          <div class="np-top-sub">Based on your interest in</div>
          <div id="npAlbumHeader" class="np-top-album">Maestro</div>
        </div>
        <button class="np-icon-btn" onclick="openAddToPlaylistCurrent()" title="Add to Playlist">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="#fff"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
        </button>
      </div>

      <!-- Clean video stage (no middle banner blocking the video) -->
      <div id="npSwipeIndicator" style="display:none;"></div>

      <!-- Bottom Overlay on Video Stage (Live Floating Lyric + Track Info + Controls) -->
      <div class="np-stage-bottom">
        <!-- Live Floating Lyric Line over Video (Screenshot 70502.jpg) -->
        <div id="npFloatingLyric" class="np-floating-lyric" onclick="scrollToLyricsCard()"></div>

        <!-- Track Info Row (Thumbnail + Title/Artist + X + Plus) -->
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

        <!-- Scrubber Seek Bar -->
        <div class="np-scrubber-wrap">
          <input type="range" id="progressBar" class="np-range-slider" min="0" max="100" step="0.1" value="0">
          <div class="np-time-row">
            <span id="currentTime">0:00</span>
            <span id="totalDuration">0:00</span>
          </div>
        </div>

        <!-- Main Transport Controls (Shuffle, Prev, Big White Circle Play/Pause, Next, Repeat) -->
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

          <button id="btnRepeat" class="np-ctrl-btn" title="Repeat / Timer">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="13" r="8"/><polyline points="12 9 12 13 15 15"/><line x1="9" y1="2" x2="15" y2="2"/></svg>
          </button>
        </div>

        <!-- Secondary Bottom Actions Row (Speaker, Share, Queue) -->
        <div class="np-sub-actions">
          <button class="np-icon-btn" onclick="scrollToLyricsCard()" title="Lyrics & Info">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
          <div style="display:flex; gap:20px;">
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

    <!-- Scrollable Cards Below Video Stage (Screenshots 70503.jpg & 70504.jpg) -->
    <div class="np-below-fold">

      <!-- 1. Lyrics Preview Card (Screenshot 70503.jpg) -->
      <div id="lyricsPreviewCard" class="np-lyrics-card">
        <div class="np-card-header">
          <span>Lyrics preview</span>
        </div>
        <div id="lyricsContent" class="np-lyrics-body">
          Tap any song to load live lyrics here...
        </div>
        <button id="btnExpandLyrics" class="np-show-lyrics-pill" onclick="toggleExpandLyrics()">
          Show lyrics
        </button>
      </div>

      <!-- 2. About the Artist Card (Screenshots 70503.jpg & 70504.jpg) -->
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
                <span class="verified-check" title="Verified Artist">✔</span>
              </div>
              <div class="np-monthly-listeners">41.3L monthly listeners</div>
            </div>
            <button class="np-follow-btn" onclick="this.textContent = this.textContent === 'Follow' ? 'Following' : 'Follow'">Follow</button>
          </div>
          <p id="npArtistBio" class="np-artist-bio">
            Featured film composer, music producer, playback vocalist, and songwriter streaming in high-definition audio on Musify.
          </p>
        </div>
      </div>

      <!-- 3. Explore Artist 3-Card Row (Screenshot 70504.jpg) -->
      <div class="np-info-card np-padded-card">
        <h3 id="npExploreHeading" class="np-section-heading">Explore Artist</h3>
        <div id="npExploreGrid" class="np-explore-grid"></div>
      </div>

      <!-- 4. Credits Card (Screenshot 70504.jpg) -->
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

  <!-- Hidden volume elements for compatibility -->
  <div style="display:none;">
    <input type="range" id="volumeBar" min="0" max="1" step="0.01" value="0.85">
    <button id="btnVolume"></button>
    <div id="lyricsBox"></div>
    <button id="btnLyricsToggle"></button>
    <button id="btnCloseLyrics"></button>
    <span id="lyricsSongTitle"></span>
    <span id="lyricsSongArtist"></span>
    <img id="lyricsThumb" alt="">
  </div>

  <!-- Bottom 5-Tab Navigation Bar (Matches Screenshot 70501.jpg) -->
  <nav class="mobile-bottom-nav">
    <button class="mob-tab active" id="tabHome" onclick="closeNowPlayingSheet(); loadCategory('home')">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3l9-8z"/></svg>
      <span>Home</span>
    </button>
    <button class="mob-tab" id="tabSearch" onclick="closeNowPlayingSheet(); openSearchView()">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <span>Search</span>
    </button>
    <button class="mob-tab" id="tabLibrary" onclick="closeNowPlayingSheet(); toggleMobileSidebar()">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="4" y1="4" x2="4" y2="20"/><line x1="9" y1="4" x2="9" y2="20"/><line x1="14" y1="6" x2="19" y2="20"/></svg>
      <span>Your Library</span>
    </button>
    <button class="mob-tab" id="tabLyrics" onclick="openNowPlayingSheet(); setTimeout(scrollToLyricsCard, 250)">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10"/><path d="M8 10c2.5-1 5.5-1 8 0M8.5 13c2-0.8 5-0.8 7 0M9.5 16c1.5-0.5 3.5-0.5 5 0" stroke="#000" stroke-width="1.6" fill="none"/></svg>
      <span>Lyrics</span>
    </button>
    <button class="mob-tab" id="tabCreate" onclick="closeNowPlayingSheet(); document.getElementById('btnNewPlaylist').click()">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="12" y1="4" x2="12" y2="20"/><line x1="4" y1="12" x2="20" y2="12"/></svg>
      <span>Create</span>
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
          <input type="text" id="playlistNameInput" placeholder="e.g. Telugu Melodies, Gym Mix" required>
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

  <!-- Modal: Quick Welcome Onboarding (Name & Age Only) -->
  <div id="welcomeModal" class="modal-overlay">
    <div class="modal-card welcome-card">
      <div class="welcome-badge">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>
      </div>
      <h2 class="welcome-title">Welcome to Musify</h2>
      <p class="welcome-subtitle">No login needed! Enter your name and age to start streaming with live video backgrounds, swipe song switching, and lyrics.</p>
      <form id="formWelcomeProfile">
        <div class="form-group">
          <label for="userNameInput">Your Name *</label>
          <input type="text" id="userNameInput" placeholder="e.g. Pranav, Karthik, Priya" required autocomplete="name">
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
  <script src="/static/js/player.js?v=10"></script>
  <script src="/static/js/app.js?v=10"></script>
  <script>
    // Force-clear any old desktop cached assets from previous service workers
    if ("caches" in window) {
      caches.keys().then((keys) => {
        keys.forEach((k) => {
          if (k !== "musify-synced-lyrics-v10") caches.delete(k);
        });
      }).catch(() => {});
    }
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js?v=10").then((reg) => reg.update()).catch(() => {});
      });
    }
  </script>
</body>
</html>
'''

STYLE_CSS = r'''/* ==========================================================================
   Musify • 1:1 Spotify Mobile & Desktop Stylesheet (Matching Screenshots 1-5)
   ========================================================================== */

:root {
  --bg-black: #000000;
  --bg-main: #121212;
  --bg-card: #1f1f1f;
  --bg-card-hover: #2a2a2a;
  --bg-tinted: rgba(255, 255, 255, 0.08);
  --bg-tinted-hover: rgba(255, 255, 255, 0.15);
  --border-color: rgba(255, 255, 255, 0.1);
  --text-main: #ffffff;
  --text-muted: #b3b3b3;
  --text-dim: #777777;
  --accent: #1db954;
  --accent-hover: #1ed760;
  --danger: #f15e6c;
  --sidebar-width: 260px;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;
  --transition: all 0.2s ease;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
  -webkit-tap-highlight-color: transparent;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-color: var(--bg-black);
  color: var(--text-main);
  overflow: hidden;
  height: var(--vh-full, 100dvh);
  display: flex;
  flex-direction: column;
  user-select: none;
}

.app-container {
  display: flex;
  flex: 1;
  height: calc(var(--vh-full, 100dvh) - 56px);
  position: relative;
  overflow: hidden;
}

/* --- Full-Bleed Edge-to-Edge Mobile Layout (Zero Black Sidebars) --- */
html, body {
  width: 100%;
  max-width: 100vw;
  overflow-x: hidden;
}

.app-container {
  width: 100%;
  max-width: 100%;
  margin: 0;
  overflow-x: hidden;
}

/* Sidebar is ALWAYS a hidden slide-over mobile drawer (never a fixed desktop sidebar) */
.sidebar {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 58px;
  width: 280px;
  max-width: 82vw;
  background-color: var(--bg-black);
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
  z-index: 75;
  transform: translateX(-105%);
  transition: transform 0.28s cubic-bezier(0.2, 0.9, 0.3, 1);
  box-shadow: 10px 0 30px rgba(0, 0, 0, 0.85);
}

.sidebar.mobile-open {
  transform: translateX(0);
}

.spotify-box {
  background-color: var(--bg-main);
  border-radius: var(--radius-md);
  padding: 14px;
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
  margin-bottom: 12px;
}

.brand-icon {
  width: 32px;
  height: 32px;
  background-color: var(--accent);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-name {
  font-size: 20px;
  font-weight: 800;
}

.nav-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}

.nav-link:hover, .nav-link.active {
  color: #fff;
  background-color: var(--bg-tinted);
}

.song-badge {
  margin-left: auto;
  background: var(--bg-tinted);
  color: #fff;
  padding: 2px 8px;
  border-radius: 99px;
  font-size: 11px;
}

.playlist-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 10px;
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 700;
}

.btn-icon-small {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 50%;
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
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
}

.playlist-item:hover, .playlist-item.active {
  color: #fff;
  background-color: var(--bg-card);
}

/* --- Main Area (Screenshot 70501.jpg) --- */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  background-color: var(--bg-main);
  position: relative;
  overflow: hidden;
}

.top-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px 8px 16px;
  background-color: var(--bg-main);
  z-index: 10;
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
  background-color: #262626;
  border: 1px solid rgba(255, 255, 255, 0.08);
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
  border-color: var(--accent);
  font-weight: 700;
}

/* Search Bar */
.search-bar-wrap {
  padding: 4px 16px 8px 16px;
}

.search-box {
  display: flex;
  align-items: center;
  background-color: #242424;
  border-radius: 8px;
  padding: 8px 12px;
}

.search-box input {
  background: transparent;
  border: none;
  outline: none;
  color: #fff;
  font-size: 14px;
  width: 100%;
  margin-left: 8px;
}

.btn-clear-search {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
}

/* Scrollable Content */
.content-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 8px 16px 135px 16px;
}

/* 2-Column 8-Card Grid (Screenshot 70501.jpg) */
.quick-tiles-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 24px;
  width: 100%;
}

.quick-tile {
  display: flex;
  min-width: 0;
  align-items: center;
  background-color: #262626;
  border-radius: 6px;
  overflow: hidden;
  height: 54px;
  cursor: pointer;
  transition: background-color 0.2s;
}

.quick-tile:hover {
  background-color: #333333;
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
  color: #fff;
  padding: 0 10px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.25;
}

/* Horizontal Carousels ("It's New Music Friday!" & "Jump back in") */
.home-section {
  margin-bottom: 24px;
}

.section-title {
  font-size: 21px;
  font-weight: 800;
  letter-spacing: -0.4px;
  margin-bottom: 12px;
}

.horiz-scroll-row {
  display: flex;
  gap: 14px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 4px;
}

.horiz-scroll-row::-webkit-scrollbar {
  display: none;
}

.horiz-card {
  width: 152px;
  flex-shrink: 0;
  cursor: pointer;
}

.horiz-card-img-wrap {
  position: relative;
  width: 152px;
  height: 152px;
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
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background-color: var(--accent);
  color: #000;
  font-size: 11px;
  font-weight: 900;
  display: flex;
  align-items: center;
  justify-content: center;
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

/* Main Song Cards Grid */
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
  grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
  gap: 14px;
}

.song-card {
  min-width: 0;
  overflow: hidden;
  background-color: #1a1a1a;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition: var(--transition);
}

.song-card:hover {
  background-color: #262626;
}

.song-card.playing-now {
  border: 1px solid var(--accent);
}

.card-img-wrapper {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 8px;
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
  font-size: 13.5px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-artist {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-actions {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px solid rgba(255,255,255,0.06);
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

/* --- Floating Mini-Player Bar (Screenshot 70501.jpg) --- */
.mini-player {
  position: fixed;
  left: 8px;
  right: 8px;
  bottom: 62px;
  background-color: #1f2429;
  border-radius: 10px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.75);
  z-index: 35;
  overflow: hidden;
  cursor: pointer;
  touch-action: pan-y;
}

.mini-banner-hint {
  font-size: 11.5px;
  color: #d1d5db;
  padding: 7px 12px 4px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.mini-player-main {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
}

.mini-thumb {
  width: 42px;
  height: 42px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
}

.mini-info {
  flex: 1;
  overflow: hidden;
}

.mini-title {
  font-size: 13.5px;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini-artist-row {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-muted);
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

.mini-actions {
  display: flex;
  align-items: center;
  gap: 10px;
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
  height: 2.5px;
  background-color: rgba(255, 255, 255, 0.2);
  width: 100%;
}

.mini-progress-fill {
  height: 100%;
  background-color: #ffffff;
  width: 0%;
  transition: width 0.15s linear;
}

/* --- Bottom 5-Tab Navigation Bar (Screenshot 70501.jpg) --- */
.mobile-bottom-nav {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 58px;
  background: linear-gradient(180deg, rgba(0,0,0,0.88) 0%, #000000 100%);
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
  gap: 4px;
  font-size: 10.5px;
  font-weight: 600;
  cursor: pointer;
}

.mob-tab.active, .mob-tab:hover {
  color: #ffffff;
}

/* =====================================================================
   FULL-SCREEN SPOTIFY VIDEO CANVAS & LYRICS SHEET (Screenshots 2-5)
   ===================================================================== */
.np-sheet {
  position: fixed;
  inset: 0;
  background-color: #0c0c0c;
  z-index: 80;
  overflow-y: auto;
  transform: translateY(100%);
  transition: transform 0.32s cubic-bezier(0.2, 0.9, 0.3, 1);
}

.np-sheet.open {
  transform: translateY(0);
}

.np-video-stage {
  position: relative;
  width: 100%;
  height: var(--stage-h, 86dvh);
  max-height: var(--stage-h, 86dvh);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  overflow: hidden;
  background-color: #141414;
  touch-action: pan-y;
}

.np-canvas-video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  z-index: 1;
}

.np-backdrop-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  filter: brightness(0.65) contrast(1.08);
  animation: kenBurnsZoom 18s ease-in-out infinite alternate;
  z-index: 0;
}

@keyframes kenBurnsZoom {
  0% { transform: scale(1) translate(0, 0); }
  100% { transform: scale(1.04) translate(0, 0); }
}

.np-video-gradient {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0.55) 0%,
    rgba(0, 0, 0, 0.1) 35%,
    rgba(0, 0, 0, 0.45) 68%,
    rgba(12, 12, 12, 0.98) 100%
  );
  z-index: 2;
}

.np-top-bar {
  position: relative;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 18px 10px 18px;
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

.np-swipe-indicator {
  position: relative;
  z-index: 5;
  align-self: center;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(8px);
  color: rgba(255, 255, 255, 0.85);
  font-size: 11px;
  font-weight: 600;
  padding: 5px 14px;
  border-radius: 999px;
  pointer-events: none;
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

/* Floating Lyric Line on Video (Screenshot 70502.jpg) */
.np-floating-lyric {
  font-size: 15px;
  font-weight: 800;
  color: #ffffff;
  text-shadow: 0 2px 10px rgba(0, 0, 0, 0.85);
  min-height: 20px;
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
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.6);
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
  font-size: 14px;
  color: rgba(255, 255, 255, 0.78);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

/* Scrubber */
.np-scrubber-wrap {
  display: flex;
  flex-direction: column;
  gap: 6px;
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

/* Transport Controls Row (Screenshot 70502.jpg & 70505.jpg) */
.np-controls-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 0;
}

.np-ctrl-btn {
  background: transparent;
  border: none;
  color: #ffffff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.np-ctrl-btn.active {
  color: var(--accent);
}

.np-big-play-btn {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background-color: #ffffff;
  color: #000000;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}

.np-sub-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 4px;
}

/* --- Below-the-Fold Cards inside Now Playing Sheet (Screenshots 3 & 4) --- */
.np-below-fold {
  max-width: 560px;
  margin: 0 auto;
  padding: 8px 16px 60px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* Lyrics Preview Card (Screenshot 70503.jpg) */
.np-lyrics-card {
  background: linear-gradient(165deg, #353f4e 0%, #272f3b 100%);
  border-radius: 16px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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
  padding: 8px 4px;
  transition: max-height 0.35s ease;
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
  transition: color 0.22s ease, transform 0.22s ease, text-shadow 0.22s ease;
  cursor: pointer;
  transform-origin: left center;
}

.lyric-line.past {
  color: rgba(255, 255, 255, 0.58);
}

.lyric-line.active {
  color: #ffffff;
  transform: scale(1.03);
  text-shadow: 0 2px 14px rgba(255, 255, 255, 0.45);
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

/* About the Artist Card (Screenshots 70503.jpg & 70504.jpg) */
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
  height: 220px;
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
  margin-bottom: 12px;
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
  margin-top: 2px;
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

/* Explore Artist 3-Column Row (Screenshot 70504.jpg) */
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
  height: 165px;
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
  line-height: 1.25;
}

/* Credits Card (Screenshot 70504.jpg) */
.np-credits-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.np-credit-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.np-credit-name {
  font-size: 15px;
  font-weight: 700;
  color: #ffffff;
}

.np-credit-role {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-top: 2px;
}

/* Queue Drawer, Modals & Responsive Rules */
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
  box-shadow: -8px 0 28px rgba(0,0,0,0.75);
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

.user-profile-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background-color: var(--bg-tinted);
  padding: 8px 10px;
  border-radius: 6px;
  margin-bottom: 10px;
  cursor: pointer;
}

.user-avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background-color: #b86b43;
  color: #fff;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
}

.user-name-display {
  font-size: 13px;
  font-weight: 700;
}

.user-age-display {
  font-size: 11px;
  color: var(--text-muted);
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

.track-table {
  width: 100%;
  border-collapse: collapse;
}

.track-table th, .track-table td {
  padding: 10px 8px;
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
  width: 40px;
  height: 40px;
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
  bottom: 125px;
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

/* Full-width edge-to-edge mini-player, bottom nav, and 2-column equal grid */
.mini-player {
  width: auto;
  max-width: none;
  left: 8px;
  right: 8px;
}

.mobile-bottom-nav {
  width: 100%;
  max-width: none;
  left: 0;
  right: 0;
}

.cards-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  gap: 12px;
  width: 100%;
}

.np-stage-bottom, .np-below-fold {
  max-width: 100%;
}
'''

PLAYER_JS = r'''/* ==========================================================================
   Musify Spotify Canvas Player:
   - Looping Background Canvas Video (<video id="canvasVideo">)
   - Touch Swipe Left -> Next Song | Touch Swipe Right -> Previous Song
   - Live Synced Lyrics Preview + Artist Bio + Explore + Credits
   ========================================================================== */
(function () {
  const $ = (id) => document.getElementById(id);
  const audio = $("audio");
  const canvasVideo = $("canvasVideo");

  const ICON_PLAY_MINI = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  const ICON_PAUSE_MINI = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
  const ICON_PLAY_BIG = '<svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  const ICON_PAUSE_BIG = '<svg width="28" height="28" viewBox="0 0 24 24" fill="#000"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';

  // Curated High-Speed Vertical Canvas Video Loops (Concert, Dance, Neon, Aesthetic Vibes)
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
    lyricLines: []
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
      canvasVideo.load();
    }
    const p = canvasVideo.play();
    if (p && p.catch) p.catch(() => {});
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

    // Tap any lyric line to seek the song directly to that vocal line!
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

    // Fallback timed lines if no external lyrics exist
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

  // ---- Artist Info, Explore Cards & Credits (Screenshots 3 & 4) ----
  function updateArtistAndCreditsCards(s) {
    if (!s) return;
    const artists = (s.artist || "Featured Artist").split(",").map((a) => a.trim()).filter(Boolean);
    const mainArtist = artists[0] || "Featured Artist";
    const secondArtist = artists[1] || s.music || mainArtist;

    if ($("npAlbumHeader")) $("npAlbumHeader").textContent = s.album || s.title || "Musify Mix";
    if ($("miniBannerHint")) $("miniBannerHint").textContent = "Similar to " + (s.album || s.title);
    if ($("npArtistBannerImg") && s.image_url) $("npArtistBannerImg").src = s.image_url;
    if ($("npAboutArtistName")) $("npAboutArtistName").textContent = mainArtist;
    if ($("npArtistBio")) {
      $("npArtistBio").textContent =
        mainArtist + " is a celebrated music artist, composer, and performer featured on " +
        (s.album || s.title) + ". Tap Follow or explore more tracks below.";
    }

    if ($("npExploreHeading")) $("npExploreHeading").textContent = "Explore " + mainArtist;
    if ($("npExploreGrid")) {
      const img = esc(s.image_url || "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=300&auto=format&fit=crop&q=80");
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
    if (audio.currentTime > 4) { audio.currentTime = 0; return; }
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
    if ($("btnPlay")) $("btnPlay").innerHTML = playing ? ICON_PAUSE_MINI : ICON_PLAY_MINI;
    if ($("btnNpPlay")) $("btnNpPlay").innerHTML = playing ? ICON_PAUSE_BIG : ICON_PLAY_BIG;
    if (canvasVideo) {
      if (playing) canvasVideo.play().catch(() => {});
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
      // Horizontal swipe threshold (> 50px and more horizontal than vertical)
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        if (dx < 0) {
          // Swiped Left -> Next Song
          toast("⏭ Next Song");
          next(false);
        } else {
          // Swiped Right -> Previous Song
          toast("⏮ Previous Song");
          prev();
        }
      }
    }, { passive: true });
  }

  attachSwipeGesture($("miniPlayer"));
  attachSwipeGesture($("npSwipeZone"));

  // ---- Full-Screen Sheet Helpers ----
  window.openNowPlayingSheet = function () {
    $("nowPlayingSheet").classList.add("open");
    if (canvasVideo && !audio.paused) canvasVideo.play().catch(() => {});
  };

  window.closeNowPlayingSheet = function () {
    $("nowPlayingSheet").classList.remove("open");
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
    playSongs(songs, startIndex, openFull) {
      state.queue = songs.slice();
      playAt(startIndex || 0);
      if (openFull) window.openNowPlayingSheet();
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

  // ---- Audio Events & Live Floating Lyric Line Sync ----
  audio.addEventListener("play", () => setPlayIcon(true));
  audio.addEventListener("pause", () => setPlayIcon(false));
  audio.addEventListener("ended", () => next(true));
  audio.addEventListener("loadedmetadata", () => {
    if ($("totalDuration")) $("totalDuration").textContent = fmt(audio.duration);
  });
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) {
      const pct = (audio.currentTime / audio.duration) * 100;
      if ($("progressBar")) $("progressBar").value = pct;
      if ($("miniProgressFill")) $("miniProgressFill").style.width = pct + "%";
      if ($("currentTime")) $("currentTime").textContent = fmt(audio.currentTime);

      // Highlight exact voice-synced lyric line & auto-scroll lyrics card
      syncLyricsToTime(audio.currentTime);
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

  $("btnPlay").addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
  $("btnNext").addEventListener("click", () => next(false));
  $("btnPrev").addEventListener("click", prev);
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

  $("btnQueueToggle").addEventListener("click", () => $("queuePanel").classList.toggle("open"));
  $("btnCloseQueue").addEventListener("click", () => $("queuePanel").classList.remove("open"));
  $("btnClearQueue").addEventListener("click", () => window.Player.clear());
  $("queueList").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-remove]");
    if (rm) { window.Player.removeAt(parseInt(rm.dataset.remove, 10)); return; }
    const item = e.target.closest(".queue-item");
    if (item) playAt(parseInt(item.dataset.i, 10));
  });

  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const k = e.key.toLowerCase();
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    else if (k === "n" || e.key === "ArrowRight") next(false);
    else if (k === "p" || e.key === "ArrowLeft") prev();
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

APP_JS = r'''/* Musify Spotify Mobile & Desktop Controller */
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
    home: ["Telugu Hits • Recommended for Today", "Top Telugu songs prioritized first • Tap any song for video & lyrics"],
    telugu: ["Telugu Blockbusters", "Sid Sriram, Thaman S, DSP, Mickey J. Meyer & Anirudh"],
    melodies: ["Feel Good Telugu Melodies", "Sirivennela, Maestro, Geetha Govindam & love hits"],
    trending: ["Trending Telugu Chartbusters", "Most streamed Telugu tracks right now"],
    hindi: ["Bollywood Top 50", "Arijit Singh, Pritam & chartbusters"],
    english: ["Global English Hits", "The Weeknd, Ed Sheeran & pop hits"],
    punjabi: ["Punjabi Party Anthems", "AP Dhillon, Diljit Dosanjh & more"]
  };

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
    closeMobileMenu();
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

  function closeMobileMenu() {
    $("sidebar").classList.remove("mobile-open");
  }

  window.toggleMobileSidebar = function () {
    $("sidebar").classList.toggle("mobile-open");
  };

  window.openSearchView = function () {
    closeMobileMenu();
    $("query").focus();
  };

  window.clearSearchInput = function () {
    $("query").value = "";
    loadCategory(currentCategory);
  };

  window.triggerArtistSearch = function (q) {
    $("query").value = q;
    runSearch(q);
  };

  // ---- Populate 2x4 Top Grid & Carousels (Screenshot 70501.jpg) ----
  function renderHomeHeroSections(songs) {
    if (!songs || !songs.length) return;
    const top8 = songs.slice(0, 8);
    if ($("quickTilesGrid")) {
      $("quickTilesGrid").innerHTML = top8.map((s, i) => `
        <div class="quick-tile" data-hero-i="${i}">
          <img src="${esc(s.image_url)}" alt="" loading="lazy">
          <div class="quick-tile-title">${esc(s.title)}</div>
        </div>`).join("");
    }

    const row1 = songs.slice(8, 18);
    if ($("newMusicRow") && row1.length) {
      $("newMusicRow").innerHTML = row1.map((s, idx) => `
        <div class="horiz-card" data-hero-i="${idx + 8}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(s.image_url)}" alt="" loading="lazy">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-sub">${esc(s.title)} • ${esc(s.artist)}</div>
        </div>`).join("");
    }

    const row2 = songs.slice(18, 28);
    if ($("jumpBackRow") && row2.length) {
      $("jumpBackRow").innerHTML = row2.map((s, idx) => `
        <div class="horiz-card" data-hero-i="${idx + 18}">
          <div class="horiz-card-img-wrap">
            <img src="${esc(s.image_url)}" alt="" loading="lazy">
            <div class="spotify-corner-badge">♪</div>
          </div>
          <div class="horiz-card-sub">${esc(s.album || s.title)} • ${esc(s.artist)}</div>
        </div>`).join("");
    }
  }

  $("homeHeroSections").addEventListener("click", (e) => {
    const el = e.target.closest("[data-hero-i]");
    if (!el) return;
    const idx = parseInt(el.dataset.heroI, 10);
    if (currentSongs[idx]) {
      window.Player.playSongs(currentSongs, idx, true);
    }
  });

  function normalizeSongKey(s) {
    const raw = String((s && (s.title || s.song)) || "").toLowerCase();
    return raw.replace(/\s*[\(\[].*?[\)\]]/g, "").replace(/\s*-\s*.*$/, "").replace(/[^a-z0-9]+/g, "") || String(s.id || "");
  }

  function deduplicateClientSongs(list) {
    const seen = new Set();
    const out = [];
    (list || []).forEach((s) => {
      if (!s) return;
      const k = normalizeSongKey(s);
      if (k && seen.has(k)) return;
      if (k) seen.add(k);
      out.push(s);
    });
    return out;
  }

  function renderSongs(songs, append, hideLoadMore) {
    currentSongs = deduplicateClientSongs(append ? currentSongs.concat(songs) : songs);
    if (!append && currentCategory === "home" && !activeSearchQuery && !openPlaylistId) {
      renderHomeHeroSections(currentSongs);
    }
    if (!currentSongs.length) {
      $("content").innerHTML = '<div style="color:var(--text-muted);padding:30px 0;">No songs found.</div>';
      return;
    }
    const cur = window.Player.currentSong();
    const isHomeDefault = (currentCategory === "home" && !activeSearchQuery && !openPlaylistId && currentSongs.length > 10);
    $("content").innerHTML = '<div class="cards-grid">' + currentSongs.map((s, i) => {
      if (isHomeDefault && i < 8) return "";
      const playing = Boolean(cur && cur.title === s.title);
      const liked = isSongLiked(s);
      return `
      <div class="song-card ${playing ? "playing-now" : ""}" data-i="${i}">
        <div class="card-img-wrapper">
          <img src="${esc(s.image_url)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
          <button class="card-play-btn" data-act="play" title="Play with Video & Lyrics">
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
          <button data-act="queue">+Queue</button>
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
      if (act === "play") window.Player.playSongs(currentSongs, i, true);
      else if (act === "like") {
        toggleLikeSong(song);
        if (btn) btn.classList.toggle("liked", isSongLiked(song));
      } else if (act === "lyrics") {
        window.Player.playSongs(currentSongs, i, true);
        setTimeout(window.scrollToLyricsCard, 250);
      } else if (act === "queue") window.Player.addToQueue(song);
      else if (act === "playlist") openAddToPlaylist(song);
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
        toast("Loaded " + more.length + " more songs");
      } else if (btn) {
        btn.style.display = "none";
      }
    } catch (err) {
      if (btn) { btn.textContent = "Load More Songs"; btn.disabled = false; }
    }
  }

  async function loadCategory(cat) {
    currentCategory = cat;
    activeSearchQuery = "";
    currentPage = 1;
    openPlaylistId = null;
    $("homeHeroSections").style.display = cat === "home" ? "block" : "none";
    document.querySelectorAll(".pill-btn").forEach((b) => b.classList.toggle("active", b.dataset.category === cat));
    const t = TITLES[cat] || [cat.charAt(0).toUpperCase() + cat.slice(1), ""];
    setHeading(t[0], t[1]);
    showLoading("Loading songs...");
    closeMobileMenu();
    try {
      const data = await api("/api/category/" + encodeURIComponent(cat) + "?page=1");
      const list = data.songs || [];
      renderSongs(list, false, false);
      // Pre-load first track into mini-player if queue is empty
      if (!window.Player.currentSong() && list.length) {
        window.Player.addToQueue(list[0]);
      }
    } catch (err) {}
  }
  window.loadCategory = loadCategory;

  async function runSearch(q) {
    openPlaylistId = null;
    activeSearchQuery = q;
    currentPage = 1;
    $("homeHeroSections").style.display = "none";
    setHeading('Results for "' + q + '"', "Tap any song to play with video & lyrics");
    showLoading("Searching...");
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(q) + "&page=1");
      renderSongs(data.songs || [], false, false);
    } catch (err) {}
  }

  $("query").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(() => {
      if (q) runSearch(q); else loadCategory(currentCategory);
    }, 420);
  });

  document.querySelectorAll(".pill-btn").forEach((b) => {
    b.addEventListener("click", () => { $("query").value = ""; loadCategory(b.dataset.category); });
  });

  // ---- Playlists (Dual-Synced: SQLite + localStorage) ----
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
      box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px;">No playlists yet. Tap + to create one.</div>';
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
    $("homeHeroSections").style.display = "none";
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
            <td><div class="track-title-cell"><img src="${esc(s.image_url)}" alt=""><span>${esc(s.title)}</span></div></td>
            <td>${esc(s.artist)}</td>
            <td><button class="btn-icon-small" data-act="remove">&times;</button></td>
          </tr>`).join("") + "</tbody></table>";
    }
    $("content").innerHTML = html;
  }

  async function openPlaylist(id) {
    openPlaylistId = id;
    closeMobileMenu();
    const localLists = getLocalPlaylists();
    const localMatch = localLists.find((p) => Number(p.id) === Number(id));
    try {
      const data = await api("/api/playlists/" + id);
      const p = data.playlist;
      if (localMatch && (!p.songs || !p.songs.length) && localMatch.songs && localMatch.songs.length) {
        p.songs = localMatch.songs;
      }
      renderPlaylistContent(p);
      loadPlaylists();
      return;
    } catch (err) {}
    if (localMatch) renderPlaylistContent(localMatch);
  }

  async function removeFromOpenPlaylist(entryId) {
    const localLists = getLocalPlaylists();
    const pl = localLists.find((p) => Number(p.id) === Number(openPlaylistId));
    if (pl && Array.isArray(pl.songs)) {
      pl.songs = pl.songs.filter((s) => String(s.id) !== String(entryId));
      pl.song_count = pl.songs.length;
      saveLocalPlaylists(localLists);
    }
    try { await api("/api/playlists/" + openPlaylistId + "/songs/" + entryId, { method: "DELETE" }); } catch (e) {}
    toast("Song removed");
    openPlaylist(openPlaylistId);
  }

  async function deleteOpenPlaylist() {
    if (!confirm("Delete this playlist?")) return;
    const localLists = getLocalPlaylists().filter((p) => Number(p.id) !== Number(openPlaylistId));
    saveLocalPlaylists(localLists);
    try { await api("/api/playlists/" + openPlaylistId, { method: "DELETE" }); } catch (e) {}
    toast("Playlist deleted");
    await loadPlaylists();
    loadCategory("home");
  }

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
    if (!lists.length) { toast("Tap Create (+) first to make a playlist"); return; }
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

  // ---- Profile Avatar & Onboarding ----
  function showProfile(p) {
    const ch = (p.name || "P").trim().charAt(0).toUpperCase();
    $("userNameDisplay").textContent = p.name;
    $("userAgeDisplay").textContent = "Age " + p.age;
    $("userAvatar").textContent = ch;
    if ($("topBarAvatar")) $("topBarAvatar").textContent = ch;
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

  saveLikedSongs(getLikedSongs());
  initProfile();
  loadPlaylists();
  loadCategory("home");
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

SW_JS = r'''const CACHE = "musify-synced-lyrics-v10";
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
