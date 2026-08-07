import logging
import math
from urllib.parse import urlencode
from datetime import datetime, timezone

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
    'admin_username': 'navidrome',      # Tech admin used to fetch the user list
    'extauth_header': 'Remote-User',    # Matches ND_EXTAUTH_USERHEADER in Navidrome
    'playlist_name': 'My Sonic Fingerprint',
    'max_tracks': 50,                   # Final playlist size
    'top_albums_to_fetch': 20,          # How many frequent albums to scan per user
    'top_songs_per_user': 50,           # Initial seed pool of top played tracks
    'num_neighbors': 50                 # Number of similar tracks to find via IVF
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
    # Clean URL for fuzzy matching
    clean_url = base_url.replace('http://', '').replace('https://', '')
    
    cur.execute("SELECT id FROM music_servers WHERE type = 'navidrome' AND url LIKE %s", (f"%{clean_url}%",))
    row = cur.fetchone()
    
    # Fallback to any navidrome server if fuzzy match fails
    if not row:
        cur.execute("SELECT id FROM music_servers WHERE type = 'navidrome' LIMIT 1")
        row = cur.fetchone()
        
    cur.close()
    return row[0] if row else None

def compute_fingerprint_vector(username, top_tracks):
    """
    Maps Navidrome provider IDs to AudioMuse canonical IDs, fetches embeddings,
    and computes a weighted average vector representing the user's taste.
    """
    server_id = get_server_id()
    if not server_id:
        logger.error("No Navidrome server found in AudioMuse-AI music_servers registry.")
        return None
        
    db = get_db()
    cur = db.cursor()
    
    provider_ids = [t['provider_id'] for t in top_tracks]
    
    # Map provider IDs to canonical item_ids
    cur.execute(
        "SELECT provider_id, item_id FROM track_server_map WHERE server_id = %s AND provider_id = ANY(%s)",
        (server_id, provider_ids)
    )
    id_map = {row[0]: row[1] for row in cur.fetchall()}
    
    # Filter out tracks that haven't been analyzed by AudioMuse yet
    valid_tracks = [t for t in top_tracks if t['provider_id'] in id_map]
    if not valid_tracks:
        logger.warning(f"No analyzed tracks found in AudioMuse-AI for user {username}.")
        return None
        
    canonical_ids = [id_map[t['provider_id']] for t in valid_tracks]
    
    # Fetch embeddings from the core database
    cur.execute(
        "SELECT item_id, embedding_vector FROM score WHERE item_id = ANY(%s) AND embedding_vector IS NOT NULL",
        (canonical_ids,)
    )
    embeddings_map = {row[0]: np.array(row[1]) for row in cur.fetchall() if row[1] is not None}
    cur.close()
    
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

def get_similar_tracks(average_vector, num_neighbors):
    """
    Finds similar tracks using the internal IVF index.
    Falls back to brute-force cosine similarity if the internal module is inaccessible.
    """
    try:
        from tasks.ivf_manager import find_nearest_neighbors_by_vector
        return find_nearest_neighbors_by_vector(query_vector=average_vector, n=num_neighbors, eliminate_duplicates=True)
    except ImportError:
        logger.warning("Could not import tasks.ivf_manager. Falling back to brute-force cosine similarity.")
        
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT item_id, embedding_vector FROM score WHERE embedding_vector IS NOT NULL")
    
    similarities = []
    norm_query = np.linalg.norm(average_vector)
    if norm_query == 0:
        cur.close()
        return []
        
    for row in cur.fetchall():
        cid, vec = row
        if vec is None: continue
        vec = np.array(vec)
        norm_vec = np.linalg.norm(vec)
        if norm_vec == 0: continue
        
        cos_sim = np.dot(average_vector, vec) / (norm_query * norm_vec)
        similarities.append({'item_id': cid, 'distance': 1.0 - cos_sim})
        
    cur.close()
    similarities.sort(key=lambda x: x['distance'])
    return similarities[:num_neighbors]

