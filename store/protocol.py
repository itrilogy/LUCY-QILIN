"""商店下发的上架规范。

正文只在这里。技能的 SKILL.md 和 references 不记载上架步骤。
AI 先 get_spec 或读取 qilin://spec/<name>，再整理目录，再上传。
"""

from __future__ import annotations

import json
from pathlib import Path

NAMES = ("upload", "knowledge", "skill", "mcp", "mcp+skill", "runtime")

# 协议的数据文件。和散文规范分开：散文走 qilin://spec/<name>，数据走 qilin://<kind>/knowledge。
# 这些文件随包分发；MCP 直接读，不在代码里再抄一份。
_SPEC_DIR = Path(__file__).resolve().parent / "spec"
DATA_RESOURCES = {
    "qilin://schema/knowledge": ("knowledge-note.schema.json", "application/schema+json"),
    "qilin://vocab/knowledge": ("knowledge-vocabulary.json", "application/json"),
    "qilin://tags/knowledge": ("knowledge-tags.json", "application/json"),
    "qilin://metadata/knowledge": ("knowledge-metadata.json", "application/json"),
    "qilin://example/knowledge": ("knowledge-example.json", "application/json"),
}
CHECKLIST_URI = "qilin://checklist/knowledge"
GENERATED_RESOURCES = {CHECKLIST_URI: "text/markdown"}
ALL_DATA_URIS = tuple(DATA_RESOURCES) + tuple(GENERATED_RESOURCES)

_BLURB = {
    "upload": "上架顺序。先取种类规范，再校验，再交目录。",
    "knowledge": "知识包。根目录上的 Markdown，链接闭合，分类与标签按商店词表。",
    "skill": "纯技能。入口是 SKILL.md，调用形式 load_skill。",
    "mcp": "纯 MCP。连接方式是 stdio 或 streamable-http。",
    "mcp+skill": "技能加 MCP。先满足两边，调用形式 load_skill_then_mcp。",
    "runtime": "商店托管的依赖。不上默认货架。只有这种可以追加平台。",
}

