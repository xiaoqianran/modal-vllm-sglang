"""Local CPU media derivative benchmark; never modifies or uploads originals."""
import argparse
import json
from pathlib import Path
import subprocess
from time import perf_counter
from PIL import Image, ImageOps, ImageChops, ImageStat, features
from math import log10
from runtime.utils.files import atomic_json, sha256_file


def run(args):
    result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,check=True)
    return result.stdout


def image_trial(source, destination):
    with Image.open(source) as opened:
        image=ImageOps.exif_transpose(opened).convert('RGB')
        image.thumbnail((640,640),Image.Resampling.LANCZOS)
    variants=[]
    for fmt,quality,options in [('WEBP',80,{'method':4}),('AVIF',70,{'speed':6})]:
        target=destination.with_suffix('.'+fmt.lower())
        t=perf_counter()
        try:
            image.save(target,format=fmt,quality=quality,**options)
            elapsed=perf_counter()-t
            with Image.open(target) as decoded:
                mse=sum(v*v for v in ImageStat.Stat(ImageChops.difference(image,decoded.convert('RGB'))).rms)/3
            variants.append({'format':fmt,'quality':quality,'bytes':target.stat().st_size,'seconds':round(elapsed,3),'psnr':round(10*log10(65025/mse),2) if mse else 99})
        except (OSError,ValueError,KeyError) as exc:
            variants.append({'format':fmt,'error':type(exc).__name__})
    return {'file':source.name,'source_bytes':source.stat().st_size,'dimensions':list(image.size),'variants':variants}


def video_trial(source,destination):
    probe=json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(source)]))
    v=next(s for s in probe['streams'] if s['codec_type']=='video')
    duration=float(probe['format']['duration'])
    poster=destination.with_suffix('.webp'); preview=destination.with_suffix('.mp4')
    t=perf_counter()
    run(['ffmpeg','-nostdin','-v','error','-y','-ss',str(min(duration*.2,1.0)),'-i',str(source),'-frames:v','1','-vf','scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2','-c:v','libwebp','-quality','80',str(poster)])
    poster_time=perf_counter()-t
    t=perf_counter()
    run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-map','0:v:0','-map','0:a:0?','-vf','scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2','-c:v','libx264','-preset','fast','-crf','26','-pix_fmt','yuv420p','-c:a','aac','-b:a','64k','-movflags','+faststart',str(preview)])
    preview_time=perf_counter()-t
    pp=json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(preview)]))
    pv=next(s for s in pp['streams'] if s['codec_type']=='video')
    return {'file':source.name,'source_bytes':source.stat().st_size,'source_dimensions':[v['width'],v['height']],'source_codec':v['codec_name'],'duration':duration,'poster_bytes':poster.stat().st_size,'poster_seconds':round(poster_time,3),'preview_bytes':preview.stat().st_size,'preview_seconds':round(preview_time,3),'preview_dimensions':[pv['width'],pv['height']],'preview_duration':float(pp['format']['duration'])}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--images',type=Path,nargs='*',default=[]);p.add_argument('--videos',type=Path,nargs='*',default=[]);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    result={'images':[],'videos':[]}
    for group,paths,fn in [('images',a.images,image_trial),('videos',a.videos,video_trial)]:
        for i,path in enumerate(paths):
            before=sha256_file(path); row=fn(path,a.output/f'{group}-{i:02}'); assert sha256_file(path)==before,'Source changed';result[group].append(row);print(json.dumps(row),flush=True)
    atomic_json(a.output/'report.json',result)

if __name__=='__main__':main()
