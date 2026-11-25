"""商店 MCP。JSON-RPC 的一个子集，工具按密钥角色可见，密钥不是参数。"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any
from urllib.parse import quote

from store.diagnostics import storage_warning
from store.protocol import ALL_DATA_URIS, NAMES, blurb, data_blurb, data_mime, data_text, name_from_uri, text, uri
from store.service import Store, StoreError

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PUBLIC = (
    "get_spec",
    "search",
    "list_versions",
    "resolve",
    "describe",
    "install_plan",
    "artifact",
    "report_problem",
    "get_report",
)
AUTHENTICATED = ("list_my_reports",)
PUBLISHER = (
    "validate",
    "begin_submit",
    "submit_package",
    "finish_submit",
    "begin_revise",
    "finish_revise",
    "list_my_submissions",
    "get_submission",
    "withdraw",
    "list_product_reports",
)
REVIEWER = (
    "list_reviews",
    "get_review",
    "review_plugin",
    "approve",
    "reject",
    "unpublish",
    "list_audit",
    "list_contributions",
    "list_reports",
    "triage_report",
)
ADMIN = ("transfer_namespace",)

_SCHEMAS: dict[str, dict] = {
    "get_spec": {
        "description": "读取商店下发的上架规范全文。打包之前先调这个。name 取 upload、knowledge、skill、mcp、mcp+skill、runtime、plugin；知识包下载后的本地入库约定取 knowledge-intake。规范不在技能正文或 references 里，不要去那里找上架步骤。返回的 text 与校验器是同一套，不要另造规则。",
        "properties": {
            "name": {
                "type": "string",
                "description": "upload 是上架顺序。其余六个是种类规范。",
            },
        },
        "required": ["name"],
    },
    "search": {
        "description": "查找已发布产品。返回每个产品当前匹配的最高版本，含 id、kind、title、summary、author。summary 是这份产品能做什么。runtime 默认不在货架上。不把旧版本折进这一条。要看全文用 describe。",
        "properties": {
            "query": {"type": "string", "description": "名称、摘要或 id 的关键词。空着则列出货架。"},
            "kind": {"type": "string", "description": "skill、mcp、mcp+skill、knowledge、plugin；runtime 需显式传。不传则返回除 runtime 外的全部。"},
            "client": {"type": "string", "description": "适用的 AI 工具，例如 reasonix。"},
            "platform": {"type": "string", "description": "适用平台，例如 darwin-arm64。"},
            "limit": {"type": "integer", "description": "最多返回几条。不传则全部。"},
            "sort": {"type": "string", "description": "updated（默认）、title 或 id。"},
            "offset": {"type": "integer", "description": "跳过前几条，默认 0。"},
        },
    },
    "list_versions": {
        "description": "一个产品的全部已发布版本。返回 version、changes、platforms、clients 和发布时间。选定精确版本之后才能 install_plan 或 describe。不接受省略成最新。",
        "properties": {
            "id": {"type": "string", "description": "产品 id。"},
            "client": {"type": "string", "description": "只留适用于这个 AI 工具的版本。"},
            "platform": {"type": "string", "description": "只留含这个平台的版本。"},
        },
        "required": ["id"],
    },
    "resolve": {
        "description": "按 AI 工具和平台从已发布版本里选出一个。返回 candidates 和 selected。install_plan 之前用它或 list_versions 得到精确版本。平台对不上不会假装选中。",
        "properties": {
            "id": {"type": "string"},
            "client": {"type": "string", "description": "要装上去的 AI 工具。"},
            "platform": {"type": "string", "description": "这台电脑的平台。不能写 any。"},
            "version": {"type": "string", "description": "精确号或范围。不传则在匹配的已发布版本里选最高的。"},
            "allow_prerelease": {"type": "boolean", "description": "是否把预发布算进候选。默认否。"},
        },
        "required": ["id", "client", "platform"],
    },
    "describe": {
        "description": "一个精确版本的公开清单：说明、调用方式、依赖和密钥名。这是看「它能做什么」的全文。version 必填，不接受「最新」。不含密钥的值。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "精确版本号。"},
        },
        "required": ["id", "version"],
    },
    "install_plan": {
        "description": "生成给本机安装器的步骤。商店不执行这些步骤。version 必须是精确号，先 resolve 或 list_versions。平台不在该版本里则失败，不生成步骤。知识包的计划另带 post_install（knowledge_intake）；按本机是否有知识包管理插件决定是否调用，见 qilin://spec/knowledge-intake。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "精确版本号，不能省略。"},
            "client": {"type": "string"},
            "platform": {"type": "string", "description": "要装上的具体平台，不能写 any。"},
        },
        "required": ["id", "version", "client", "platform"],
    },
    "artifact": {
        "description": "已发布制品的下载地址和 sha256。不返回文件正文。技能包、知识包、插件与含脚本的 stdio MCP 是一份 zip，platform 可省略；纯远程无文件 MCP 返回 KIND_NO_ARTIFACT。地址给安装器下载，不要把正文塞进对话。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "精确版本号。"},
            "platform": {"type": "string", "description": "runtime 按平台取文件。zip 类制品可省略。"},
        },
        "required": ["id", "version"],
    },
    "report_problem": {
        "description": "向商店提交一条已发布产品版本的使用或安装问题反馈。匿名与已登录用户均可调用；返回 report_id 供后续查询处理进度。请勿在描述中粘贴真实密钥。",
        "properties": {
            "id": {"type": "string", "description": "产品 id。"},
            "version": {"type": "string", "description": "精确版本号。"},
            "category": {
                "type": "string",
                "description": "问题类别：install_failed、artifact_missing、doc_mismatch、runtime_error 或 other。",
            },
            "summary": {"type": "string", "description": "问题现象与错误码摘要，最多 2000 字。"},
            "steps": {"type": "string", "description": "可选。复现步骤与关键输出，最多 4000 字。"},
            "client": {"type": "string", "description": "可选。复现时的 AI 工具名，例如 antigravity。"},
            "platform": {"type": "string", "description": "可选。操作系统与架构，例如 darwin-arm64。"},
            "contact": {"type": "string", "description": "可选。回访联系方式（公开列表中不展示）。"},
        },
        "required": ["id", "version", "category", "summary"],
    },
    "get_report": {
        "description": "按 report_id 查询一条问题反馈的当前状态（open / triaged / resolved / rejected）与处理结论。",
        "properties": {
            "report_id": {"type": "string", "description": "report_problem 返回的反馈单号。"},
        },
        "required": ["report_id"],
    },
    "list_my_reports": {
        "description": "列出当前登录账号提交过的问题反馈与处理进展。",
        "properties": {
            "limit": {"type": "integer", "description": "最多返回几条，默认 100，上限 500。"},
            "offset": {"type": "integer", "description": "跳过前几条，默认 0。"},
        },
    },
    "list_product_reports": {
        "description": "作者查看自己命名空间下产品收到的问题反馈列表与状态/类别统计。",
        "properties": {
            "id": {"type": "string", "description": "可选。只看指定产品 id。"},
            "version": {"type": "string", "description": "可选。只看指定版本。"},
            "category": {"type": "string", "description": "可选。按类别过滤。"},
            "status": {"type": "string", "description": "可选。按 open、triaged、resolved、rejected 过滤。"},
            "limit": {"type": "integer", "description": "默认 100，上限 500。"},
            "offset": {"type": "integer", "description": "默认 0。"},
        },
    },
    "list_reports": {
        "description": "审核/管理员查看全站消费者问题分诊队列，支持按产品、品类、问题类别与状态过滤。",
        "properties": {
            "id": {"type": "string", "description": "可选。按产品 id 过滤。"},
            "kind": {"type": "string", "description": "可选。按产品 kind 过滤。"},
            "category": {"type": "string", "description": "可选。按问题类别过滤。"},
            "status": {"type": "string", "description": "可选。按状态过滤。"},
            "limit": {"type": "integer", "description": "默认 100，上限 500。"},
            "offset": {"type": "integer", "description": "默认 0。"},
        },
    },
    "triage_report": {
        "description": "审核/管理员推进反馈单状态（open → triaged → resolved / rejected），结单或驳回时必填 resolution 结论。",
        "properties": {
            "report_id": {"type": "string"},
            "status": {"type": "string", "description": "triaged、resolved 或 rejected。"},
            "resolution": {"type": "string", "description": "处理结论或修复版本号；resolved / rejected 时必填。"},
        },
        "required": ["report_id", "status"],
    },
    "validate": {
        "description": "上传前校验，不入库。打包前先 get_spec 取规范，再调这个。只传 manifest 做清单级静态校验；同时传 files（相对路径到 base64）或 skill_md（SKILL.md 原文）可在上传前做全量跨文件预检（含 SKILL.md frontmatter、入口文件、绑定脚本与密钥扫描）。返回 ok、scope、rules 和 findings（rule、path、message、severity）。还有 severity 为 error 的就按 path 和 message 改包，再调一次。ok 为 false 时不要 begin_submit。",
        "properties": {
            "manifest": {"type": "object", "description": "manifest.json 的对象。也可以是它的 JSON 文本。"},
            "files": {
                "type": "object",
                "description": "可选。相对路径到 base64 内容的映射，用于在上传前执行完整包文件预检。",
            },
            "skill_md": {
                "type": "string",
                "description": "可选。SKILL.md 的 UTF-8 原文，便于在不传完整文件树时预检 frontmatter 与清单一致性。",
            },
        },
        "required": ["manifest"],
    },
    "submit_package": {
        "description": "小包直接走 MCP 提交：传清单和 base64 文件树，服务端校验、打包、入库，不需要 CLI 或单独终端 PUT。单次文件合计上限 8MiB；更大的包用 begin_submit 拿一次性上传地址。",
        "properties": {
            "manifest": {"type": "object", "description": "manifest.json 的对象或 JSON 文本。"},
            "files": {
                "type": "object",
                "description": "相对路径到 base64 内容的映射，例如 {\"SKILL.md\": \"LS0t...\"}。",
            },
            "submit_kind": {"type": "string", "description": "new_version 或 add_platform。add_platform 只给 runtime。"},
        },
        "required": ["manifest", "files"],
    },
    "begin_submit": {
        "description": "静态校验通过后开始一次提交，返回 submission_id 和免 KEY 的一次性上传凭据（put_url，或 upload_url + upload_headers）。submit_kind 为 new_version 或 add_platform。大文件不进参数，只传 byte_size（纯远程无文件 MCP 传 0，无需 PUT 直接调 finish_submit）。校验还有错误时不要调。优先用 upload_url 配合 X-Upload-Ticket 请求头（或直接对 put_url）发送二进制 PUT，不要在命令里附带 QILIN_STORE_KEY。",
        "properties": {
            "manifest": {"type": "object"},
            "submit_kind": {"type": "string", "description": "new_version 或 add_platform。add_platform 只给 runtime。"},
            "byte_size": {"type": "integer", "description": "zip 的字节数；纯远程无文件 MCP 填 0。"},
        },
        "required": ["manifest", "byte_size"],
    },
    "begin_revise": {
        "description": "修订一条待审核或已退回的提交。和 begin_submit 一样分三段：先校验清单，拿免 KEY 一次性上传凭据（put_url 或 upload_url + upload_headers），PUT 制品，再 finish_revise。修订保持同一个 submission_id，不新开一条让审核员对不上。",
        "properties": {
            "submission_id": {"type": "string"},
            "manifest": {"type": "object"},
            "byte_size": {"type": "integer", "description": "zip 的字节数；纯远程无文件 MCP 填 0。"},
        },
        "required": ["submission_id", "manifest", "byte_size"],
    },
    "finish_revise": {
        "description": "核对修订制品并按原单替换。通过回到 pending；不通过回到 rejected。只有待审核或已退回的提交可以修订。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "finish_submit": {
        "description": "核对已经 PUT 的制品。通过则 pending，进入审核。只有外部地址在 strict 模式下当时打不开才留在 uploading（规则 DEP_EXTERNAL_UNREACHABLE，可以再调一次）。其他失败为 rejected，并带规则。把状态原样告诉用户。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "list_my_submissions": {
        "description": "自己的提交、状态和退回原因。审核退回之后先在这里读原因，改包，再 validate。",
        "properties": {},
    },
    "get_submission": {
        "description": "自己的某一条提交，含状态、退回原因和校验规则。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "withdraw": {
        "description": "撤回或取消仍为 uploading、pending 或 rejected 的提交并回收临时上传包。已发布的版本用 unpublish，不能用这个。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "list_reviews": {
        "description": "审核队列。作者的 KEY 看不见。审核不改清单，只发布或退回。",
        "properties": {"status": {"type": "string", "description": "队列目前只返回 pending。"}},
    },
    "get_review": {
        "description": "一条待审核提交的清单、校验结果和压缩包里的文件名。不是整个压缩包，这里也不能改字段。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "review_plugin": {
        "description": "记录插件审核清单。只有审核/管理员能调，不能审自己提交的插件。checklist 必须逐项为 true；记录后 approve 才放行。",
        "properties": {
            "submission_id": {"type": "string"},
            "checklist": {"type": "object", "description": "逐项为 true 的审核清单。"},
        },
        "required": ["submission_id", "checklist"],
    },
    "approve": {
        "description": "发布这条提交。new_version 成为新的已发布版本；add_platform 只给该版本加上这个平台，已有平台的文件不变。不改清单正文。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "reject": {
        "description": "退回。reason 是作者下一轮在 list_my_submissions 里能看见的原因。不在这里改正文。",
        "properties": {
            "submission_id": {"type": "string"},
            "reason": {"type": "string", "description": "作者能看见的退回原因。"},
        },
        "required": ["submission_id", "reason"],
    },
    "list_contributions": {
        "description": "发布贡献排行/明细。审核/管理员可读；不传 account_id 返回按产品数排序的贡献者，传 account_id 返回该人的已发布产品明细。",
        "properties": {
            "account_id": {"type": "string", "description": "只看某个账号的贡献明细。"},
            "limit": {"type": "integer", "description": "最多返回几条，默认 100。"},
        },
    },
    "list_audit": {
        "description": "admin 查审计记录。按 id 倒序，可按 package_id 与 action 过滤；limit 最大 500。审计只读，不改任何状态。",
        "properties": {
            "limit": {"type": "integer", "description": "默认 100，最大 500。"},
            "offset": {"type": "integer", "description": "默认 0。"},
            "package_id": {"type": "string"},
            "action": {"type": "string"},
        },
    },
    "transfer_namespace": {
        "description": "admin 把命名空间转给另一个作者。只改所有者记录，不改写历史提交与审计。namespace 是 id 第一个点之前的部分，例如 luxi。",
        "properties": {
            "namespace": {"type": "string", "description": "小写字母开头、只含字母和数字。"},
            "account_id": {"type": "string", "description": "接收者的账号 id。"},
        },
        "required": ["namespace", "account_id"],
    },
    "unpublish": {
        "description": "下架一个已发布版本。reason 必填。不会卸载已经装到别人电脑上的副本，也不会改清单。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "要下架的精确版本。"},
            "reason": {"type": "string"},
        },
        "required": ["id", "version", "reason"],
    },
}

_SHELF = (
    "栖林是技能、MCP 和知识包的货架。商店不运行它们，也不在这台服务器上执行安装。"
    "打包和上架的规范不在技能正文里。先 get_spec，name 取 upload、knowledge、skill、mcp、mcp+skill、runtime、plugin 之一；"
    "或读取资源 qilin://spec/ 加上这个名字。按返回的文本整理目录。"
    "看有什么：search 给出已发布产品的名称、摘要和当前最高版本。"
    "要看一份产品能做什么，用 describe，version 必须是精确号。"
    "安装前先 list_versions 或 resolve 选定版本，再 install_plan。"
    "install_plan 只返回步骤，交给用户本机的安装器执行。知识包的计划另带 post_install：如果本机有知识包管理插件/技能，按 qilin://spec/knowledge-intake 调用 knowledge_intake 完成入库；没有就保持目录形态。artifact 只返回下载地址和 sha256。"
    "连接用的 KEY 已经在客户端配置里，不要当成工具参数，也不要写进结果。"
)
_PACKAGE = (
    "要上架时：先 get_spec 取对应种类的规范，在本地整理目录，再 validate（推荐同时传 files 或 skill_md 做跨文件预检）。"
    "ok 为 false，或 findings 里还有 severity 为 error 的，就按 path 和 message 改包，再 validate。"
    "不要在还有错误时 begin_submit。"
    "ok 为 true 之后，8MiB 以内小包可直接用 submit_package 传清单和 base64 文件树；大包用 begin_submit 拿免 KEY 一次性上传凭据（upload_url + upload_headers 或 put_url），二进制 PUT 后再 finish_submit。"
    "不要重复调用 begin_submit 以免交成两条；半途放弃可调 withdraw 回收 uploading 记录。不要把密钥文件或密钥值打进包或命令行。"
    "把 pending、uploading 或规则原样告诉用户。"
    "退回原因在 list_my_submissions 和 get_submission 里。"
)
_REVIEW = (
    "审核只做发布、退回或下架，不改清单字段。"
    "list_reviews 看队列，get_review 看提交，approve 发布，reject 要写下作者能看见的原因，unpublish 下架已发布版本。"
    "list_reports 看消费者问题反馈队列，triage_report 推进分诊并填写结论。"
    "内容不对就退回，让作者改包再传。"
)


def handle(store: Store, token: str, message: Any, *, base_url: str, source_ip: str = "") -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(None, -32600, "请求不是 JSON-RPC")
    method = message["method"]
    if method == "notifications/initialized":
        return None
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        # 枢纽令牌验签可能要取公钥（网络）。先出锁验掉，再读取身份。
        store.prepare_token(token)
        principal = store.principal_from_token(token) if token else None
    except StoreError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32001, "message": exc.message, "data": {"rule": exc.rule}},
        }
    role = principal.role if principal else None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _ok(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "qilin-store", "version": "0.1.0"},
            "instructions": _instructions(role),
        })
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": [_tool(name) for name in _visible(role)]})
    if method == "resources/list":
        items = [_resource(name) for name in NAMES]
        items.extend(_data_resource(uri_value) for uri_value in ALL_DATA_URIS)
        return _ok(request_id, {"resources": items})
    if method == "resources/read":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        target = str(params.get("uri") or "")
        if target in ALL_DATA_URIS:
            body = data_text(target)
            if body is None:
                return _error(request_id, -32002, "没有这份规范")
            return _ok(request_id, {"contents": [{
                "uri": target,
                "mimeType": data_mime(target),
                "text": body,
            }]})
        spec_name = name_from_uri(target)
        body = text(spec_name) if spec_name else None
        if body is None:
            return _error(request_id, -32002, "没有这份规范")
        return _ok(request_id, {"contents": [{
            "uri": uri(spec_name),
            "mimeType": "text/plain",
            "text": body,
        }]})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in _SCHEMAS:
            return _fail(request_id, "RESOURCE_NOT_FOUND", "没有这个工具")
        if name not in _visible(role):
            return _fail(request_id, "ROLE_FORBIDDEN", "密钥无权做这件事")
        try:
            payload = _call(store, token, name, arguments, base_url, source_ip=source_ip)
        except StoreError as exc:
            return _fail(request_id, exc.rule, exc.message)
        except json.JSONDecodeError:
            return _fail(request_id, "SPEC_VERSION", "manifest 不是合法 JSON")
        except (KeyError, TypeError, ValueError, AttributeError, sqlite3.ProgrammingError):
            # 参数类型不对是调用方的问题，要回 JSON-RPC 错误，不能变成 HTTP 500。
            return _fail(request_id, "SPEC_VERSION", "工具参数不完整")
        return _ok(request_id, _content(payload, False))
    return _error(request_id, -32601, "没有这个方法")


def _decode_inline_files(raw_files: Any) -> dict[str, bytes]:
    if not isinstance(raw_files, dict):
        raise StoreError("CHECKSUM", "files 必须是对象")
    files: dict[str, bytes] = {}
    total = 0
    for raw_name, encoded in raw_files.items():
        if not isinstance(raw_name, str) or not isinstance(encoded, str):
            raise StoreError("CHECKSUM", "files 必须是路径到 base64 文本的映射")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise StoreError("CHECKSUM", f"{raw_name} 不是合法 base64") from exc
        total += len(content)
        if total > 8 * 1024 * 1024:
            raise StoreError("CHECKSUM", "inline 文件合计超过 8MiB，请改用 begin_submit")
        files[raw_name] = content
    return files


def _attach_upload_urls(started: dict, base_url: str) -> dict:
    root = base_url.rstrip("/")
    if started.get("put_path"):
        started["put_url"] = root + started["put_path"]
    elif "put_path" in started:
        started["put_url"] = None
    if started.get("upload_path") and started.get("upload_ticket"):
        started["upload_url"] = root + started["upload_path"]
        started["upload_headers"] = {"X-Upload-Ticket": started["upload_ticket"]}
    started.pop("put_path", None)
    started.pop("upload_path", None)
    started.pop("upload_ticket", None)
    return started


def _call(store: Store, token: str, name: str, arguments: dict, base_url: str, *, source_ip: str = "") -> dict:
    if name == "get_spec":
        spec_name = str(arguments.get("name") or "")
        body = text(spec_name)
        if body is None:
            raise StoreError("SPEC_UNKNOWN", "没有这份规范。name 取 upload、knowledge、skill、mcp、mcp+skill、runtime、plugin")
        return {"name": spec_name, "text": body}
    if name == "search":
        return store.search_page(
            str(arguments.get("query") or ""),
            kind=arguments.get("kind"),
            client=arguments.get("client"),
            platform=arguments.get("platform"),
            limit=arguments.get("limit"),
            offset=arguments.get("offset") or 0,
            sort=arguments.get("sort") or "updated",
        )
    if name == "list_versions":
        return {"versions": store.list_versions(
            arguments["id"], client=arguments.get("client"), platform=arguments.get("platform"),
        )}
    if name == "resolve":
        return store.resolve_package(
            arguments["id"],
            client=arguments["client"],
            platform=arguments["platform"],
            version=arguments.get("version"),
            allow_prerelease=bool(arguments.get("allow_prerelease")),
        )
    if name == "describe":
        return store.describe(arguments["id"], arguments["version"])
    if name == "install_plan":
        return store.make_install_plan(
            arguments["id"],
            str(arguments.get("version") or ""),
            client=arguments["client"],
            platform=arguments["platform"],
            base_url=base_url,
        )
    if name == "artifact":
        path, digest = store.artifact_file(
            arguments["id"], arguments["version"], arguments.get("platform"),
        )
        if not path.is_file():
            storage_warning(f"artifact missing on disk: {arguments['id']}@{arguments['version']}")
            raise StoreError("ARTIFACT_MISSING", "制品文件不可读")
        return {"url": _artifact_url(base_url, arguments), "sha256": digest}
    if name == "report_problem":
        return store.report_problem(
            token,
            arguments["id"],
            arguments["version"],
            category=arguments["category"],
            summary=arguments["summary"],
            steps=arguments.get("steps") or "",
            client=arguments.get("client") or "",
            platform=arguments.get("platform") or "",
            contact=arguments.get("contact") or "",
            source_ip=source_ip,
            channel="mcp",
        )
    if name == "get_report":
        return store.get_report(token, arguments["report_id"])
    if name == "list_my_reports":
        return store.list_my_reports(
            token,
            limit=int(arguments.get("limit") or 100),
            offset=int(arguments.get("offset") or 0),
        )
    if name == "list_product_reports":
        return store.list_product_reports(
            token,
            package_id=arguments.get("id") or None,
            version=arguments.get("version") or None,
            category=arguments.get("category") or None,
            status=arguments.get("status") or None,
            limit=int(arguments.get("limit") or 100),
            offset=int(arguments.get("offset") or 0),
        )
    if name == "list_reports":
        return store.list_reports(
            token,
            package_id=arguments.get("id") or None,
            kind=arguments.get("kind") or None,
            category=arguments.get("category") or None,
            status=arguments.get("status") or None,
            limit=int(arguments.get("limit") or 100),
            offset=int(arguments.get("offset") or 0),
        )
    if name == "triage_report":
        return store.triage_report(
            token,
            arguments["report_id"],
            status=arguments["status"],
            resolution=arguments.get("resolution") or "",
            channel="mcp",
        )
    if name == "validate":
        manifest = arguments["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        has_files = arguments.get("files") is not None
        has_skill_md = arguments.get("skill_md") is not None
        inline_files: dict[str, bytes] | None = None
        if has_files or has_skill_md:
            inline_files = _decode_inline_files(arguments["files"]) if has_files else {}
            if has_skill_md:
                raw_skill_md = arguments["skill_md"]
                if not isinstance(raw_skill_md, str):
                    raise StoreError("CHECKSUM", "skill_md 必须是字符串")
                inline_files.setdefault("SKILL.md", raw_skill_md.encode("utf-8"))
        findings = store.explain_manifest(token, manifest, files=inline_files)
        rules = [item["rule"] for item in findings if item["severity"] == "error"]
        return {
            "ok": not rules,
            "scope": "package" if inline_files is not None else "manifest_only",
            "rules": rules,
            "findings": findings,
        }
    if name == "submit_package":
        manifest = arguments["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        files = _decode_inline_files(arguments.get("files") or {})
        result = store.submit(
            token, manifest, files,
            submit_kind=str(arguments.get("submit_kind") or "new_version"),
            channel="mcp",
        )
        return {"submission_id": result.submission_id, "status": result.status, "rules": result.rules}
    if name == "begin_submit":
        manifest = arguments["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        started = store.begin_submit(
            token,
            manifest,
            submit_kind=str(arguments.get("submit_kind") or "new_version"),
            byte_size=int(arguments["byte_size"]),
        )
        return _attach_upload_urls(started, base_url)
    if name == "finish_submit":
        result = store.finish_submit(token, arguments["submission_id"])
        return {"submission_id": result.submission_id, "status": result.status, "rules": result.rules}
    if name == "begin_revise":
        manifest = arguments["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        started = store.begin_revise(
            token,
            arguments["submission_id"],
            manifest,
            byte_size=int(arguments["byte_size"]),
        )
        return _attach_upload_urls(started, base_url)
    if name == "finish_revise":
        result = store.finish_revise(token, arguments["submission_id"])
        return {"submission_id": result.submission_id, "status": result.status, "rules": result.rules}
    if name == "list_my_submissions":
        return {"items": store.list_my_submissions(token)}
    if name == "get_submission":
        return store.get_submission(token, arguments["submission_id"])
    if name == "withdraw":
        store.withdraw(token, arguments["submission_id"])
        return {"status": "withdrawn"}
    if name == "list_reviews":
        return {"items": store.list_reviews(token)}
    if name == "get_review":
        return store.get_review(token, arguments["submission_id"])
    if name == "review_plugin":
        return store.review_plugin(
            token, arguments["submission_id"], arguments.get("checklist") or {},
        )
    if name == "approve":
        store.approve(token, arguments["submission_id"])
        return {"status": "published"}
    if name == "reject":
        store.reject(token, arguments["submission_id"], str(arguments.get("reason") or ""))
        return {"status": "rejected"}
    if name == "list_contributions":
        return store.contributions(
            token,
            account_id=arguments.get("account_id") or None,
            limit=arguments.get("limit") or 100,
        )
    if name == "list_audit":
        return store.list_audit(
            token,
            limit=int(arguments.get("limit") or 100),
            offset=int(arguments.get("offset") or 0),
            package_id=arguments.get("package_id"),
            action=arguments.get("action"),
        )
    if name == "transfer_namespace":
        store.transfer_namespace(token, arguments["namespace"], arguments["account_id"])
        return {"status": "transferred"}
    if name == "unpublish":
        store.unpublish(token, arguments["id"], arguments["version"], str(arguments.get("reason") or ""))
        return {"status": "unpublished"}
    raise StoreError("RESOURCE_NOT_FOUND", "没有这个工具")


def _instructions(role: str | None) -> str:
    if role == "admin":
        return _SHELF + _PACKAGE + _REVIEW
    if role == "reviewer":
        return _SHELF + _PACKAGE + _REVIEW
    if role == "publisher":
        return _SHELF + _PACKAGE + "这把 KEY 不能审核。"
    return _SHELF + "上架和审核需要门户里作者或审核的 KEY。匿名可以看已发布的、读取规范，也可以用 report_problem 反馈问题。"


def _visible(role: str | None) -> tuple[str, ...]:
    if role == "admin":
        return PUBLIC + AUTHENTICATED + PUBLISHER + REVIEWER + ADMIN
    if role == "reviewer":
        return PUBLIC + AUTHENTICATED + PUBLISHER + REVIEWER
    if role == "publisher":
        return PUBLIC + AUTHENTICATED + PUBLISHER
    if role == "member":
        return PUBLIC + AUTHENTICATED
    return PUBLIC


def _resource(name: str) -> dict:
    return {
        "uri": uri(name),
        "name": name,
        "description": blurb(name),
        "mimeType": "text/plain",
    }


def _data_resource(uri_value: str) -> dict:
    return {
        "uri": uri_value,
        "name": uri_value.removeprefix("qilin://"),
        "description": data_blurb(uri_value),
        "mimeType": data_mime(uri_value),
    }


def _tool(name: str) -> dict:
    schema = _SCHEMAS[name]
    return {
        "name": name,
        "description": schema["description"],
        "inputSchema": {
            "type": "object",
            "properties": schema["properties"],
            "required": schema.get("required", []),
        },
    }


def _artifact_url(base_url: str, arguments: dict) -> str:
    pkg_id = quote(str(arguments["id"]), safe="")
    version = quote(str(arguments["version"]), safe="")
    url = f"{base_url.rstrip('/')}/v1/artifacts/{pkg_id}/{version}"
    platform = arguments.get("platform")
    if platform:
        return f"{url}?platform={quote(str(platform), safe='')}"
    return url


def _content(payload: dict, is_error: bool) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def _ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _fail(request_id: Any, rule: str, message: str) -> dict:
    return _ok(request_id, _content({"rule": rule, "message": message}, True))


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
