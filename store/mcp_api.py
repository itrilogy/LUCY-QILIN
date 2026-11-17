"""商店 MCP。JSON-RPC 的一个子集，工具按密钥角色可见，密钥不是参数。"""

from __future__ import annotations

import json
from typing import Any

from store.protocol import ALL_DATA_URIS, NAMES, blurb, data_blurb, data_mime, data_text, name_from_uri, text, uri
from store.service import Store, StoreError

PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2024-11-05")
PUBLIC = ("get_spec", "search", "list_versions", "resolve", "describe", "install_plan", "artifact")
PUBLISHER = (
    "validate",
    "begin_submit",
    "finish_submit",
    "begin_revise",
    "finish_revise",
    "list_my_submissions",
    "get_submission",
    "withdraw",
)
ADMIN = ("list_reviews", "get_review", "review_plugin", "approve", "reject", "unpublish", "transfer_namespace", "list_audit", "list_contributions")

_SCHEMAS: dict[str, dict] = {
    "get_spec": {
        "description": "读取商店下发的上架规范全文。打包之前先调这个。name 取 upload、knowledge、skill、mcp、mcp+skill、runtime、plugin。规范不在技能正文或 references 里，不要去那里找上架步骤。返回的 text 与校验器是同一套，不要另造规则。",
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
        "description": "生成给本机安装器的步骤。商店不执行这些步骤。version 必须是精确号，先 resolve 或 list_versions。平台不在该版本里则失败，不生成步骤。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "精确版本号，不能省略。"},
            "client": {"type": "string"},
            "platform": {"type": "string", "description": "要装上的具体平台，不能写 any。"},
        },
        "required": ["id", "version", "client", "platform"],
    },
    "artifact": {
        "description": "已发布制品的下载地址和 sha256。不返回文件正文。技能包是一份 zip，platform 可省略。地址给安装器下载，不要把正文塞进对话。",
        "properties": {
            "id": {"type": "string"},
            "version": {"type": "string", "description": "精确版本号。"},
            "platform": {"type": "string", "description": "runtime 按平台取文件。技能包可省略。"},
        },
        "required": ["id", "version"],
    },
    "validate": {
        "description": "只校验清单，不入库。打包前先 get_spec 取规范，再调这个。返回 ok 和 findings（rule、path、message、severity）。还有 severity 为 error 的就按 path 和 message 改包，再调一次。ok 为 false 时不要 begin_submit。",
        "properties": {
            "manifest": {"type": "object", "description": "manifest.json 的对象。也可以是它的 JSON 文本。"},
        },
        "required": ["manifest"],
    },
    "begin_submit": {
        "description": "静态校验通过后开始一次提交，返回 submission_id 和一次性 put_url。submit_kind 为 new_version 或 add_platform。大文件不进参数，只传 byte_size。校验还有错误时不要调。由安装器对 put_url 做 PUT，不要自己编上传命令。",
        "properties": {
            "manifest": {"type": "object"},
            "submit_kind": {"type": "string", "description": "new_version 或 add_platform。add_platform 只给 runtime。"},
            "byte_size": {"type": "integer", "description": "zip 的字节数。"},
        },
        "required": ["manifest", "byte_size"],
    },
    "begin_revise": {
        "description": "修订一条待审核或已退回的提交。和 begin_submit 一样分三段：先校验清单，拿一次性 put_url，PUT 制品，再 finish_revise。修订保持同一个 submission_id，不新开一条让审核员对不上。",
        "properties": {
            "submission_id": {"type": "string"},
            "manifest": {"type": "object"},
            "byte_size": {"type": "integer", "description": "zip 的字节数。"},
        },
        "required": ["submission_id", "manifest", "byte_size"],
    },
    "finish_revise": {
        "description": "核对修订制品并按原单替换。通过回到 pending；不通过回到 rejected。只有待审核或已退回的提交可以修订。",
        "properties": {"submission_id": {"type": "string"}},
        "required": ["submission_id"],
    },
    "finish_submit": {
        "description": "核对已经 PUT 的制品。通过则 pending，进入审核。只有外部地址当时打不开才留在 uploading，规则 DEP_EXTERNAL_UNREACHABLE，可以再调一次。其他失败为 rejected，并带规则。把状态原样告诉用户。",
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
        "description": "撤回仍为 pending 的提交。已发布的版本用 unpublish，不能用这个。",
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
        "description": "记录插件审核清单。只有 admin 能调，不能审自己提交的插件。checklist 必须逐项为 true；记录后 approve 才放行。",
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
        "description": "发布贡献排行/明细。只给 admin；不传 account_id 返回按产品数排序的贡献者，传 account_id 返回该人的已发布产品明细。",
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
    "install_plan 只返回步骤，交给用户本机的安装器执行。artifact 只返回下载地址和 sha256。"
    "连接用的 KEY 已经在客户端配置里，不要当成工具参数，也不要写进结果。"
)
_PACKAGE = (
    "要上架时：先 get_spec 取对应种类的规范，在本地整理目录，再 validate。"
    "ok 为 false，或 findings 里还有 severity 为 error 的，就按 path 和 message 改包，再 validate。"
    "不要在还有错误时 begin_submit。"
    "ok 为 true 之后，交整个目录用本机的 store submit。它会自己 begin_submit、PUT 和 finish_submit。"
    "不要再手动调一遍，以免交成两条。不要自己编上传命令，不要把密钥文件或密钥值打进包。"
    "把 pending、uploading 或规则原样告诉用户。"
    "退回原因在 list_my_submissions 和 get_submission 里。"
)
_REVIEW = (
    "审核只做发布、退回或下架，不改清单字段。"
    "list_reviews 看队列，get_review 看提交，approve 发布，reject 要写下作者能看见的原因，unpublish 下架已发布版本。"
    "内容不对就退回，让作者改包再传。"
)


