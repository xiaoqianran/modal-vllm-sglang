"""Offline quality-first video benchmark. Never publishes or replaces originals."""
import argparse
import json
import re
import subprocess
from pathlib import Path
from time import perf_counter
from runtime.utils.files import atomic_json, sha256_file


def ff(*args):
    p=subprocess.run([str(a) for a in args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,check=True)
    return p.stdout,p.stderr


def probe(path):
    raw=json.loads(ff('ffprobe','-v','error','-show_streams','-show_format','-of','json',path)[0]);v=next(s for s in raw['streams'] if s['codec_type']=='video');return {'width':v['width'],'height':v['height'],'fps':v['avg_frame_rate'],'frames':v.get('nb_frames'),'duration':float(raw['format']['duration']),'audio':sum(s['codec_type']=='audio' for s in raw['streams']),'codec':v['codec_name']}


def quality(candidate,source,info):
    # Compare at original dimensions; upscaling low-res candidates exposes detail loss.
    filters=f"[0:v]scale={info['width']}:{info['height']}:flags=bicubic,setpts=PTS-STARTPTS,split=3[a][b][c];[1:v]setpts=PTS-STARTPTS,split=3[d][e][f];[a][d]libvmaf=n_threads=2[v];[b][e]ssim[s];[c][f]psnr[p]"
    _,log=ff('ffmpeg','-nostdin','-hide_banner','-i',candidate,'-i',source,'-filter_complex_threads','2','-filter_complex',filters,'-map','[v]','-map','[s]','-map','[p]','-an','-f','null','-')
    return {'vmaf':float(re.findall(r'VMAF score:\s*([\d.]+)',log)[-1]),'ssim':float(re.findall(r'All:([\d.]+)',log)[-1]),'psnr':float(re.findall(r'average:([\d.]+|inf)',log)[-1])}


def trial(source,folder):
    info=probe(source); digest=sha256_file(source); rows=[]
    for label,crf,scale in [('360p-crf26',26,True),('native-crf18',18,False),('native-crf21',21,False)]:
        target=folder/(label+'.mp4');cmd=['ffmpeg','-nostdin','-v','error','-y','-i',source,'-map','0:v:0','-map','0:a?']
        if scale:cmd+=['-vf','scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2']
        cmd+=['-c:v','libx264','-threads','2','-preset','medium','-crf',str(crf),'-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',target]
        t=perf_counter();ff(*cmd);seconds=perf_counter()-t;out=probe(target);q=quality(target,source,info);ratio=target.stat().st_size/source.stat().st_size
        eligible=(not scale and out['width']==info['width'] and out['height']==info['height'] and out['fps']==info['fps'] and out['frames']==info['frames'] and out['audio']==info['audio'] and abs(out['duration']-info['duration'])<=.05 and q['vmaf']>=97 and q['ssim']>=.98 and ratio<=.8)
        rows.append({'variant':label,'bytes':target.stat().st_size,'ratio':round(ratio,4),'seconds':round(seconds,3),**q,'eligible_experimental':eligible})
    assert sha256_file(source)==digest
    return {'file':source.name,'sha256':digest,'bytes':source.stat().st_size,'source':info,'variants':rows,'default':'original'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('videos',type=Path,nargs='+');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);rows=[]
    for i,source in enumerate(a.videos):
        folder=a.output/f'{i:02}';folder.mkdir();row=trial(source.resolve(),folder.resolve());rows.append(row);print(json.dumps(row),flush=True);atomic_json(a.output/'report.json',rows)

if __name__=='__main__':main()