_TEXT = {
    "upload": """上架说明。这份文本由商店 MCP 下发。技能正文和 references 不记载上架步骤。先取规范，再整理目录，再按本文上传。

1. 调 get_spec。name 取要交的种类：knowledge、skill、mcp、mcp+skill、runtime。按返回的文本整理目录根上的 manifest.json 和文件。不要另造字段，也不要去技能文件里找上架步骤。
2. 调 validate，只传清单。返回 ok、rules、findings。ok 为 false，或 findings 里还有 severity 为 error，就按 path 和 message 改，再 validate。还有错误就不要 begin_submit。
3. ok 为 true 之后，在栖林仓库根执行 python -m store.cli submit <目录>。这条命令自己会 begin_submit、对返回的地址做 PUT、再 finish_submit。不要自己编 curl。已经用了这条命令，就不要再手动 begin_submit，以免交成两条。
4. 钥匙只来自环境变量 QILIN_STORE_KEY。不进命令行，不进命令输出，不写进笔记、清单或记忆。命令不生成钥匙。这不是登录口令，也不是 QILIN_HUB_SECRET。地址用 QILIN_STORE_URL，不设则是 http://127.0.0.1:8787。
5. 成功时 status 为 pending，表示待审核，不是已发布。把 submission_id 原样告诉用户。rejected 或 invalid 时把 rules 和 message 原样告诉用户。没有 QILIN_STORE_KEY 时退出码是 2，停下来让用户自己设。
6. 作者的 KEY 不能发布。退回原因在 list_my_submissions 和 get_submission。改包后再交。同一 id 归首个发布者，别人不能再发新版本；新 id 直接提交，不需要登记。
7. 不要把密钥文件或密钥值打进包。secrets 只写环境变量名。spec_version 固定为 1。""",
    "knowledge": """知识包规范。kind 为 knowledge。商店不判断文章好不好，只看这组笔记能不能按协议闭合。

目录根上只有 manifest.json 和 NN-*.md。不要子目录、图片、SKILL.md 或其他文件（NOTE_FILE）。文件名必须是 00-名称.md 或 01-99-名称.md。

包级：
- knowledge.entry 必须是 00-*.md，且那一篇的 type 是 map_of_content（NOTE_ENTRY）。
- knowledge.taxonomy 必填三段：business_category、business_domain、knowledge_kind。取值只认商店下发的词表，见 qilin://vocab/knowledge；领域必须属于所选类目（KNOWLEDGE_TAXONOMY）。
- knowledge.security_level 必填：public、internal、restricted、confidential。篇级不得高于包级（NOTE_SECURITY）。
- knowledge.scope 可选：enterprise 或 work，默认 enterprise。knowledge.vocabulary 可选，当前只支持 kv/1。
- knowledge.topics 可选，最多 10 个本地主题词；写了就能在 tags 里用 topic/<本地词>。
- platforms 只能是 ["any"]，depends 四项为空，secrets 为空（KNOWLEDGE_SCOPE）。
- knowledge.producer 可选，只作溯源，不参与校验。
- invoke 固定为 { "form": "read_notes", "entry": "<与 knowledge.entry 相同>" }。

每篇题头必填 type、status、created、updated、tags：
- type 只能是 reference_note、guide_note、tutorial_note、reflection_note、map_of_content。
- status 只能是 evergreen 或 seedling；日期 YYYY-MM-DD，updated 不早于 created。
- tags 是数组，1 到 12 个，至少一个 topic/；写作 topic/、entity/、method/、source/ 四种前缀。topic 只认词表或本包 knowledge.topics；method 与 source 是封闭词表；entity 开放。见 qilin://tags/knowledge。
- aliases 可选数组（最多 8，不作链接目标）；prerequisite 写数组或「无」；summary 最多 200 字；source 最多 500 字。
- domain 可选，必须在包级类目下；knowledge_kind 可选，必须与 type 对应（NOTE_FRONTMATTER / KNOWLEDGE_TAXONOMY）。

链接只认 [[篇名]]、[[篇名|显示]]、[[篇名#标题]]。篇名是文件名去掉 .md，必须对上包内一篇（NOTE_LINK）；带 #标题 时，目标篇必须真有这个标题。代码块与行内代码里的链接不查。没有任何入链的篇（入口除外）记 warning NOTE_ORPHAN，不阻断。

单篇不超过 256KiB，整包不超过 8MiB（NOTE_FILE）。安装仍是下载并解压到 ${KNOWLEDGE_ROOT}/<id>/<version>，再核对入口文件在；知识树路径按 taxonomy 计算，但当前安装目录不按树走。环境变量是 QILIN_KNOWLEDGE_ROOT。

协议资源：qilin://schema/knowledge、qilin://vocab/knowledge、qilin://tags/knowledge、qilin://metadata/knowledge、qilin://checklist/knowledge、qilin://example/knowledge。""",
    "skill": """纯技能规范。kind 为 skill。安装是解压，不建立 MCP 连接。

zip 根上有 manifest.json 和 SKILL.md。references、scripts、platform 可选。不进包：工程过程/、.models/、.git/、.DS_Store、密钥文件。

- skill.entry 必须是 SKILL.md，文件必须在。否则 SKILL_ENTRY_MISSING。
- invoke.form 必须是 load_skill，invoke.entry 与 skill.entry 相同。否则 SKILL_INVOKE。
- clients 里每个工具都要能装技能目录。当前只有 reasonix 有 skill_dir。不要给没有这个能力的工具填技能。规则 CLIENT_CAPABILITY。
- 声明了 platform/<工具>.md 时，文件必须在。也可以用 skill.platform_files 写实际文件名。缺了则 PLATFORM_FILE_MISSING。
- 纯提示词、没有本机二进制时，platforms 写 ["any"]。脚本只在某些系统能跑，就写具体平台 id。
- SKILL.md 的 frontmatter 继续给运行端用。name、description、能解析的 version 不得和清单说成另一件事。规则 FRONTMATTER_CONFLICT。
- 密钥只写 secrets 里的变量名。文件名命中 secret、credential、.env、*key*.md（SKILL.md 除外），或正文里出现密钥样式，则 SECRET_FILE 或 SECRET_PATTERN。扫描是拒收，不是打码后放行。

depends 按需要填写。外部命令要有 name、version_range、detect（字符串数组，不是一条 shell）、至少 20 字且含依赖名的 install_guide，以及 https 的 resources。地址不是 https 则 DEP_EXTERNAL_URL。地址当时打不开则留在 uploading，规则 DEP_EXTERNAL_UNREACHABLE，可以再交一次。""",
    "mcp": """纯 MCP 规范。kind 为 mcp。安装是写入一条连接，商店不代理流量。

必填 mcp 与 invoke。
- transport 只能是 stdio 或 streamable-http。否则 MCP_TRANSPORT。
- streamable-http 必须有 https 的 url，不要 command。stdio 必须有 command 和 args，不要 url。否则 MCP_CONNECT。
- invoke.form 为 list_tools（连上后向服务器要工具列表）或 documented（清单里写清工具名和一句话）。否则 MCP_INVOKE。
- 远程地址且没有本机依赖时，platforms 才能是 ["any"]。否则 PLATFORM_TOO_BROAD。
- stdio 的 client 必须有 mcp_stdio。streamable-http 的 client 必须有 mcp_http。规则 CLIENT_CAPABILITY。
- 纯远程可以只交 manifest.json，没有 zip。

密钥只写变量名。不要把钥匙的值写进 url、args 或说明。""",
    "mcp+skill": """技能加 MCP。kind 为 mcp+skill。同时满足 get_spec name=mcp 和 name=skill。

invoke.form 必须是 load_skill_then_mcp。先加载技能，再由技能去调用 MCP 工具。

连接可以写在本包的 mcp 块里。若要先装本商店已发布的另一个 MCP，写进 depends.mcp，带 id 和版本范围。未发布则 DEP_STORE_UNPUBLISHED。

安装顺序固定，作者不能改：商店托管依赖，外部依赖检查，depends.skills，depends.mcp，本包的连接，解压技能，索要密钥名，验证。""",
    "runtime": """运行时规范。kind 为 runtime。这是商店托管的命令或字体一类依赖，不出现在默认货架。只有显式 kind=runtime 才查得到。

- platforms 禁止 any。规则 PLATFORM_TOO_BROAD。每个 platform 都要有对应的 runtime.assets 条目：platform、path、sha256。缺了则 RUNTIME_ASSET。
- invoke.form 为 exec_bin，bin_name 与 runtime.bin_name 相同。
- sha256 由上传时重算。与清单不一致则 CHECKSUM。
- clients 仍要填，且那些工具必须有 local_bin。
- 同一个已发布版本只能追加还没有的平台，用 submit_kind=add_platform。不能换掉已经发出去的文件。技能、MCP、知识包不能追加平台。规则 VERSION_IMMUTABLE。

被别人引用时写在对方的 depends.store，指向已发布的 runtime id 和版本范围。""",
}