def handle(store: Store, token: str, message: Any, *, base_url: str) -> dict | None:
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
            return _fail(request_id, "VERSION_UNAVAILABLE", "没有这个工具")
        if name not in _visible(role):
            return _fail(request_id, "ROLE_FORBIDDEN", "密钥无权做这件事")
        try:
            payload = _call(store, token, name, arguments, base_url)
        except StoreError as exc:
            return _fail(request_id, exc.rule, exc.message)
        except (KeyError, TypeError, ValueError):
            return _fail(request_id, "SPEC_VERSION", "工具参数不完整")
        return _ok(request_id, _content(payload, False))
    return _error(request_id, -32601, "没有这个方法")


def _call(store: Store, token: str, name: str, arguments: dict, base_url: str) -> dict:
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
        _body, digest = store.artifact(
            arguments["id"], arguments["version"], arguments.get("platform"),
        )
        return {"url": _artifact_url(base_url, arguments), "sha256": digest}
    if name == "validate":
        manifest = arguments["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        findings = store.explain_manifest(token, manifest)
        rules = [item["rule"] for item in findings if item["severity"] == "error"]
        return {"ok": not rules, "rules": rules, "findings": findings}
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
        if started["put_path"]:
            started["put_url"] = base_url.rstrip("/") + started["put_path"]
        started.pop("put_path", None)
        return started
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
        if started["put_path"]:
            started["put_url"] = base_url.rstrip("/") + started["put_path"]
        started.pop("put_path", None)
        return started
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
    raise StoreError("VERSION_UNAVAILABLE", "没有这个工具")


def _instructions(role: str | None) -> str:
    if role == "admin":
        return _SHELF + _PACKAGE + _REVIEW
    if role == "publisher":
        return _SHELF + _PACKAGE + "这把 KEY 不能审核。"
    return _SHELF + "上架和审核需要门户里作者或审核的 KEY。匿名可以看已发布的，也可以读取规范。"


def _visible(role: str | None) -> tuple[str, ...]:
    if role == "admin":
        return PUBLIC + PUBLISHER + ADMIN
    if role == "publisher":
        return PUBLIC + PUBLISHER
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
    url = f"{base_url.rstrip('/')}/v1/artifacts/{arguments['id']}/{arguments['version']}"
    platform = arguments.get("platform")
    if platform:
        return f"{url}?platform={platform}"
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
