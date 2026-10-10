"""Bound tool payloads without silently dropping coordination information."""
import json

MAX_RESPONSE_CHARS = 24000


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def check(method, value):
    from .core import BoardError
    size = len(encode(value))
    if size <= MAX_RESPONSE_CHARS:
        return
    hints = {
        'board_status': 'Use target_agent_id="self" or an exact ID, paths/resources, and include_rules=false. Query fewer entities at a time.',
        'heartbeat': 'Use board_status with target_agent_id="self", paths/resources and include_rules=false.',
        'noticeboard_read': 'Use limit=1 (or a smaller limit) and after_id; advance after_id to the last returned post ID.',
        'inbox_read': 'Use limit=1 (or a smaller limit) and after_id; advance after_id to the last returned message ID. Reading never acknowledges messages.',
        'job_list': 'Use state, limit=1 (or a smaller limit) and after_id; advance after_id to the last returned job ID.',
    }
    hint = hints.get(method, 'Use a smaller batch or shorter text fields. Narrow board_status by agent/path/resource to inspect existing state.')
    raise BoardError(f'RESPONSE_TOO_LARGE: {method} produced {size} characters; cap is {MAX_RESPONSE_CHARS}. No result was truncated. This call was rolled back. Be more precise: {hint}')


def page(params):
    from .core import BoardError
    after = params.get('after_id', 0)
    limit = params.get('limit', 100)
    if type(after) is not int or after < 0:
        raise BoardError('after_id must be a non-negative integer')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BoardError('limit must be an integer between 1 and 100')
    return after, limit
