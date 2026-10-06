import ast

# Exact code from the file
code = '''        llm.add_response(json.dumps({
            "intent": "tool",
            "confidence": 0.95,
            "tool": "open_file",
            "arguments": {"target": "report.pdf", "app": "chrome"},
            "requires_clarification": False,
            "clarification_question": "",
            "reasoning": "User wants to open a PDF file"
        }))'''

try:
    ast.parse(code)
    print('Exact code parses OK')
except SyntaxError as e:
    print(f'FAIL - {e.msg} at offset {e.offset}')
    print(f'Line {e.lineno}')
    lines = code.split('\n')
    for i, line in enumerate(lines):
        print(f'  {i+1}: {repr(line)}')