def blurb(name: str) -> str:
    return _BLURB.get(name, "")


def text(name: str) -> str | None:
    body = _TEXT.get(name)
    if body is None:
        return None
    return body.strip() + "\n"


def uri(name: str) -> str:
    return f"qilin://spec/{name}"


def _checklist_text() -> str | None:
    """从元数据词典的 workfow 生成自检清单，避免第二份手写清单漂移。"""
    try:
        data = json.loads((_SPEC_DIR / "knowledge-metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    workflow = data.get("workflow") or {}
    lines = [
        f"# {workflow.get('name', '知识包生产')}自检清单",
        "",
        "商店只验收产物。逐条打勾，再跑 store validate。",
        "",
    ]
    for stage in workflow.get("stages") or []:
        lines.append(f"## {stage.get('name')}（{stage.get('id')}）")
        for check in stage.get("checks") or []:
            lines.append(f"- [ ] {check}")
        lines.append("")
    forbidden = workflow.get("forbidden") or []
    if forbidden:
        lines.append("## 禁止")
        lines.extend(f"- {item}" for item in forbidden)
        lines.append("")
    return "\n".join(lines)


def data_text(uri_value: str) -> str | None:
    """读取协议数据文件。文件缺失时返回 None，由调用方回 -32002。"""
    if uri_value == CHECKLIST_URI:
        return _checklist_text()
    entry = DATA_RESOURCES.get(uri_value)
    if entry is None:
        return None
    path = _SPEC_DIR / entry[0]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def data_mime(uri_value: str) -> str:
    if uri_value in GENERATED_RESOURCES:
        return GENERATED_RESOURCES[uri_value]
    entry = DATA_RESOURCES.get(uri_value)
    return entry[1] if entry else "application/json"


def data_blurb(uri_value: str) -> str:
    return {
        "qilin://schema/knowledge": "知识包笔记题头 JSON Schema。",
        "qilin://vocab/knowledge": "知识包分类词表：类目、领域、形态、密级、范围。",
        "qilin://tags/knowledge": "知识包标签词表：topic、entity、method、source 四个分面。",
        "qilin://metadata/knowledge": "知识包元数据词典与十阶段生产流程。",
        "qilin://checklist/knowledge": "打包前逐条自检清单。",
        "qilin://example/knowledge": "最小合格知识包：清单与三篇笔记。",
    }.get(uri_value, "")


def name_from_uri(value: str) -> str | None:
    prefix = "qilin://spec/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    name = value[len(prefix):]
    if name not in _TEXT:
        return None
    return name
