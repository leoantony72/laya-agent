import ast

# Test various versions
codes = [
    'json.dumps({})',
    'json.dumps({"a": 1})',
    'json.dumps({\n    "a": 1\n})',
    'llm.add_response(json.dumps({\n    "a": 1\n}))',
    'llm.add_response(json.dumps({\n            "intent": "tool",\n}))',
]

for i, code in enumerate(codes):
    try:
        ast.parse(code)
        print(f'Test {i+1}: OK')
    except SyntaxError as e:
        print(f'Test {i+1}: FAIL - {e.msg} at offset {e.offset}')
        print(f'  Code: {repr(code)}')