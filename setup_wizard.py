"""Four-step setup using the existing family/import/SMS actions."""
import hashlib
import json
import threading
from urllib.parse import urlsplit
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import store

STEPS = [('program', 'Program details'), ('families', 'Families'), ('preview', 'Preview'), ('test', 'Test text')]
_ready = None
_lock = threading.Lock()

def ensure_tables():
    global _ready
    key = (store.USE_PG, store.DATABASE_URL, store.DB_PATH)
    if _ready == key:
        return
    with _lock:
        if _ready == key:
            return
        with store.db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS setup_progress (provider_id INTEGER PRIMARY KEY, program_at TEXT, preview_digest TEXT, test_digest TEXT)')
        _ready = key

def snapshot(provider):
    provider = dict(provider)
    ensure_tables()
    pid = provider['id']
    locations = []
    families = []
    rooms = []
    for loc in store.list_locations(pid):
        loc = dict(loc)
        loc['classrooms'] = [dict(r) for r in store.list_classrooms(loc['id'])]
        locations.append(loc)
        for room in loc['classrooms']:
            rooms.append({**room, 'location_name': loc['name']})
            families.extend(dict(f) for f in store.list_families(room['id']))
    with store.db() as conn:
        progress = conn.execute('SELECT * FROM setup_progress WHERE provider_id = ?', (pid,)).fetchone()
    progress = dict(progress) if progress else {}
    sending = store.get_sending_settings(pid)
    templates = dict(store.get_templates(pid))
    payload = {'company': provider.get('company', ''), 'locations': [(l['id'], l['name'], l['payment_url']) for l in locations],
               'families': [(f['id'], f['language'], f['tuition_amount'], f['due_day']) for f in families],
               'templates': templates, 'sending': sending}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    program = bool(locations and rooms and (progress.get('program_at') or provider.get('company')))
    preview = bool(program and families and progress.get('preview_digest') == digest)
    tested = bool(preview and progress.get('test_digest') == digest)
    done = [program, bool(families), preview, tested]
    next_step = next((key for (key, _), complete in zip(STEPS, done) if not complete), 'test')
    return {'steps': [{'key': key, 'label': label, 'done': complete} for (key, label), complete in zip(STEPS, done)],
            'locations': locations, 'rooms': rooms, 'families': families, 'sending': sending,
            'templates': templates, 'digest': digest, 'preview_done': preview, 'complete': all(done),
            'done_count': sum(done), 'next_step': next_step}

def mark_test(provider):
    state = snapshot(provider)
    if state['preview_done']:
        with store.db() as conn:
            conn.execute('UPDATE setup_progress SET test_digest = ? WHERE provider_id = ? AND preview_digest = ?',
                         (state['digest'], provider['id'], state['digest']))

