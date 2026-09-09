import py_compile
import os

# Check various files
files = [
    r'backend/app/orchestrator/state.py',
    r'backend/app/orchestrator/student_session.py',
    r'backend/app/services/registry.py',
    r'backend/app/chat/routes.py',
    r'backend/app/orchestrator/engine.py',
]

for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f'{os.path.basename(f)}: OK')
    except SyntaxError as e:
        print(f'{os.path.basename(f)}: SYNTAX ERROR - {e}')