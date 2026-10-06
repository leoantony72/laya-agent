import ast

with open('winbrow/llm.py', 'r') as f:
    content = f.read()

# Check the if __name__ block
idx = content.find('if __name__ == "__main__":')
print('Found at:', idx)
print(repr(content[idx:idx+500]))