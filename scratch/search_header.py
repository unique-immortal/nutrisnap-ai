import re

with open("templates/index.html", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Search for .header in <style> block
style_block = re.search(r'<style>(.*?)</style>', content, re.DOTALL)
if style_block:
    styles = style_block.group(1)
    # Find CSS rules matching header
    rules = re.findall(r'(\.[^{}]*header[^{}]*\{[^{}]*\})', styles, re.IGNORECASE)
    print("CSS Rules containing 'header':")
    for r in rules:
        print(r)
        print("-" * 20)

# 2. Search for <header> HTML tag and its class attributes
print("\nHTML tags matching <header:")
matches = re.finditer(r'<header[^>]*>', content)
for m in matches:
    start = max(0, m.start() - 50)
    end = min(len(content), m.end() + 200)
    print(content[start:end].replace('\n', ' '))
    print("=" * 40)