def create_private_playlist(username, canonical_ids):
    """Reverse maps canonical IDs to Navidrome IDs, creates the playlist, and hides it."""
    server_id = get_server_id()
    if not server_id: return
    
    db = get_db()
    cur = db.cursor()
    
    # Reverse map to get Navidrome provider IDs
    cur.execute(
        "SELECT item_id, provider_id FROM track_server_map WHERE server_id = %s AND item_id = ANY(%s)",
        (server_id, canonical_ids)
    )
    reverse_map = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    
    navidrome_ids = [reverse_map[cid] for cid in canonical_ids if cid in reverse_map]
    if not navidrome_ids:
        logger.error(f"No valid provider IDs found to create playlist for user {username}.")
        return
        
    playlist_name = get_s('playlist_name')
    base_url_full = get_s('navidrome_url').rstrip('/')
    extauth_header = get_s('extauth_header')
    headers = {extauth_header: username}
    
    # Subsonic createPlaylist requires an array of songId parameters
    base_params = {'name': playlist_name}
    song_ids_param = [('songId', tid) for tid in navidrome_ids]
    
    query = urlencode(base_params)
    song_query = urlencode(song_ids_param, doseq=True)
    full_url = f"{base_url_full}/rest/createPlaylist.view?{query}&{song_query}&v=1.16.1&c=AudioMusePlugin&f=json"
    
    try:
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
        logger.exception(f"Failed to create/update playlist for {username}: {e}")

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
        
        # Safe integer parsing
        try: set_setting('max_tracks', int(request.form.get('max_tracks', DEFAULT_SETTINGS['max_tracks'])))
        except ValueError: pass
        try: set_setting('top_albums_to_fetch', int(request.form.get('top_albums_to_fetch', DEFAULT_SETTINGS['top_albums_to_fetch'])))
        except ValueError: pass
        try: set_setting('top_songs_per_user', int(request.form.get('top_songs_per_user', DEFAULT_SETTINGS['top_songs_per_user'])))
        except ValueError: pass
        try: set_setting('num_neighbors', int(request.form.get('num_neighbors', DEFAULT_SETTINGS['num_neighbors'])))
        except ValueError: pass
        
        return redirect(manage_plugins_url())

    body = (
        '<form method="post">'
        '<h4>Navidrome Connection</h4>'
        f'<label>Navidrome URL:<br><input type="text" name="navidrome_url" value="{get_s("navidrome_url")}" style="width:100%;max-width:400px;"></label><br><br>'
        f'<label>Admin Username (must have permissions to list all users):<br><input type="text" name="admin_username" value="{get_s("admin_username")}"></label><br><br>'
        '<hr>'
        '<h4>ND_EXTAUTH & Playlist Settings</h4>'
        f'<label>ND_EXTAUTH_USERHEADER Name (from Navidrome config):<br><input type="text" name="extauth_header" value="{get_s("extauth_header")}"></label><br><br>'
        f'<label>Playlist Name:<br><input type="text" name="playlist_name" value="{get_s("playlist_name")}"></label><br><br>'
        f'<label>Final Playlist Size (Max Tracks):<br><input type="number" name="max_tracks" value="{get_s("max_tracks")}" style="width:100px;"></label><br><br>'
        '<h4>Algorithm Tuning</h4>'
        f'<label>Frequent Albums to Scan per User:<br><input type="number" name="top_albums_to_fetch" value="{get_s("top_albums_to_fetch")}" style="width:100px;"></label><br><br>'
        f'<label>Seed Pool Size (Top Songs per User):<br><input type="number" name="top_songs_per_user" value="{get_s("top_songs_per_user")}" style="width:100px;"></label><br><br>'
        f'<label>Number of Similar Neighbors to Fetch via IVF:<br><input type="number" name="num_neighbors" value="{get_s("num_neighbors")}" style="width:100px;"></label><br><br>'
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
