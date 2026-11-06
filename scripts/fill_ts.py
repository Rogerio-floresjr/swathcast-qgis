"""Fill a Qt .ts file from a translations module.

    fill_ts.py <file.ts> <module> <DICT> <language> [--partial]

Without --partial, any string left untranslated is an error.
"""
import importlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
path, module, name, language = sys.argv[1:5]
partial = "--partial" in sys.argv
table = getattr(importlib.import_module(module), name)

tree = ET.parse(path)
tree.getroot().set("language", language)
missing = []
for msg in tree.iter("message"):
    src = msg.find("source").text
    tr = msg.find("translation")
    want = table.get(src)
    if want is None:
        missing.append(src)
        continue
    tr.attrib.pop("type", None)
    if msg.get("numerus") == "yes":
        for child in list(tr):
            tr.remove(child)
        for form in want:
            ET.SubElement(tr, "numerusform").text = form
    else:
        tr.text = want
tree.write(path, encoding="utf-8", xml_declaration=True)
if missing and not partial:
    sys.exit("untranslated:\n  " + "\n  ".join(missing))
