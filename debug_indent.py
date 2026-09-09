with open('C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS-AI-BOT\\backend/app/orchestrator/engine.py', 'rb') as f:
    content = f.read()
    print(f'File size: {len(content)} bytes')
    print(f'Contains CRLF: {b"\\r\\n" in content}')
    print(f'Contains CR: {b"\\r" in content}')
    print(f'Contains LF: {b"\\n" in content}')
    
    # Try splitting by \n only
    lines = content.split(b'\n')
    print(f'Lines after split by \\n: {len(lines)}')
    for i in range(min(10, len(lines))):
        line = lines[i]
        # Count leading spaces
        stripped = line.lstrip(b' ')
        leading = len(line) - len(stripped)
        print(f'Line {i+1}: {leading:2d} spaces | {stripped[:40]}')