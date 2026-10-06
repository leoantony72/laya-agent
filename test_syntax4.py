import ast

code = '''if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        # Test with fake LLM
        llm = FakeLLM()
        llm.add_response(json.dumps({
            "intent": "tool",
            "confidence": 0.95,
            "tool": "open_file",
            "arguments": {"target": "report.pdf", "app": "chrome"},
            "requires_clarification": False,
            "clarification_question": "",
            "reasoning": "User wants to open a PDF file"
        })
        
        result = await llm.extract_intent("open report.pdf in chrome")
        print(f"Intent: {result}")
        
        # Test reference resolution
        ref = await llm.resolve_reference(
            "open that file",
            "Just opened report.pdf",
            [{"path": "/home/user/report.pdf", "label": "report.pdf"}]
        )
        print(f"Reference resolved to: {ref}")
    
    asyncio.run(test())'''

try:
    ast.parse(code)
    print('Manual code parses OK')
except SyntaxError as e:
    print(f'Error at line {e.lineno}, offset {e.offset}: {e.msg}')
    lines = code.split('\n')
    for i in range(max(0, e.lineno-5), min(len(lines), e.lineno+5)):
        marker = '>>>' if i == e.lineno-1 else '   '
        print(f'{marker} {i+1}: {repr(lines[i])}')