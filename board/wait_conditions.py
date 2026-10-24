"""Validated JSON predicates evaluated against one board transaction."""
import json
from .core import BoardError
from .claims import intersects, resource


def selected(db, node, now, agent=None):
    kinds = node.get('lock_types', ['file', 'resource', 'queue'])
    explicit = 'claim_ids' in node or 'batch_ids' in node
    owner = node.get('owner')
    owner = agent if owner == 'self' else owner
    path, resources = node.get('path'), node.get('resources')
    def matches(kind, value, row=None):
        if path is None and resources is None:
            return True
        if kind == 'resource':
            return resources is not None and value in resources
        return path is not None and (intersects(row, path) if row else
            path == '.' or value == '.' or value == path or value.startswith(path + '/') or path.startswith(value + '/'))
    claims = [row['id'] for row in db.execute('SELECT * FROM claims WHERE active=1 AND expires>?', (now,))
              if row['kind'] in kinds and (not owner or row['agent'] == owner)
              and (not explicit or row['id'] in node.get('claim_ids', []))
              and row['id'] not in node.get('exclude_claim_ids', [])
              and (row['kind'] != 'resource' or row['path'] not in node.get('exclude_resources', []))
              and matches(row['kind'], row['path'], row)]
    batches = []
    if 'queue' in kinds:
        for row in db.execute("SELECT id,path FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')"):
            if explicit and row['id'] not in node.get('batch_ids', []):
                continue
            if row['id'] in node.get('exclude_batch_ids', []) or not matches('queue', row['path']):
                continue
            if owner and not db.execute("SELECT 1 FROM patch_members WHERE batch_id=? AND agent=? AND state='ACTIVE'", (row['id'], owner)).fetchone():
                continue
            batches.append(row['id'])
    return claims, batches


def compile_condition(hub, db, value, agent, now):
    if len(json.dumps(value)) > 8000:
        raise BoardError('condition exceeds 8000 characters')
    budget = [64]
    def walk(node, depth=0):
        budget[0] -= 1
        if depth > 6 or budget[0] < 0 or not isinstance(node, dict):
            raise BoardError('condition requires objects, at most 64 nodes and depth 6')
        if 'all' in node or 'any' in node:
            key = 'all' if 'all' in node else 'any'
            children = node[key]
            if set(node) != {key} or not isinstance(children, list) or not 1 <= len(children) <= 32:
                raise BoardError('all/any requires one non-empty array of at most 32 conditions')
            return {key: [walk(child, depth + 1) for child in children]}
        kind = node.get('kind')
        if kind == 'locks':
            allowed = {'kind','scope','remaining_lte','lock_types','path','resources','owner','claim_ids','batch_ids','exclude_claim_ids','exclude_batch_ids','exclude_resources'}
            if set(node) - allowed:
                raise BoardError('Unknown locks condition field')
            result = dict(node)
            threshold = node.get('remaining_lte', 0)
            if type(threshold) is not int or not 0 <= threshold <= 10000:
                raise BoardError('remaining_lte must be an integer from 0 to 10000')
            result['remaining_lte'] = threshold
            scope = result.pop('scope', 'dynamic')
            if scope not in ('dynamic', 'snapshot'):
                raise BoardError('scope must be dynamic or snapshot')
            for key in ('lock_types','resources','claim_ids','batch_ids','exclude_claim_ids','exclude_batch_ids','exclude_resources'):
                if key not in result:
                    continue
                values = result[key]
                if not isinstance(values, list) or len(values) > 100:
                    raise BoardError(f'{key} must be an array of at most 100 entries')
                if key.endswith('_ids'):
                    if any(type(v) is not int or v < 1 for v in values):
                        raise BoardError(f'{key} requires positive integer IDs')
                    table = 'patch_batches' if 'batch' in key else 'claims'
                    if any(not db.execute(f'SELECT 1 FROM {table} WHERE id=?', (v,)).fetchone() for v in values):
                        raise BoardError(f'{key} contains an unknown ID')
                elif key == 'lock_types':
                    if not values or any(not isinstance(v, str) or v not in ('file','resource','queue') for v in values):
                        raise BoardError('lock_types must contain file, resource or queue')
                else:
                    values = [resource(v) for v in values]
                result[key] = list(dict.fromkeys(values))
            if 'path' in result:
                result['path'] = hub.path(result['path'])
            if 'owner' in result:
                owner = agent if result['owner'] == 'self' else result['owner']
                if not isinstance(owner, str) or not db.execute('SELECT 1 FROM agents WHERE id=?', (owner,)).fetchone():
                    raise BoardError('owner must be self or an exact agent ID')
            if scope == 'snapshot':
                claims, batches = selected(db, result, now, agent)
                if len(claims) + len(batches) > 100:
                    raise BoardError('Snapshot exceeds 100 locks; narrow its filters')
                result = {'kind':'locks','remaining_lte':threshold,'claim_ids':claims,'batch_ids':batches}
            return result
        fields = {'message_ack':('message_id','messages'), 'patch_finalized':('batch_id','patch_batches'), 'job_state':('job_id','jobs')}
        if kind not in fields:
            raise BoardError('Condition kind must be locks, message_ack, patch_finalized or job_state')
        key, table = fields[kind]
        if set(node) != {'kind', key} | ({'state'} if kind == 'job_state' else set()):
            raise BoardError('Invalid fields for condition kind')
        ident = node[key]
        if type(ident) is not int or ident < 1 or not db.execute(f'SELECT 1 FROM {table} WHERE id=?', (ident,)).fetchone():
            raise BoardError(f'{key} must identify an existing record')
        if kind == 'message_ack' and not db.execute('SELECT 1 FROM messages WHERE id=? AND sender=?', (ident, agent)).fetchone():
            raise BoardError('message_id must identify a message you sent')
        if kind == 'job_state' and node['state'] not in ('OPEN','CLAIMED','DONE'):
            raise BoardError('job state must be OPEN, CLAIMED or DONE')
        return dict(node)
    compiled = walk(value)
    if len(json.dumps(compiled)) > 8000:
        raise BoardError('Normalized condition exceeds 8000 characters; narrow its filters')
    return compiled


def evaluate(db, node, now, agent=None):
    if 'all' in node:
        return all(evaluate(db, child, now, agent) for child in node['all'])
    if 'any' in node:
        return any(evaluate(db, child, now, agent) for child in node['any'])
    kind = node['kind']
    if kind == 'locks':
        claims, batches = selected(db, node, now, agent)
        return len(claims) + len(batches) <= node['remaining_lte']
    if kind == 'message_ack':
        row = db.execute('SELECT acknowledged FROM messages WHERE id=?', (node['message_id'],)).fetchone()
        return bool(row and row[0] is not None)
    if kind == 'patch_finalized':
        row = db.execute('SELECT state FROM patch_batches WHERE id=?', (node['batch_id'],)).fetchone()
        return bool(row and row[0] in ('APPLIED','BLOCKED'))
    row = db.execute('SELECT state FROM jobs WHERE id=?', (node['job_id'],)).fetchone()
    return bool(row and row[0] == node['state'])
