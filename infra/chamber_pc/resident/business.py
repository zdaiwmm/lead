"""Business release v1. The broker supplies a single eligible action or draft."""
import json
import sys

event=json.load(sys.stdin)
if event['event']=='admit':
    result={'execute':True}
elif event['event']=='draft':
    result={'body':event['body']}
else:
    raise ValueError('unsupported_event')
print(json.dumps(result,ensure_ascii=False))
