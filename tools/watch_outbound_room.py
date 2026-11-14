'''Watch one tournament model connection without persisting credentials.'''

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import time


ROOM_SCRIPT = r'''
import json
from pathlib import Path
request = REQUEST
path = Path('/var/lib/ygo-tournament') / (request['room_id'] + '.json')
if not path.is_file():
    print(json.dumps({'terminal': True}))
else:
    room = json.loads(path.read_text())
    match = next((item for item in room.get('matches', []) if item.get('id') == request['match_id']), None)
    sid = (match.get('session') or {}).get('pvp_session_id') if match else None
    terminal = room.get('status') in ('closed', 'finished') or not match or match.get('status') in ('closed', 'finished')
    if terminal or sid != request['pvp_session_id']:
        print(json.dumps({'terminal': True}))
    else:
        session = json.loads((Path('/var/lib/ygo-pvp') / (sid + '.json')).read_text())
        connection = (session.get('connections') or {}).get(request['side']) or {}
        other = 'b' if request['side'] == 'a' else 'a'
        print(json.dumps({
            'terminal': session.get('status') in ('closed', 'finished', 'error'),
            'connected': bool(connection.get('connected')),
            'opponent_connected': bool(((session.get('connections') or {}).get(other) or {}).get('connected')),
            'game_number': session.get('game_number'),
            'password': (session.get('credentials') or {}).get(request['side']),
        }))
'''

AGENT_SCRIPT = r'''
import hashlib
import json
from pathlib import Path
import urllib.request
request = REQUEST
root = Path('/root/autodl-tmp/agent195m-room-20260915')
config = json.loads((root / 'admin/agent.json').read_text())
key = Path(config['key_file']).read_text().strip()
base = 'http://127.0.0.1:' + str(config.get('port') or 7918)
def call(route, body=None):
    headers = {'Authorization': 'Bearer ' + key}
    payload = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        payload = json.dumps(body).encode()
    req = urllib.request.Request(base + route, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=150) as response:
        return json.load(response)
try:
    listing = call('/outbound')
    sid = request['outbound_session']
    row = next((item for item in listing.get('items', []) if item.get('session') == sid), None)
    active = listing.get('active') or []
    metadata_path = Path(config['data']) / 'outbound/sessions' / sid / 'session.json'
    metadata = json.loads(metadata_path.read_text())
    if request['action'] == 'bootstrap':
        job = json.loads((Path(config['data']) / 'outbound/jobs' / (sid + '.json')).read_text())
        deck = next(item for item in call('/decks')['items'] if item.get('name') == metadata['model_deck_name'])
        if metadata.get('deck_source') != 'local' or metadata.get('selftest'):
            raise ValueError('unsupported session')
        print(json.dumps({'active': any(item.get('session') == sid for item in active), 'settings': {
            'host': metadata['host'], 'port': metadata['port'],
            'model_id': metadata['checkpoint_sha256'], 'deck_id': deck['id'],
            'deck_file_sha256': hashlib.sha256((metadata_path.parent / 'model.ydk').read_bytes()).hexdigest(),
            'deck_name': metadata['model_deck_name'],
            'nickname': metadata.get('nickname', 'ModelBot'),
            'preference': job.get('preference', 'auto'), 'version': request['version'],
            'deck_source': 'local', 'auth_mode': 'none',
        }}))
    else:
        settings = request['settings']
        expected = tuple(settings[name] for name in ('host', 'port', 'model_id', 'deck_name'))
        actual = tuple(metadata.get(name) for name in ('host', 'port', 'checkpoint_sha256', 'model_deck_name'))
        if expected != actual:
            raise ValueError('tracked session changed')
        competing = [item for item in active if item.get('session') != sid and item.get('host') == settings['host'] and item.get('port') == settings['port']]
        result = {'state': (row or {}).get('state'), 'error': (row or {}).get('error'), 'active': any(item.get('session') == sid for item in active), 'competing': bool(competing)}
        if request['action'] == 'recover':
            if result['active'] or competing or result['error'] == 'stopped_by_owner' or result['state'] == 'stopped':
                result['recovered'] = False
            else:
                deck = call('/decks/' + settings['deck_id'])
                if hashlib.sha256(deck['ydk'].encode('utf-8')).hexdigest() != settings['deck_file_sha256']:
                    raise ValueError('deck changed')
                body = {name: settings[name] for name in ('host', 'port', 'model_id', 'deck_id', 'nickname', 'preference', 'version', 'deck_source', 'auth_mode')}
                body.update(action='start', password=request['password'])
                created = call('/outbound', body)
                result.update(recovered=True, new_session=created['session'])
        print(json.dumps(result))
except Exception as error:
    print(json.dumps({'error_type': type(error).__name__}))
'''


def remote(host, template, request, timeout=50):
    script = template.replace('REQUEST', repr(request), 1)
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host]
    command.append('sudo -n python3 -' if host == 'aliyun' else 'python3 -')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run(command, input=script, capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout, creationflags=flags)
    if result.returncode:
        raise RuntimeError('remote call failed: ' + host)
    value = json.loads(result.stdout)
    if value.get('error_type'):
        raise RuntimeError('remote operation failed: ' + value['error_type'])
    return value


