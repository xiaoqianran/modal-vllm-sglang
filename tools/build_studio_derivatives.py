"""Build bounded WebP gallery thumbnails and video posters from verified sources.

Default is local cache only. --publish publishes derivatives, never source media.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote
from urllib.request import Request, urlopen
from hashlib import sha256
import subprocess
from PIL import Image, ImageOps
from orchestration import studio
from orchestration.gallery_derivatives import INDEX, POLICY, derivative_url
from tools.studio_manifest import receipt_rows, receipt_is_valid
from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.archive.huggingface import HuggingFaceBucket, public_url
from runtime.archive.preview import verified_preview
from runtime.utils.files import atomic_json, atomic_write, file_lock, sha256_file

MAX_SOURCE=128*1024*1024


def download(row, path):
    size=row['stored_bytes']; expected=row['stored_sha256']; h=sha256(); n=0
    if not 0<size<=MAX_SOURCE: raise ValueError('Source exceeds derivative limit')
    with urlopen(Request(row['url'],headers={'Accept-Encoding':'identity'}),timeout=45) as response, path.open('wb') as out:
        if response.status!=200: raise ValueError('Expected full source')
        for block in iter(lambda:response.read(1024*1024),b''):
            n+=len(block)
            if n>size: raise ValueError('Source size mismatch')
            h.update(block);out.write(block)
    if n!=size or h.hexdigest()!=expected: raise ValueError('Source integrity mismatch')


def local_source(root, asset, row):
    for name in (asset.get('local_url'),asset.get('id')):
        if not isinstance(name,str): continue
        p=(root/unquote(name)).resolve()
        if not p.is_relative_to(root) or not p.is_file(): continue
        size=p.stat().st_size
        expected={digest for digest,n in [(row.get('original_sha256'),row.get('original_bytes')),(row['stored_sha256'],row['stored_bytes'])] if size==n}
        if expected and sha256_file(p) in expected: return p
    return None


def encode(source, kind, work):
    if kind=='video':
        frame=work/'frame.png'
        cmd=['ffmpeg','-nostdin','-v','error','-y','-ss','0.5','-i',str(source),'-frames:v','1','-vf','scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2',str(frame)]
        subprocess.run(cmd,check=True,capture_output=True,timeout=60)
        if not frame.is_file():
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-frames:v','1','-vf','scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2',str(frame)],check=True,capture_output=True,timeout=60)
        source=frame
    from io import BytesIO
    with Image.open(source) as opened:
        if opened.width*opened.height>48_000_000: raise ValueError('Image dimensions exceed limit')
        image=ImageOps.exif_transpose(opened)
        image.thumbnail((640,640),Image.Resampling.LANCZOS)
        image=image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
        buf=BytesIO();image.save(buf,'WEBP',quality=82,method=4)
        payload=buf.getvalue();dims=list(image.size)
    if not 0<len(payload)<=512*1024: raise ValueError('Derivative size exceeds limit')
    with Image.open(BytesIO(payload)) as decoded:
        decoded.load()
        if decoded.size!=tuple(dims): raise ValueError('Derivative decode mismatch')
    return payload,dims


def generate(root, asset, receipt, allow_remote, preview_index):
    with TemporaryDirectory(prefix='studio-derivative-') as tmp:
        work=Path(tmp);src=local_source(root,asset,receipt);origin='local'
        if src is None:
            if not allow_remote: return None,'missing-local'
            # A verified full-resolution preview is sufficient for a 640px thumbnail.
            # Never use that preview as the archival source or the playback URL.
            candidate=receipt
            if asset['kind']=='image':
                preview_url=verified_preview(receipt,preview_index)
                candidates=[receipt.get('preview'),preview_index.get(receipt['url'],{}).get('preview')]
                for preview in candidates:
                    if preview_url and isinstance(preview,dict) and preview.get('url')==preview_url:
                        if type(preview.get('bytes')) is int and preview['bytes']>0:
                            candidate={'url':preview_url,'stored_bytes':preview['bytes'],'stored_sha256':preview['sha256'],'path':preview['path']}
                            break
            src=work/('source'+Path(candidate['path']).suffix);download(candidate,src);origin='remote-preview' if candidate is not receipt else 'remote-original'
        payload,dims=encode(src,asset['kind'],work);digest=sha256(payload).hexdigest()
        target=root/'.runtime/gallery-thumbnails'/(digest+'.webp');atomic_write(target,payload)
        return {'source_sha256':receipt['stored_sha256'],'source_bytes':receipt['stored_bytes'],'kind':receipt['kind'],'policy':POLICY,'verification':'sha256','sha256':digest,'bytes':len(payload),'path':f'thumbnails/sha256/{digest[:2]}/{digest}.webp','dimensions':dims},origin


def build(root, *, publish=False, allow_remote=False, limit=None):
    root=Path(root).resolve();output=root/'outputs';path=output/INDEX
    preview_index=studio.read_json(output/'.runtime/preview-index.json',{}) or {}
    assets=studio.collect(output,thumbs=False)['assets'];receipts={r['url']:r for r in receipt_rows(root) if receipt_is_valid(r,DEFAULT_ASSET_BUCKET)}
    with file_lock(path.parent,filename='.gallery-derivatives.lock',timeout=0):
        index=studio.read_json(path,{}) or {};counts=Counter();todo=[];seen=set()
        for a in sorted(assets,key=lambda a:a['kind']!='video'):
            url=a['url'];r=receipts.get(url)
            if url in seen: continue
            seen.add(url)
            if not r or r['stored_bytes']!=a['bytes'] or r['kind']!=a['kind']: counts['no-receipt']+=1;continue
            cached=derivative_url(a,index,output)
            if cached and (not publish or cached.startswith('https://')):counts['cached']+=1;continue
            todo.append((a,r,cached))
        if limit is not None:todo=todo[:limit]
        provider=HuggingFaceBucket(DEFAULT_ASSET_BUCKET) if publish else None
        def work(item):
            a,r,cached=item
            try:
                if cached:
                    row=index[a['url']];p=output/cached
                    if p.is_file() and sha256_file(p)==row['sha256']:return a['url'],row,'cached-local'
                row,status=generate(output,a,r,allow_remote,preview_index);return a['url'],row,status
            except Exception as exc:return a['url'],None,'error:'+type(exc).__name__
        for offset in range(0,len(todo),8):
            with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(work,todo[offset:offset+8]))
            batch={url:row for url,row,status in results if row}
            for url,row,status in results:counts[status]+=1
            index.update(batch);atomic_json(path,index)
            if provider and batch:
                entries={r['path']:(output/'.runtime/gallery-thumbnails'/(r['sha256']+'.webp'),r['sha256']) for r in batch.values()}
                try: outcomes=provider.publish_many(entries)
                except Exception as exc: outcomes={};counts['publish-batch-error']+=1
                for url,row in batch.items():
                    result=outcomes.get(row['path'])
                    if isinstance(result,tuple) and result[1]=='sha256' and result[0]==public_url(provider.bucket,row['path']):row['url']=result[0];counts['published']+=1
                    else:counts['publish-error']+=1
                atomic_json(path,index)
            print('PROGRESS',offset+len(results),'/',len(todo),dict(counts),flush=True)
        print('FINISHED',dict(counts),'index',len(index),flush=True)
        return counts


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path.cwd());p.add_argument('--remote',action='store_true',help='Read verified HF source if no local file');p.add_argument('--publish',action='store_true');p.add_argument('--limit',type=int);a=p.parse_args();build(a.root,publish=a.publish,allow_remote=a.remote,limit=a.limit)

if __name__=='__main__':main()