def register(app, templates, *, require_login, location_limit, timezones, hours, preview_texts, test_preview, sms_demo, needs_verification):
    def page(request, provider, step=None, error='', status=200, values=None):
        state = snapshot(provider)
        step = step if step in dict(STEPS) else state['next_step']
        if step != 'program' and not state['steps'][0]['done']:
            step = 'program'
        elif step in ('preview', 'test') and not state['families']:
            step = 'families'
        elif step == 'test' and not state['preview_done']:
            step = 'preview'
        selected_id = request.query_params.get('location_id', '')
        loc = next((loc for loc in state['locations'] if str(loc['id']) == selected_id), state['locations'][0] if state['locations'] else {})
        values = values or {'location_id': loc.get('id', 0), 'name': loc.get('name') or provider.get('company', ''),
                            'address': loc.get('address', ''), 'payment_url': loc.get('payment_url', ''),
                            **state['sending']}
        import datetime
        left = max(0, store.TEST_TEXT_DAILY_LIMIT - store.count_test_texts_today(provider['id'], datetime.datetime.now(datetime.timezone.utc).date().isoformat()))
        response = templates.TemplateResponse(request, 'setup.html', {'request': request, 'provider': provider,
            'setup': state, 'step': step, 'error': error, 'values': values, 'timezones': timezones,
            'hours': hours(), 'previews': preview_texts(provider['id'], state['templates'], 'en') + preview_texts(provider['id'], state['templates'], 'es'),
            'test_preview': test_preview(provider['id']), 'test_texts_left': left, 'sms_demo': sms_demo, 'unverified': needs_verification(provider),
            'test_sent': request.query_params.get('test') == 'sent'}, status_code=status)
        response.headers.update({'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex'})
        return response

    @app.get('/setup', response_class=HTMLResponse)
    def wizard(request: Request, step: str = ''):
        provider, redirect = require_login(request)
        return redirect if redirect else page(request, provider, step)

    @app.post('/setup/program')
    def program(request: Request, name: str = Form(''), location_id: int = Form(0), address: str = Form(''),
                payment_url: str = Form(''), timezone: str = Form(''), quiet_start: str = Form(''), quiet_end: str = Form('')):
        provider, redirect = require_login(request)
        if redirect:
            return redirect
        values = dict(name=name, location_id=location_id, address=address, payment_url=payment_url,
                      timezone=timezone, quiet_start=quiet_start, quiet_end=quiet_end)
        def invalid(message):
            return page(request, provider, 'program', message, 400, values)
        name, address, payment_url = name.strip(), address.strip(), payment_url.strip()
        if not name or len(name) > 200 or len(address) > 300 or len(payment_url) > 500:
            return invalid('Enter a program name (up to 200 characters), a shorter address or a shorter payment link.')
        if timezone not in timezones or quiet_start not in dict(hours()) or quiet_end not in dict(hours()) or quiet_start == quiet_end:
            return invalid('Choose a timezone and different start and end hours.')
        if payment_url:
            try:
                url = urlsplit(payment_url)
                valid = url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in payment_url):
                return invalid('Use a complete HTTPS payment link, or leave it blank if families pay another way.')
        state = snapshot(provider)
        if location_id and not store.get_location(location_id, provider['id']):
            return HTMLResponse('Location not found.', status_code=404)
        # A second submit of the first-location form updates the saved location.
        location_id = location_id or (state['locations'][0]['id'] if state['locations'] else 0)
        if not location_id:
            limit = location_limit(provider)
            if limit:
                return limit
        store.ensure_sending_columns()
        store.ensure_admin_columns()
        with store.db() as conn:
            # Serialize repeated first-location submissions on the provider row.
            conn.execute('UPDATE providers SET company=company WHERE id=?', (provider['id'],))
            if not location_id:
                saved = conn.execute('SELECT id FROM locations WHERE provider_id=? ORDER BY id LIMIT 1', (provider['id'],)).fetchone()
                if saved:
                    location_id = saved['id']
            if location_id:
                conn.execute('UPDATE locations SET name=?, address=?, payment_url=? WHERE id=? AND provider_id=?',
                             (name, address, payment_url, location_id, provider['id']))
            else:
                location_id = store._insert_and_get_id(conn, 'INSERT INTO locations (provider_id,name,address,payment_url) VALUES (?,?,?,?)',
                                                       (provider['id'], name, address, payment_url))
            if not conn.execute('SELECT id FROM classrooms WHERE location_id=?', (location_id,)).fetchone():
                conn.execute('INSERT INTO classrooms (location_id,label) VALUES (?,?)', (location_id, 'Families'))
            conn.execute('UPDATE providers SET company=?, timezone=?, quiet_start=?, quiet_end=? WHERE id=?',
                         (name, timezone, quiet_start, quiet_end, provider['id']))
            conn.execute('INSERT INTO setup_progress (provider_id,program_at) VALUES (?,?) ON CONFLICT(provider_id) DO UPDATE SET program_at=excluded.program_at',
                         (provider['id'], store.now_iso()))
        return RedirectResponse('/setup?step=families', status_code=303)

    @app.post('/setup/preview')
    def confirm_preview(request: Request, digest: str = Form(''), reviewed: str = Form('')):
        provider, redirect = require_login(request)
        if redirect:
            return redirect
        state = snapshot(provider)
        if not state['steps'][0]['done'] or not state['families']:
            return RedirectResponse('/setup', status_code=303)
        if reviewed != 'on' or digest != state['digest']:
            return page(request, provider, 'preview', 'Review the current previews and confirm before continuing. Settings may have changed.', 409)
        with store.db() as conn:
            conn.execute('INSERT INTO setup_progress (provider_id,preview_digest) VALUES (?,?) ON CONFLICT(provider_id) DO UPDATE SET preview_digest=excluded.preview_digest',
                         (provider['id'], state['digest']))
        return RedirectResponse('/setup?step=test', status_code=303)