def decision(room, job, disconnected_seconds, cooldown_seconds, recoveries):
    if room.get('terminal'):
        return 'room_finished'
    if job.get('state') == 'stopped' or job.get('error') == 'stopped_by_owner':
        return 'owner_stopped'
    if job.get('competing'):
        return 'another_task_present'
    if room.get('connected'):
        return 'connected'
    if job.get('active'):
        return 'worker_reconnecting'
    if disconnected_seconds < 60 or cooldown_seconds < 120:
        return 'waiting_before_recovery'
    if recoveries >= 12:
        return 'recovery_limit'
    return 'recover'


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--room', required=True)
    parser.add_argument('--pvp-session', required=True)
    parser.add_argument('--outbound-session', required=True)
    parser.add_argument('--match', default='m-01')
    parser.add_argument('--side', choices=('a', 'b'), default='a')
    parser.add_argument('--version', type=lambda value: int(value, 0), default=0x1362)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--interval', type=float, default=20)
    parser.add_argument('--hours', type=float, default=24)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    plan_path = args.state_dir / 'plan.json'
    state_path = args.state_dir / 'state.json'
    log_path = args.state_dir / 'events.jsonl'
    request = {'room_id': args.room, 'pvp_session_id': args.pvp_session,
               'match_id': args.match, 'side': args.side}
    if args.bootstrap:
        room = remote('aliyun', ROOM_SCRIPT, request)
        job = remote('agent', AGENT_SCRIPT, {'action': 'bootstrap',
                     'outbound_session': args.outbound_session, 'version': args.version})
        if room.get('terminal') or not room.get('connected') or not job['active']:
            raise RuntimeError('initial connection is not active')
        plan = {**request, 'outbound_session': args.outbound_session,
                'settings': job['settings'], 'created_at': timestamp()}
        save(plan_path, plan)
        save(state_path, {'state': 'ready', 'room_id': args.room,
                         'outbound_session': args.outbound_session, 'updated_at': timestamp()})
        print(json.dumps({'state': 'ready', 'room_id': args.room, 'outbound_session': args.outbound_session}))
        return
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if any(plan.get(key) != value for key, value in request.items()):
        raise RuntimeError('watch target does not match bootstrap')
    lock_path = args.state_dir / 'watch.lock'
    lock_handle = lock_path.open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock_handle.seek(0)
        if lock_path.stat().st_size == 0:
            lock_handle.write(b'0')
            lock_handle.flush()
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    disconnected_at = None
    last_recovery = float('-inf')
    recovery_count = 0
    errors = 0
    previous = None
    deadline = time.monotonic() + args.hours * 3600
    while time.monotonic() < deadline:
        if (args.state_dir / 'stop').exists():
            save(state_path, {'state': 'monitor_stopped', 'pid': os.getpid(), 'updated_at': timestamp()})
            return
        try:
            room = remote('aliyun', ROOM_SCRIPT, request)
            job = {} if room.get('terminal') else remote('agent', AGENT_SCRIPT, {
                'action': 'status', 'outbound_session': plan['outbound_session'],
                'settings': plan['settings'],
            })
            now = time.monotonic()
            if room.get('connected'):
                disconnected_at = None
            elif disconnected_at is None:
                disconnected_at = now
            elapsed = 0 if disconnected_at is None else now - disconnected_at
            action = decision(room, job, elapsed, now - last_recovery, recovery_count)
            if action == 'recover':
                credential = room.get('password')
                if not isinstance(credential, str) or not credential:
                    raise RuntimeError('missing room credential')
                result = remote('agent', AGENT_SCRIPT, {
                    'action': 'recover', 'outbound_session': plan['outbound_session'],
                    'settings': plan['settings'], 'password': credential,
                }, timeout=180)
                last_recovery = time.monotonic()
                recovery_count += 1
                if result.get('recovered'):
                    plan['outbound_session'] = result['new_session']
                    save(plan_path, plan)
                    action = 'recovery_started'
                else:
                    action = 'recovery_skipped'
            state = {'state': action, 'pid': os.getpid(), 'room_id': args.room,
                     'pvp_session_id': args.pvp_session,
                     'outbound_session': plan['outbound_session'],
                     'model_connected': bool(room.get('connected')),
                     'opponent_connected': bool(room.get('opponent_connected')),
                     'game_number': room.get('game_number'),
                     'worker_state': job.get('state'), 'recoveries': recovery_count,
                     'updated_at': timestamp()}
            save(state_path, state)
            transition = (action, plan['outbound_session'], state['game_number'], state['opponent_connected'])
            if transition != previous:
                with log_path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(state) + '\n')
                previous = transition
            errors = 0
            if action in ('room_finished', 'owner_stopped', 'another_task_present', 'recovery_limit'):
                return
        except Exception as error:
            errors += 1
            save(state_path, {'state': 'monitor_error', 'pid': os.getpid(),
                             'room_id': args.room, 'error_type': type(error).__name__,
                             'consecutive_errors': errors, 'updated_at': timestamp()})
        time.sleep(args.interval)
    save(state_path, {'state': 'expired', 'pid': os.getpid(), 'updated_at': timestamp()})


if __name__ == '__main__':
    main()
