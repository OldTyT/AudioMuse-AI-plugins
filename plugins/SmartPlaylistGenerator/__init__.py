import json
import os
import glob
import requests

from flask import Blueprint, request, redirect

from plugin.api import get_setting, set_setting, render_page, manage_plugins_url, logger, config

bp = Blueprint("smart_playlist_generator", __name__)

# ---------------------------------------------------------------------------
# Navidrome Subsonic API helpers
# ---------------------------------------------------------------------------

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

    # Fallback: single-server / default config
    return {
        "url": (getattr(config, "NAVIDROME_URL", "") or "").rstrip("/"),
        "user": getattr(config, "NAVIDROME_USER", ""),
        "password": getattr(config, "NAVIDROME_PASSWORD", ""),
    }


def _api_request(endpoint, params=None, method="get"):
    """Make a request to the Navidrome Subsonic API."""
    creds = _get_navidrome_creds()
    if not creds["url"] or not creds["user"] or not creds["password"]:
        logger.error("Navidrome credentials not configured")
        return None

    url = f"{creds['url']}/rest/{endpoint}.view"
    auth_params = {
        "u": creds["user"],
        "p": f"enc:{creds['password'].encode('utf-8').hex()}",
        "v": "1.16.1",
        "c": "AudioMuse-AI-SmartPlaylistGenerator",
        "f": "json",
    }
    all_params = {**auth_params, **(params or {})}

    try:
        r = requests.request(method, url, params=all_params, timeout=30)
        r.raise_for_status()
        data = r.json().get("subsonic-response", {})
        if data.get("status") == "failed":
            err = data.get("error", {})
            logger.error(f"Navidrome API error on {endpoint}: {err.get('message')}")
            return None
        return data
    except Exception as e:
        logger.error(f"Navidrome API request failed: {e}")
        return None


def get_all_users():
    """Fetch all users from Navidrome via Subsonic getUsers endpoint."""
    data = _api_request("getUsers")
    if not data:
        return []
    users_data = data.get("users", {})
    users = users_data.get("user", [])
    if isinstance(users, dict):
        users = [users]
    return [
        {
            "username": u.get("username"),
            "id": u.get("id"),
            "is_admin": u.get("adminRole", False),
        }
        for u in users
        if u.get("username")
    ]


# ---------------------------------------------------------------------------
# .nsp file generation
# ---------------------------------------------------------------------------

def _build_nsp_content(playlist_name, rules, sort_field="dateadded", order="desc", limit=100):
    """Build the JSON content for a .nsp file."""
    nsp = {
        "name": playlist_name,
        "all": rules.get("all", []),
    }
    if rules.get("any"):
        nsp["any"] = rules["any"]
    if sort_field:
        nsp["sort"] = sort_field
    if order:
        nsp["order"] = order
    if limit:
        nsp["limit"] = limit
    # Private by default - each user owns their own playlist
    nsp["public"] = False
    return nsp


