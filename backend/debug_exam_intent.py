import asyncio
import sys
import uuid
sys.path.insert(0, 'C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT\\backend')

from app.orchestrator.planner import plan, _detect_examination_intent
from app.orchestrator.extractor import extract_entities
from app.orchestrator.context import ConversationContext

# Debug: check what the examination intent detector returns
ctx = ConversationContext()
text = 'examination fee of bba'
e = extract_entities(text)
raw = text

exam_intent = _detect_examination_intent(text, e, raw)
print('exam_intent:', exam_intent)
print('programme from exam_intent:', exam_intent.get('programme') if exam_intent else None)

# Also test with 'BBA examination fee'
text2 = 'BBA examination fee'
e2 = extract_entities(text2)
exam_intent2 = _detect_examination_intent(text2, e2, text2)
print('exam_intent2:', exam_intent2)
print('programme from exam_intent2:', exam_intent2.get('programme') if exam_intent2 else None)