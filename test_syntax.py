import ast

with open('winbrow/llm.py', 'r') as f:
    content = f.read()

try:
    ast.parse(content)
    print('OK - No syntax errors')
except SyntaxError as e:
    print(f'Error at line {e.lineno}, offset {e.offset}: {e.msg}')
    lines = content.split('\n')
    for i in range(max(0, e.lineno-5), min(len(lines), e.lineno+5)):
        marker = '>>>' if i == e.lineno-1 else '   '
        print(f'{marker} {i+1}: {repr(lines[i])}')