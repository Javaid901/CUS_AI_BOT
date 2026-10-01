import sys
sys.path.insert(0, 'C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT\\backend')

from app.orchestrator.planner import plan
from app.orchestrator.extractor import extract_entities
from app.orchestrator.context import ConversationContext
import uuid

def _route(raw: str):
    ctx = ConversationContext()
    e = extract_entities(raw)
    return plan(raw, ctx, "rt-" + uuid.uuid4().hex[:8], e)

# Test
result = _route("bca examination fee")
print('action:', result.action)
print('target:', result.target)
print('reason:', result.reason)
if result.response:
    print('response type:', result.response.get('type'))
    print('fields:', result.response.get('fields'))