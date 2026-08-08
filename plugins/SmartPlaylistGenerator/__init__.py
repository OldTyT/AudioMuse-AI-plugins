import json
import os
import glob
import time
import requests
from datetime import datetime

from flask import Blueprint, request, redirect

from plugin.api import get_setting, set_setting, render_page, manage_plugins_url, logger, config

bp = Blueprint("smart_playlist_generator", __name__)

# ---------------------------------------------------------------------------
# Navidrome credentials
# ---------------------------------------------------------------------------

_jwt_token_cache = {"token": None, "expires": 0}


def _get_navidrome_creds():
    """
    Get Navidrome connection details.
    Tries the active server context first (multi-server support),
    then falls back to the global config (single-server / default).
    """
    try:
        from plugin.api import active_server_id, list_servers
        server_id = active_server_id()
        if server_id:
            for srv in list_servers():
                if srv.get("server_id") == server_id and srv.get("server_type") == "navidrome":
                    creds = srv.get("creds") or {}
                    return {
                        "url": (creds.get("url") or "").rstrip("/"),
                        "user": creds.get("user", ""),
                        "password": creds.get("password", ""),
                    }
    except Exception:
        pass

    return {
        "url": (getattr(config, "NAVIDROME_URL", "") or "").rstrip("/"),
        "user": getattr(config, "NAVIDROME_USER", ""),
        "password": getattr(config, "NAVIDROME_PASSWORD", ""),
    }


# ---------------------------------------------------------------------------
# Subsonic API (used for startScan)
# ---------------------------------------------------------------------------

def _subsonic_auth_params():
    """Build Subsonic auth dict from current credentials."""
    creds = _get_navidrome_creds()
    if not creds["user"] or not creds["password"]:
        return None
    return {
        "u": creds["user"],
        "p": f"enc:{creds['password'].encode('utf-8').hex()}",
        "v": "1.16.1",
        "c": "AudioMuse-AI-SmartPlaylistGenerator",
        "f": "json",
    }


def _subsonic_request(endpoint, params=None, method="get"):
    """Make a request to the Navidrome Subsonic API."""
    auth = _subsonic_auth_params()
    if not auth:
        logger.error("Navidrome credentials not configured")
        return None

    creds = _get_navidrome_creds()
    url = f"{creds['url']}/rest/{endpoint}.view"
    all_params = {**auth, **(params or {})}

    try:
        r = requests.request(method, url, params=all_params, timeout=30)
        r.raise_for_status()
        data = r.json().get("subsonic-response", {})
        if data.get("status") == "failed":
            err = data.get("error", {})
            logger.error(f"Navidrome Subsonic API error on {endpoint}: {err.get('message')}")
            return None
        return data
    except Exception as e:
        logger.error(f"Navidrome Subsonic API request failed on {endpoint}: {e}")
        return None


def trigger_quick_scan():
    """
    Trigger Navidrome scan via Subsonic API: POST /rest/startScan.view
    Navidrome will pick up new/changed .nsp files during the scan.
    """
    data = _subsonic_request("startScan", method="post")
    if data is not None:
        scan_info = data.get("scanStatus", {})
        logger.info(
            f"Navidrome scan triggered. Status: {scan_info.get('scanning', '?')}, "
            f"count: {scan_info.get('count', '?')}"
        )
        return True
    logger.error("Failed to trigger Navidrome scan via startScan")
    return False


# ---------------------------------------------------------------------------
# Native API (used for users, playlists, owner assignment)
# ---------------------------------------------------------------------------

