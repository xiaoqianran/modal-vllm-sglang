"""用无 GPU 的只读容器验证跨项目共享的 Modal Volume V2 模型文件。"""
from __future__ import annotations

import argparse
import json
from pathlib import PurePosixPath


def validate_relative_path(value: str) -> str:
    """拒绝路径穿越、绝对路径和非规范相对路径。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Model path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
        raise ValueError("Model path must be a canonical relative path")
    return value


def validate_sub_path(value: str) -> str:
    """Require a canonical, non-root Volume subtree."""
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value.startswith("/")
            or value != path.as_posix() or value == "/"
            or ".." in path.parts or "\\" in value):
        raise ValueError("sub_path must be a canonical absolute subtree")
    return value


def probe(volume_name: str, relative_path: str, *, sub_path: str = "/inference") -> dict:
    """只读挂载模型卷，确认精确文件在另一个 Modal App 内可见。"""
    import modal

    relative_path = validate_relative_path(relative_path)
    sub_path = validate_sub_path(sub_path)
    volume = modal.Volume.from_name(volume_name, version=2)
    app = modal.App("modal-shared-model-read-probe")

    @app.function(
        image=modal.Image.debian_slim(python_version="3.12"),
        cpu=1,
        memory=512,
        timeout=120,
        volumes={"/data": volume.with_mount_options(read_only=True, sub_path=sub_path)},
        serialized=True,
    )
    def inspect(path: str) -> dict:
        import hashlib
        from pathlib import Path, PurePosixPath

        root = Path("/data").resolve()
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid model path")
        target = root.joinpath(*relative.parts)
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not target.is_file():
            raise ValueError("Model file escapes the shared Volume")
        size = resolved.stat().st_size
        if not size:
            raise RuntimeError("Model file is empty")
        digest = None
        if size <= 16 * 1024 * 1024:
            h = hashlib.sha256()
            with resolved.open("rb") as stream:
                for part in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(part)
            digest = h.hexdigest()
        return {"relative_path": path, "bytes": size, "sha256": digest,
                "is_symlink": target.is_symlink(), "mount_read_only": True}

    with app.run():
        result = inspect.remote(relative_path)
    return {"volume_name": volume_name, "sub_path": sub_path, **result}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", required=True, help="已验证的 Modal Volume V2 名称")
    parser.add_argument("--path", required=True, help="所选子目录内的相对文件路径")
    parser.add_argument("--sub-path", default="/inference", help="Volume 子目录，默认 /inference，可设 /comfyui")
    args = parser.parse_args(argv)
    print(json.dumps(probe(args.volume, args.path, sub_path=args.sub_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
