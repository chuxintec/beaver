"""Create a bounded static contact sheet from explicit labeled images; never edit sources."""
import argparse,json,math,unicodedata
from beaver.toolkit.support.host.paths import Path
from beaver.toolkit.support.common import hash_file,write_new,GreaError
from beaver.toolkit.support.safe_paths import canonical_path,disjoint,relative_name
from .image_alpha_audit import inspect_image

def create(manifest,output,font=None,columns=4,cell_size=(192,192),native_size=False):
    from PIL import Image,ImageDraw,ImageFont
    manifest=canonical_path(manifest)
    if manifest.stat().st_size>1024**2:raise ValueError('Manifest exceeds 1 MiB')
    initial=hash_file(manifest);spec=json.loads(manifest.read_text(encoding='utf-8'))
    if set(spec)!={'schema_version','images'} or (type(spec['schema_version']) is not int or spec['schema_version']!=1) or not 1<=len(spec['images'])<=128:raise ValueError('Expected manifest v1 with 1..128 explicit images')
    if not 1<=columns<=16 or any(not 1<=x<=8192 for x in cell_size):raise ValueError('Invalid columns/cell dimensions')
    ids=set();images=[];total=0
    for row in spec['images']:
        if set(row)!={'id','path'} or not isinstance(row['id'],str) or not 1<=len(row['id'])<=64 or any(ord(c)<32 for c in row['id']):raise ValueError('Expected short unique label and image path')
        key=unicodedata.normalize('NFC',row['id']).casefold()
        if key in ids:raise ValueError('Duplicate normalized image label')
        ids.add(key);path=manifest.parent/relative_name(row['path'])
        if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Linked image refused')
        info=inspect_image(path);total+=info['width']*info['height']
        if total>40000000:raise ValueError('Total source pixels exceed 40 million')
        images.append((row,path,info))
    if font:
        font=canonical_path(font);font_info=hash_file(font,128*1024**2);face=ImageFont.truetype(str(font),16)
    else:
        if any(not row['id'].isascii() for row,_,_ in images):raise ValueError('Non-ASCII labels require an explicit font supporting those glyphs')
        font_info=None;face=ImageFont.load_default(size=16)
    # Bound each label by rendered width; reject rather than silently truncate unique IDs.
    image_w=max(info['width'] for _,_,info in images) if native_size else cell_size[0]
    image_h=max(info['height'] for _,_,info in images) if native_size else cell_size[1]
    label_w=max(math.ceil(face.getlength(row['id'])) for row,_,_ in images)
    if label_w>4096:raise ValueError('Label width limit')
    cell_w=max(image_w,label_w)+16;cell_h=image_h+44
    width=cell_w*min(columns,len(images));height=cell_h*math.ceil(len(images)/columns)
    if width*height>20000000 or max(width,height)>16384:raise ValueError('Sheet pixel/dimension budget exceeded')
    out=disjoint(output,manifest,*[path for _,path,_ in images],*[font] if font else [])
    if out.exists() or not out.parent.is_dir():raise ValueError('Use a new output directory')
    canvas=Image.new('RGB',(width,height),(238,238,238));draw=ImageDraw.Draw(canvas);records=[]
    for index,(row,path,info) in enumerate(images):
        with Image.open(path) as im:
            rgba=im.convert('RGBA')
            if not native_size:rgba.thumbnail(cell_size,Image.Resampling.NEAREST)
            x=index%columns*cell_w+8;y=index//columns*cell_h+8
            canvas.paste(rgba,(x,y),rgba)
            draw.text((x,y+image_h+4),row['id'],font=face,fill=(0,0,0))
            records.append(dict(id=row['id'],source=info,display_size=list(rgba.size),scale=rgba.width/info['width']))
        if hash_file(path)['sha256']!=info['sha256']:raise ValueError('Source changed during rendering')
    if hash_file(manifest)!=initial or font and hash_file(font)!=font_info:raise ValueError('Manifest/font changed during rendering')
    out.mkdir();canvas.save(out/'sheet.png')
    result=dict(schema_version=1,status='static_preview_not_engine_acceptance',mode='native_size' if native_size else 'fit_preserve_aspect',canvas_size=[width,height],font=font_info,images=records,limitations=['Font glyph support and visual content require inspection.','Preview does not prove in-game scale, pivot, animation or input.'])
    write_new(out/'sheet.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True);p.add_argument('--output',required=True);p.add_argument('--font');p.add_argument('--columns',type=int,default=4);p.add_argument('--cell-size',nargs=2,type=int,default=[192,192]);p.add_argument('--native-size',action='store_true');a=p.parse_args()
    try:r=create(a.manifest,a.output,a.font,a.columns,a.cell_size,a.native_size);print(json.dumps(dict(status=r['status'],images=len(r['images']),canvas=r['canvas_size'])));return 0
    except (OSError,ValueError,TypeError,KeyError,ImportError,GreaError) as e:print(str(e));return 2
if __name__=='__main__':raise SystemExit(main())
