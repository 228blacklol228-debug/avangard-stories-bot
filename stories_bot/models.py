from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Slide(Strict):
    title_lines: list[str] = Field(min_length=1, max_length=3)
    accent_line: int = Field(ge=-1, le=2)
    body: str = Field(max_length=260)
    layout: Literal['photo', 'comparison', 'plan', 'plan_detail']
    image_ids: list[str] = Field(min_length=1, max_length=3)
    crop: list[float] | None
    heading_size: int = Field(ge=56, le=122)
    body_size: int = Field(ge=32, le=50)
    image_y: int = Field(ge=720, le=1100)
    evidence: list[str] = Field(min_length=1)

class Draft(Strict):
    slides: list[Slide] = Field(min_length=1, max_length=10)
    notice: str

class Decision(Strict):
    action: Literal['apply', 'clarify', 'unsupported']
    message: str
    draft: Draft | None

class Selection(Strict):
    url: str | None
    message: str

def validate_draft(draft: Draft, project: dict):
    assets = {a['id']: a for a in project['assets']}
    for s in draft.slides:
        if any(i not in assets for i in s.image_ids):
            raise ValueError('Unknown image reference')
        if s.accent_line >= len(s.title_lines):
            raise ValueError('Unknown highlighted line')
        if s.layout == 'comparison' and len(s.image_ids) != 3:
            raise ValueError('A comparison requires three existing images')
        if s.layout in ('plan', 'plan_detail') and assets[s.image_ids[0]]['kind'] != 'plan':
            raise ValueError('A plan must use the project plan')
        if s.layout in ('photo', 'comparison') and any(assets[i]['kind'] != 'photo' for i in s.image_ids):
            raise ValueError('A photo layout cannot use a plan')
        if s.crop is not None:
            if len(s.crop) != 4 or any(not 0 <= x <= 1 for x in s.crop):
                raise ValueError('Invalid crop')
            x,y,w,h = s.crop
            if w < .15 or h < .15 or x+w > 1.001 or y+h > 1.001:
                raise ValueError('Crop exceeds original')
        if s.layout == 'plan' and s.crop is not None:
            raise ValueError('Full plans must keep dimensions')

