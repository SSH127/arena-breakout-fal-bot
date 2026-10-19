from PIL import Image, ImageDraw
import glob
files=sorted(glob.glob('tmp/p_*.png'))
print(len(files))
for s in range(0,len(files),14):
    grp=files[s:s+14]
    w,h=Image.open(grp[0]).size
    sheet=Image.new('RGB',((w+60)*2,h*7))
    d=ImageDraw.Draw(sheet)
    for i,f in enumerate(grp):
        n=int(f[-7:-4]); x=(i//7)*(w+60); y=(i%7)*h
        sheet.paste(Image.open(f),(x,y)); d.text((x+w+2,y+8),f"{(n-1)*4}s",fill='yellow')
    sheet.save(f'tmp/logs_{s//14}.png')
