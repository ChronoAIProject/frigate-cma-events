"""Local rule registry. Runtime credentials and destinations are deployment-owned."""
from dataclasses import replace
import json
from copy import deepcopy
import re
import sqlite3
import threading
import time

from evidence import EvidenceOptions
from pipeline import Pipeline, path_value


class Problem(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Monitor:
    MAX_RULES = 32
    MAX_REVISIONS = 256

    def __init__(self, config, factory):
        self.config, self.factory = config, factory
        self.lock = threading.RLock()
        self.workers = {}
        self.media_lock = threading.Lock()
        self.media_cache = {}
        self.media_busy = False
        self.runtime_errors = {}
        self.last_tick_at = None
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = config.data_dir / 'monitoring.sqlite3'
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS rule_revisions (
                id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(id,revision)
            );
            CREATE TABLE IF NOT EXISTS rule_heads (id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
        ''')
        path.chmod(0o600)
        if not self.db.execute('SELECT 1 FROM rule_heads').fetchone():
            # Import once. Never overwrite locally edited rules with startup options.
            imported = deepcopy(config.pipeline.spec)
            imported['evidence']['options'] = dict(config.evidence_options.__dict__)
            document = dict(id='default', revision=1, label=config.pipeline.spec['name'],
                            enabled=True, delivery_enabled=bool(config.cma_event_url), archived=False,
                            created_at=time.time(), pipeline=imported, legacy=True,
                            evidence_options=config.evidence_options.__dict__)
            worker = factory(config)
            self.workers[('default', 1)] = worker
            with self.db:
                self._insert(document)
        # Open the original state even if default has since been edited/archived.
        self.legacy = self.worker('default', 1)
        # Fail startup on incompatible persisted state rather than silently dropping it.
        for row in self.db.execute('SELECT id,revision FROM rule_heads').fetchall():
            self.worker(row['id'], row['revision'])

    def _insert(self, document):
        self.db.execute('INSERT INTO rule_revisions VALUES(?,?,?)',
                        (document['id'], document['revision'], encode(document)))
        self.db.execute('INSERT OR REPLACE INTO rule_heads VALUES(?,?)',
                        (document['id'], document['revision']))

    def get(self, identity, revision=None):
        with self.lock:
            if revision is None:
                row = self.db.execute('SELECT revision FROM rule_heads WHERE id=?', (identity,)).fetchone()
                if row is None:
                    raise Problem(404, 'rule_not_found')
                revision = row[0]
            if type(revision) is not int or not 1 <= revision <= self.MAX_REVISIONS:
                raise Problem(404, 'rule_revision_not_found')
            row = self.db.execute('SELECT document FROM rule_revisions WHERE id=? AND revision=?',
                                  (identity, revision)).fetchone()
            if row is None:
                raise Problem(404, 'rule_revision_not_found')
            return json.loads(row[0])

    def list(self):
        with self.lock:
            return [self.get(row[0]) for row in self.db.execute('SELECT id FROM rule_heads ORDER BY id').fetchall()]

    def history(self, identity):
        with self.lock:
            self.get(identity)
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT document FROM rule_revisions WHERE id=? ORDER BY revision DESC', (identity,))]

    def worker(self, identity, revision):
        with self.lock:
            key = (identity, revision)
            if key not in self.workers:
                doc = self.get(identity, revision)
                pipeline = Pipeline.parse(doc['pipeline'])
                legacy = doc.get('legacy', False)
                cfg = replace(self.config, rules=pipeline, event_source=pipeline.spec['source']['source'],
                              data_dir=(self.config.data_dir if legacy else self.config.data_dir / 'rules' / identity / str(revision)),
                              evidence_options=EvidenceOptions(**doc['evidence_options']),
                              cma_event_url=self.config.cma_event_url if doc['delivery_enabled'] else '',
                              cma_context_config=self.config.cma_context_config if legacy else '',
                              cma_context_token=self.config.cma_context_token if legacy else '',
                              rule_id='' if legacy else identity, rule_revision=0 if legacy else revision,
                              not_before=0 if legacy else doc['created_at'])
                self.workers[key] = self.factory(cfg)
            return self.workers[key]

    def save(self, value, identity=None, expected=None, archive=False):
        with self.lock:
            if not self.config.processing_enabled:
                raise Problem(503, 'processing_paused')
            if self.config.mongo_uri:
                raise Problem(409, 'managed_rules_require_local_persistence')
            old = self.get(identity) if identity else None
            if old:
                if expected != str(old['revision']):
                    raise Problem(409, 'revision_conflict')
                if old['archived']:
                    raise Problem(409, 'rule_archived')
                revision = old['revision'] + 1
                if self.db.execute('SELECT count(*) FROM rule_revisions').fetchone()[0] >= self.MAX_REVISIONS:
                    raise Problem(409, 'rule_revision_limit')
                allowed = {'pipeline', 'label', 'enabled', 'delivery_enabled'}
            else:
                allowed = {'id', 'pipeline', 'label', 'enabled', 'delivery_enabled'}
                identity = value.get('id')
                if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', identity) or identity == 'default':
                    raise Problem(400, 'invalid_rule_id')
                if self.db.execute('SELECT 1 FROM rule_heads WHERE id=?', (identity,)).fetchone():
                    raise Problem(409, 'rule_already_exists')
                if self.db.execute('SELECT count(*) FROM rule_heads').fetchone()[0] >= self.MAX_RULES:
                    raise Problem(409, 'rule_limit')
                revision = 1
                if self.db.execute('SELECT count(*) FROM rule_revisions').fetchone()[0] >= self.MAX_REVISIONS:
                    raise Problem(409, 'rule_revision_limit')
            if set(value) - allowed:
                raise Problem(400, 'unknown_rule_fields')
            doc = {**(old or {}), **value, 'id': identity, 'revision': revision,
                   'archived': archive, 'created_at': time.time(), 'legacy': False}
            doc.setdefault('enabled', False)
            doc.setdefault('delivery_enabled', False)
            if archive:
                doc['enabled'] = False
            if type(doc['enabled']) is not bool or type(doc['delivery_enabled']) is not bool:
                raise Problem(400, 'invalid_rule_switch')
            if doc['delivery_enabled'] and not self.config.cma_event_url:
                raise Problem(400, 'cma_destination_not_configured')
            try:
                policy = Pipeline.parse(doc.get('pipeline'))
                if not policy.spec['source']['source']:
                    raise ValueError()
                opts = EvidenceOptions(**policy.spec['evidence']['options'])
                if policy.spec['evidence']['adapter'] == 'frigate' and (not self.config.frigate_proxy_url
                        or not self.config.evidence_service or not self.config.evidence_token):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise Problem(400, 'invalid_rule_pipeline') from None
            doc['pipeline'], doc['evidence_options'] = policy.spec, opts.__dict__
            doc.setdefault('label', policy.spec['name'])
            if not isinstance(doc['label'], str) or not 1 <= len(doc['label']) <= 128:
                raise Problem(400, 'invalid_rule_label')
            # No state is opened before the validated revision is committed.
            with self.db:
                self._insert(doc)
            return doc

    def ingest_relay(self, event):
        with self.lock:
            if not self.config.processing_enabled:
                raise Problem(503, 'processing_paused')
            if (event.get('specversion') != '1.0' or not isinstance(event.get('id'), str)
                    or not event['id'] or not isinstance(event.get('source'), str) or not event['source']):
                raise Problem(400, 'invalid_source_event')
            outcomes = {}
            for doc in self.list():
                if not doc['enabled'] or doc['archived']:
                    continue
                src = doc['pipeline']['source']
                if event['source'] != src['source']:
                    continue
                health = event.get('type') == src['health']['event_type']
                if not health and event.get('type') not in src['types']:
                    continue
                # An explicit global heartbeat describes this shared bridge.
                current = event
                if health and event.get('subject') == '*' and src['subject'] != '*':
                    current = {**event, 'subject': src['subject']}
                subject = (current.get('subject') if health or src['adapter'] == 'frigate_reviews'
                           else path_value(current, src['fields'].get('subject', 'subject')))
                if src['subject'] != '*' and subject != src['subject']:
                    continue
                worker = self.worker(doc['id'], doc['revision'])
                with worker.lock:
                    outcomes[doc['id']] = worker.ingest_relay(current)
            if len(outcomes) == 1:
                return next(iter(outcomes.values())), None
            return ('routed' if outcomes else 'ignored'), outcomes

    def flush(self):
        # Serialize cutover with finalization: a retired revision never starts new work.
        with self.lock:
            count = 0
            for doc in self.list():
                if not doc['enabled'] or doc['archived']:
                    continue
                try:
                    count += self.worker(doc['id'], doc['revision']).flush()
                    self.runtime_errors.pop(doc['id'], None)
                except Exception as exc:
                    # One failed rule must not kill the timer or other rules.
                    # Report exception classes only, never upstream bodies/secrets.
                    self.runtime_errors[doc['id']] = {'revision': doc['revision'], 'type': type(exc).__name__}
            self.last_tick_at = time.time()
            return count

    def status(self):
        with self.lock:
            current = self.list()
            # Include retired queues/history in totals; they are retained, paused.
            states = []
            for row in self.db.execute('SELECT id,revision FROM rule_revisions').fetchall():
                key = (row['id'], row['revision'])
                directory = self.config.data_dir / 'rules' / row['id'] / str(row['revision'])
                if key in self.workers or any(directory.glob('*.sqlite3')):
                    states.append(self.worker(*key).status())
            result = dict(mode='observe', model_calls=0, rules=len(current),
                          enabled_rules=sum(d['enabled'] and not d['archived'] for d in current),
                          runtime_errors=dict(self.runtime_errors), last_tick_at=self.last_tick_at,
                          bindings=sum(s['bindings'] for s in states), decisions=sum(s['decisions'] for s in states),
                          turns=sum(s['turns'] for s in states),
                          cma_deliveries={k: sum(s['cma_deliveries'][k] for s in states)
                                          for k in ('pending', 'delivered', 'failed')},
                          context_cleanup=(dict(self.legacy.context_cleanup.status) if self.legacy.context_cleanup else {'enabled': False}))
            return result

    def close(self):
        with self.lock:
            for worker in self.workers.values():
                with worker.lock:
                    worker.close()
            self.db.close()
