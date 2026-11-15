"""Pika duration/target constraints; defaults live in model.json."""
import re


def validate(params):
    target = params["target"]
    if not isinstance(target, dict) or set(target) != {"short_edge", "aspect_ratio", "duration_seconds"}:
        raise ValueError("target requires short_edge, aspect_ratio, duration_seconds")
    if type(target["short_edge"]) is not int or target["short_edge"] < 1:
        raise ValueError("Invalid short_edge")
    if not isinstance(target["aspect_ratio"], str) or not re.fullmatch(r"auto|[1-9][0-9]*:[1-9][0-9]*", target["aspect_ratio"]):
        raise ValueError("Invalid aspect_ratio")
    if type(target["duration_seconds"]) not in (int, float) or target["duration_seconds"] != params["seconds"]:
        raise ValueError("target.duration_seconds must equal seconds")
