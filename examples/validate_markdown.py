"""Example domain validator: a completed task must provide a Markdown checklist."""
import json
from pathlib import Path
import sys

task = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
result_path = Path(sys.argv[2]).resolve()
result = json.loads(result_path.read_text(encoding='utf-8'))
paths = [Path(p) if Path(p).is_absolute() else result_path.parent / p for p in result['artifacts']]
if not any(p.suffix == '.md' and p.is_file() and '- ' in p.read_text(encoding='utf-8') for p in paths):
    raise SystemExit('A Markdown checklist artifact is required')
print('Checklist accepted for', task['id'])
