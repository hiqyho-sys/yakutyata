#!/usr/bin/env python3
"""Keep the static catalogue and its CSP in sync; Python 3.10+, no packages.

Run from any folder:
    python3 tools/catalog.py --check
    python3 tools/catalog.py --build

Edit data/dogs.json and data/story.json, rather than duplicate catalogue HTML.
This command does not upload files or change the stated facts about any dog.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import date, datetime
from html.parser import HTMLParser


class CatalogError(ValueError):
    """A data, generated HTML, or CSP inconsistency."""


CATEGORIES = {
    "adult": "Взрослая собака",
    "puppy": "Щенок",
    "teen": "Подросток",
    "unknown": "Краткая анкета",
}
IMAGE_PATH = re.compile(r"catalog/[A-Za-z0-9_-]+\.webp\Z")
DOG_ID = re.compile(r"dog-[A-Za-z0-9_-]+\Z")
CSP_FIXED = {
    "default-src": ["'none'"],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:"],
    "connect-src": ["'none'"],
    "object-src": ["'none'"],
    "base-uri": ["'none'"],
    "form-action": ["'none'"],
    "frame-src": ["'none'"],
    "worker-src": ["'none'"],
    "font-src": ["'none'"],
    "upgrade-insecure-requests": [],
}
SHA256_SOURCE = re.compile(r"'sha256-[A-Za-z0-9+/]{43}='\Z")
OPTIONAL_TEXT = (
    "sex", "age", "birth_date", "height", "weight", "team_since", "health",
    "features", "info_notice", "edited_at",
)
REQUIRED_TEXT = ("id", "name", "category", "about", "source_text", "date", "photo_type")


def read_json(path: Path):
    def reject_constant(value):
        raise CatalogError(f"{path.name}: nonfinite JSON number {value} is not permitted")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogError(f"{path.name}: repeated JSON field")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot read {path.name}: {exc}") from exc


def image_path(value, root: Path, label: str) -> str:
    if not isinstance(value, str) or not IMAGE_PATH.fullmatch(value):
        raise CatalogError(f"{label}: expected a local catalog/*.webp filename")
    file = root / value
    catalog_root = (root / "catalog").resolve()
    if not file.resolve().is_relative_to(catalog_root) or not file.is_file():
        raise CatalogError(f"{label}: image missing or outside catalog/")
    try:
        with file.open("rb") as handle:
            signature = handle.read(12)
    except OSError as exc:
        raise CatalogError(f"{label}: image cannot be read") from exc
    if len(signature) != 12 or signature[:4] != b"RIFF" or signature[8:] != b"WEBP":
        raise CatalogError(f"{label}: file does not have a WebP signature")
    return value


def validate_data(catalog, story, root: Path):
    if not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise CatalogError("dogs.json: schema_version must be 1")
    if not isinstance(story, dict) or type(story.get("schema_version")) is not int or story["schema_version"] != 1:
        raise CatalogError("story.json: schema_version must be 1")
    try:
        exported_at = date.fromisoformat(catalog["exported_at"])
    except (KeyError, TypeError, ValueError):
        raise CatalogError("dogs.json: exported_at must be an ISO date, YYYY-MM-DD")
    dogs = catalog.get("dogs")
    if not isinstance(dogs, list) or not dogs:
        raise CatalogError("dogs.json: dogs must be a nonempty list")
    ids = set()
    for index, dog in enumerate(dogs):
        label = f"dogs[{index}]"
        if not isinstance(dog, dict):
            raise CatalogError(f"{label}: expected an object")
        for field in REQUIRED_TEXT:
            if not isinstance(dog.get(field), str) or not dog[field].strip():
                raise CatalogError(f"{label}.{field}: expected a nonempty string")
        if not DOG_ID.fullmatch(dog["id"]) or dog["id"] in ids:
            raise CatalogError(f"{label}.id: invalid or duplicate dog ID")
        ids.add(dog["id"])
        if dog["category"] not in CATEGORIES:
            raise CatalogError(f"{label}.category: use adult, puppy, teen, or unknown")
        if dog["photo_type"] not in {"individual", "named_collage", "none"}:
            raise CatalogError(f"{label}.photo_type: invalid photo type")
        for field in OPTIONAL_TEXT:
            if field not in dog or (dog[field] is not None and not isinstance(dog[field], str)):
                raise CatalogError(f"{label}.{field}: expected a string or null")
        for field in ("partial", "special_care"):
            if type(dog.get(field)) is not bool:
                raise CatalogError(f"{label}.{field}: expected true or false")
        aliases = dog.get("aliases")
        if not isinstance(aliases, list) or any(not isinstance(v, str) or not v.strip() for v in aliases):
            raise CatalogError(f"{label}.aliases: expected a list of nonempty strings")
        if not isinstance(dog.get("images"), list):
            raise CatalogError(f"{label}.images: expected a list of local WebP filenames")
        if any(not isinstance(v, str) for v in dog["images"]) or len(set(dog["images"])) != len(dog["images"]):
            raise CatalogError(f"{label}.images: invalid or repeated image paths")
        for photo in dog["images"]:
            image_path(photo, root, label + ".images")
        thumb = dog.get("thumbnail")
        if thumb is not None:
            image_path(thumb, root, label + ".thumbnail")
        if not dog["images"] and (dog["photo_type"] != "none" or thumb is not None):
            raise CatalogError(f"{label}: a profile without photos must use photo_type none and thumbnail null")
        if dog["images"] and dog["photo_type"] == "none":
            raise CatalogError(f"{label}: photo_type none contradicts the supplied photos")
        if dog["partial"] and not dog["info_notice"]:
            raise CatalogError(f"{label}: a partial profile must explain what is missing in info_notice")
        try:
            published = date.fromisoformat(dog["date"])
            edited = datetime.fromisoformat(dog["edited_at"]).date() if dog["edited_at"] else published
            if edited < published:
                raise ValueError("edit predates publication")
        except ValueError:
            raise CatalogError(f"{label}: invalid date or edited_at; use ISO dates")
    chapters = story.get("chapters")
    if not isinstance(chapters, list) or len(chapters) != 3:
        raise CatalogError("story.json: exactly three chapter lists are required")
    by_id = {dog["id"]: dog for dog in dogs}
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, list) or not chapter:
            raise CatalogError(f"story.chapters[{index}]: expected a nonempty list")
        seen = set()
        for item in chapter:
            if not isinstance(item, dict) or set(item) != {"dog_id", "photo"}:
                raise CatalogError("story entry: use exactly dog_id and photo")
            identity = item["dog_id"]
            if not isinstance(identity, str) or identity not in by_id or identity in seen:
                raise CatalogError(f"story.chapters[{index}]: unknown or repeated dog_id; select an existing dog deliberately")
            seen.add(identity)
            image_path(item["photo"], root, f"story.chapters[{index}]")
            if item["photo"] not in by_id[identity]["images"]:
                raise CatalogError(f"story.chapters[{index}]: photo must belong to this dog's images")
    return dogs, chapters, exported_at


def script_json(value) -> str:
    # Never allow user text to close its containing <script> element.
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def replace_const(document: str, name: str, value) -> str:
    match = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*", document)
    if not match:
        raise CatalogError(f"index.html: missing const {name}")
    try:
        _, length = json.JSONDecoder().raw_decode(document[match.end():])
    except json.JSONDecodeError as exc:
        raise CatalogError(f"index.html: {name} is not a JSON literal") from exc
    return document[:match.end()] + script_json(value) + document[match.end() + length:]


def replace_once(pattern: str, replacement, document: str, label: str) -> str:
    result, count = re.subn(pattern, replacement, document, flags=re.S)
    if count != 1:
        raise CatalogError(f"index.html: expected one {label}, found {count}; do not change generated markup by hand")
    return result


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def render_card(dog) -> str:
    name = escape(dog["name"])
    photo = dog.get("thumbnail") or (dog["images"][0] if dog["images"] else None)
    if photo:
        media = f'<img src="{escape(photo)}" alt="{name} — фото из каталога команды" loading="lazy" decoding="async" width="480" height="600">'
    else:
        media = f'<div class="catalog-fallback"><strong>{name}</strong><span>Фотография уточняется у команды</span></div>'
    status = "Особая забота" if dog["special_care"] else ("Данные уточняются" if dog["partial"] else "Анкета команды")
    meta = " · ".join(v for v in (dog["sex"], dog["age"]) if v) or "Возраст уточняется"
    # Keep the trailing space of the original searchable aliases payload.
    search = dog["name"].lower() + " " + " ".join(dog["aliases"]).lower()
    return (
        f'<article class="dog-card" data-category="{dog["category"]}" data-special="{str(dog["special_care"]).lower()}" data-name="{escape(search)}" data-tilt>'
        f'<div class="dog-media">{media}<span class="dog-status">{status}</span></div>'
        f'<div class="dog-info"><span class="dog-category">{CATEGORIES[dog["category"]]}</span><h3>{name}</h3>'
        f'<p class="dog-meta">{escape(meta)}</p><button class="btn ghost" data-dog="{dog["id"]}" aria-label="Открыть карточку: {name}">Познакомиться</button></div></article>'
    )


def set_attribute(tag: str, name: str, value: str) -> str:
    attribute = name + '="' + escape(value) + '"'
    pattern = r"(?<!\S)" + re.escape(name) + r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?(?=\s|/?>)"
    if re.search(pattern, tag):
        return re.sub(pattern, lambda _: attribute, tag, count=1)
    return tag[:-1] + " " + attribute + ">"


def sync_story_html(document: str, chapters, by_id) -> str:
    frame_pattern = r'<div\b(?=[^>]*\bdata-scene-photo(?:\s|>))[^>]*>.*?</div>'
    frames = list(re.finditer(frame_pattern, document, re.S))
    if len(frames) != 3:
        raise CatalogError("index.html: expected three story photo frames")
    for index, match in reversed(list(enumerate(frames))):
        chapter = chapters[index]
        first, second = chapter[0], chapter[1] if len(chapter) > 1 else chapter[0]
        frame = match.group()
        frame = re.sub(r"^<div[^>]*>", lambda m: set_attribute(m.group(), "data-dog-name", by_id[first["dog_id"]]["name"]), frame, count=1)
        images = list(re.finditer(r"<img\b[^>]*>", frame))
        if len(images) != 2:
            raise CatalogError("index.html: story frames need exactly two image buffers")
        for image_index, image in reversed(list(enumerate(images))):
            item = first if image_index == 0 else second
            name = by_id[item["dog_id"]]["name"]
            tag = set_attribute(image.group(), "src", item["photo"])
            tag = set_attribute(tag, "alt", name + " — собака из каталога команды «Якутята»")
            tag = set_attribute(tag, "aria-hidden", "false" if image_index == 0 else "true")
            frame = frame[:image.start()] + tag + frame[image.end():]
        frame = replace_once(r"(<strong\b[^>]*\bdata-story-name[^>]*>).*?(</strong>)", lambda m: m[1] + escape(by_id[first["dog_id"]]["name"]) + m[2], frame, "story name")
        document = document[:match.start()] + frame + document[match.end():]
    figures = list(re.finditer(r'<figure class="mobile-scene">.*?</figure>', document, re.S))
    if len(figures) != 3:
        raise CatalogError("index.html: expected three static/mobile story photos")
    for index, match in reversed(list(enumerate(figures))):
        first = chapters[index][0]
        name = by_id[first["dog_id"]]["name"]
        figure = match.group()
        def update_image(image):
            return set_attribute(set_attribute(image.group(), "src", first["photo"]), "alt", name + " — собака из каталога команды «Якутята»")
        figure, count = re.subn(r"<img\b[^>]*>", update_image, figure)
        if count != 1:
            raise CatalogError("index.html: a static/mobile story figure needs one image")
        figure = replace_once(r"(<figcaption>).*?(</figcaption>)", lambda m: m[1] + escape(name) + m[2], figure, "static/mobile story name")
        document = document[:match.start()] + figure + document[match.end():]
    return document


def plural(number: int, forms) -> str:
    if 11 <= number % 100 <= 14:
        return forms[2]
    return forms[0] if number % 10 == 1 else (forms[1] if 2 <= number % 10 <= 4 else forms[2])


def render_catalog(document: str, dogs, chapters, exported_at: date) -> str:
    view = copy.deepcopy(dogs)
    for dog in view:
        dog.pop("thumbnail", None)
        dog["category_label"] = CATEGORIES[dog["category"]]
    document = replace_const(document, "DOGS", view)
    by_id = {dog["id"]: dog for dog in dogs}
    albums = [[{"name": by_id[item["dog_id"]]["name"], "src": item["photo"]} for item in chapter] for chapter in chapters]
    document = replace_const(document, "storyAlbums", albums)
    document = sync_story_html(document, chapters, by_id)
    cards = "".join(render_card(dog) for dog in dogs)
    document = replace_once(r'(<div class="dog-list">).*?(</div>\s*<p class="empty"(?=\s|>))', lambda m: m[1] + cards + m[2], document, "dog-list")
    total, partial = len(dogs), sum(dog["partial"] for dog in dogs)
    full = total - partial
    description = f'{full} {plural(full, ("подробная анкета", "подробные анкеты", "подробных анкет"))}'
    if partial:
        description += f' + {partial} {plural(partial, ("краткое упоминание", "кратких упоминания", "кратких упоминаний"))}'
    summary = f'<div class="catalog-summary"><strong>{total}</strong><span>{plural(total, ("подопечный", "подопечных", "подопечных"))} в каталоге команды<br>{description}</span></div>'
    document = replace_once(r'<div class="catalog-summary">.*?</div>', lambda _: summary, document, "catalog summary")
    document = replace_once(r'(<button\b[^>]*\bdata-filter="all"[^>]*>).*?(</button>)', lambda m: m[1] + f"Все · {total}" + m[2], document, "all dogs filter count")
    # Also maintain count spans if the design later includes them in other filters.
    for category in ("puppy", "teen", "adult", "special"):
        count = sum(dog["special_care"] if category == "special" else dog["category"] == category for dog in dogs)
        pattern = r'(<button\b[^>]*\bdata-filter="' + category + r'"[^>]*>.*?<span class="filter-count">).*?(</span>.*?</button>)'
        document = re.sub(pattern, lambda m: m[1] + str(count) + m[2], document, flags=re.S)
    document = replace_once(r"Выгрузка каталога — \d{2}\.\d{2}\.\d{4}\.", lambda _: "Выгрузка каталога — " + exported_at.strftime("%d.%m.%Y") + ".", document, "catalogue export date")
    return document


class CSPParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.policies = []
        self.sensitive_tags = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("http-equiv", "").lower() == "content-security-policy":
            self.policies.append((self.get_starttag_text(), attributes.get("content", "")))
        if tag in {"script", "style"} or (tag == "link" and "stylesheet" in attributes.get("rel", "").lower().split()):
            self.sensitive_tags.append(self.get_starttag_text())


def csp_policy(document: str):
    parser = CSPParser()
    parser.feed(document)
    if len(parser.policies) != 1:
        raise CatalogError("index.html: exactly one Content-Security-Policy meta is required")
    raw, policy = parser.policies[0]
    position = document.find(raw)
    if any(document.find(tag) < position for tag in parser.sensitive_tags):
        raise CatalogError("CSP meta must precede scripts and style content")
    directives = []
    names = set()
    for item in policy.split(";"):
        tokens = item.split()
        if not tokens:
            continue
        if tokens[0] in names:
            raise CatalogError("CSP: duplicate directive")
        names.add(tokens[0])
        directives.append(tokens)
    if "script-src" not in names:
        raise CatalogError("CSP: script-src is required")
    return raw, directives


def validate_policy(directives):
    policies = {tokens[0]: tokens[1:] for tokens in directives}
    if set(policies) != set(CSP_FIXED) | {"script-src"}:
        raise CatalogError("CSP: required directives are missing or an unreviewed directive was added")
    for name, tokens in CSP_FIXED.items():
        if policies[name] != tokens:
            raise CatalogError(f"CSP: {name} changed from the reviewed policy; restore it before building")
    sources = policies["script-src"]
    if not sources or any(not SHA256_SOURCE.fullmatch(token) for token in sources):
        raise CatalogError("CSP: script-src must contain only SHA-256 hashes; external, inline and eval allowances are forbidden")
    if len(sources) != len(set(sources)):
        raise CatalogError("CSP: repeated script hashes")


def script_hashes(document: str) -> set[str]:
    hashes = set()
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script\s*>", document, re.S | re.I):
        attrs, source = match.groups()
        # Parse attributes, rather than matching the words 'src' inside a value.
        class Attributes(HTMLParser):
            def handle_starttag(self, tag, pairs):
                self.values = dict(pairs)
        parsed = Attributes()
        parsed.feed("<script" + attrs + "></script>")
        values = parsed.values
        if "src" in values:
            raise CatalogError("External scripts are forbidden, including same-origin script src")
        if values.get("type", "").lower() in {"application/ld+json", "application/json"}:
            continue
        digest = base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii")
        hashes.add("'sha256-" + digest + "'")
    if not hashes:
        raise CatalogError("index.html: expected at least one inline script")
    return hashes


def seal_csp(document: str) -> str:
    raw, directives = csp_policy(document)
    validate_policy(directives)
    hashes = sorted(script_hashes(document))
    for tokens in directives:
        if tokens[0] == "script-src":
            tokens[:] = ["script-src"] + hashes
    policy = "; ".join(" ".join(tokens) for tokens in directives)
    replacement = set_attribute(raw, "content", policy)
    return document.replace(raw, replacement, 1)


def check_csp(document: str):
    _, directives = csp_policy(document)
    validate_policy(directives)
    expected = script_hashes(document)
    for tokens in directives:
        if tokens[0] == "script-src":
            actual = set(tokens[1:])
            if actual != expected:
                raise CatalogError("CSP: inline script hashes are out of date or the script policy is unsafe; run --build")


def atomic_write(path: Path, content: str):
    mode = path.stat().st_mode & 0o777
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=".catalog-", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read-only: validate data, generated HTML, and CSP SHA-256")
    mode.add_argument("--build", action="store_true", help="regenerate catalogue fragments and update CSP SHA-256 in index.html")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        catalog = read_json(root / "data/dogs.json")
        story = read_json(root / "data/story.json")
        dogs, chapters, exported_at = validate_data(catalog, story, root)
        index = root / "index.html"
        original = index.read_text(encoding="utf-8")
        rendered = render_catalog(original, dogs, chapters, exported_at)
        if args.check:
            check_csp(original)
            if rendered != original:
                raise CatalogError("index.html is out of sync with data/*.json; run --build, then --check")
        else:
            rendered = seal_csp(rendered)
            check_csp(rendered)
            atomic_write(index, rendered)
        images = {photo for dog in dogs for photo in dog["images"]}
        print(f"OK: {len(dogs)} profiles, {len(images)} album photos, {sum(len(c) for c in chapters)} story selections; HTML and CSP synchronized.")
        return 0
    except (CatalogError, OSError, UnicodeError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
