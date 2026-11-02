"""Deterministic composition using originals and approved design fragments. No generative tools."""
from io import BytesIO
from pathlib import Path
import base64
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from .config import ROOT, UserError
from .models import Draft, validate_draft

ASSETS=ROOT/'assets'
W,H=1080,1920

def font(path,size): return ImageFont.truetype(str(path),size)

def feather(im,top=0,bottom=0,sides=0):
    im=im.convert('RGBA');w,h=im.size
    def smooth(a):
        a=max(0,min(1,a));return round(255*a*a*(3-2*a))
    if top or bottom:
        m=Image.new('L',(1,h));m.putdata([smooth(min(y/top if top else 1,(h-1-y)/bottom if bottom else 1)) for y in range(h)])
        im.putalpha(ImageChops.multiply(im.getchannel('A'),m.resize((w,h))))
    if sides:
        m=Image.new('L',(w,1));m.putdata([smooth(min(x/sides,(w-1-x)/sides)) for x in range(w)])
        im.putalpha(ImageChops.multiply(im.getchannel('A'),m.resize((w,h))))
    return im

def paste(canvas,path,box,crop=None,fades=(0,0,0),blur=0):
    with Image.open(path) as source:
        im=source.convert('RGBA')
    if crop:
        x,y,w,h=crop;im=im.crop((round(x*im.width),round(y*im.height),round((x+w)*im.width),round((y+h)*im.height)))
    width,height=box[2:]
    im=im.resize((width,height),Image.Resampling.LANCZOS)
    if blur:im=im.filter(ImageFilter.GaussianBlur(blur))
    im=feather(im,*fades)
    canvas.alpha_composite(im,(box[0],box[1]))

def wrap(text,draw,face,width):
    lines=[]
    for para in text.split('\n'):
        line=''
        for word in para.split():
            if draw.textlength(word,font=face)>width:raise UserError('Слишком длинное слово для макета. Сократите текст.')
            candidate=(line+' '+word).strip()
            if line and draw.textlength(candidate,font=face)>width:
                lines.append(line);line=word
            else:line=candidate
        lines.append(line)
    return lines

class Renderer:
    def render(self,draft:Draft,project:dict,brand:dict,out:Path,only=None):
        validate_draft(draft,project)
        out.mkdir(parents=True,exist_ok=True)
        assets={a['id']:a for a in project['assets']}
        heading=Path(brand.get('heading_font') or ASSETS/'fonts/ariblk.ttf')
        bodypath=Path(brand.get('body_font') or ASSETS/'fonts/arial.ttf')
        ink=brand.get('ink','#001b19');yellow=brand.get('accent','#ffcc0f')
        paths=[]
        for idx,s in enumerate(draft.slides):
            target=out/f'story-{idx+1:02}.png'
            if only is not None and idx not in only:continue
            canvas=Image.new('RGBA',(W,H),'white')
            paste(canvas,ASSETS/'sky.png',(0,590,1080,520),fades=(210,160,0),blur=12)
            paste(canvas,ASSETS/'greenery.png',(0,1430,1080,490),fades=(240,0,0),blur=18)
            if s.layout in ('plan','plan_detail'):
                canvas.alpha_composite(Image.new('RGBA',(W,1220),(255,255,255,120)),(0,700))
            # Layout calculations validate fit before exporting any approved wording.
            draw=ImageDraw.Draw(canvas)
            size=s.heading_size
            while size>=56:
                face=font(heading,size)
                if max(draw.textlength(t,font=face) for t in s.title_lines)<=930:break
                size-=1
            if size<56:raise UserError(f'Заголовок слайда {idx+1} слишком длинный. Сократите его.')
            y=330 if len(s.title_lines)==1 else 280 if len(s.title_lines)==2 else 250
            step=round(size*1.23)
            body_y=y+step*len(s.title_lines)+34
            body_face=font(bodypath,s.body_size)
            lines=wrap(s.body,draw,body_face,930)
            line_h=round(s.body_size*1.22)
            body_bottom=body_y+len(lines)*line_h
            if body_bottom>850:raise UserError(f'Текст слайда {idx+1} не помещается. Сократите его или разделите слайд.')
            image_y=max(s.image_y,body_bottom+70)
            def photo(asset_id,x,y,width,crop=None,top=140,bottom=100,side=0):
                a=assets[asset_id];height=round(width*a['height']/a['width'])
                if crop:height=round(width*a['height']*crop[3]/(a['width']*crop[2]))
                if y+height>H+50:
                    raise UserError(f'Изображение слайда {idx+1} не помещается. Нужна другая компоновка.')
                paste(canvas,a['path'],(x,y,width,height),crop,(top,bottom,side))
                return height
            if s.layout=='photo':photo(s.image_ids[0],0,image_y,W,s.crop)
            elif s.layout=='comparison':
                main_h=photo(s.image_ids[0],0,image_y,W,s.crop,120,80)
                row_y=min(image_y+main_h-15,1490)
                if row_y<image_y+main_h-110:raise UserError('Для сравнения нужно сократить текст или изменить кадрирование.')
                photo(s.image_ids[1],0,row_y,540,None,35,65,14)
                photo(s.image_ids[2],540,row_y,540,None,35,65,14)
            else:
                a=assets[s.image_ids[0]];crop=s.crop
                width=1020 if s.layout=='plan' else 984
                x=30 if s.layout=='plan' else 48
                h=round(width*a['height']*(crop[3] if crop else 1)/(a['width']*(crop[2] if crop else 1)))
                if image_y+h>1860:
                    ratio=(1860-image_y)/h;width=round(width*ratio);h=round(h*ratio);x=(W-width)//2
                paste(canvas,a['path'],(x,image_y,width,h),crop,(24,35,16))
            # Text is composited last, retaining clear space over soft backgrounds.
            draw=ImageDraw.Draw(canvas)
            for line,t in enumerate(s.title_lines):
                ly=y+line*step
                if line==s.accent_line:
                    tw=draw.textlength(t,font=face)
                    draw.rectangle((64,ly-2,round(89+tw),ly+round(size*1.16)),fill=yellow)
                draw.text((75,ly),t,font=face,fill=ink,anchor='lt')
            for i,t in enumerate(lines):draw.text((75,body_y+i*line_h),t,font=body_face,fill=ink,anchor='lt')
            logo=Path(brand.get('logo') or ASSETS/'logo.png')
            with Image.open(logo) as im:
                im=im.convert('RGBA');im.thumbnail((300,100),Image.Resampling.LANCZOS)
                canvas.alpha_composite(im,(1010-im.width,112))
            canvas.convert('RGB').save(target)
            paths.append(target)
        return paths

def image_data(path,max_size=(900,900)):
    with Image.open(path) as im:
        im=im.convert('RGB');im.thumbnail(max_size)
        buf=BytesIO();im.save(buf,format='JPEG',quality=82)
    return 'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()

def contact_sheet(project):
    assets=project['assets'];cw,ch=450,300
    rows=(len(assets)+1)//2
    im=Image.new('RGB',(cw*2,ch*rows),'white');d=ImageDraw.Draw(im)
    face=font(ASSETS/'fonts/arial.ttf',22)
    for i,a in enumerate(assets):
        with Image.open(a['path']) as img:
            img=img.convert('RGB');img.thumbnail((cw-12,ch-38))
            x=(i%2)*cw;y=(i//2)*ch
            im.paste(img,(x+(cw-img.width)//2,y))
            d.text((x+10,y+ch-32),a['id'],fill='black',font=face)
    buf=BytesIO();im.save(buf,'JPEG',quality=84)
    return 'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()
