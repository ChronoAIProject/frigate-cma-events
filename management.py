"""Owner-only management API and a small HA Ingress UI. No runtime secrets in responses."""
import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from evidence import EvidenceUnavailable, FrigateEvidence
from monitoring import Problem

UI = Path(__file__).parent / 'ui'


def analysis_status(candidate, result, delivery, *, processing, delivery_enabled):
    """Describe observed state; a missing callback alone is not an analysis failure."""
    if result:
        status = result['status']
        known = {'recorded': 'completed', 'processed': 'completed', 'integration_test': 'test_completed',
                 'pending': 'pending', 'failed': 'failed', 'error': 'failed'}
        return {'state': known.get(status, 'reported'), 'result_status': status}
    reason = candidate['decision']
    if reason != 'candidate':
        return {'state': 'skipped', 'reason': reason}
    if delivery is not None and delivery['delivered_at'] is not None:
        return {'state': 'awaiting_result'}
    if delivery is None and not delivery_enabled:
        return {'state': 'local_only'}
    if not processing:
        return {'state': 'paused'}
    if delivery is not None:
        status = delivery['last_status']
        if status is not None and 400 <= status < 500 and status != 429:
            return {'state': 'delivery_failed', 'http_status': status}
        if delivery['attempts']:
            return {'state': 'retrying'}
        return {'state': 'queued'}
    return {'state': 'not_sent'}


def install_management(app, authorize, body, ingress, cma_event_id):
    def manager(request):
        authorize(request, operator=True)
        return request.app.state.monitor

    def revision_precondition(request, value):
        header = request.headers.get('if-match')
        expected = value.pop('expected_revision', None)
        if expected is not None and (type(expected) is not int or expected < 1):
            raise Problem(400, 'invalid_expected_revision')
        if header:
            header = header.strip('"')
            if expected is not None and header != str(expected):
                raise Problem(400, 'conflicting_revision_preconditions')
            return header
        if expected is None:
            raise Problem(428, 'revision_required')
        return str(expected)

    @app.get('/')
    def index():
        return FileResponse(UI / 'index.html', headers={
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'",
            'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-cache'})

    @app.get('/ui/{asset}')
    def asset(asset: str):
        if asset not in {'app.js', 'style.css', 'i18n.js'}:
            raise Problem(404, 'asset_not_found')
        return FileResponse(UI / asset, headers={'Cache-Control': 'no-cache'})

    @app.get('/api/management')
    def settings(request: Request):
        mon = manager(request)
        return {'source': mon.get('default')['pipeline']['source']['source'], 'ingress': ingress(request),
                'cma_configured': bool(mon.config.cma_event_url),
                'frigate_configured': bool(mon.config.frigate_proxy_url),
                'max_rules': mon.MAX_RULES, 'max_revisions': mon.MAX_REVISIONS,
                'history_policy': 'Edits and enable/disable create isolated revisions. Retired queues are retained and paused; callbacks remain available.'}

    @app.get('/api/cameras')
    def cameras(request: Request):
        mon = manager(request)
        cfg = mon.config
        if not cfg.frigate_proxy_url:
            return {'cameras': []}
        try:
            client = FrigateEvidence(cfg.frigate_proxy_url, cfg.frigate_api_key, '', allow_http=cfg.frigate_direct)
            raw = json.loads(client.get('api/config'))
            rows = []
            for name, camera in raw['cameras'].items():
                rows.append({'name': name, 'zones': sorted(camera.get('zones', {})),
                             'labels': camera.get('objects', {}).get('track', []),
                             'recording': bool(camera.get('record', {}).get('enabled'))})
            return {'cameras': rows}
        except (EvidenceUnavailable, ValueError, KeyError, TypeError, AttributeError):
            raise Problem(424, 'camera_inventory_unavailable') from None

    @app.get('/api/rules')
    def rules(request: Request):
        mon = manager(request)
        return {'rules': mon.list()}

    @app.post('/api/rules', status_code=201)
    async def create(request: Request):
        mon = manager(request)
        doc = mon.save(await body(request))
        return JSONResponse(doc, status_code=201, headers={'ETag': '"%s"' % doc['revision']})

    @app.get('/api/rules/{rule_id}')
    def get(rule_id: str, request: Request):
        doc = manager(request).get(rule_id)
        return JSONResponse(doc, headers={'ETag': '"%s"' % doc['revision']})

    @app.put('/api/rules/{rule_id}')
    @app.patch('/api/rules/{rule_id}')
    async def update(rule_id: str, request: Request):
        mon = manager(request)
        value = await body(request)
        expected = revision_precondition(request, value)
        doc = mon.save(value, rule_id, expected)
        return JSONResponse(doc, headers={'ETag': '"%s"' % doc['revision']})

    @app.delete('/api/rules/{rule_id}')
    async def archive(rule_id: str, request: Request):
        mon = manager(request)
        value = await body(request) if await request.body() else {}
        expected = revision_precondition(request, value)
        if value:
            raise Problem(400, 'unknown_rule_fields')
        doc = mon.save({}, rule_id, expected, archive=True)
        return doc

    @app.get('/api/rules/{rule_id}/revisions')
    def history(rule_id: str, request: Request):
        return {'revisions': manager(request).history(rule_id)}

    @app.get('/api/rules/{rule_id}/revisions/{revision:int}/status')
    def revision_status(rule_id: str, revision: int, request: Request):
        mon = manager(request)
        with mon.lock:
            doc = mon.get(rule_id, revision)
            current = mon.get(rule_id)
            return {'rule': doc, 'current': revision == current['revision'],
                    'processing': revision == current['revision'] and doc['enabled'] and not doc['archived'],
                    'runtime_error': mon.runtime_errors.get(rule_id) if current['revision'] == revision else None,
                    'status': mon.worker(rule_id, revision).status()}

    @app.get('/api/rules/{rule_id}/revisions/{revision:int}/decisions')
    def decisions(rule_id: str, revision: int, request: Request):
        mon = manager(request)
        with mon.lock:
            doc = mon.get(rule_id, revision)
            current = mon.get(rule_id)
            processing = (mon.config.processing_enabled and revision == current['revision']
                          and doc['enabled'] and not doc['archived'])
            state = mon.worker(rule_id, revision)
        with state.lock:
            entries = []
            for binding, engine in state.engines.items():
                for row in engine.db.execute('SELECT id,at,payload FROM decisions ORDER BY at DESC LIMIT 100'):
                    result = engine.db.execute('SELECT payload FROM analysis_results WHERE id=?', (row['id'],)).fetchone()
                    candidate = json.loads(row['payload'])
                    result = json.loads(result[0]) if result else None
                    event_id = cma_event_id(state.config.event_source, row['id'], state.pipeline.spec['output']['event_type'])
                    delivery = engine.db.execute(
                        'SELECT attempts,delivered_at,last_status FROM cma_deliveries WHERE event_id=?', (event_id,)).fetchone()
                    entries.append({'binding': binding, 'at': row['at'], 'candidate': candidate, 'result': result,
                                    'analysis': analysis_status(candidate, result, delivery, processing=processing,
                                                                delivery_enabled=bool(state.config.cma_event_url))})
            return {'decisions': sorted(entries, key=lambda d: d['at'], reverse=True)[:100]}
