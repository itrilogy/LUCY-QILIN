"""商店下发的上架规范。

正文只在这里。技能的 SKILL.md 和 references 不记载上架步骤。
AI 先 get_spec 或读取 qilin://spec/<name>，再整理目录，再上传。
"""

from __future__ import annotations

import json
from pathlib import Path

NAMES = ("upload", "knowledge", "skill", "mcp", "mcp+skill", "runtime", "plugin", "knowledge-intake")

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
    "plugin": "插件。Host/Client 可执行代码，按 Tier A/B/C 分级；配置式 bundle 仍走 mcp。",
    "knowledge-intake": "知识包下载后的本地入库约定：有管理插件就调用它，没有就保持目录形态。",
}

_TEXT = {
    "upload": """上架说明。这份文本由商店 MCP 下发。技能正文和 references 不记载上架步骤。先取规范，再整理目录，再按本文上传。

1. 调 get_spec。name 取要交的种类：knowledge、skill、mcp、mcp+skill、runtime、plugin。按返回的文本整理目录根上的 manifest.json 和文件。不要另造字段，也不要去技能文件里找上架步骤。
2. 调 validate。既可只传 manifest 做静态校验，也可同时传 files（相对路径到 base64）或 skill_md（SKILL.md 原文）在上传前完成跨文件全量预检（含 frontmatter、入口文件、绑定脚本与密钥扫描）。返回 ok、scope、rules、findings。ok 为 false，或 findings 里还有 severity 为 error，就按 path 和 message 改，再 validate。还有错误就不要 begin_submit。
3. ok 为 true 之后，按包体大小二选一提交（只选一条路，不要重复提交成两条）：
   - 8MiB 以内小包：直接调 MCP 的 submit_package(manifest, files)，全流程不依赖本地 CLI 或外部命令。
   - 大包或多文件目录：若有栖林 CLI，在仓库根执行 python -m store.cli submit <目录>；若无本地 CLI，调 begin_submit(manifest, byte_size) 拿到免 KEY 一次性上传凭据（upload_url + upload_headers，或含 ticket 的 put_url），对本地打好的标准 zip 发送二进制 HTTP PUT（例如 curl --fail-with-body -X PUT -H "X-Upload-Ticket: <ticket>" --data-binary @package.zip <upload_url>，无需携带 Authorization），上传 200 后再调 finish_submit(submission_id)。纯远程无文件 MCP（byte_size=0）返回 put_url 为 null，直接调 finish_submit。
4. 钥匙只来自环境变量 QILIN_STORE_KEY（或客户端 MCP 配置），绝不进命令行、不进命令输出，不写进笔记、清单或记忆。命令不生成钥匙。这不是登录口令，也不是 QILIN_HUB_SECRET。地址用 QILIN_STORE_URL，不设则是 http://127.0.0.1:8787。
5. 成功时 status 为 pending，表示待审核，不是已发布。把 submission_id 原样告诉用户。rejected 或 invalid 时把 rules 和 message 原样告诉用户。若提交卡在 uploading 或被 rejected 需要放弃重来，可调 withdraw(submission_id) 取消并回收临时上传包。没有 QILIN_STORE_KEY 时 CLI 退出码是 2，停下来让用户自己设。
6. 作者的 KEY 不能发布。退回原因在 list_my_submissions 和 get_submission。改包后再交。同一命名空间（id 第一个点之前）归首个发布者，别人不能再发；新命名空间直接提交，不需要登记。
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

单篇不超过 256KiB，整包不超过 8MiB（NOTE_FILE）。安装仍是下载并解压到 ${KNOWLEDGE_ROOT}/<id>/<version>，再核对入口文件在；知识树路径按 taxonomy 计算，但当前安装目录不按树走。环境变量是 QILIN_KNOWLEDGE_ROOT，未设置则 ~/.qilin/knowledge。安装后按 qilin://spec/knowledge-intake 处理入库。

协议资源：qilin://schema/knowledge、qilin://vocab/knowledge、qilin://tags/knowledge、qilin://metadata/knowledge、qilin://checklist/knowledge、qilin://example/knowledge。""",
    "skill": """纯技能规范。kind 为 skill。安装是解压，不建立 MCP 连接。

zip 根上有 manifest.json 和 SKILL.md。references、scripts、platform 可选。不进包：工程过程/、.models/、.git/、.DS_Store、密钥文件。

- skill.entry 必须是 SKILL.md，文件必须在。否则 SKILL_ENTRY_MISSING。
- invoke.form 必须是 load_skill，invoke.entry 与 skill.entry 相同。否则 SKILL_INVOKE。
- clients 里每个工具都要能装技能目录（当前支持 skill_dir 的客户端：reasonix、deepseek-harness、antigravity）。不要填 generic 或没有 skill_dir 能力的客户端。规则 CLIENT_CAPABILITY。
- 包内若含 platform/*.md，必须在 skill.platform_files 对象中显式登记每个客户端对应的相对路径（例如 {"reasonix": "platform/reasonix.md"}），且声明的文件必须在包内存在；否则报 PLATFORM_FILE_MISSING。没有 platform/*.md 时可不写 skill.platform_files。
- 纯提示词、没有本机二进制时，platforms 写 ["any"]。脚本只在某些系统能跑，就写具体平台 id。
- SKILL.md 的 frontmatter 继续给运行端用：
  - 若写了可解析 SemVer 的 version，必须与 manifest.version 完全一致（error：FRONTMATTER_CONFLICT）；
  - 若写了 dependency 列表，其中每一项必须在 depends.external[].name 或 depends.store[].id 中声明（error：FRONTMATTER_CONFLICT）；
  - 若写了 allowed-tools 且包声明了 depends.mcp，其中 mcp__<server>__* 前缀对应的服务必须在 depends.mcp 中出现（error：FRONTMATTER_CONFLICT）；
  - manifest.summary 建议取 frontmatter description 的原句或子串；若不一致会给出 severity=warning 的 FRONTMATTER_CONFLICT 提示，不阻断提交。上传前可调 validate(manifest, skill_md=...) 提前核对。
- 密钥只写 secrets 里的变量名。文件名命中 secret、credential、.env、*key*.md（SKILL.md 除外），或正文里出现密钥样式，则 SECRET_FILE 或 SECRET_PATTERN。扫描是拒收，不是打码后放行。

depends 按需要填写。外部命令要有 name、version_range、detect（字符串数组，不是一条 shell）、至少 20 字且含依赖名的 install_guide，以及 https 的 resources（内网白名单主机可走 http）。地址协议不合规报 DEP_EXTERNAL_URL。外网不可达时按服务端 QILIN_URL_CHECK 策略处理：strict 模式留在 uploading（规则 DEP_EXTERNAL_UNREACHABLE，可重试），内网 warn 模式记 warning 由审核人工复核，off 模式跳过探测。""",
    "mcp": """纯 MCP 规范。kind 为 mcp。商店不代理运行时流量。

必填 mcp 与 invoke。
- transport 只能是 stdio 或 streamable-http。否则 MCP_TRANSPORT。
- streamable-http 必须有 https 的 url（内网白名单主机可走 http），不要 command。stdio 必须有 command 和 args，不要 url。否则 MCP_CONNECT。
- invoke.form 为 list_tools（连上后向服务器要工具列表）或 documented（清单里写清工具名和一句话）。否则 MCP_INVOKE。
- 远程地址且没有本机依赖时，platforms 才能是 ["any"]；若含跨平台通用脚本需注明支持平台。否则 PLATFORM_TOO_BROAD。
- stdio 的 client 必须有 mcp_stdio（如 reasonix、claude-desktop、cursor、deepseek-harness、antigravity、generic）。streamable-http 的 client 必须有 mcp_http。规则 CLIENT_CAPABILITY。
- 制品形态（口径 A）：
  - 纯远程 streamable-http（或仅调用系统既有命令、无随包脚本的 stdio）无需 zip 制品：submit_package 传空 files {}，或 begin_submit 传 byte_size=0 后直接 finish_submit；此类版本调 artifact 返回 KIND_NO_ARTIFACT。
  - 含随包桥接脚本或代码的 stdio MCP：将脚本随 manifest.json 一并打成单份 zip 提交。stdio.args 中引用的相对路径脚本必须在包内真实存在（缺失报 MCP_CONNECT）。发布后 artifact 返回该 zip 的下载地址与 sha256，install_plan 自动生成 download + unpack（解压至 ${MCP_DIR}/<id>/<version>）并将相对脚本参数重写为解压目录绝对路径后执行 mcp_add 与 verify。
- kind=mcp 不支持 submit_kind=add_platform（只有 runtime 支持按平台追加资产）。

密钥只写变量名。不要把钥匙的值写进 url、args 或说明。""",
    "mcp+skill": """技能加 MCP。kind 为 mcp+skill。同时满足 get_spec name=mcp 和 name=skill。

invoke.form 必须是 load_skill_then_mcp。先加载技能，再由技能去调用 MCP 工具。

连接可以写在本包的 mcp 块里。若要先装本商店已发布的另一个 MCP，写进 depends.mcp，带 id 和版本范围。未发布则 DEP_STORE_UNPUBLISHED。

安装顺序固定，作者不能改：商店托管依赖，外部依赖检查，depends.skills，depends.mcp，本包的连接，解压技能，索要密钥名，验证。""",
    "plugin": """插件规范。kind 为 plugin。只收 DSH 的 Host/Client 可执行代码；只插入 MCP 连接的配置式 bundle 仍走 mcp，不算插件。

目录根上必须有 manifest.json、package.json，以及清单声明的运行文件。不要子目录越界、符号链接、.node 原生模块或可执行二进制。

包级：
- plugin.tier 必填：A（零 npm 运行时依赖，可声明外部 CLI）、B（白名单内的纯 JS 依赖）、C（原生模块，当前不接受）。
- plugin.form 必填：host、client 或 both。form 为 host 时 plugin.entry 必须存在；为 client 时 plugin.client 必须存在；both 两者都要。
- plugin.patch 必填，指向 cordis.patch.yml。
- plugin.files 必填，列出所有运行文件；entry、client、patch、locale、icon 都要在里面。
- plugin.dsh 必填，写 DSH 版本范围，例如 >=1.5 <2。
- plugin.capabilities 可选，取 tools、client-slots、commands、services、network、credentials:read、credentials:write、filesystem:read、filesystem:write、process:spawn、session:read、session:write。声明只供审核对照，不是运行时权限。
- plugin.external 可选，列出需要的外部命令名（如 rg、pandoc）；写了就要求 capabilities 含 process:spawn。安装器只检查命令在 PATH，不自动安装。

package.json：
- version 必须与清单 version 相同；name 只要求是合法 npm 包名。
- scripts 必须为空，不允许 postinstall、prepare、build。
- Tier A：dependencies / optionalDependencies 必须为空；peerDependencies 只能是 @deepseek-ai/*。
- Tier B：dependencies 只能是商店白名单里的纯 JS 包；optionalDependencies 为空；peerDependencies 只能是 @deepseek-ai/*。
- Tier C：当前拒绝（PLUGIN_TIER）。
- 不允许 .node 文件或 binding.gyp。

调用形式固定为 { "form": "install_plugin" }。商店只产出下载、plugin_stage、plugin_confirm 与 verify，不执行安装；实际安装由用户在 DSH 的 Plugins 里确认，或让 agent 调 plugin_manager install_bundle。

平台的 platforms 只允许 any（纯 JS）；原生模块和平台二进制不在 v1 范围。""",
    "runtime": """运行时规范。kind 为 runtime。这是商店托管的命令或字体一类依赖，不出现在默认货架。只有显式 kind=runtime 才查得到。

- platforms 禁止 any。规则 PLATFORM_TOO_BROAD。每个 platform 都要有对应的 runtime.assets 条目：platform、path、sha256。缺了则 RUNTIME_ASSET。
- invoke.form 为 exec_bin，bin_name 与 runtime.bin_name 相同。
- sha256 由上传时重算。与清单不一致则 CHECKSUM。
- clients 仍要填，且那些工具必须有 local_bin。
- 同一个已发布版本只能追加还没有的平台，用 submit_kind=add_platform。不能换掉已经发出去的文件。技能、MCP、知识包不能追加平台。规则 VERSION_IMMUTABLE。

被别人引用时写在对方的 depends.store，指向已发布的 runtime id 和版本范围。""",
    "knowledge-intake": """知识包入库规范。知识包下载并解压之后，客户端按本段决定「怎么把它登记进本机知识库」。商店不执行入库，也不在本机运行插件。

一、位置。知识根按 QILIN_KNOWLEDGE_ROOT 解析，未设置则 ~/.qilin/knowledge。安装计划把包解压到 ${KNOWLEDGE_ROOT}/<id>/<version>；不要按 taxonomy 改目录，也不要使用 tree_dest 作为实际落点。

二、何时调用。解压并核对入口之后，如果客户端存在知识包管理插件/技能（约定：工具 knowledge_intake，技能 zhilin-intake），调用它完成入库。参数：
- path：解压后的目录，${KNOWLEDGE_ROOT}/<id>/<version>；
- id、version：安装计划里的精确值。
插件负责校验清单与入口、复制到主根、原子更新 library.json；重复调用是幂等的。

三、没有插件时。保持目录形态即可。任何按知识根扫描的阅读器都能读到；不要让 AI 手工移动、改名、删文件，也不要手写 library.json。

四、边界。不要修改包内文件；不要因为入库失败删除源目录；不要声称已经按 security_level 做了访问控制——密级是声明，不是 ACL。失败时按插件返回的 code 处理：BAD_MANIFEST / BAD_ENTRY 不要重试；TOO_LARGE 按商店上限处理；TARGET_EXISTS 让用户决定保留哪一份。

五、安装计划。kind=knowledge 的 install_plan 会带 post_install 数组，第一项 op 为 knowledge_intake，含 path、id、version、tool、skill、required=false。required 为 false 表示没有插件时跳过，不影响包本身可用。

六、与上架规范的关系。本段只管「下载之后」；包的字段、链接、标签、密级仍按 qilin://spec/knowledge 和 qilin://vocab/knowledge 校验，两者不互相替代。""",
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
