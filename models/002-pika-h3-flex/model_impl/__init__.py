"""Pika H3 Flex cache implementation; loaded through model.json, not a global path."""


def download_sources(spec):
    from .overlay import download_sources as build_sources
    return build_sources(spec)


def build_publisher_image(spec, *, modal_version):
    from .materializer_image import build_pika_materializer_image
    return build_pika_materializer_image(spec, modal_version=modal_version)


def publish_cache(spec, manifest, base, root, volume):
    from .overlay import publish_pika_cache
    return publish_pika_cache(spec, manifest, base, root, volume)
