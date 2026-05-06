"""
data/curriculum.json を grammar_v3.html の placeholder に埋め込む
"""
import os, json

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, "grammar_v3_template.html")
DATA = os.path.join(ROOT, "data", "curriculum.json")
OUT = os.path.join(ROOT, "grammar_v3.html")

PLACEHOLDER = "/* @@CURRICULUM_DATA@@ */"

def main():
    with open(TEMPLATE, encoding="utf-8") as fp:
        html = fp.read()
    with open(DATA, encoding="utf-8") as fp:
        data_str = fp.read()
    if PLACEHOLDER not in html:
        raise RuntimeError("placeholder not found")
    html = html.replace(PLACEHOLDER, data_str)
    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write(html)
    sz = os.path.getsize(OUT)/1024
    print(f"wrote {OUT} ({sz:.1f}KB)")

if __name__ == "__main__":
    main()
