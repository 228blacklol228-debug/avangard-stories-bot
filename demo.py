"""Render an offline example with the approved source assets; no API calls or credentials."""
import json
from pathlib import Path
from stories_bot.config import ROOT
from stories_bot.models import Draft
from stories_bot.render import Renderer

def example():
    base=ROOT/'examples/scandi-78'
    project=json.loads((base/'project.json').read_text(encoding='utf-8'))
    for a in project['assets']:a['path']=str(base/'originals'/Path(a['path']).name)
    draft=Draft.model_validate_json((base/'draft.json').read_text(encoding='utf-8'))
    brand=json.loads((ROOT/'assets/brand.json').read_text(encoding='utf-8'))
    return project,draft,brand

if __name__=='__main__':
    project,draft,brand=example()
    paths=Renderer().render(draft,project,brand,ROOT/'demo-output')
    print('Готово: '+str(len(paths))+' картинок в '+str(ROOT/'demo-output'))
