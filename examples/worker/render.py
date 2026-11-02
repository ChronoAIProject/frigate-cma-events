"""Render one shared worker bootstrap; site settings stay outside the checkout."""
import argparse
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def render(settings):
    required = {'owner_id', 'model_service', 'model', 'services'}
    if not isinstance(settings, dict) or not required <= settings.keys() or set(settings) - required - {'name', 'thinking_effort', 'hmac_key_id'}:
        raise ValueError('Use owner_id, model_service, model, services and optional name/thinking_effort/hmac_key_id')
    if any(not isinstance(settings[k], str) or not settings[k] for k in required - {'services'}):
        raise ValueError('Owner and model fields must be nonempty strings')
    if not isinstance(settings['services'], list) or not settings['services'] or any(not isinstance(s, str) or not s for s in settings['services']):
        raise ValueError('Grant explicit service names, including the Monitor worker service')
    values = {'CMA_OWNER_ID': settings['owner_id'], 'WORKER_NAME': settings.get('name', 'VideoEventAnalysisWorker'),
              'MODEL_SERVICE': settings['model_service'], 'MODEL_NAME': settings['model'],
              'MODEL_THINKING_EFFORT': settings.get('thinking_effort', 'medium'),
              'CMA_HMAC_KEY_ID': settings.get('hmac_key_id', 'monitor-v1'),
              'WORKER_PROMPT': (HERE / 'system-prompt.md').read_text(), 'CHANNEL_RECORD_ID': str(uuid.uuid4())}
    def expand(value):
        if isinstance(value, dict): return {k: expand(v) for k,v in value.items()}
        if isinstance(value, list): return [expand(v) for v in value]
        if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
            return values.get(value[2:-1], value)
        return value
    result = expand(json.loads((HERE / 'channel.example.json').read_text()))
    result['profile']['permission']['service_scope'] = list(dict.fromkeys(settings['services']))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(ROOT): parser.error('Keep installation files outside the source checkout')
    payload = render(json.loads(args.settings.read_text()))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output: json.dump(payload, output, ensure_ascii=False, indent=2)
    print('Wrote shared worker definition. Fill distinct channel credentials before applying through CMA.')
