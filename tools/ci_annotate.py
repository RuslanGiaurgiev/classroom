"""Для GitHub Actions: показать упавшие тесты как аннотации (видны без входа в GitHub)."""
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
for case in root.iter("testcase"):
    for bad in list(case.findall("failure")) + list(case.findall("error")):
        text = (bad.get("message") or "") + "\n" + (bad.text or "")
        text = text.strip().replace("%", "%25").replace("\r", "").replace("\n", "%0A")[-3000:]
        print(f"::error title={case.get('classname')}.{case.get('name')}::{text}")
