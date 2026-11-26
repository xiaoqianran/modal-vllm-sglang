"""Verified, optional Studio gallery thumbnails, separate from archival media."""
from pathlib import Path
from urllib.parse import urlsplit
import re
from runtime.archive.huggingface import public_url

INDEX = Path('.runtime/gallery-derivatives.json')
POLICY = 'webp-640-q82-v1'
HEX = re.compile(r'[0-9a-f]{64}')


def derivative_url(asset, index, root):
    """Read a previously verified receipt; never fetch or encode during polling."""
    try:
        url = asset['url']
        row = index[url]
        source = Path(urlsplit(url).path).stem
        if (not HEX.fullmatch(source) or row['source_sha256'] != source
                or row['source_bytes'] != asset['bytes'] or row['kind'] != asset['kind']
                or row['policy'] != POLICY or row['verification'] != 'sha256'):
            return ''
        digest = row['sha256']
        if not HEX.fullmatch(digest) or type(row['bytes']) is not int or not 0 < row['bytes'] <= 512*1024:
            return ''
        remote = f'thumbnails/sha256/{digest[:2]}/{digest}.webp'
        if row.get('path') != remote:
            return ''
        parts = urlsplit(url).path.split('/')
        bucket = '/'.join(parts[2:4])
        if row.get('url') == public_url(bucket, remote):
            return row['url']
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        pass
    return ''


def apply_derivatives(assets, index, root):
    for asset in assets:
        # Full-resolution verified previews remain the default for images.
        # A smaller derivative is useful only as a video poster.
        if asset.get('kind') != 'video':
            continue
        thumb = derivative_url(asset,index,root)
        if thumb:
            asset['preview'] = ''
            asset['gallery_thumb'] = thumb
            asset['thumb'] = thumb
