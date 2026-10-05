"""Regression checks for real catalogue data and malicious update payloads.

    python3 -m unittest discover -s tools -p 'test_*.py'

No test edits the real website or accesses the network.
"""

import copy
import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

import catalog


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.data = catalog.read_json(cls.root / "data/dogs.json")
        cls.story = catalog.read_json(cls.root / "data/story.json")
        cls.document = (cls.root / "index.html").read_text(encoding="utf-8")

    def validated(self, data=None, story=None):
        return catalog.validate_data(data or self.data, story or self.story, self.root)

    def rendered(self, data=None, story=None):
        dogs, chapters, stamp = self.validated(data, story)
        return catalog.render_catalog(self.document, dogs, chapters, stamp)

    @staticmethod
    def literal(document, name):
        offset = re.search(r"\bconst\s+" + name + r"\s*=\s*", document).end()
        return json.JSONDecoder().raw_decode(document[offset:])[0]

    def test_supplied_catalogue_and_story_are_valid(self):
        dogs, chapters, stamp = self.validated()
        self.assertGreater(len(dogs), 0)
        self.assertEqual(len(dogs), len({dog["id"] for dog in dogs}))
        rendered = self.rendered()
        self.assertEqual(len(dogs), len(self.literal(rendered, "DOGS")))
        self.assertEqual(sum(len(chapter) for chapter in chapters), sum(len(chapter) for chapter in self.literal(rendered, "storyAlbums")))
        self.assertEqual(self.data["exported_at"], stamp.isoformat())

    def test_empty_partial_profiles_do_not_borrow_photos(self):
        dog = copy.deepcopy(self.data["dogs"][0])
        dog.update(partial=True, info_notice="Fixture: details missing", images=[], thumbnail=None, photo_type="none", age=None, sex=None, special_care=False, category="unknown")
        card = catalog.render_card(dog)
        self.assertIn("catalog-fallback", card)
        self.assertNotIn("<img", card)
        self.assertIn("Возраст уточняется", card)

    def test_xss_payload_is_text_in_script_attributes_and_captions(self):
        data = copy.deepcopy(self.data)
        payload = '</script><script>alert(1)</script><img src=x onerror="alert(2)">'
        data["dogs"][0]["name"] = payload
        data["dogs"][0]["aliases"] = [payload]
        data["dogs"][0]["source_text"] = payload
        rendered = self.rendered(data)
        self.assertEqual(1, len(re.findall(r"<script\b", rendered)))
        self.assertIn("&lt;/script&gt;", rendered)
        self.assertIn("\\u003c/script>", rendered)
        self.assertEqual(payload, self.literal(rendered, "DOGS")[0]["source_text"])
        self.assertNotIn('<img src=x onerror=', rendered)
        sealed = catalog.seal_csp(rendered)
        catalog.check_csp(sealed)

    def test_images_reject_remote_urls_script_schemes_and_path_escape(self):
        payloads = ["https://example.com/dog.webp", "javascript:alert(1)", "catalog/../index.html", "catalog/a.webp?x=1", 'catalog/a" onerror="alert(1).webp', "catalog/missing.webp"]
        for payload in payloads:
            with self.subTest(payload=payload):
                data = copy.deepcopy(self.data)
                data["dogs"][0]["images"] = [payload]
                with self.assertRaises(catalog.CatalogError):
                    self.validated(data)

    def test_duplicate_ids_bad_types_and_categories_fail_before_render(self):
        duplicate = copy.deepcopy(self.data)
        duplicate["dogs"].append(copy.deepcopy(duplicate["dogs"][0]))
        with self.assertRaises(catalog.CatalogError):
            self.validated(duplicate)
        for field, value in [("category", "adlut"), ("special_care", "false"), ("aliases", "Бим"), ("images", [{}]), ("date", "not-a-date")]:
            with self.subTest(field=field):
                data = copy.deepcopy(self.data)
                data["dogs"][0][field] = value
                with self.assertRaises(catalog.CatalogError):
                    self.validated(data)

    def test_story_dog_removal_requires_an_explicit_replacement(self):
        data = copy.deepcopy(self.data)
        identity = self.story["chapters"][0][0]["dog_id"]
        data["dogs"] = [dog for dog in data["dogs"] if dog["id"] != identity]
        with self.assertRaises(catalog.CatalogError):
            self.validated(data)

    def test_story_rejects_other_dogs_photos(self):
        story = copy.deepcopy(self.story)
        dog = next(d for d in self.data["dogs"] if d["id"] == story["chapters"][0][0]["dog_id"])
        other = next((photo for d in self.data["dogs"] for photo in d["images"] if photo not in dog["images"]), None)
        if other is None:
            self.skipTest("Current catalogue contains no photo outside the first story dog's album")
        story["chapters"][0][0]["photo"] = other
        with self.assertRaises(catalog.CatalogError):
            self.validated(story=story)

    def test_reordering_story_updates_both_buffers_and_mobile_figure(self):
        story = copy.deepcopy(self.story)
        story["chapters"][0].reverse()
        rendered = self.rendered(story=story)
        first = story["chapters"][0][0]
        dog = next(d for d in self.data["dogs"] if d["id"] == first["dog_id"])
        self.assertEqual(dog["name"], self.literal(rendered, "storyAlbums")[0][0]["name"])
        frame = re.search(r'<div\b(?=[^>]*\bdata-scene-photo(?:\s|>))[^>]*>.*?</div>', rendered, re.S).group()
        figure = re.search(r'<figure class="mobile-scene">.*?</figure>', rendered, re.S).group()
        self.assertIn('data-dog-name="' + dog["name"] + '"', frame)
        self.assertIn('src="' + first["photo"] + '"', frame)
        self.assertIn('src="' + first["photo"] + '"', figure)
        self.assertIn('<figcaption>' + dog["name"] + '</figcaption>', figure)
        self.assertIn('aria-hidden="true"', frame)

    def test_removing_a_nonstory_dog_updates_all_counts_and_cards(self):
        data = copy.deepcopy(self.data)
        story_ids = {item["dog_id"] for chapter in self.story["chapters"] for item in chapter}
        identity = next((dog["id"] for dog in data["dogs"] if dog["id"] not in story_ids), None)
        if identity is None:
            self.skipTest("Every current profile is referenced by the story")
        data["dogs"] = [dog for dog in data["dogs"] if dog["id"] != identity]
        total = len(data["dogs"])
        partial = sum(dog["partial"] for dog in data["dogs"])
        rendered = self.rendered(data)
        self.assertEqual(total, len(self.literal(rendered, "DOGS")))
        self.assertEqual(total, rendered.count('<article class="dog-card"'))
        self.assertNotIn('data-dog="' + identity + '"', rendered)
        self.assertIn(f'<strong>{total}</strong><span>', rendered)
        self.assertIn(f'Все · {total}', rendered)
        summary = re.search(r'<div class="catalog-summary">.*?</div>', rendered, re.S).group()
        self.assertIn(f'<br>{total - partial} ', summary)
        if partial:
            self.assertIn(f' + {partial} ', summary)
        else:
            self.assertNotIn(' + ', summary)

    def test_summary_plural_forms(self):
        forms = ("подробная анкета", "подробные анкеты", "подробных анкет")
        for number, expected in [(1, forms[0]), (2, forms[1]), (5, forms[2]), (11, forms[2]), (21, forms[0]), (22, forms[1]), (111, forms[2])]:
            self.assertEqual(expected, catalog.plural(number, forms))

    def test_csp_hash_changes_with_script_and_keeps_other_restrictions(self):
        sealed = catalog.seal_csp(self.rendered())
        catalog.check_csp(sealed)
        changed = sealed.replace("const DOGS=", "/* test change */const DOGS=", 1)
        with self.assertRaises(catalog.CatalogError):
            catalog.check_csp(changed)
        repaired = catalog.seal_csp(changed)
        catalog.check_csp(repaired)
        self.assertNotEqual(catalog.script_hashes(sealed), catalog.script_hashes(repaired))
        self.assertEqual(catalog.csp_policy(sealed)[1][-1], catalog.csp_policy(repaired)[1][-1])
        self.assertIn(["object-src", "'none'"], catalog.csp_policy(repaired)[1])
        self.assertEqual(repaired, catalog.seal_csp(repaired))

    def test_nonfinite_numbers_and_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "dogs.json"
            for payload in ['{"dogs": NaN}', '{"dogs": [], "dogs": []}']:
                file.write_text(payload, encoding="utf-8")
                with self.assertRaises(catalog.CatalogError):
                    catalog.read_json(file)

    def test_external_scripts_are_rejected_instead_of_whitelisted(self):
        sealed = catalog.seal_csp(self.rendered())
        for path in ["https://example.com/a.js", "/a.js"]:
            changed = sealed.replace("</body>", f'<script src="{path}"></script></body>')
            with self.assertRaises(catalog.CatalogError):
                catalog.check_csp(changed)
            with self.assertRaises(catalog.CatalogError):
                catalog.seal_csp(changed)

    def test_csp_cannot_be_broadened_or_moved_after_content(self):
        sealed = catalog.seal_csp(self.rendered())
        raw, _ = catalog.csp_policy(sealed)
        late = sealed.replace(raw, "", 1).replace("</head>", raw + "</head>", 1)
        with self.assertRaises(catalog.CatalogError):
            catalog.seal_csp(late)
        for before, after in [("connect-src &#x27;none&#x27;", "connect-src &#x27;self&#x27;"), ("script-src ", "script-src &#x27;self&#x27; ")]:
            changed = sealed.replace(before, after, 1)
            self.assertNotEqual(sealed, changed)
            with self.assertRaises(catalog.CatalogError):
                catalog.check_csp(changed)
            with self.assertRaises(catalog.CatalogError):
                catalog.seal_csp(changed)

    def test_cli_build_is_repeatable_and_check_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(self.root / "catalog", root / "catalog")
            shutil.copytree(self.root / "data", root / "data")
            index = root / "index.html"
            index.write_text(self.document, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, catalog.main(["--root", str(root), "--build"]))
                before = index.read_bytes()
                timestamp = index.stat().st_mtime_ns
                self.assertEqual(0, catalog.main(["--root", str(root), "--check"]))
                self.assertEqual(timestamp, index.stat().st_mtime_ns)
                self.assertEqual(0, catalog.main(["--root", str(root), "--build"]))
            self.assertEqual(before, index.read_bytes())


if __name__ == "__main__":
    unittest.main()
