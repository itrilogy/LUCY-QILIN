"""知识包。一组 Markdown 笔记，链接只在包内闭合。

规则只放在这个模块。技能、MCP、运行时的校验不要来这里借，这里也不要去管它们。

v2：分类、密级、标签都按 `store/spec/knowledge-*.json` 的词表校验。词表是
商店 MCP 下发的生产协议；笔记由哪个技能生产与本模块无关。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from store.blobs import is_ignored_path, load_storeignore

if TYPE_CHECKING:
    from store.validator.validate import Report

NOTE_TYPES = {
    "reference_note",
    "guide_note",
    "tutorial_note",
    "reflection_note",
    "map_of_content",
}
KIND_BY_TYPE = {
    "reference_note": "reference",
    "guide_note": "guide",
    "tutorial_note": "tutorial",
    "reflection_note": "reflection",
    "map_of_content": "moc",
}
SECURITY_RANK = {"public": 0, "internal": 1, "restricted": 2, "confidential": 3}
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOTE_NAME = re.compile(r"^\d{2}-.+\.md$")
TAG = re.compile(
    r"^(topic|entity|method|source)/([a-z0-9\u4e00-\u9fff]+(?:-[a-z0-9\u4e00-\u9fff]+)*)$"
)
FENCE = re.compile(r"```.*?```", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
WIKI = re.compile(r"!\[\[([^\]]+)\]\]|\[\[([^\]]+)\]\]")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
MAX_NOTE_BYTES = 256 * 1024
MAX_PACK_BYTES = 8 * 1024 * 1024
_SPEC_DIR = Path(__file__).resolve().parents[1] / "spec"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    """读取词表。文件缺失时返回空集合，让 taxonomy 报「不在词表内」，而不是崩。"""
    empty = {
        "categories": {}, "domains": {}, "kinds": set(),
        "topics": set(), "methods": set(), "sources": set(),
    }
    try:
        vocab = json.loads((_SPEC_DIR / "knowledge-vocabulary.json").read_text(encoding="utf-8"))
        tags = json.loads((_SPEC_DIR / "knowledge-tags.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    categories = {str(item.get("id")): item for item in vocab.get("categories") or []}
    domains: dict[str, str] = {}
    for category in categories.values():
        for domain in category.get("domains") or []:
            domains[str(domain.get("id"))] = str(category.get("id"))
    facets = tags.get("facets") or {}
    return {
        "categories": categories,
        "domains": domains,
        "kinds": {str(item.get("id")) for item in vocab.get("kinds") or []},
        "topics": {str(item.get("id")) for item in (facets.get("topic") or {}).get("terms") or []},
        "methods": {str(item.get("id")) for item in (facets.get("method") or {}).get("terms") or []},
        "sources": {str(item.get("id")) for item in (facets.get("source") or {}).get("terms") or []},
    }


def resolve_knowledge_tree_path(manifest: dict) -> str:
    """根据知识包元数据计算商店知识树（Knowledge Tree）逻辑存储相对路径。

    - 若声明了完整的 `knowledge.taxonomy` 且三段都通过词表，路径为
      `<business_category>/<business_domain>/<knowledge_kind>/<id>/<version>`；
    - 否则归入 `_unclassified/<id>/<version>`。
    """
    package_id = str(manifest.get("id") or "unknown").strip()
    version = str(manifest.get("version") or "0.0.0").strip()
    block = manifest.get("knowledge")
    taxonomy = block.get("taxonomy") if isinstance(block, dict) else None
    if isinstance(taxonomy, dict):
        cat = str(taxonomy.get("business_category") or "").strip()
        dom = str(taxonomy.get("business_domain") or "").strip()
        kind = str(taxonomy.get("knowledge_kind") or "").strip()
        if (
            cat
            and dom
            and kind
            and all(SAFE_SEGMENT.fullmatch(seg) and ".." not in seg for seg in (cat, dom, kind))
        ):
            return f"{cat}/{dom}/{kind}/{package_id}/{version}"
    return f"_unclassified/{package_id}/{version}"


def check(report: Report, manifest: dict, root: Path, *, check_files: bool) -> None:
    block = manifest.get("knowledge")
    catalog = _catalog()
    entry = _entry_name(block)
    if entry is None:
        report.add("NOTE_ENTRY", "knowledge.entry", "入口必须是根上的 00-*.md，且是 .md 文件")
    invoke = manifest.get("invoke") if isinstance(manifest.get("invoke"), dict) else {}
    if invoke.get("form") != "read_notes" or invoke.get("entry") != entry:
        report.add("NOTE_ENTRY", "invoke.form", "invoke.form 必须是 read_notes，入口与 knowledge.entry 相同")
    platforms = manifest.get("platforms")
    if platforms != ["any"]:
        report.add("KNOWLEDGE_SCOPE", "platforms", "知识包不按电脑平台区分，只写 any")
    depends = manifest.get("depends") if isinstance(manifest.get("depends"), dict) else {}
    if any(depends.get(key) for key in ("store", "external", "skills", "mcp")):
        report.add("KNOWLEDGE_SCOPE", "depends", "知识包不引用别的产品，依赖留空")
    secrets = manifest.get("secrets")
    if secrets not in ([], None):
        report.add("KNOWLEDGE_SCOPE", "secrets", "知识包不声明密钥")

    taxonomy = _check_taxonomy(report, block, catalog)
    security = _check_pack_fields(report, block, catalog)
    local_topics = _local_topics(report, block)

    _check_knowledge_map(report, block, notes=None)
    if not check_files or entry is None:
        return

    notes = _notes(report, root)
    stem = entry[:-3]
    if stem not in notes:
        report.add("NOTE_ENTRY", "knowledge.entry", "入口那一篇不在这组笔记里")
        return

    texts: dict[str, str] = {}
    fields_by_note: dict[str, dict] = {}
    for name, path in notes.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        texts[name] = text
        fields = _frontmatter(report, name + ".md", text, pack_security=security)
        fields_by_note[name] = fields
        _check_note_semantics(
            report, name + ".md", fields, catalog,
            category=(taxonomy or ("", "", ""))[0], local_topics=local_topics,
        )

    entry_type, _ = fields_by_note.get(stem, {}).get("type", ("", []))
    if entry_type != "map_of_content":
        report.add("NOTE_ENTRY", "knowledge.entry", "入口那一篇的 type 必须是 map_of_content")

    _check_knowledge_map(report, block, notes=notes)
    _check_links_and_orphans(report, texts, entry_stem=stem)


def _check_taxonomy(report: Report, block: object, catalog: dict) -> tuple[str, str, str] | None:
    if not isinstance(block, dict) or block.get("taxonomy") is None:
        report.add("KNOWLEDGE_TAXONOMY", "knowledge.taxonomy", "知识包必须声明 business_category、business_domain、knowledge_kind")
        return None
    taxonomy = report.guard(
        block["taxonomy"], dict, "KNOWLEDGE_TAXONOMY", "knowledge.taxonomy",
        "taxonomy 必须是对象", None,
    )
    if taxonomy is None:
        return None
    values: dict[str, str] = {}
    for key in ("business_category", "business_domain", "knowledge_kind"):
        value = taxonomy.get(key)
        if not isinstance(value, str) or not value.strip():
            report.add("KNOWLEDGE_TAXONOMY", f"knowledge.taxonomy.{key}", f"{key} 不能为空")
            continue
        values[key] = value.strip()
    category = values.get("business_category", "")
    domain = values.get("business_domain", "")
    kind = values.get("knowledge_kind", "")
    if category and category not in catalog["categories"]:
        report.add("KNOWLEDGE_TAXONOMY", "knowledge.taxonomy.business_category", f"不在词表内的类目：{category}")
    if domain:
        if domain not in catalog["domains"]:
            report.add("KNOWLEDGE_TAXONOMY", "knowledge.taxonomy.business_domain", f"不在词表内的领域：{domain}")
        elif category and catalog["domains"][domain] != category:
            report.add("KNOWLEDGE_TAXONOMY", "knowledge.taxonomy.business_domain", f"{domain} 不属于 {category}")
    if kind and kind not in catalog["kinds"]:
        report.add("KNOWLEDGE_TAXONOMY", "knowledge.taxonomy.knowledge_kind", f"不在词表内的形态：{kind}")
    return (category, domain, kind) if all(values.values()) else None


def _check_pack_fields(report: Report, block: object, catalog: dict) -> str:
    """校验包级字段，返回密级 id（缺省 internal）。"""
    if not isinstance(block, dict):
        return "internal"
    security = block.get("security_level")
    if security is None:
        report.add("NOTE_SECURITY", "knowledge.security_level", "知识包必须声明 security_level")
        security = "internal"
    elif not isinstance(security, str) or security not in SECURITY_RANK:
        report.add("NOTE_SECURITY", "knowledge.security_level", "security_level 只能是 public、internal、restricted、confidential")
        security = "internal"
    scope = block.get("scope")
    if scope is not None and scope not in {"enterprise", "work"}:
        report.add("KNOWLEDGE_SCOPE", "knowledge.scope", "scope 只能是 enterprise 或 work")
    vocabulary = block.get("vocabulary")
    if vocabulary is not None and vocabulary != "kv/1":
        report.add("KNOWLEDGE_TAXONOMY", "knowledge.vocabulary", "不认识的词表版本，当前只支持 kv/1")
    language = block.get("language")
    if language is not None and language not in {"zh-CN", "en", "bilingual"}:
        report.add("KNOWLEDGE_SCOPE", "knowledge.language", "language 只能是 zh-CN、en 或 bilingual")
    license_name = block.get("license")
    if license_name is not None and license_name not in {"internal", "cc-by", "cc-by-sa", "all-rights-reserved"}:
        report.add("KNOWLEDGE_SCOPE", "knowledge.license", "license 不在允许的取值内")
    producer = block.get("producer")
    if producer is not None and (not isinstance(producer, str) or len(producer) > 120):
        report.add("KNOWLEDGE_SCOPE", "knowledge.producer", "producer 是最多 120 字的溯源字符串，不参与校验")
    return security


def _local_topics(report: Report, block: object) -> set[str]:
    if not isinstance(block, dict):
        return set()
    raw = block.get("topics")
    if raw is None:
        return set()
    rows = report.guard(raw, list, "KNOWLEDGE_TAXONOMY", "knowledge.topics", "topics 必须是数组", [])
    if not rows:
        return set()
    if len(rows) > 10:
        report.add("KNOWLEDGE_TAXONOMY", "knowledge.topics", "本地主题词最多 10 个")
    found: set[str] = set()
    for index, item in enumerate(rows):
        value = str(item).strip() if isinstance(item, str) else ""
        slug = value.split("/", 1)[-1] if value.startswith("topic/") else value
        if not slug or not re.fullmatch(r"[a-z0-9\u4e00-\u9fff]+(?:-[a-z0-9\u4e00-\u9fff]+)*", slug):
            report.add("KNOWLEDGE_TAXONOMY", f"knowledge.topics[{index}]", "本地主题词只能用小写英文或中文，段内用连字符")
            continue
        found.add(slug)
    return found


def _check_note_semantics(
    report: Report,
    filename: str,
    fields: dict,
    catalog: dict,
    *,
    category: str,
    local_topics: set[str],
) -> None:
    tags = fields.get("tags")
    if not tags:
        return
    _inline, items = tags
    allowed_topics = catalog["topics"] | local_topics
    for item in items:
        matched = TAG.fullmatch(item)
        if not matched:
            continue  # 结构问题已由 `_frontmatter` 记过
        facet, slug = matched.group(1), matched.group(2)
        if facet == "topic" and slug not in allowed_topics:
            report.add("NOTE_FRONTMATTER", filename, f"主题词 {slug} 不在词表内；要新增请写进 knowledge.topics")
        elif facet == "method" and slug not in catalog["methods"]:
            report.add("NOTE_FRONTMATTER", filename, f"方法词 {slug} 不在词表内")
        elif facet == "source" and slug not in catalog["sources"]:
            report.add("NOTE_FRONTMATTER", filename, f"来源词 {slug} 不在词表内")
    domain, _items = fields.get("domain", ("", []))
    if domain:
        if domain not in catalog["domains"]:
            report.add("KNOWLEDGE_TAXONOMY", filename, f"domain {domain} 不在词表内")
        elif category and catalog["domains"][domain] != category:
            report.add("KNOWLEDGE_TAXONOMY", filename, f"domain {domain} 不属于 {category}")


def _check_knowledge_map(report: Report, block: object, *, notes: dict[str, Path] | None) -> None:
    if not isinstance(block, dict) or block.get("knowledge_map") is None:
        return
    kmap = block["knowledge_map"]
    if not isinstance(kmap, dict):
        if notes is None:
            report.add("KNOWLEDGE_MAP", "knowledge.knowledge_map", "knowledge_map 必须是对象")
        return
    nodes = kmap.get("nodes")
    if nodes is None:
        return
    if not isinstance(nodes, list):
        if notes is None:
            report.add("KNOWLEDGE_MAP", "knowledge.knowledge_map.nodes", "nodes 必须是数组")
        return
    if notes is None:
        return
    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            report.add("KNOWLEDGE_MAP", f"knowledge.knowledge_map.nodes[{idx}]", "节点必须是对象")
            continue
        note_ref = str(node.get("id") or node.get("file") or "").strip()
        stem = note_ref[:-3] if note_ref.endswith(".md") else note_ref
        if stem and stem not in notes:
            report.add(
                "KNOWLEDGE_MAP",
                f"knowledge.knowledge_map.nodes[{idx}]",
                f"知识地图引用的节点 {note_ref} 不在笔记集合中",
            )


def _entry_name(block: object) -> str | None:
    if not isinstance(block, dict):
        return None
    name = str(block.get("entry") or "").strip()
    if (
        not name.startswith("00-")
        or not name.endswith(".md")
        or "/" in name
        or "\\" in name
        or name.startswith(".")
        or name == "manifest.json"
    ):
        return None
    return name


def _notes(report: Report, root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    total = 0
    if not root.is_dir():
        return found
    ignore_patterns = load_storeignore(root)
    for path in sorted(root.iterdir()):
        if is_ignored_path(path.name, ignore_patterns):
            continue
        if path.name == "manifest.json" and path.is_file():
            continue
        if not path.is_file() or path.suffix != ".md":
            report.add("NOTE_FILE", path.name, "知识包里只能有清单和根上的 Markdown")
            continue
        if path.name in {"SKILL.md", "README.md"}:
            report.add("NOTE_FILE", path.name, "技能入口与说明文件不进知识包")
            continue
        if not NOTE_NAME.fullmatch(path.name):
            report.add("NOTE_FILE", path.name, "笔记文件名必须是 NN-名称.md（00 为入口）")
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_NOTE_BYTES:
            report.add("NOTE_FILE", path.name, f"单篇笔记超过 {MAX_NOTE_BYTES // 1024}KiB")
            continue
        found[path.stem] = path
    if total > MAX_PACK_BYTES:
        report.add("NOTE_FILE", "manifest.json", f"整包超过 {MAX_PACK_BYTES // (1024 * 1024)}MiB")
    return found


def _check_links_and_orphans(report: Report, texts: dict[str, str], *, entry_stem: str) -> None:
    headings = {name: _headings(text) for name, text in texts.items()}
    inbound: dict[str, int] = {}
    for name, text in texts.items():
        for target, anchor in _links(text):
            if target not in texts:
                report.add("NOTE_LINK", name + ".md", f"[[{target}]] 在这组笔记里没有对应文件")
                continue
            if anchor and anchor not in headings[target]:
                report.add("NOTE_LINK", name + ".md", f"[[{target}#{anchor}]] 的目标里没有这个标题")
            if target != name:
                inbound[target] = inbound.get(target, 0) + 1
    for name in texts:
        if name == entry_stem:
            continue
        if inbound.get(name, 0) == 0:
            report.add("NOTE_ORPHAN", name + ".md", "这篇没有任何入链；入口篇应链接到它", "warning")


def _frontmatter(report: Report, filename: str, text: str, *, pack_security: str | None = None) -> dict:
    block = _block(text)
    if block is None:
        report.add("NOTE_FRONTMATTER", filename, "每篇笔记开头要有 YAML 题头")
        return {}
    fields = _fields(block)
    kind, _items = fields.get("type", ("", []))
    if kind not in NOTE_TYPES:
        report.add(
            "NOTE_FRONTMATTER", filename,
            "type 只能是 reference_note、guide_note、tutorial_note、reflection_note、map_of_content",
        )
    status, _items = fields.get("status", ("", []))
    if status not in {"evergreen", "seedling"}:
        report.add("NOTE_FRONTMATTER", filename, "status 只能是 evergreen 或 seedling")
    dates: dict[str, str] = {}
    for key in ("created", "updated"):
        value, _items = fields.get(key, ("", []))
        dates[key] = value
        if not DATE.match(value):
            report.add("NOTE_FRONTMATTER", filename, f"{key} 要写成 YYYY-MM-DD")
    if DATE.match(dates.get("created", "")) and DATE.match(dates.get("updated", "")):
        if dates["updated"] < dates["created"]:
            report.add("NOTE_FRONTMATTER", filename, "updated 不能早于 created")
    if not _is_list(fields, "tags"):
        report.add("NOTE_FRONTMATTER", filename, "tags 要写成 YAML 数组，一项一行或 [a, b]")
    else:
        items = fields["tags"][1]
        if not 1 <= len(items) <= 12:
            report.add("NOTE_FRONTMATTER", filename, "tags 要有 1 到 12 个")
        has_topic = False
        for item in items:
            if not TAG.fullmatch(item):
                report.add("NOTE_FRONTMATTER", filename, f"标签 {item} 必须写成 topic/、entity/、method/ 或 source/")
                continue
            has_topic = has_topic or item.startswith("topic/")
        if items and not has_topic:
            report.add("NOTE_FRONTMATTER", filename, "至少要有一个 topic/ 标签")
    if "prerequisite" in fields and not _prerequisite_ok(fields):
        report.add("NOTE_FRONTMATTER", filename, "prerequisite 没有前置时写「无」，有前置时写成数组")
    if "aliases" in fields:
        if not _is_list(fields, "aliases"):
            report.add("NOTE_FRONTMATTER", filename, "aliases 要写成数组。链接仍指向文件名，不指向别名")
        elif len(fields["aliases"][1]) > 8:
            report.add("NOTE_FRONTMATTER", filename, "aliases 最多 8 个")
    summary, _items = fields.get("summary", ("", []))
    if len(summary) > 200:
        report.add("NOTE_FRONTMATTER", filename, "summary 不超过 200 字")
    source, _items = fields.get("source", ("", []))
    if len(source) > 500:
        report.add("NOTE_FRONTMATTER", filename, "source 不超过 500 字")
    security, _items = fields.get("security_level", ("", []))
    if security:
        if security not in SECURITY_RANK:
            report.add("NOTE_SECURITY", filename, "security_level 只能是 public、internal、restricted、confidential")
        elif pack_security and SECURITY_RANK[security] > SECURITY_RANK.get(pack_security, 0):
            report.add("NOTE_SECURITY", filename, f"篇级密级 {security} 高于包级 {pack_security}")
    note_kind, _items = fields.get("knowledge_kind", ("", []))
    if note_kind:
        expected = KIND_BY_TYPE.get(kind)
        if note_kind not in KIND_BY_TYPE.values() or note_kind != expected:
            report.add("NOTE_FRONTMATTER", filename, "knowledge_kind 必须与 type 对应")
    return fields


def _prerequisite_ok(fields: dict[str, tuple[str, list[str]]]) -> bool:
    inline, items = fields["prerequisite"]
    if "[[" in inline:
        return False
    if inline in {"", "无", "[]"}:
        return True
    return not inline and bool(items)


def _is_list(fields: dict[str, tuple[str, list[str]]], key: str) -> bool:
    if key not in fields:
        return False
    inline, items = fields[key]
    if inline == "[]":
        return True
    return inline == "" and bool(items)


def _block(text: str) -> str | None:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    return text[4:end]


def _fields(block: str) -> dict[str, tuple[str, list[str]]]:
    """逐行读 YAML 题头。

    不支持完整的 YAML：只认「键: 值」「键: [a, b]」与「键:」加缩进列表。
    折叠标量（`>`、`|`）按「有值但不解析」处理，避免把它的正文当成列表项。
    """
    current: str | None = None
    found: dict[str, list] = {}
    in_block_scalar = False
    for line in block.splitlines():
        if in_block_scalar and (line.startswith("  ") or line.startswith("\t")):
            continue
        in_block_scalar = False
        if current and line.startswith(("  - ", "- ")):
            found[current][1].append(line.split("- ", 1)[1].strip().strip("\"'"))
            continue
        matched = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not matched:
            continue
        current = matched.group(1)
        raw = matched.group(2).strip()
        if raw in {">", "|", ">-", "|-", ">+", "|+"}:
            found[current] = ["", []]
            in_block_scalar = True
            continue
        # 注意 `[[篇名]]` 是 wiki 链接，不是流式数组。
        if raw.startswith("[") and raw.endswith("]") and not raw.startswith("[["):
            inner = raw[1:-1].strip()
            items = [part.strip().strip("\"'") for part in inner.split(",") if part.strip()]
            found[current] = ["" if items else "[]", items]
            continue
        found[current] = [raw.strip("\"'"), []]
    return {key: (value[0], value[1]) for key, value in found.items()}


def _plain(text: str) -> str:
    return INLINE_CODE.sub("", FENCE.sub("", text))


def _headings(text: str) -> set[str]:
    return {match.group(1).strip() for match in HEADING.finditer(_plain(text))}


def _links(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in WIKI.finditer(_plain(text)):
        raw = (match.group(1) or match.group(2) or "").strip()
        head = raw.split("|", 1)[0]
        name, _sep, anchor = head.partition("#")
        name = name.strip()
        if name:
            found.append((name, anchor.strip()))
    return found