def _write_nsp_file(filepath, nsp_content):
    """Write a .nsp file with proper formatting."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(nsp_content, f, indent=2, ensure_ascii=False)


def _get_output_dir():
    """Get the configured output directory for .nsp files."""
    # Use APP_DATA_DIR if available (the correct variable in AudioMuse-AI),
    # fall back to /data which is the standard Navidrome data volume path.
    app_data = getattr(config, "APP_DATA_DIR", None) or "/data"
    default_path = os.path.join(app_data, "smart-playlists")
    return get_setting("output_dir", default_path)


def _get_playlist_configs():
    """Get the list of configured playlists from settings."""
    return get_setting("playlists", [])


def _generate_playlists():
    """
    Main logic: get all users, generate .nsp files for each user,
    remove stale .nsp files, and return a summary.
    """
    output_dir = _get_output_dir()
    playlist_configs = _get_playlist_configs()

    if not playlist_configs:
        logger.info("No playlist configurations found. Nothing to do.")
        return {"created": 0, "deleted": 0, "users": 0, "error": "No playlists configured"}

    users = get_all_users()
    if not users:
        logger.warning("No users found in Navidrome")
        return {"created": 0, "deleted": 0, "users": 0, "error": "No users found"}

    # Track which .nsp files we create in this run
    expected_files = set()

    created = 0
    for user in users:
        username = user["username"]
        for pl_config in playlist_configs:
            pl_name = pl_config.get("name", "Smart Playlist")
            # Include username in the playlist name to differentiate per-user
            full_name = f"{pl_name} ({username})"
            # File name: sanitized username + playlist name
            safe_username = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in username
            )
            safe_name = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in pl_name
            )
            filename = f"{safe_username}_{safe_name}.nsp"
            filepath = os.path.join(output_dir, filename)
            expected_files.add(filepath)

            rules = {
                "all": pl_config.get("all_rules", []),
                "any": pl_config.get("any_rules", []),
            }
            nsp_content = _build_nsp_content(
                playlist_name=full_name,
                rules=rules,
                sort_field=pl_config.get("sort", "dateadded"),
                order=pl_config.get("order", "desc"),
                limit=pl_config.get("limit", 100),
            )
            try:
                _write_nsp_file(filepath, nsp_content)
                created += 1
                logger.info(f"Created .nsp file: {filepath}")
            except Exception as e:
                logger.error(f"Failed to write {filepath}: {e}")

    # Delete .nsp files in output_dir that were NOT created in this run
    # (i.e., playlists removed from config or users removed from Navidrome)
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

    return {
        "created": created,
        "deleted": deleted,
        "users": len(users),
        "playlists_per_user": len(playlist_configs),
        "output_dir": output_dir,
    }


# ---------------------------------------------------------------------------
# Cron task wrapper
# ---------------------------------------------------------------------------

def generate_playlists_task():
    """Cron task entry point."""
    result = _generate_playlists()
    logger.info(
        f"Smart playlist generation complete: "
        f"{result['created']} created, {result['deleted']} deleted, "
        f"{result['users']} users"
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

    # Status section
    body += "<h3>Configuration Summary</h3>"
    body += f"<p><strong>Output directory:</strong> <code>{_get_output_dir()}</code></p>"
    body += f"<p><strong>Navidrome users found:</strong> {len(users)}</p>"
    body += f"<p><strong>Playlist configurations:</strong> {len(playlist_configs)}</p>"

    if users:
        body += "<h4>Users</h4><ul>"
        for u in users:
            admin_badge = " 👑" if u["is_admin"] else ""
            body += f"<li>{u['username']}{admin_badge}</li>"
        body += "</ul>"

    if playlist_configs:
        body += "<h4>Configured Playlists</h4><ul>"
        for pl in playlist_configs:
            body += (
                f"<li><strong>{pl.get('name', 'Unnamed')}</strong> — "
                f"sort: {pl.get('sort', 'dateadded')}, "
                f"order: {pl.get('order', 'desc')}, "
                f"limit: {pl.get('limit', 100)}</li>"
            )
        body += "</ul>"

    # Last run results
    if last_run:
        body += "<h3>Last Run Results</h3>"
        body += f"<p><strong>Created:</strong> {last_run.get('created', 0)} files</p>"
        body += f"<p><strong>Deleted (stale):</strong> {last_run.get('deleted', 0)} files</p>"
        body += f"<p><strong>Users processed:</strong> {last_run.get('users', 0)}</p>"
        body += f"<p><strong>Time:</strong> {last_run.get('timestamp', 'unknown')}</p>"
        if last_run.get("error"):
            body += f"<p><strong>Error:</strong> {last_run['error']}</p>"

    # Manual run button
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
    from datetime import datetime

    result = _generate_playlists()
    result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_setting("last_run", result)
    return redirect("/plugins/smart_playlist_generator/")


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page for configuring playlists and output directory."""
    if request.method == "POST":
        # Save output directory
        output_dir = request.form.get("output_dir", "").strip()
        if output_dir:
            set_setting("output_dir", output_dir)

        # Parse playlist configurations from the form
        playlist_configs = []
        playlist_count = int(request.form.get("playlist_count", 0))

        for i in range(playlist_count):
            name = request.form.get(f"pl_{i}_name", "").strip()
            if not name:
                continue

            try:
                all_rules = json.loads(request.form.get(f"pl_{i}_all_rules", "[]"))
            except (json.JSONDecodeError, TypeError):
                all_rules = []

            try:
                any_rules = json.loads(request.form.get(f"pl_{i}_any_rules", "[]"))
            except (json.JSONDecodeError, TypeError):
                any_rules = []

            sort_field = request.form.get(f"pl_{i}_sort", "dateadded")
            order = request.form.get(f"pl_{i}_order", "desc")

            try:
                limit = int(request.form.get(f"pl_{i}_limit", 100))
            except (ValueError, TypeError):
                limit = 100

            playlist_configs.append({
                "name": name,
                "all_rules": all_rules,
                "any_rules": any_rules,
                "sort": sort_field,
                "order": order,
                "limit": limit,
            })

        set_setting("playlists", playlist_configs)
        return redirect(manage_plugins_url())

    # GET: render the settings form
    output_dir = _get_output_dir()
    playlist_configs = _get_playlist_configs()

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

    body += "<h3>Playlists</h3>"
    body += (
        '<p>Each playlist below will be created for <strong>every</strong> '
        "Navidrome user. The playlist name will be suffixed with the username "
        "(e.g., <em>Recently Played (john)</em>). "
        "Rules use Navidrome's smart playlist JSON syntax.</p>"
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

    # JavaScript for dynamic playlist add/remove
    body += """
    <script>
    let playlistIndex = %d;

    document.getElementById('add-playlist').addEventListener('click', function() {
        const container = document.getElementById('playlists-container');
        const html = buildPlaylistForm(playlistIndex, {
            name: 'New Playlist',
            all_rules: [],
            any_rules: [],
            sort: 'dateadded',
            order: 'desc',
            limit: 100
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
        const allRulesStr = JSON.stringify(pl.all_rules || [], null, 2);
        const anyRulesStr = JSON.stringify(pl.any_rules || [], null, 2);
        return `
        <div class="playlist-block" data-index="${idx}"
             style="border:1px solid #ccc;padding:1rem;margin:1rem 0;border-radius:4px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h4 style="margin:0;">Playlist #${idx + 1}</h4>
                <button type="button" class="remove-playlist btn"
                        style="color:red;">Remove</button>
            </div>
            <label>Name:
                <input type="text" name="pl_${idx}_name"
                       value="${pl.name || ''}" style="width:60%%;padding:.3rem;">
            </label><br><br>
            <label>Sort by:
                <select name="pl_${idx}_sort">
                    ${['dateadded','lastplayed','playcount','rating','title','album','artist','year','random']
                        .map(s => `<option value="${s}" ${s===pl.sort?'selected':''}>${s}</option>`).join('')}
                </select>
            </label>
            <label style="margin-left:1rem;">Order:
                <select name="pl_${idx}_order">
                    <option value="desc" ${pl.order==='desc'?'selected':''}>Descending</option>
                    <option value="asc" ${pl.order==='asc'?'selected':''}>Ascending</option>
                </select>
            </label>
            <label style="margin-left:1rem;">Limit:
                <input type="number" name="pl_${idx}_limit"
                       value="${pl.limit || 100}" style="width:80px;padding:.3rem;">
            </label><br><br>
            <label>ALL rules (JSON array):
                <textarea name="pl_${idx}_all_rules" rows="5"
                          style="width:100%%;font-family:monospace;padding:.3rem;">${allRulesStr}</textarea>
            </label><br>
            <label>ANY rules (JSON array, optional):
                <textarea name="pl_${idx}_any_rules" rows="3"
                          style="width:100%%;font-family:monospace;padding:.3rem;">${anyRulesStr}</textarea>
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
    all_rules_str = json.dumps(pl.get("all_rules", []), indent=2)
    any_rules_str = json.dumps(pl.get("any_rules", []), indent=2)
    sort_options = [
        "dateadded", "lastplayed", "playcount", "rating",
        "title", "album", "artist", "year", "random",
    ]
    sort_select = "".join(
        f'<option value="{s}" {"selected" if s == pl.get("sort") else ""}>{s}</option>'
        for s in sort_options
    )
    order_desc_selected = "selected" if pl.get("order", "desc") == "desc" else ""
    order_asc_selected = "selected" if pl.get("order", "desc") == "asc" else ""

    return f"""
    <div class="playlist-block" data-index="{idx}"
         style="border:1px solid #ccc;padding:1rem;margin:1rem 0;border-radius:4px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <h4 style="margin:0;">Playlist #{idx + 1}</h4>
            <button type="button" class="remove-playlist btn"
                    style="color:red;">Remove</button>
        </div>
        <label>Name:
            <input type="text" name="pl_{idx}_name"
                   value="{pl.get('name', '')}" style="width:60%;padding:.3rem;">
        </label><br><br>
        <label>Sort by:
            <select name="pl_{idx}_sort">{sort_select}</select>
        </label>
        <label style="margin-left:1rem;">Order:
            <select name="pl_{idx}_order">
                <option value="desc" {order_desc_selected}>Descending</option>
                <option value="asc" {order_asc_selected}>Ascending</option>
            </select>
        </label>
        <label style="margin-left:1rem;">Limit:
            <input type="number" name="pl_{idx}_limit"
                   value="{pl.get('limit', 100)}" style="width:80px;padding:.3rem;">
        </label><br><br>
        <label>ALL rules (JSON array):
            <textarea name="pl_{idx}_all_rules" rows="5"
                      style="width:100%;font-family:monospace;padding:.3rem;">{all_rules_str}</textarea>
        </label><br>
        <label>ANY rules (JSON array, optional):
            <textarea name="pl_{idx}_any_rules" rows="3"
                      style="width:100%;font-family:monospace;padding:.3rem;">{any_rules_str}</textarea>
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
