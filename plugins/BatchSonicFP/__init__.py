import re
import logging
import math
from datetime import datetime, timezone, date
from urllib.parse import urlencode

import requests
import numpy as np
from flask import Blueprint, request, redirect, url_for, flash

from rq import Queue
from redis import Redis
from flask import flash
from database import save_task_status

import config
from plugin.api import (
    get_db, get_setting, set_setting, render_page, manage_plugins_url, table
)

logger = logging.getLogger(__name__)
bp = Blueprint('batch_sonic_fp', __name__)

# Default settings applied on first installation
DEFAULT_SETTINGS = {
    'navidrome_url': 'http://navidrome:4533',
    'admin_username': 'navidrome',
    'extauth_header': 'Remote-User',
    'playlist_name': 'My Sonic Fingerprint',
    'max_tracks': 50,
    'top_albums_to_fetch': 20,
    'top_songs_per_user': 50,
    'num_neighbors': 50,
    'playlists_to_keep': 3,
}

def get_s(key):
    """Safely retrieve a plugin setting with a fallback to defaults."""
    val = get_setting(key)
    return DEFAULT_SETTINGS.get(key, '') if val is None else val

def migrate(db):
    """Installation hook. Populates default settings in the database."""
    for key, default in DEFAULT_SETTINGS.items():
        if get_setting(key) is None:
            set_setting(key, default)
    db.commit()

def _parse_subsonic_list(data):
    """
    Subsonic JSON responses sometimes return a single dict instead of a list 
    when there is only one element. This helper normalizes it to a list.
    """
    if isinstance(data, dict):
        return [data]
    return data or []

