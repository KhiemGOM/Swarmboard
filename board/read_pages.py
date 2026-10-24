"""Size-aware pages with explicit cursors and full-record detail chunks."""
import json
from .core import BoardError
from .responses import encode, MAX_RESPONSE_CHARS

LISTS = {'noticeboard_read': 'posts', 'inbox_read': 'messages', 'job_list': 'jobs', 'board_wait': 'waits', 'board_coordination': 'incidents', 'board_file': 'records'}
STATUS_KEYS = ('agents', 'claims', 'custody', 'patch_queues', 'patch_custody', 'matches')


def compact(value):
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + '… [use board_read for full record]'
    if isinstance(value, list):
        return [compact(v) for v in value[:10]]
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items()}
    return value


def reference(key, row):
    ref = {'tool': 'board_read', 'entity': key, 'record_id': row.get('id', row.get('path', row.get('query')))}
    if key == 'matches':
        ref['kind'] = row['kind']
    return ref


def fit(method, result, params):
    if method in ('board_wait', 'board_coordination', 'board_file') and params.get('action') not in ('list', 'history'):
        return result
    if method not in LISTS and method not in ('board_status', 'heartbeat'):
        return result
    keys = (LISTS[method],) if method in LISTS else STATUS_KEYS
    cursor = params.get('cursor', {})
    if not isinstance(cursor, dict) or any(k not in STATUS_KEYS or type(v) is not int or v < 0 for k, v in cursor.items()):
        raise BoardError('cursor must map status section names to non-negative offsets')
    limit = params.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BoardError('limit must be an integer between 1 and 100')
    original = {key: result.get(key, []) for key in keys}
    starts = {key: cursor.get(key, 0) if method not in LISTS else 0 for key in keys}
    for key in keys:
        result[key] = []
    result['has_more'] = False
    for _ in range(limit):
        for key in keys:
            index = starts[key] + len(result[key])
            if index >= len(original[key]):
                continue
            row = original[key][index]
            result[key].append(row)
            if len(encode(result)) > MAX_RESPONSE_CHARS - 1500:
                result[key].pop()
                if not result[key]:
                    small = compact(row)
                    small['_detail'] = reference(key, row)
                    if key == 'matches' and params.get('target_agent_id'):
                        small['_detail']['target_agent_id'] = params['target_agent_id']
                    result[key].append(small)
                    if len(encode(result)) > MAX_RESPONSE_CHARS - 1500:
                        result[key].pop()
    remaining = {key: starts[key] + len(result[key]) < len(original[key]) for key in keys}
    result['has_more'] = any(remaining.values())
    if method in LISTS:
        rows = result[keys[0]]
        result['next_after_id'] = rows[-1]['id'] if rows else params.get('after_id', 0)
    elif result['has_more']:
        result['next_cursor'] = {key: starts[key] + len(result[key]) for key in keys}
    if method in LISTS:
        result['returned_count'] = len(result[keys[0]])
    else:
        result['totals'] = {key: len(original[key]) for key in keys}
    return result


def detail(hub, db, p, agent):
    entity, ident = p.get('entity'), p.get('record_id')
    if not isinstance(entity, str) or type(ident) not in (str, int) or ident == '':
        raise BoardError('board_read requires entity and record_id (non-empty string or integer)')
    offset, limit = p.get('offset', 0), p.get('limit', 4000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise BoardError('board_read needs offset>=0 and limit between 1 and 8000')
    if entity in ('matches', 'scratch'):
        if entity == 'matches':
            kind = p.get('kind', 'file')
            if kind not in ('file', 'resource'):
                raise BoardError('kind must be file or resource')
            query = {'include_rules': False, 'paths' if kind == 'file' else 'resources': [ident]}
            if p.get('target_agent_id'):
                query['target_agent_id'] = p['target_agent_id']
            import time
            record = hub.dispatch(db, 'board_status', query, agent, time.time())['matches'][0]
        else:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scratch_completions'").fetchone():
                raise BoardError('No scratch completion history for record_id')
            record = [dict(r) for r in db.execute('SELECT * FROM scratch_completions WHERE path=? ORDER BY id', (hub.path(ident),))]
        return chunk(entity, ident, record, offset, limit)
    tables = {'posts': 'posts', 'messages': 'messages', 'jobs': 'jobs', 'claims': 'claims', 'custody': 'custody', 'agents': 'agents', 'patch_queues': 'patch_batches', 'patch_custody': 'patches', 'waits': 'board_waits', 'incidents': 'coordination_incidents', 'records': 'file_deletions'}
    if entity not in tables:
        raise BoardError('entity must identify a board_read schema entity; use the entity supplied by _detail')
    key = 'path' if entity == 'custody' else 'id'
    row = db.execute(f'SELECT * FROM {tables[entity]} WHERE {key}=?', (ident,)).fetchone()
    if not row or entity == 'messages' and row['recipient'] != agent or entity == 'waits' and row['agent'] != agent:
        raise BoardError('record_id is unknown or inaccessible')
    record = dict(row)
    if entity == 'jobs':
        from .jobs import view
        record = view(row, agent)
    if entity == 'agents':
        for private in ('token', 'inheritance_hash'):
            record.pop(private, None)
        from .agent_status import enrich
        enrich(db, [record])
    return chunk(entity, ident, record, offset, limit)


def chunk(entity, ident, record, offset, limit):
    content = encode(record)
    return {'entity': entity, 'record_id': ident, 'content': content[offset:offset+limit],
            'offset': offset, 'next_offset': min(len(content), offset+limit), 'total_chars': len(content), 'has_more': offset+limit < len(content)}