def _get_jwt_token():
    """
    Authenticate with Navidrome Native API and get a JWT token.
    Token is cached for 5 minutes to avoid excessive logins.
    """
    now = time.time()
    if _jwt_token_cache["token"] and _jwt_token_cache["expires"] > now:
        return _jwt_token_cache["token"]

    creds = _get_navidrome_creds()
    if not creds["url"] or not creds["user"] or not creds["password"]:
        logger.error("Navidrome credentials not configured")
        return None

    login_url = f"{creds['url']}/auth/login"
    try:
        r = requests.post(
            login_url,
            json={"username": creds["user"], "password": creds["password"]},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("token")
        if not token:
            logger.error("Navidrome login succeeded but no token in response")
            return None
        _jwt_token_cache["token"] = token
        _jwt_token_cache["expires"] = now + 300
        logger.debug("Navidrome JWT token acquired and cached")
        return token
    except Exception as e:
        logger.error(f"Navidrome login failed: {e}")
        return None


def _native_api_request(endpoint, params=None, method="get", json_body=None):
    """
    Make a request to the Navidrome Native API using JWT authentication.
    Endpoint should NOT include /api/ prefix - it will be added automatically.
    """
    token = _get_jwt_token()
    if not token:
        return None

    creds = _get_navidrome_creds()
    url = f"{creds['url']}/api/{endpoint.lstrip('/')}"

    headers = {
        "X-Nd-Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    def _do_request(tk):
        hdrs = {**headers, "X-Nd-Authorization": f"Bearer {tk}"}
        r = requests.request(
            method, url, params=params, headers=hdrs, json=json_body, timeout=30
        )
        r.raise_for_status()
        if r.status_code == 204 or not r.content:
            return {"ok": True}
        return r.json()

    try:
        return _do_request(token)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.warning("JWT token rejected, clearing cache and retrying login")
            _jwt_token_cache["token"] = None
            _jwt_token_cache["expires"] = 0
            new_token = _get_jwt_token()
            if new_token:
                try:
                    return _do_request(new_token)
                except Exception as retry_e:
                    logger.error(f"Navidrome API retry failed: {retry_e}")
            return None
        logger.error(f"Navidrome API HTTP error on {endpoint}: {e}")
        return None
    except Exception as e:
        logger.error(f"Navidrome API request failed on {endpoint}: {e}")
        return None


def get_all_users():
    """
    Fetch all users from Navidrome via Native API endpoint /api/user.
    Requires admin privileges.
    """
    data = _native_api_request("user")
    if data is None:
        return []

    users = data if isinstance(data, list) else []

    return [
        {
            "username": u.get("userName", ""),
            "id": u.get("id", ""),
            "name": u.get("name", ""),
            "email": u.get("email", ""),
            "is_admin": u.get("isAdmin", False),
        }
        for u in users
        if u.get("userName")
    ]


def get_playlist_by_name(playlist_name):
    """
    Search for a playlist by exact name in Navidrome.
    Returns the playlist dict if found, None otherwise.
    """
    data = _native_api_request(
        "playlist",
        params={
            "_search": playlist_name,
            "_sort": "name",
            "_order": "ASC",
            "_start": 0,
            "_end": 50,
        },
    )
    if data is None:
        return None

    playlists = data if isinstance(data, list) else []
    for pl in playlists:
        if pl.get("name") == playlist_name:
            return pl
    return None


def set_playlist_owner(playlist_id, owner_user_id):
    """
    Change the owner of a playlist via Navidrome Native API.
    PATCH /api/playlist/{playlistId} with ownerId field.
    """
    # Navidrome uses ownerId (camelCase) in its native API
    result = _native_api_request(
        f"playlist/{playlist_id}",
        method="patch",
        json_body={"ownerId": owner_user_id},
    )
    if result is not None:
        logger.info(f"Set owner {owner_user_id} on playlist {playlist_id}")
        return True
    logger.error(f"Failed to set owner on playlist {playlist_id}")
    return False


def wait_and_assign_owners(users, playlist_configs, timeout=60, poll_interval=3):
    """
    After .nsp files are created and scan is triggered:
    1. Poll Navidrome until the expected playlists appear.
    2. Once found, change each playlist's owner to the corresponding user.
    """
    # Build expected playlist names (no username in name anymore)
    expected = []
    for user in users:
        username = user["username"]
        user_id = user["id"]
        for pl_config in playlist_configs:
            pl_name = pl_config.get("name", "Smart Playlist")
            # The .nsp "name" field is just pl_name (no username suffix)
            # but we still match by exact name in Navidrome
            expected.append({
                "name": pl_name,
                "owner_id": user_id,
                "username": username,
            })

    if not expected:
        return {"assigned": 0, "not_found": [], "errors": []}

    assigned = 0
    not_found = []
    errors = []

    start_time = time.time()
    pending = list(expected)

    while pending and (time.time() - start_time) < timeout:
        still_pending = []
        for item in pending:
            playlist = get_playlist_by_name(item["name"])
            if playlist:
                playlist_id = playlist.get("id")
                if playlist_id:
                    # Only change owner if it's not already this user
                    current_owner = playlist.get("ownerId") or playlist.get("owner_id")
                    if current_owner == item["owner_id"]:
                        assigned += 1
                        logger.debug(
                            f"Playlist '{item['name']}' already owned by '{item['username']}'"
                        )
                    elif set_playlist_owner(playlist_id, item["owner_id"]):
                        assigned += 1
                        logger.info(
                            f"Assigned owner '{item['username']}' to playlist '{item['name']}'"
                        )
                    else:
                        errors.append(f"Failed to set owner for '{item['name']}'")
                else:
                    errors.append(f"Playlist '{item['name']}' found but has no id")
            else:
                still_pending.append(item)

        pending = still_pending
        if pending:
            elapsed = int(time.time() - start_time)
            logger.info(
                f"Waiting for {len(pending)} playlist(s) to appear in Navidrome "
                f"(elapsed: {elapsed}s, timeout: {timeout}s)"
            )
            time.sleep(poll_interval)

    for item in pending:
        not_found.append(item["name"])
        logger.warning(
            f"Playlist '{item['name']}' (owner: {item['username']}) "
            f"not found in Navidrome after {timeout}s"
        )

    return {
        "assigned": assigned,
        "not_found": not_found,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# .nsp file generation
# ---------------------------------------------------------------------------

def _build_nsp_content(playlist_name, description, raw_json_str):
    """
    Build the JSON content for a .nsp file.

    If raw_json_str is provided and valid JSON, parse it and use it as the
    base. Then always inject/override "name" and optionally "comment".
    If raw_json_str is empty or invalid, create a minimal empty playlist.
    """
    nsp = {}

    # Try to parse user-provided raw JSON
    if raw_json_str and raw_json_str.strip():
        try:
            parsed = json.loads(raw_json_str)
            if isinstance(parsed, dict):
                nsp = parsed
            else:
                logger.warning(
                    f"Raw JSON for playlist '{playlist_name}' is not a dict, "
                    f"falling back to empty playlist"
                )
        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON for playlist '{playlist_name}': {e}. "
                f"Falling back to empty playlist."
            )

    # Always set name (no username suffix in .nsp content)
    nsp["name"] = playlist_name

    # Set comment if description is provided
    if description and description.strip():
        nsp["comment"] = description

    # Ensure public is False (private per-user playlist)
    nsp["public"] = False

    return nsp


def _write_nsp_file(filepath, nsp_content):
    """Write a .nsp file with proper formatting."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(nsp_content, f, indent=2, ensure_ascii=False)


def _get_output_dir():
    """Get the configured output directory for .nsp files."""
    app_data = getattr(config, "APP_DATA_DIR", None) or "/data"
    default_path = os.path.join(app_data, "smart-playlists")
    return get_setting("output_dir", default_path)


def _get_playlist_configs():
    """Get the list of configured playlists from settings."""
    return get_setting("playlists", [])


def _generate_playlists():
    """
    Main logic:
    1. Get all users from Navidrome.
    2. Generate .nsp files for each user x each playlist config.
       File name includes username for uniqueness, but .nsp "name" field
       does NOT include username (so each user gets a playlist with the
       same display name, just different owner).
    3. Remove stale .nsp files.
    4. Trigger Navidrome scan via Subsonic startScan.
    5. Wait for playlists to appear and assign owners via Native API.
    """
    output_dir = _get_output_dir()
    playlist_configs = _get_playlist_configs()

    if not playlist_configs:
        logger.info("No playlist configurations found. Nothing to do.")
        return {"created": 0, "deleted": 0, "users": 0, "error": "No playlists configured"}

    users = get_all_users()
    if not users:
        logger.warning("No users found in Navidrome")
        return {
            "created": 0, "deleted": 0, "users": 0,
            "error": "No users found (check admin credentials)",
        }

    # Track which .nsp files we create in this run
    expected_files = set()

    created = 0
    file_errors = []
    for user in users:
        username = user["username"]
        for pl_config in playlist_configs:
            pl_name = pl_config.get("name", "Smart Playlist")
            description = pl_config.get("description", "")
            raw_json_str = pl_config.get("raw_json", "")

            # File name includes username for filesystem uniqueness
            safe_username = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in username
            )
            safe_name = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in pl_name
            )
            filename = f"{safe_username}_{safe_name}.nsp"
            filepath = os.path.join(output_dir, filename)
            expected_files.add(filepath)

            nsp_content = _build_nsp_content(pl_name, description, raw_json_str)

            try:
                _write_nsp_file(filepath, nsp_content)
                created += 1
                logger.info(f"Created .nsp file: {filepath}")
            except Exception as e:
                msg = f"Failed to write {filepath}: {e}"
                logger.error(msg)
                file_errors.append(msg)

    # Delete stale .nsp files
    deleted = 0
    if os.path.isdir(output_dir):
        for existing_file in glob.glob(os.path.join(output_dir, "*.nsp")):
            if existing_file not in expected_files:
                try:
                    os.remove(existing_file)
                    deleted += 1
                    logger.info(f"Deleted stale .nsp file: {existing_file}")
                except Exception as e:
                    logger.error(f"Failed to delete {existing_file}: {e}")

    result = {
        "created": created,
        "deleted": deleted,
        "users": len(users),
        "playlists_per_user": len(playlist_configs),
        "output_dir": output_dir,
    }
    if file_errors:
        result["file_errors"] = file_errors

    # --- Trigger Navidrome Scan ---
    logger.info("Triggering Navidrome scan via /rest/startScan to detect new .nsp files...")
    scan_ok = trigger_quick_scan()
    result["scan_triggered"] = scan_ok

    if not scan_ok:
        result["error"] = "Failed to trigger Navidrome scan"
        return result

    # --- Wait for playlists to appear and assign owners ---
    timeout = int(get_setting("scan_timeout", 60))
    poll_interval = int(get_setting("poll_interval", 3))

    logger.info(
        f"Waiting up to {timeout}s for playlists to appear in Navidrome, "
        f"then assigning owners..."
    )
    owner_result = wait_and_assign_owners(
        users, playlist_configs, timeout=timeout, poll_interval=poll_interval
    )
    result["owners_assigned"] = owner_result["assigned"]
    result["owners_not_found"] = owner_result["not_found"]
    if owner_result["errors"]:
        result["owner_errors"] = owner_result["errors"]

    if owner_result["not_found"]:
        logger.warning(
            f"{len(owner_result['not_found'])} playlist(s) not found after scan: "
            f"{owner_result['not_found'][:5]}"
        )

    return result


# ---------------------------------------------------------------------------
# Cron task wrapper
# ---------------------------------------------------------------------------

def generate_playlists_task():
    """Cron task entry point."""
    result = _generate_playlists()
    logger.info(
        f"Smart playlist generation complete: "
        f"{result['created']} created, {result['deleted']} deleted, "
        f"{result['users']} users, "
        f"{result.get('owners_assigned', 0)} owners assigned"
    )
    return result


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

@bp.route("/")
def home():
    """Main plugin page showing status and last run results."""
    last_run = get_setting("last_run", None)
    playlist_configs = _get_playlist_configs()

    try:
        users = get_all_users()
    except Exception:
        users = []

    body = '<div style="max-width:800px;">'

    body += "<h3>Configuration Summary</h3>"
    body += f"<p><strong>Output directory:</strong> <code>{_get_output_dir()}</code></p>"
    body += f"<p><strong>Navidrome users found:</strong> {len(users)}</p>"
    body += f"<p><strong>Playlist configurations:</strong> {len(playlist_configs)}</p>"
    body += f"<p><strong>Scan timeout:</strong> {get_setting('scan_timeout', 60)}s</p>"
    body += f"<p><strong>Poll interval:</strong> {get_setting('poll_interval', 3)}s</p>"

    if users:
        body += "<h4>Users</h4><ul>"
        for u in users:
            admin_badge = " 👑" if u["is_admin"] else ""
            body += f"<li>{u['username']}{admin_badge} (id: <code>{u['id'][:8]}...</code>)</li>"
        body += "</ul>"

    if playlist_configs:
        body += "<h4>Configured Playlists</h4><ul>"
        for pl in playlist_configs:
            desc = pl.get("description", "")
            desc_str = f" — <em>{desc[:50]}{'...' if len(desc) > 50 else ''}</em>" if desc else ""
            has_raw = "📋 custom JSON" if pl.get("raw_json", "").strip() else "empty"
            body += (
                f"<li><strong>{pl.get('name', 'Unnamed')}</strong>"
                f"{desc_str} ({has_raw})</li>"
            )
        body += "</ul>"

    if last_run:
        body += "<h3>Last Run Results</h3>"
        body += f"<p><strong>Created:</strong> {last_run.get('created', 0)} .nsp files</p>"
        body += f"<p><strong>Deleted (stale):</strong> {last_run.get('deleted', 0)} files</p>"
        body += f"<p><strong>Users processed:</strong> {last_run.get('users', 0)}</p>"
        body += f"<p><strong>Scan triggered:</strong> {'✅' if last_run.get('scan_triggered') else '❌'}</p>"
        body += f"<p><strong>Owners assigned:</strong> {last_run.get('owners_assigned', 0)}</p>"
        body += f"<p><strong>Time:</strong> {last_run.get('timestamp', 'unknown')}</p>"

        if last_run.get("error"):
            body += f"<p style='color:red;'><strong>Error:</strong> {last_run['error']}</p>"
        if last_run.get("owners_not_found"):
            body += "<p style='color:orange;'><strong>Playlists not found after scan:</strong></p><ul>"
            for name in last_run["owners_not_found"][:10]:
                body += f"<li>{name}</li>"
            body += "</ul>"
        if last_run.get("owner_errors"):
            body += "<p style='color:red;'><strong>Owner assignment errors:</strong></p><ul>"
            for err in last_run["owner_errors"][:10]:
                body += f"<li>{err}</li>"
            body += "</ul>"
        if last_run.get("file_errors"):
            body += "<p style='color:red;'><strong>File errors:</strong></p><ul>"
            for err in last_run["file_errors"][:10]:
                body += f"<li>{err}</li>"
            body += "</ul>"

    body += (
        '<form method="post" action="/plugins/smart_playlist_generator/run" '
        'style="margin-top:1.5rem;">'
        '<button type="submit" class="btn btn-primary">'
        "▶ Generate Playlists Now</button>"
        "</form>"
    )

    body += "</div>"
    return render_page(body, title="Smart Playlist Generator")


@bp.route("/run", methods=["POST"])
def run_now():
    """Manual trigger: generate playlists immediately."""
    result = _generate_playlists()
    result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_setting("last_run", result)
    return redirect("/plugins/smart_playlist_generator/")


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page for configuring playlists and output directory."""
    if request.method == "POST":
        output_dir = request.form.get("output_dir", "").strip()
        if output_dir:
            set_setting("output_dir", output_dir)

        try:
            scan_timeout = int(request.form.get("scan_timeout", 60))
            set_setting("scan_timeout", max(10, scan_timeout))
        except (ValueError, TypeError):
            pass

        try:
            poll_interval = int(request.form.get("poll_interval", 3))
            set_setting("poll_interval", max(1, poll_interval))
        except (ValueError, TypeError):
            pass

        playlist_configs = []
        playlist_count = int(request.form.get("playlist_count", 0))

        for i in range(playlist_count):
            name = request.form.get(f"pl_{i}_name", "").strip()
            if not name:
                continue

            description = request.form.get(f"pl_{i}_description", "").strip()
            raw_json = request.form.get(f"pl_{i}_raw_json", "")

            # Validate raw_json if provided
            if raw_json.strip():
                try:
                    parsed = json.loads(raw_json)
                    if not isinstance(parsed, dict):
                        logger.warning(
                            f"Playlist '{name}' raw JSON is not a dict, "
                            f"it will be ignored"
                        )
                except json.JSONDecodeError as e:
                    logger.warning(f"Playlist '{name}' has invalid JSON: {e}")

            playlist_configs.append({
                "name": name,
                "description": description,
                "raw_json": raw_json,
            })

        set_setting("playlists", playlist_configs)
        return redirect(manage_plugins_url())

    # GET
    output_dir = _get_output_dir()
    playlist_configs = _get_playlist_configs()
    scan_timeout = get_setting("scan_timeout", 60)
    poll_interval = get_setting("poll_interval", 3)

    body = '<div style="max-width:900px;">'
    body += (
        '<form method="post">'
        '<h3>Output Directory</h3>'
        '<p>Path where .nsp files will be written. This should be a directory '
        "that Navidrome scans (e.g., your music library or "
        '<code>PlaylistsPath</code>).</p>'
        f'<input type="text" name="output_dir" value="{output_dir}" '
        'style="width:100%;padding:.5rem;margin-bottom:1rem;">'
    )

    body += "<h3>Scan Settings</h3>"
    body += (
        '<label style="display:block;margin-bottom:.5rem;">'
        'Scan timeout (seconds, how long to wait for playlists to appear): '
        f'<input type="number" name="scan_timeout" value="{scan_timeout}" '
        'min="10" max="300" style="width:80px;padding:.3rem;">'
        '</label>'
        '<label style="display:block;margin-bottom:1rem;">'
        'Poll interval (seconds, how often to check): '
        f'<input type="number" name="poll_interval" value="{poll_interval}" '
        'min="1" max="30" style="width:80px;padding:.3rem;">'
        '</label>'
    )

    body += "<h3>Playlists</h3>"
    body += (
        '<p>Each playlist below will be created for <strong>every</strong> '
        "Navidrome user. Each user gets their own copy with themselves as owner. "
        'The <strong>Raw JSON</strong> field accepts any valid Navidrome smart '
        'playlist JSON (rules, sort, limit, etc.) — it is written to the .nsp '
        "file as-is, with <code>name</code>, <code>comment</code> and "
        "<code>public</code> always overridden.</p>"
    )

    body += '<div id="playlists-container">'
    for i, pl in enumerate(playlist_configs):
        body += _render_playlist_form(i, pl)

    body += "</div>"

    body += (
        '<button type="button" id="add-playlist" '
        'class="btn" style="margin:1rem 0;">+ Add Playlist</button>'
    )

    body += (
        '<div style="margin-top:1.5rem;">'
        '<button type="submit" class="btn btn-primary">Save Settings</button>'
        "</div>"
    )

    body += "</form>"

    body += """
    <script>
    let playlistIndex = %d;

    document.getElementById('add-playlist').addEventListener('click', function() {
        const container = document.getElementById('playlists-container');
        const html = buildPlaylistForm(playlistIndex, {
            name: 'New Playlist',
            description: '',
            raw_json: '{\\n  "all": [],\\n  "sort": "dateadded",\\n  "order": "desc",\\n  "limit": 100\\n}'
        });
        container.insertAdjacentHTML('beforeend', html);
        playlistIndex++;
        updatePlaylistCount();
    });

    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('remove-playlist')) {
            e.target.closest('.playlist-block').remove();
            updatePlaylistCount();
        }
    });

    function updatePlaylistCount() {
        const blocks = document.querySelectorAll('.playlist-block');
        let countInput = document.getElementById('playlist-count-input');
        if (!countInput) {
            countInput = document.createElement('input');
            countInput.type = 'hidden';
            countInput.name = 'playlist_count';
            countInput.id = 'playlist-count-input';
            document.querySelector('form').appendChild(countInput);
        }
        countInput.value = blocks.length;
        blocks.forEach(function(block, idx) {
            block.dataset.index = idx;
            block.querySelectorAll('[name]').forEach(function(input) {
                input.name = input.name.replace(/pl_\\d+_/, 'pl_' + idx + '_');
            });
        });
    }

    function buildPlaylistForm(idx, pl) {
        return `
        <div class="playlist-block" data-index="${idx}"
             style="border:1px solid #ccc;padding:1rem;margin:1rem 0;border-radius:4px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h4 style="margin:0;">Playlist #${idx + 1}</h4>
                <button type="button" class="remove-playlist btn"
                        style="color:red;">Remove</button>
            </div>
            <label>Playlist name (shown in Navidrome, same for all users):
                <input type="text" name="pl_${idx}_name"
                       value="${(pl.name || '').replace(/"/g, '&quot;')}"
                       style="width:60%%;padding:.3rem;">
            </label><br><br>
            <label>Description (optional, becomes the comment field in .nsp):
                <input type="text" name="pl_${idx}_description"
                       value="${(pl.description || '').replace(/"/g, '&quot;')}"
                       style="width:100%%;padding:.3rem;">
            </label><br><br>
            <label>Raw JSON (full .nsp content — rules, sort, limit, etc.):
                <textarea name="pl_${idx}_raw_json" rows="8"
                          style="width:100%%;font-family:monospace;padding:.3rem;">${pl.raw_json || ''}</textarea>
            </label>
        </div>`;
    }

    updatePlaylistCount();
    </script>
    """ % len(playlist_configs)

    body += "</div>"
    return render_page(body, title="Smart Playlist Generator Settings")


def _render_playlist_form(idx, pl):
    """Render a single playlist configuration form block."""
    import html as html_mod
    name_escaped = html_mod.escape(pl.get("name", ""))
    desc_escaped = html_mod.escape(pl.get("description", ""))
    raw_json_escaped = html_mod.escape(pl.get("raw_json", ""))

    return f"""
    <div class="playlist-block" data-index="{idx}"
         style="border:1px solid #ccc;padding:1rem;margin:1rem 0;border-radius:4px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <h4 style="margin:0;">Playlist #{idx + 1}</h4>
            <button type="button" class="remove-playlist btn"
                    style="color:red;">Remove</button>
        </div>
        <label>Playlist name (shown in Navidrome, same for all users):
            <input type="text" name="pl_{idx}_name"
                   value="{name_escaped}" style="width:60%;padding:.3rem;">
        </label><br><br>
        <label>Description (optional, becomes the comment field in .nsp):
            <input type="text" name="pl_{idx}_description"
                   value="{desc_escaped}" style="width:100%;padding:.3rem;">
        </label><br><br>
        <label>Raw JSON (full .nsp content — rules, sort, limit, etc.):
            <textarea name="pl_{idx}_raw_json" rows="8"
                      style="width:100%;font-family:monospace;padding:.3rem;">{raw_json_escaped}</textarea>
        </label>
    </div>
    """


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx):
    ctx.add_blueprint(bp)
    ctx.add_menu_item("Smart Playlists", "smart_playlist_generator.home")
    ctx.add_cron_task("generate_playlists", generate_playlists_task)