def get_all_users():
    """Fetches the list of all users using admin credentials via Subsonic getUsers."""
    base_url = get_s('navidrome_url').rstrip('/')
    admin_user = get_s('admin_username')
    extauth_header = get_s('extauth_header')

    if not admin_user:
        logger.error("Admin credentials not configured in plugin settings.")
        return []

    headers = {extauth_header: admin_user}

    try:
        res = requests.get(f"{base_url}/api/user", headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()

        users_raw = data
        users = []
        for user in users_raw:
            users.append(user["userName"])
        return users
    except Exception as e:
        logger.error(f"Failed to fetch Navidrome users: {e}")
        return []

def nd_request(endpoint, username, params=None):
    """
    Makes an authenticated request to Navidrome Subsonic API using ND_EXTAUTH headers.
    This completely bypasses standard Subsonic 'u'/'p' parameters for the target user.
    """
    base_url = get_s('navidrome_url').rstrip('/')
    extauth_header = get_s('extauth_header')
    
    params = params or {}
    params.update({'v': '1.16.1', 'c': 'AudioMusePlugin', 'f': 'json'})
    
    headers = {extauth_header: username}
    url = f"{base_url}/rest/{endpoint}.view"
    
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        res.raise_for_status()
        return res.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Navidrome API request failed for user {username} at {endpoint}: {e}")
        return None

def get_user_top_tracks(username):
    """Fetches top played tracks for a specific user using ND_EXTAUTH mechanics."""
    num_albums = get_s('top_albums_to_fetch')
    top_songs_limit = get_s('top_songs_per_user')
    
    # Fetch most frequently played albums for this user
    albums_data = nd_request('getAlbumList2', username, {'type': 'frequent', 'size': num_albums})
    if not albums_data:
        return []
        
    albums_raw = albums_data.get('subsonic-response', {}).get('albumList2', {}).get('album', [])
    albums = _parse_subsonic_list(albums_raw)
    
    all_tracks = []
    for album in albums:
        album_id = album.get('id')
        album_data = nd_request('getAlbum', username, {'id': album_id})
        if not album_data:
            continue
            
        songs_raw = album_data.get('subsonic-response', {}).get('album', {}).get('song', [])
        songs = _parse_subsonic_list(songs_raw)
        
        for song in songs:
            all_tracks.append({
                'provider_id': str(song.get('id')),
                'playCount': song.get('playCount', 0),
                'played': song.get('played', '')
            })
            
    # Sort by playCount descending and limit to the configured seed pool size
    all_tracks.sort(key=lambda x: x['playCount'], reverse=True)
    return all_tracks[:top_songs_limit]

def get_server_id():
    """Resolves the internal AudioMuse server_id for the configured Navidrome URL."""
    db = get_db()
    cur = db.cursor()
    base_url = get_s('navidrome_url').rstrip('/')
    clean_url = base_url.replace('http://', '').replace('https://', '')
    
    try:
        # FIXED: URL is stored inside JSONB 'creds' column, not as a separate column
        # FIXED: column is 'server_id', not 'id'; column is 'server_type', not 'type'
        cur.execute(
            "SELECT server_id FROM music_servers WHERE server_type = 'navidrome' AND creds::text LIKE %s",
            (f"%{clean_url}%",)
        )
        row = cur.fetchone()
        
        # Fallback to any navidrome server if fuzzy match fails
        if not row:
            cur.execute("SELECT server_id FROM music_servers WHERE server_type = 'navidrome' LIMIT 1")
            row = cur.fetchone()
            
        return row[0] if row else None
    except Exception as e:
        db.rollback()  # CRITICAL: reset transaction state after error
        logger.error(f"Failed to get server_id: {e}")
        return None
    finally:
        cur.close()

def compute_fingerprint_vector(username, top_tracks):
    """
    Maps Navidrome provider IDs to AudioMuse canonical IDs, fetches embeddings
    using the official get_tracks_by_ids() function, and computes a weighted 
    average vector representing the user's taste.
    """
    from database import get_tracks_by_ids
    
    server_id = get_server_id()
    if not server_id:
        logger.error("No Navidrome server found in AudioMuse-AI music_servers registry.")
        return None
        
    db = get_db()
    cur = db.cursor()
    
    provider_ids = [t['provider_id'] for t in top_tracks]
    
    try:
        # FIXED: column is 'provider_track_id', not 'provider_id'
        cur.execute(
            "SELECT provider_track_id, item_id FROM track_server_map WHERE server_id = %s AND provider_track_id = ANY(%s)",
            (server_id, provider_ids)
        )
        id_map = {row[0]: row[1] for row in cur.fetchall()}
        
        # Filter out tracks that haven't been analyzed by AudioMuse yet
        valid_tracks = [t for t in top_tracks if t['provider_id'] in id_map]
        if not valid_tracks:
            logger.warning(f"No analyzed tracks found in AudioMuse-AI for user {username}.")
            return None
            
        canonical_ids = [id_map[t['provider_id']] for t in valid_tracks]
        
        # FIXED: Use the official get_tracks_by_ids() function from database.py
        # This properly JOINs score with embedding table and returns embedding_vector as numpy array
        track_details = get_tracks_by_ids(canonical_ids)
        
        if not track_details:
            logger.warning(f"No track details found in database for user {username}'s top tracks.")
            return None
            
        # Build embeddings map from the returned track details
        embeddings_map = {}
        for track in track_details:
            if 'embedding_vector' in track and track['embedding_vector'] is not None:
                vec = track['embedding_vector']
                # get_tracks_by_ids returns numpy array, check if it has data
                if hasattr(vec, 'size') and vec.size > 0:
                    embeddings_map[track['item_id']] = vec
        
        if not embeddings_map:
            logger.warning(f"No embeddings found for user {username}'s top tracks.")
            return None
            
        # Calculate weighted average vector (decay based on recency and play count)
        weighted_vectors = []
        total_weight = 0.0
        
        for track in valid_tracks:
            cid = id_map[track['provider_id']]
            if cid not in embeddings_map:
                continue
                
            vector = embeddings_map[cid]
            play_count = track['playCount']
            
            # Logarithmic weight to prevent single massive playcount from dominating
            weight = math.log10(play_count + 1)
            
            # Apply time-decay based on last played date
            if track.get('played'):
                try:
                    played_str = track['played']
                    # Fix potential microsecond formatting issues in Subsonic dates
                    if '.' in played_str and played_str.endswith('Z'):
                        dot_idx = played_str.rfind('.')
                        played_str = played_str[:dot_idx + 7] + 'Z'
                    last_played_dt = datetime.fromisoformat(played_str.replace('Z', '+00:00'))
                    days_ago = (datetime.now(timezone.utc) - last_played_dt).days
                    
                    half_life = 30.0 # Days
                    decay_rate = -math.log(0.5) / half_life
                    weight *= math.exp(-decay_rate * max(0, days_ago))
                except Exception:
                    pass
                    
            weighted_vectors.append(vector * weight)
            total_weight += weight
            
        if total_weight == 0:
            return None
            
        average_vector = np.sum(weighted_vectors, axis=0) / total_weight
        valid_seed_ids = [id_map[t['provider_id']] for t in valid_tracks if id_map[t['provider_id']] in embeddings_map]
        
        return average_vector, valid_seed_ids
        
    except Exception as e:
        db.rollback()  # CRITICAL: reset transaction state after error
        logger.error(f"compute_fingerprint_vector failed: {e}")
        return None
    finally:
        cur.close()

def get_similar_tracks(average_vector, num_neighbors):
    """
    Finds similar tracks using the internal IVF index.
    Falls back to brute-force cosine similarity using get_tracks_by_ids() if IVF is inaccessible.
    """
    try:
        from tasks.ivf_manager import find_nearest_neighbors_by_vector
        return find_nearest_neighbors_by_vector(query_vector=average_vector, n=num_neighbors, eliminate_duplicates=True)
    except ImportError:
        logger.warning("Could not import tasks.ivf_manager. Falling back to brute-force cosine similarity.")
    
    from database import get_tracks_by_ids
    
    db = get_db()
    cur = db.cursor()
    
    try:
        # Get all item_ids from score table
        cur.execute("SELECT item_id FROM score LIMIT 10000")  # Limit for performance
        all_ids = [row[0] for row in cur.fetchall()]
        
        # Use official get_tracks_by_ids() to fetch embeddings
        all_tracks = get_tracks_by_ids(all_ids)
        
        similarities = []
        norm_query = np.linalg.norm(average_vector)
        if norm_query == 0:
            return []
            
        for track in all_tracks:
            vec = track.get('embedding_vector')
            if vec is None or not hasattr(vec, 'size') or vec.size == 0:
                continue
                
            norm_vec = np.linalg.norm(vec)
            if norm_vec == 0:
                continue
            
            cos_sim = np.dot(average_vector, vec) / (norm_query * norm_vec)
            similarities.append({'item_id': track['item_id'], 'distance': 1.0 - cos_sim})
            
        similarities.sort(key=lambda x: x['distance'])
        return similarities[:num_neighbors]
        
    except Exception as e:
        db.rollback()  # CRITICAL: reset transaction state after error
        logger.error(f"get_similar_tracks failed: {e}")
        return []
    finally:
        cur.close()

def get_matching_playlists(username, base_playlist_name):
    """Fetches all playlists matching the naming pattern and returns them sorted by date (newest first)."""
    playlists_data = nd_request('getPlaylists', username)
    if not playlists_data:
        return []
    
    playlists_raw = playlists_data.get('subsonic-response', {}).get('playlists', {}).get('playlist', [])
    playlists = _parse_subsonic_list(playlists_raw)
    
    escaped_username = re.escape(username)
    escaped_name = re.escape(base_playlist_name)
    pattern = rf'^\[{escaped_username}\] {escaped_name} - (\d{{4}}-\d{{2}}-\d{{2}})$'
    
    matching = []
    for pl in playlists:
        pl_name = pl.get('name', '')
        pl_id = pl.get('id')
        match = re.match(pattern, pl_name)
        if match and pl_id:
            date_str = match.group(1)
            try:
                pl_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                matching.append({
                    'id': pl_id,
                    'name': pl_name,
                    'date': pl_date,
                    'date_str': date_str
                })
            except ValueError:
                continue
    
    # Sort by date descending (newest first)
    matching.sort(key=lambda x: x['date'], reverse=True)
    return matching


def create_private_playlist(username, canonical_ids):
    """
    Creates or updates a dated playlist and rotates old ones.
    Rotation happens BEFORE creation to avoid exceeding the limit.
    """
    server_id = get_server_id()
    if not server_id:
        return
    
    db = get_db()
    cur = db.cursor()
    
    try:
        # Map canonical IDs to Navidrome provider IDs
        cur.execute(
            "SELECT item_id, provider_track_id FROM track_server_map WHERE server_id = %s AND item_id = ANY(%s)",
            (server_id, canonical_ids)
        )
        reverse_map = {row[0]: row[1] for row in cur.fetchall()}
        
        navidrome_ids = [reverse_map[cid] for cid in canonical_ids if cid in reverse_map]
        if not navidrome_ids:
            logger.error(f"No valid provider IDs found to create playlist for user {username}.")
            return
        
        base_playlist_name = get_s('playlist_name')
        playlists_to_keep = get_s('playlists_to_keep')
        today_str = date.today().strftime('%Y-%m-%d')
        full_playlist_name = f"[{username}] {base_playlist_name} - {today_str}"
        
        base_url_full = get_s('navidrome_url').rstrip('/')
        extauth_header = get_s('extauth_header')
        headers = {extauth_header: username}
        
        # ============================================================
        # STEP 1: Get existing playlists matching our pattern
        # ============================================================
        matching_playlists = get_matching_playlists(username, base_playlist_name)
        
        # ============================================================
        # STEP 2: Check if today's playlist already exists
        # ============================================================
        today_playlist_id = None
        for pl in matching_playlists:
            if pl['date_str'] == today_str:
                today_playlist_id = pl['id']
                break
        
        # ============================================================
        # STEP 3: ROTATION - delete old playlists BEFORE creating new one
        # ============================================================
        playlists_to_delete = []
        if today_playlist_id:
            # Today's playlist already exists: keep it, delete extras beyond playlists_to_keep
            # matching_playlists is sorted newest first; today's should be index 0
            playlists_to_delete = matching_playlists[playlists_to_keep:]
        else:
            # Today's playlist doesn't exist yet: we'll create one,
            # so keep only (playlists_to_keep - 1) existing ones to make room
            playlists_to_delete = matching_playlists[playlists_to_keep - 1:]
        
        deleted_count = 0
        for pl in playlists_to_delete:
            try:
                delete_data = nd_request('deletePlaylist', username, {'id': pl['id']})
                if delete_data:
                    deleted_count += 1
                    logger.info(f"Deleted old playlist: {pl['name']} (ID: {pl['id']})")
            except Exception as e:
                logger.error(f"Failed to delete playlist {pl['name']}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Rotation for {username}: deleted {deleted_count} old playlist(s).")
        
        # ============================================================
        # STEP 4: Create or update the playlist
        # ============================================================
        if today_playlist_id:
            # Today's playlist already exists - UPDATE it using createPlaylist with playlistId
            logger.info(f"Updating existing playlist '{full_playlist_name}' (ID: {today_playlist_id})")
            base_params = {'playlistId': today_playlist_id, 'name': full_playlist_name}
        else:
            # Create brand new playlist
            logger.info(f"Creating new playlist '{full_playlist_name}'")
            base_params = {'name': full_playlist_name}
        
        song_ids_param = [('songId', tid) for tid in navidrome_ids]
        query = urlencode(base_params)
        song_query = urlencode(song_ids_param, doseq=True)
        full_url = f"{base_url_full}/rest/createPlaylist.view?{query}&{song_query}&v=1.16.1&c=AudioMusePlugin&f=json"
        
        res = requests.get(full_url, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()
        
        playlist_id = data.get('subsonic-response', {}).get('playlist', {}).get('id')
        if not playlist_id:
            logger.error(f"Failed to parse playlist ID from createPlaylist for user {username}")
            return
        
        logger.info(f"Playlist '{full_playlist_name}' created/updated successfully (ID: {playlist_id})")
        
        # ============================================================
        # STEP 5: Make it private
        # ============================================================
        update_params = {'playlistId': playlist_id, 'public': 'false'}
        update_data = nd_request('updatePlaylist', username, update_params)
        if update_data:
            logger.info(f"Set playlist {playlist_id} to private for {username}")
        
    except Exception as e:
        db.rollback()
        logger.exception(f"Failed to create/update playlist for {username}: {e}")
    finally:
        cur.close()

def rotate_user_playlists(username, base_playlist_name, current_date_str):
    """
    Fetches all playlists for the user, filters by the mask:
    '[{username}] {base_playlist_name} - YYYY-MM-DD'
    Keeps only the last 3 playlists by date, deletes the rest.
    """
    try:
        # Fetch all user's playlists via Subsonic API
        playlists_data = nd_request('getPlaylists', username)
        if not playlists_data:
            logger.warning(f"Could not fetch playlists for user {username} during rotation.")
            return
        
        playlists_raw = playlists_data.get('subsonic-response', {}).get('playlists', {}).get('playlist', [])
        playlists = _parse_subsonic_list(playlists_raw)
        
        # Build regex pattern to match the naming convention
        # Escape username and base_playlist_name for regex safety
        escaped_username = re.escape(username)
        escaped_name = re.escape(base_playlist_name)
        pattern = rf'^\[{escaped_username}\] {escaped_name} - (\d{{4}}-\d{{2}}-\d{{2}})$'
        
        matching_playlists = []
        for pl in playlists:
            pl_name = pl.get('name', '')
            pl_id = pl.get('id')
            
            # Check if playlist name matches our mask
            match = re.match(pattern, pl_name)
            if match and pl_id:
                date_str = match.group(1)
                try:
                    pl_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    matching_playlists.append({
                        'id': pl_id,
                        'name': pl_name,
                        'date': pl_date,
                        'date_str': date_str
                    })
                except ValueError:
                    logger.warning(f"Could not parse date from playlist name: {pl_name}")
        
        # Sort by date descending (newest first)
        matching_playlists.sort(key=lambda x: x['date'], reverse=True)
        
        # Keep only the last 3, mark the rest for deletion
        to_keep = matching_playlists[:3]
        to_delete = matching_playlists[3:]
        
        if not to_delete:
            logger.info(f"User {username} has {len(matching_playlists)} matching playlist(s). No rotation needed.")
            return
        
        logger.info(f"Rotating playlists for {username}: keeping {len(to_keep)}, deleting {len(to_delete)} old ones.")
        
        # Delete old playlists
        deleted_count = 0
        for pl in to_delete:
            try:
                # Delete playlist via Subsonic API
                delete_data = nd_request('deletePlaylist', username, {'id': pl['id']})
                if delete_data:
                    deleted_count += 1
                    logger.debug(f"Deleted old playlist: {pl['name']} (ID: {pl['id']})")
            except Exception as e:
                logger.error(f"Failed to delete playlist {pl['name']}: {e}")
        
        logger.info(f"Successfully rotated playlists for {username}: kept {len(to_keep)}, deleted {deleted_count}.")
        
    except Exception as e:
        logger.exception(f"Failed to rotate playlists for user {username}: {e}")

def create_private_playlist(username, canonical_ids):
    """Reverse maps canonical IDs to Navidrome IDs, creates the playlist, and hides it."""
    server_id = get_server_id()
    if not server_id: return
    
    db = get_db()
    cur = db.cursor()
    
    try:
        # FIXED: column is 'provider_track_id', not 'provider_id'
        cur.execute(
            "SELECT item_id, provider_track_id FROM track_server_map WHERE server_id = %s AND item_id = ANY(%s)",
            (server_id, canonical_ids)
        )
        reverse_map = {row[0]: row[1] for row in cur.fetchall()}
        
        navidrome_ids = [reverse_map[cid] for cid in canonical_ids if cid in reverse_map]
        if not navidrome_ids:
            logger.error(f"No valid provider IDs found to create playlist for user {username}.")
            return
            
        playlist_name = get_s('playlist_name')
        base_url_full = get_s('navidrome_url').rstrip('/')
        extauth_header = get_s('extauth_header')
        headers = {extauth_header: username}
        
        # Subsonic createPlaylist requires an array of songId parameters
        base_params = {'name': f"[{username}] {playlist_name} - {date.today()}"}
        song_ids_param = [('songId', tid) for tid in navidrome_ids]
        
        query = urlencode(base_params)
        song_query = urlencode(song_ids_param, doseq=True)
        full_url = f"{base_url_full}/rest/createPlaylist.view?{query}&{song_query}&v=1.16.1&c=AudioMusePlugin&f=json"
        
        res = requests.get(full_url, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()
        
        playlist_id = data.get('subsonic-response', {}).get('playlist', {}).get('id')
        if not playlist_id:
            logger.error(f"Failed to parse playlist ID from createPlaylist for user {username}")
            return
            
        logger.info(f"Created playlist '{playlist_name}' (ID: {playlist_id}) for {username}")
        
        # Make it private using Navidrome's updatePlaylist extension (public=false)
        update_params = {'playlistId': playlist_id, 'public': 'false'}
        update_data = nd_request('updatePlaylist', username, update_params)
        if update_data:
            logger.info(f"Successfully set playlist {playlist_id} to private for {username}")
            
    except Exception as e:
        db.rollback()  # CRITICAL: reset transaction state after error
        logger.exception(f"Failed to create/update playlist for {username}: {e}")
    finally:
        cur.close()

def run_batch_task():
    """Main entry point executed by the AudioMuse-AI Cron scheduler or RQ worker."""
    
    # RQ workers operate outside the Flask application context.
    # We must explicitly create it to access get_setting(), get_db(), etc.
    try:
        from flask_app import app
        ctx = app.app_context()
        ctx.push()
    except Exception as e:
        logger.error(f"Failed to push app context in RQ worker: {e}")
        ctx = None

    try:
        logger.info("=== Starting Batch Sonic Fingerprint via ND_EXTAUTH ===")
        users = get_all_users()
        
        if not users:
            logger.warning("No users fetched. Aborting.")
            return
            
        num_neighbors = get_s('num_neighbors')
        max_tracks = get_s('max_tracks')
        
        for user in users:
            try:
                logger.info(f"Processing user: {user}")
                top_tracks = get_user_top_tracks(user)
                if not top_tracks:
                    logger.warning(f"No top tracks found for user {user}. Skipping.")
                    continue
                    
                result = compute_fingerprint_vector(user, top_tracks)
                if not result:
                    logger.warning(f"Could not compute fingerprint vector for user {user}. Skipping.")
                    continue
                    
                average_vector, seed_ids = result
                
                # Expand the seed pool using vector search
                similar_tracks = get_similar_tracks(average_vector, num_neighbors)
                similar_ids = [t['item_id'] for t in similar_tracks]
                
                # Combine seeds and neighbors, removing duplicates while preserving order
                final_ids = []
                seen = set()
                for cid in seed_ids + similar_ids:
                    if cid not in seen:
                        final_ids.append(cid)
                        seen.add(cid)
                        
                # Truncate to the final desired size
                final_ids = final_ids[:max_tracks]
                
                if not final_ids:
                    logger.warning(f"No final tracks generated for user {user}.")
                    continue
                    
                create_private_playlist(user, final_ids)
                
            except Exception as e:
                logger.exception(f"Fatal error processing user {user}: {e}")
                
        logger.info("=== Batch Sonic Fingerprint completed ===")
        
    finally:
        # Clean up the application context to prevent memory leaks in the worker
        if ctx is not None:
            try:
                ctx.pop()
            except Exception:
                pass

# --- UI ROUTES ---

@bp.route('/')
def home():
    # Generate URLs using Flask's url_for with blueprint prefix
    settings_url = url_for('batch_sonic_fp.settings')
    run_now_url = url_for('batch_sonic_fp.run_now')
    
    body = f'''
        <h3>Batch Sonic Fingerprint for Navidrome (ND_EXTAUTH)</h3>
        <p>This plugin generates personalized Sonic Fingerprints for <strong>all Navidrome users</strong> 
        without requiring their passwords. It utilizes Navidrome's externalized authentication headers.</p>
        
        <hr style="margin: 1.5rem 0;">
        
        <h4>Configuration & Management</h4>
        <div style="display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem;">
            <a href="{settings_url}" class="btn btn-primary" style="flex: 1; min-width: 150px; text-align: center; text-decoration: none; padding: .5rem 1rem; background: #007bff; color: white; border-radius: 4px;">
                ⚙️ Plugin Settings
            </a>
            
            <a href="{manage_plugins_url()}" class="btn btn-secondary" style="flex: 1; min-width: 150px; text-align: center; text-decoration: none; padding: .5rem 1rem; background: #6c757d; color: white; border-radius: 4px;">
                📦 Manage Plugins
            </a>
        </div>
        
        <hr style="margin: 1.5rem 0;">
        
        <h4>Task Execution</h4>
        <p style="margin-bottom: 1rem;">
            <strong>Cron Task ID:</strong> <code>plugin.batch_sonic_fp.run</code><br>
            To configure the automatic schedule, go to <strong>Administration &gt; Scheduled Tasks</strong> in the main menu.
        </p>
        
        <form method="post" action="{run_now_url}" style="display: inline-block;">
            <button type="submit" class="btn btn-success" 
                    style="margin: .5rem 0; padding: .6rem 1.2rem; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1rem;"
                    onclick="return confirm('Start batch generation for all Navidrome users now? This may take a while.');">
                ▶ Run Now (Manual Trigger)
            </button>
        </form>
        
        <p style="margin-top: 1rem; color: #666; font-size: .9rem;">
            <em>After clicking "Run Now", the task will appear in the main Tasks Panel on the dashboard. 
            You can monitor progress there.</em>
        </p>
    '''
    return render_page(body, title='Batch Sonic FP')

@bp.route('/run-now', methods=['POST'])
def run_now():
    """Manually trigger the batch task outside of cron."""
    result_message = ""
    result_type = "success"
    
    try:
        # Safely get Redis URL from Flask app config, fallback to Docker default
        redis_url = config.REDIS_URL
        redis_conn = Redis.from_url(redis_url)
        
        # Use 'high' queue for critical/coordinator tasks
        q = Queue('high', connection=redis_conn)
        
        # Pass the function object directly to RQ
        job = q.enqueue(run_batch_task, job_timeout='2h')
        
        # Initialize task status so it appears in the UI task panel immediately
        save_task_status(
            job.id, 'sonic_fingerprint', 'started', progress=0,
            details={"message": "Manual batch run triggered by admin..."}
        )
        
        result_message = (
            f"Task enqueued successfully.<br>"
            f"<strong>Job ID:</strong> <code>{job.id}</code><br>"
            f"Go to the main dashboard to monitor progress in the Tasks Panel."
        )
        
    except Exception as e:
        result_type = "error"
        result_message = f"Failed to start task:<br><code>{str(e)}</code>"
        logger.exception("Error enqueuing manual run")
        
    # Render a dedicated result page instead of relying on Flask's flash/session
    alert_color = '#d4edda' if result_type == 'success' else '#f8d7da'
    text_color = '#155724' if result_type == 'success' else '#721c24'
    
    body = f'''
        <h3>Batch Sonic Fingerprint - Manual Trigger Result</h3>
        <div style="padding: 1.5rem; margin: 1.5rem 0; border-radius: 4px; 
                    background-color: {alert_color}; color: {text_color}; 
                    border: 1px solid rgba(0,0,0,.1);">
            <strong style="font-size: 1.1rem;">
                {'✅ Success!' if result_type == 'success' else '❌ Error:'}
            </strong><br><br>
            {result_message}
        </div>
        
        <div style="margin-top: 2rem;">
            <a href="{url_for('batch_sonic_fp.home')}" class="btn btn-primary" 
               style="padding: .5rem 1rem; background: #007bff; color: white; 
                      text-decoration: none; border-radius: 4px;">
                ← Back to Plugin Home
            </a>
        </div>
    '''
    return render_page(body, title='Task Trigger Result')

@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        set_setting('navidrome_url', request.form.get('navidrome_url', DEFAULT_SETTINGS['navidrome_url']))
        set_setting('admin_username', request.form.get('admin_username', ''))
        set_setting('extauth_header', request.form.get('extauth_header', DEFAULT_SETTINGS['extauth_header']))
        set_setting('playlist_name', request.form.get('playlist_name', DEFAULT_SETTINGS['playlist_name']))
        
        try: set_setting('max_tracks', int(request.form.get('max_tracks', DEFAULT_SETTINGS['max_tracks'])))
        except ValueError: pass
        try: set_setting('top_albums_to_fetch', int(request.form.get('top_albums_to_fetch', DEFAULT_SETTINGS['top_albums_to_fetch'])))
        except ValueError: pass
        try: set_setting('top_songs_per_user', int(request.form.get('top_songs_per_user', DEFAULT_SETTINGS['top_songs_per_user'])))
        except ValueError: pass
        try: set_setting('num_neighbors', int(request.form.get('num_neighbors', DEFAULT_SETTINGS['num_neighbors'])))
        except ValueError: pass
        try: set_setting('playlists_to_keep', int(request.form.get('playlists_to_keep', DEFAULT_SETTINGS['playlists_to_keep'])))
        except ValueError: pass
        
        return redirect(manage_plugins_url())

    body = (
        '<form method="post">'
        '<h4>Navidrome Connection</h4>'
        f'<label>Navidrome URL:<br><input type="text" name="navidrome_url" value="{get_s("navidrome_url")}" style="width:100%;max-width:400px;"></label><br><br>'
        f'<label>Admin Username:<br><input type="text" name="admin_username" value="{get_s("admin_username")}"></label><br><br>'
        '<hr>'
        '<h4>ND_EXTAUTH & Playlist Settings</h4>'
        f'<label>ND_EXTAUTH_USERHEADER Name:<br><input type="text" name="extauth_header" value="{get_s("extauth_header")}"></label><br><br>'
        f'<label>Playlist Name (base):<br><input type="text" name="playlist_name" value="{get_s("playlist_name")}"></label><br><br>'
        f'<label>Final Playlist Size (Max Tracks):<br><input type="number" name="max_tracks" value="{get_s("max_tracks")}" style="width:100px;"></label><br><br>'
        f'<label>Playlists to Keep per User (rotation):<br><input type="number" name="playlists_to_keep" value="{get_s("playlists_to_keep")}" style="width:100px;" min="1" max="30"></label><br><br>'
        '<h4>Algorithm Tuning</h4>'
        f'<label>Frequent Albums to Scan per User:<br><input type="number" name="top_albums_to_fetch" value="{get_s("top_albums_to_fetch")}" style="width:100px;"></label><br><br>'
        f'<label>Seed Pool Size (Top Songs per User):<br><input type="number" name="top_songs_per_user" value="{get_s("top_songs_per_user")}" style="width:100px;"></label><br><br>'
        f'<label>Number of Similar Neighbors via IVF:<br><input type="number" name="num_neighbors" value="{get_s("num_neighbors")}" style="width:100px;"></label><br><br>'
        '<button type="submit" class="btn btn-primary">Save Settings</button>'
        '</form>'
    )
    return render_page(body, title='Batch Sonic FP Settings')

# --- PLUGIN REGISTRATION ---

def register(ctx):
    ctx.on_install(migrate)
    ctx.add_blueprint(bp)
    ctx.add_menu_item('Batch Sonic FP', 'batch_sonic_fp.home', admin_only=True)
    ctx.add_menu_item('Batch FP Settings', 'batch_sonic_fp.settings', admin_only=True)
    
    # Registers the task in the global Cron manager.
    # Visible in AudioMuse UI as: plugin.batch_sonic_fp.run
    ctx.add_cron_task('run', run_batch_task)
