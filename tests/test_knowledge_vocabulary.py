"""知识词表与题头 schema 的结构校验。

词表是数据文件，不是代码；但它是知识包校验的输入，改坏了要在 CI 里立刻发现。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "store" / "spec"

ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
TAG_ID = re.compile(r"^[a-z0-9][a-z0-9]*(?:-[a-z0-9]+)*$")


class VocabularyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vocab = json.loads((SPEC / "knowledge-vocabulary.json").read_text(encoding="utf-8"))
        cls.note_schema = json.loads((SPEC / "knowledge-note.schema.json").read_text(encoding="utf-8"))
        cls.tags = json.loads((SPEC / "knowledge-tags.json").read_text(encoding="utf-8"))
        cls.metadata = json.loads((SPEC / "knowledge-metadata.json").read_text(encoding="utf-8"))

    def test_version_and_status(self) -> None:
        self.assertEqual(self.vocab["version"], "kv/1")
        self.assertIn(self.vocab["status"], {"draft", "active"})

    def test_categories_and_domains_are_unique_and_well_formed(self) -> None:
        categories = self.vocab["categories"]
        self.assertGreaterEqual(len(categories), 16)
        seen_categories: set[str] = set()
        seen_domains: set[str] = set()
        for category in categories:
            self.assertRegex(category["id"], ID)
            self.assertNotIn(category["id"], seen_categories)
            seen_categories.add(category["id"])
            self.assertTrue(category["definition"].strip())
            self.assertGreaterEqual(len(category["domains"]), 3)
            for domain in category["domains"]:
                self.assertRegex(domain["id"], ID)
                self.assertNotIn(domain["id"], seen_domains)
                seen_domains.add(domain["id"])
                self.assertTrue(domain["definition"].strip())

    def test_scopes(self) -> None:
        scopes = {item["id"] for item in self.vocab["scopes"]}
        self.assertEqual(scopes, {"enterprise", "work"})

    def test_tag_vocabulary_shapes(self) -> None:
        facets = self.tags["facets"]
        self.assertEqual(set(facets), {"topic", "entity", "method", "source"})
        self.assertTrue(facets["topic"]["terms"])
        self.assertEqual(facets["method"]["mode"], "closed")
        self.assertEqual(facets["source"]["mode"], "closed")
        for name, facet in facets.items():
            terms = facet.get("terms") or facet.get("core") or []
            ids = [item["id"] for item in terms]
            self.assertEqual(len(ids), len(set(ids)), f"{name} 有重复 id")
            for item in terms:
                self.assertRegex(item["id"], TAG_ID)
                self.assertTrue(item["name"].strip())
                if name == "topic":
                    self.assertTrue(item["definition"].strip())

    def test_topic_terms_point_at_known_categories(self) -> None:
        known = {item["id"] for item in self.vocab["categories"]}
        for term in self.tags["facets"]["topic"]["terms"]:
            self.assertIn(term["category"], known, term["id"])

    def test_metadata_dictionary_covers_the_note_schema(self) -> None:
        note_fields = {item["name"] for item in self.metadata["note"]["fields"]}
        for required in self.note_schema["required"]:
            self.assertIn(required, note_fields)
        pack_fields = {item["name"] for item in self.metadata["pack"]["fields"]}
        for required in (
            "knowledge.entry",
            "knowledge.taxonomy.business_category",
            "knowledge.taxonomy.business_domain",
            "knowledge.taxonomy.knowledge_kind",
            "knowledge.security_level",
            "knowledge.scope",
        ):
            self.assertIn(required, pack_fields)

    def test_protocol_does_not_register_producers(self) -> None:
        # 知识包可以由任意技能、工具或人工生产；协议只约束产物。
        self.assertNotIn("skill_catalog", self.metadata)
        self.assertNotIn("skill", self.metadata)
        names = {item["name"] for item in self.metadata["pack"]["fields"]}
        self.assertIn("knowledge.producer", names)

    def test_workflow_covers_the_production_stages(self) -> None:
        stages = self.metadata["workflow"]["stages"]
        self.assertGreaterEqual(len(stages), 8)
        self.assertEqual(stages[0]["id"], "scope")
        self.assertEqual(stages[-1]["id"], "submit")
        for stage in stages:
            self.assertTrue(stage["checks"], stage["id"])

    def test_kinds_map_to_the_five_note_types(self) -> None:
        kinds = {item["id"]: item for item in self.vocab["kinds"]}
        self.assertEqual(set(kinds), {"reference", "guide", "tutorial", "reflection", "moc"})
        self.assertIn("map_of_content", self.note_schema["properties"]["type"]["enum"])
        for item in kinds.values():
            self.assertIn(item["note_type"], self.note_schema["properties"]["type"]["enum"])

    def test_security_levels_match_the_note_schema(self) -> None:
        levels = [item["id"] for item in self.vocab["security_levels"]]
        self.assertEqual(set(levels), {"public", "internal", "restricted", "confidential"})
        self.assertEqual(
            set(self.note_schema["properties"]["security_level"]["enum"]),
            set(levels),
        )

    def test_tag_namespaces_match_the_schema_pattern(self) -> None:
        pattern = self.note_schema["properties"]["tags"]["items"]["pattern"]
        for facet in self.tags["facets"]:
            self.assertIn(facet, pattern)
        self.assertTrue(self.tags["rules"]["topic_required"])

    def test_required_note_fields(self) -> None:
        self.assertEqual(
            set(self.note_schema["required"]),
            {"type", "status", "created", "updated", "tags"},
        )


if __name__ == "__main__":
    unittest.main()
