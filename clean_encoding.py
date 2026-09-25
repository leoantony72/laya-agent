import os

def clean_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # Replacements of corrupted characters
    corruptions = [
        ("â€”", " - "),
        ("â”€", "-"),
        ("â€™", "'"),
        ("â€˜", "'"),
        ("â€œ", '"'),
        ("â€\x9d", '"'),
        ("â€", '"'),
        ("\u2014", " - "),
        ("\u2013", "-"),
        ("\u2500", "-"),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
    ]
    for bad, good in corruptions:
        content = content.replace(bad, good)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Cleaned {path}")

if __name__ == "__main__":
    clean_file("winbrow/generator.py")
    clean_file("winbrow/registry.py")
    clean_file("winbrow/agent.py")
