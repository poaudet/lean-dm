"""Shared text and JSON response helpers."""
import json
import os
import pathlib
import re
import threading
import uuid


def atomic_write(path, text, mode=None):
    path = pathlib.Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def append_jsonl(path, items):
    path = pathlib.Path(path)
    data = "".join(
        (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n"
        for item in items
    )
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                data = "\n" + data
    with path.open("a", encoding="utf-8") as stream:
        stream.write(data)


def replace_lines(path, lines):
    atomic_write(path, "".join(line + "\n" for line in lines))


def clean_reply(text):
    """Strip reasoning blocks that some models emit inline."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.split("<think>", 1)[0].strip()


def parse_json_response(raw):
    """Parse the first JSON object or array in a model response."""
    text = clean_reply(raw)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    return None
