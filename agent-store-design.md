# 智能体商店：设计方案与开发方案

规格版本：`store-spec/1`  
日期：2026-11-11  
修订：2026-11-12。上架规范只由商店 MCP 下发（`get_spec` 与 `qilin://spec/`）。技能正文不转载上架步骤。页面下载须先登录；安装器仍按步骤里的制品地址下载。  
状态：供实现与改造使用的约定。未写入本文的类型、工具、步骤，一律拒绝，不在实现时临场发明。

这份文档同时干三件事：

1. 约束商店本身怎么做。
2. 指导已有技能如何改成可上架的包。
3. 指导一个新的 MCP 产品如何注册。

商店是菜市场，不是厨房。它不运行上架产品，不代理产品流量，不保存产品密钥的值，不替客户端执行安装。

---

## 1. 边界

商店只有四块：

| 块 | 职责 |
|---|---|
| 目录 | 已发布产品的名称、类型、作用、作者、全部已发布版本、适用平台、适用 AI 工具 |
| 制品库 | 技能 zip、商店托管的二进制、清单 |
| 审核流 | 作者上传后处于待审核；审核发布或退回。角色在栖林用户里，第一位是审核 |
| 商店 MCP | 对 AI 工具提供查找、说明、安装步骤、上架规范、上传、审核。规范正文不写进技能文件 |

明确不做：

- 不把编排技能做成「一个技能一个 MCP 服务」。
- 不做产品调用网关，不做沙箱，不做产品密钥保管。栖林只存用户那把 KEY 的校验值，不存产品密钥的值。枢纽令牌是可选的另一条入口，不是日常登录。
- 不在商店服务器上执行依赖检测命令（服务器系统与用户电脑不同）。
- 不镜像 apt / pip / npm。商店只托管有人上传并审核过的文件。
- 管理端没有产品编辑器。改产品 = 重新打包再上传。

---

## 2. 角色与密钥

登录和商店 MCP 用两样东西，不能混。

口令由本人在创建账号时设置，用来登录门户。必填是用户名、姓名、昵称、口令、再输入一次口令。部门和岗位可以空着。口令 10 到 64 位，须同时含小写字母、大写字母和数字，首尾不能是空格，不能包含用户名（`PASSWORD_INVALID`）。两次不一致则 `PASSWORD_MISMATCH`。库里只存盐和校验值，响应不回显口令。本人可以改自己的口令，改口令不影响那把 KEY。

KEY 不由本人编，也不由管理员抄给人。创建账号时商店另生成一把随机 KEY，每人同时只有一把，只作为商店 MCP 的 Bearer。内网部署的明确取舍：`key_plain` 持久化，登录后的 `/v1/me` 和登录响应带 `store_key`，连接页每次进入都能直接显示发布配置。旧库账号没有明文时，本人重新生成一次；之后进入连接页直接显示。拿 KEY 当口令登录会被拒绝。

账号资料：用户名、姓名、昵称、部门、岗位。用户名创建后不改。昵称是货架和顶栏上的名字。本人可以改姓名、昵称、部门和岗位，不能改别人的。

用户管理是单独一页，只对**管理员**开放：改角色、停用，以及给别人重置口令。审核没有用户管理权。重置由商店生成一把新的随机口令，只在这一次响应里出现，并清掉对方的登录会话。不能自己编这把口令，不能重置自己的，也看不见原来的口令或 KEY。对方的商店 KEY 不因重置口令而改变。作者打开这一页只会看到自己不能管理。

| 谁 | 角色 | 能做 | 不能做 |
|---|---|---|---|
| 第一位创建账号的人 | `admin` 管理员 | 作者能力 + 审核、发布、退回、下架 + 用户管理、改角色、停用、重置口令、命名空间转移 | 看见原来的口令或 KEY；自己编别人的新口令；重置自己的口令；停用自己；把最后一位管理员降下去；批准自己的提交 |
| 之后自己创建账号的人 | `publisher` 作者 | 校验、上传到待审核、查看/修订/撤回自己的提交、改自己的资料和口令、重新生成自己的 KEY | 审核、发布、下架、用户管理、角色分配、命名空间转移、批准自己的提交 |
| 管理员提升的人 | `reviewer` 审核 | 作者能力 + 审核、发布、退回、下架、审计、贡献排行/明细 | 用户管理、改角色、停用、重置口令、命名空间转移、批准自己的提交 |
| 枢纽侧只读 | `member` 只读 | 浏览、读规范、取安装步骤和制品地址 | 上传、审核、发布、用户管理 |
| 未登录 | 匿名 | 浏览已发布内容、读规范、取安装步骤和制品地址 | 上传、审核、发布、下载页面动作 |

本地创建的账号默认是 `publisher`（作者），不是只读。`member` 只用于枢纽侧 viewer/外部只读身份；如果希望枢纽用户也能提交，把 `_role` 的 viewer 映射改成 `publisher`。管理员可以随时把作者提升为审核，或再提升为管理员；最后一位管理员不能被降级或停用。

约定：

- 登录是登录框里填写用户名和口令，不从名单里点人。门户随后只用会话，不把口令留在页面上。账号名单不对匿名公开。
- 左栏「连接」在生成的那一次给出能交给 AI 工具的商店 MCP 配置。配置里的 `Authorization: Bearer` 就是这把 KEY。工具参数里没有密钥字段，工具结果里不回显 KEY。作者的 KEY 和审核的 KEY 不要装在同一个 AI 工具里。
- 重新生成会作废原来的那一把，并删掉旧会话。这不是第二把。请求里若自己带了 KEY，拒收（`KEY_INVALID`）。KEY 与别人重复则拒收（`KEY_TAKEN`）。
- 用户名须以小写字母开头，3 到 32 位（`USERNAME_INVALID` / `USERNAME_TAKEN`）。姓名和昵称不能空，资料字段不超过 64 个字（`NAME_INVALID`）。
- 匿名连接可以查找已发布内容，也可以读取上架规范，不能上传。
- 下载不需要 KEY：制品地址匿名可下载，安装器也不带口令。发布、审核、用户管理和写操作需要 KEY。货架的「安装说明」在未登录时不请求安装计划；AI 工具侧的浏览、下载、使用走商店 MCP，说明在「连接」菜单。
- `GET /v1/artifacts/<id>/<version>` 仍不要求登录。安装器下载 `install_plan` 里的地址时不带口令。把这一刀切到制品地址上，会让安装步骤里的下载失败。页面动作和制品地址是两件事，不要只改文案假装 HTTP 已经要求登录。
- 清单里的 `author.display_name` 由作者填写；`author.publisher_id` 以登录身份为准。两者冲突则拒收（规则 `AUTHOR_MISMATCH`）。作者不能冒充别人。

若配置了 `QILIN_HUB_ORIGIN`，长度超过 256 的 Bearer 仍按枢纽访问令牌验签（`aud=lucy-qilin`，角色 `publisher` / `reviewer` / `admin`；其余映射 `member`）。这是实验室统一用户管理的另一条入口，不代替上面这把 KEY。枢纽令牌签不上得到 `IDENTITY_INVALID`。未配置枢纽时，不把这种长令牌当成匿名。

配置了枢纽地址，并且进程环境里有 `QILIN_HUB_SECRET` 时，枢纽可以把人重定向到 `GET /auth/callback?code=&state=`。栖林用应用密钥签名，向枢纽换访问令牌，验签后建立自己的会话。访问令牌不出现在浏览器地址里。枢纽若一并给了刷新令牌，它只留在库里，不进任何接口响应；访问令牌快过期时用它再换一次，旧的立即作废，换不到就结束这次会话。枢纽这次没给刷新令牌时，会话停在该访问令牌的过期时间，人从枢纽再进一次，不用把令牌贴进页面。这条会话不产生商店 KEY。没配应用密钥时，跳转登录拒绝（`IDENTITY_UNCONFIGURED`）。日常登录仍是口令和 KEY。枢纽登出回调会清掉这个人在栖林的跳转会话。

`state` 与这次登录请求的绑定分两种情况。配置了授权页（`--hub-authorize` 或 `QILIN_HUB_AUTHORIZE`，值是含 `{state}`、`{redirect_uri}`、`{app_id}` 占位的模板）时，商店通过 `GET /auth/start` 签发一次性 state，写进 `Path=/auth` 的 `HttpOnly` Cookie，回调必须与地址里的 state 一致才换令牌，校验通过立即清除。没配时跳转只能由枢纽自行发起，state 由枢纽决定、商店无从预知，回调仍可用但每次记 `STATE_UNBOUND`，不假装已绑定。`redirect_uri` 由商店自己的对外地址推出，不接受请求里的值。

AI 工具连接商店 MCP（`streamable-http`，`POST /mcp`）。「连接」页给出的就是这一段，地址按当时打开门户的主机填写：

```json
{
  "mcpServers": {
    "qilin": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp",
      "headers": {
        "Authorization": "Bearer <商店生成的那把 KEY>"
      }
    }
  }
}
```

握手至少包括 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`、`resources/list`、`resources/read`。`initialize` 的 `capabilities` 同时声明 `tools` 和 `resources`。服务器在响应里给出 `Mcp-Session-Id`。`GET /mcp` 返回 405，因为没有服务器主动推送。

---

## 3. 状态

```
上传完成校验
    → pending          待审核，搜索不可见
        → published    admin 令牌发布；该 version 立即不可变
        → rejected     admin 令牌退回，附原因；作者可原版本重新提交
published
    → unpublished      下架；搜索不可见；已安装的客户端不自动卸载
```

规则：

- 同一 `id` + `version` 一旦 `published`，清单正文和已发布平台上的制品都不可覆盖（`VERSION_IMMUTABLE`）。改说明、改调用方式、换掉某个平台的文件，都必须升版本。
- 同一 `id` 与它所在的命名空间（`id` 第一个点之前）归首个发布者。别人提交同一 `id` 的新版本、追加平台，或在同一命名空间下发新 `id`，`submit` 与 `approve` 都拒绝（`ROLE_FORBIDDEN`），admin 也不能代发。新命名空间不需要登记，按普通新商品处理。admin 可以用 `transfer_namespace` 把命名空间转给另一个作者；转移只改所有者记录，不改写历史提交与审计。
- 已发布版本允许**追加**尚未存在的适用平台。追加走单独的审核，不改该版本已有平台的文件，也不改清单正文。见第 3.1 节。
- `pending` 和 `rejected` 允许同一作者用同一版本重新上传，覆盖上一次未发布的制品。
- 搜索、`describe`、`list_versions`、`resolve`、`install_plan`、制品下载只对 `published` 生效。旧版本不下架就不消失，供 AI 工具挑选。
- 作者看自己的提交用另一组工具，不走公开搜索。

---

## 3.1 版本与适用平台

技能包、MCP、商店托管的二进制，都按版本发布，多个已发布版本同时存在。AI 工具不默认盲装「最新版」，先看候选再选定。

适用平台是封闭枚举，作者不能自造字符串：

| platform id | 含义 |
|---|---|
| `darwin-arm64` | macOS Apple 芯片 |
| `darwin-amd64` | macOS Intel |
| `linux-arm64` | Linux ARM64 |
| `linux-amd64` | Linux x86_64 |
| `windows-arm64` | Windows ARM |
| `windows-amd64` | Windows x86_64 |
| `any` | 与操作系统无关 |

`any` 不能和其他平台 id 混在同一版本里（`PLATFORM_MIXED`）。远程 MCP、纯提示词技能用 `any`。带本机二进制的必须写具体平台，禁止用 `any` 蒙混（`PLATFORM_TOO_BROAD`）。

这里的「平台」只指操作系统和 CPU。`platform/` 目录仍是 AI 工具适配说明，两者不要混用。适用的 AI 工具继续写在 `clients`。

`install_plan` 不接受平台 `any`。版本上写 `any`，只说明它不挑操作系统。生成安装说明时，调用方仍要给出一台具体的电脑（六种平台 id 之一）。这个选择不是作者写进版本里的字段，也不会写回产品。门户把版本表里的适用平台和「装到这台电脑」分成两处，避免把临时选择看成创建时保存的平台。

### 版本里有什么

每个已发布版本独立保存：`version`、`changes`（相对上一版改了什么，至少 10 字；第一个版本写「首个发布」）、`platforms`、`clients`、`summary`、发布时间。`list_versions` 把这些列出来，供选择。

### 两种上传

| 提交类型 | 何时用 | 审核通过后 |
|---|---|---|
| `new_version` | 新的 semver，或覆盖尚未发布的同一版本 | 多一个可选版本。技能包是一整份 zip。工具若按平台分文件，这份提交里的平台资产一并发布 |
| `add_platform` | 某个**已发布**版本要增加一个还没有的平台 | 只增加该平台的制品。清单正文必须与已发布版本一致，不一致则 `PLATFORM_SCOPE`。该平台已存在则 `PLATFORM_TAKEN` |

技能包一个版本只有一份 zip，平台是这份 zip 所声明的适用平台列表，不能对同一版本拆成多份 zip。某个平台的脚本真的不同，就另发一个版本，并在 `changes` 里写明。

MCP 与 `runtime` 这类工具按平台管理制品：一个版本可以有多份资产，每份对应一个平台。缺的平台以后用 `add_platform` 补，不必为了补一个 Windows 包去改已经发出的 macOS 文件。

### AI 工具如何选版本

工具名是 `resolve`。参数：`id`、`client`、`platform`，以及可选的 `version`（精确号或范围，如 `>=1.2.0 <2.0.0`）。

返回：

- `candidates`：所有已发布、未下架、且 `clients` 含该 AI 工具、`platforms` 含该平台或为 `any` 的版本。每项带 `changes`、平台列表、适用 AI 工具、发布时间。
- `selected`：在候选中按下面的默认规则挑一个，并给出 `reason`。AI 工具可以不接受它，改用某个候选的精确版本再调 `install_plan`。

默认规则，按顺序：

1. 去掉预发布版本，除非调用方明确要预发布，或传入的版本号本身带 `-` 预发布后缀。
2. 调用方给了精确版本：必须在候选里，否则 `VERSION_UNAVAILABLE`，`selected` 为空，候选仍返回。
3. 调用方给了范围：留下满足范围的候选。一个都不剩则同样 `VERSION_UNAVAILABLE`。
4. 在剩下的里面取 semver 最高的一个。`reason` 为 `highest_match`。
5. 不因为「最新版没有这个平台」而静默降级。最新版缺少该平台时，它根本不在候选里；若较低版本在候选里，`selected` 仍是候选中的最高版本，`reason` 写 `highest_match_platform`，让调用方看见自己没拿到绝对最新版。

依赖里的 `depends.skills`、`depends.mcp`、`depends.store` 写版本范围，不写「最新」。安装时对每一项调用同一套 `resolve`。范围写 `*` 等价于不限制，仍然会选中当前最高的匹配版本，并记在安装计划里，避免事后说不清装的是哪一版。

`depends.store` 只接受本商店的 `runtime`；技能、插件、MCP 之间的依赖分别走 `depends.skills` / `depends.mcp`，不能混写成 `depends.store`。安装计划按 `(id, version)` 对共享依赖去重，同一个依赖只展开一次；不同版本仍会同时出现，商店不做跨分支的版本统一。

---

## 4. 产品范式

根上必须有 `manifest.json`。`spec_version` 必须是 `1`。`kind` 只允许下表里的值。

| kind | 货架 | 是什么 |
|---|---|---|
| `mcp` | 上架 | 纯 MCP。安装 = 写入一条连接 |
| `skill` | 上架 | 纯技能包。安装 = 解压到技能目录 |
| `mcp+skill` | 上架 | 先装声明的 MCP，再解压技能 |
| `runtime` | 不上架 | 商店托管的依赖（二进制、字体包等）。被别的产品引用，不出现在默认搜索里 |
| `knowledge` | 上架 | 知识包。一组 Markdown 笔记。安装 = 解压到知识目录，不连接、不执行 |
| `plugin` | 上架 | DSH 插件。Host/Client 可执行代码，按 Tier A/B/C 分级。安装 = 暂存后由用户在 Plugins 确认 |

不在表里的 kind 直接拒绝（`KIND`）。再加一种，先写进这张表，并单独放一个校验模块。不把 `spec_version` 改成 2，已有产品仍是 1。

### 4.1 所有 kind 的必填项

```json
{
  "spec_version": 1,
  "id": "lab.gov-writer",
  "version": "3.2.0",
  "kind": "skill",
  "title": "公文撰写",
  "summary": "一句话，供搜索。不超过 200 字。",
  "description": "说明阐述。做什么、不做什么、交付物是什么。",
  "author": { "display_name": "某团队" },
  "platforms": ["any"],
  "changes": "首个发布",
  "clients": ["reasonix"],
  "depends": {
    "store": [],
    "external": [],
    "skills": [],
    "mcp": []
  },
  "secrets": [],
  "invoke": {}
}
```

| 字段 | 约定 |
|---|---|
| `id` | 小写，`命名空间.名字`，发布后不可改 |
| `version` | 严格 semver：`主.次.修订`，可带 `-预发布`。禁止 `3.2.0 (Agent-Driven Architecture)` 这种写法 |
| `title` | 货架上给用户看的短名。非空，不超过 40 字。不合法为 `TITLE` |
| `summary` | 搜索结果里的「作用」 |
| `description` | 说明阐述，至少 40 字。不写安装步骤，安装步骤来自结构化字段 |
| `platforms` | 适用平台，第 3.1 节的 id。远程 MCP 和纯提示词技能用 `["any"]` |
| `changes` | 这一版的变更说明。供 `list_versions` 和 AI 工具选择，不能空 |
| `clients` | 适用的 AI 工具，必须是商店客户端表里的 id |
| `secrets` | 只有名字和说明，没有值。`{ "name": "GS_API_KEY", "description": "国信接口密钥" }` |
| `invoke` | 调用形式，按 kind 不同，见下。缺了拒收 |

`platforms` 含 `any` 时，只允许远程 MCP 或没有本机二进制的技能。`runtime` 禁止 `any`（`PLATFORM_TOO_BROAD`）。清单里不再使用 `environments` 字段。

提交时服务端按登录账号写入 `author` 快照：`publisher_id`、`username`、`real_name`、`nickname`、`department`、`job_title`。清单里自填的这几项会被覆盖，防止冒用。公开货架与 `describe` 仍只输出 `author.display_name`；审核页、`get_review`、贡献页/`list_contributions` 可见完整快照。

### 4.2 商店认识的 AI 工具

作者不能自造客户端能力。商店持有一张表，v1 不在运行中改它。初始值：

| client id | 能力 |
|---|---|
| `reasonix` | `skill_dir`、`mcp_stdio`、`local_bin` |
| `claude-desktop` | `mcp_stdio`、`mcp_http` |
| `cursor` | `mcp_stdio`、`mcp_http` |
| `deepseek-harness` | `skill_dir`、`mcp_stdio`、`mcp_http`、`local_bin`、`plugin` |
| `generic` | 作者在清单里另外写 `client_caps`，不能靠它绕过校验 |

上传时做能力闭合（`CLIENT_CAPABILITY`）：

- `kind=skill` 或含技能的包：所填每个 client 必须有 `skill_dir`。因此 v1 里纯技能**不能**宣称 `claude-desktop` 或 `cursor`，除非客户端表先给他们加上 `skill_dir`。
- `transport=stdio`：client 必须有 `mcp_stdio`。
- `transport=streamable-http`：client 必须有 `mcp_http`。
- 引用了 `runtime` 或外部命令：client 必须有 `local_bin`。
- `kind=plugin`：client 必须有 `plugin`；v1 只接受 `deepseek-harness`。

这条是改造指南的硬约束：技能若只在 Reasonix 上跑，`clients` 就只写 `reasonix`，不要写成全平台通用。

### 4.3 `kind=mcp` 纯 MCP

必填 `mcp` 与 `invoke`：

```json
{
  "kind": "mcp",
  "mcp": {
    "transport": "streamable-http",
    "url": "https://mcp.example.internal/market"
  },
  "invoke": {
    "form": "list_tools",
    "tools": [
      { "name": "query_quote", "summary": "查询一只证券的实时行情" }
    ]
  }
}
```

`stdio` 则改为 `"command"` + `"args"`，不要 `"url"`。

| 规则 | id |
|---|---|
| `transport` 只能是 `stdio` 或 `streamable-http` | `MCP_TRANSPORT` |
| http 必须有 `https` URL；stdio 必须有命令 | `MCP_CONNECT` |
| `invoke.form` 为 `list_tools`（连上后向服务器要工具列表）或 `documented`（清单里写清工具名和一句话） | `MCP_INVOKE` |
| 远程 URL 且无本机依赖时，`platforms` 才能是 `["any"]` | `PLATFORM_TOO_BROAD` |

调用形式写在 `invoke` 里，不写在一段散文里。官方注册中心的 [`server.json`](https://github.com/modelcontextprotocol/registry) 字段与这里的对应关系：`remotes[]` → `mcp.url`；`packages[]` → 若命令来自 npm/PyPI，把 `command`/`args` 写成 `npx` 或 `uvx` 的具体参数。商店仍然自己存这份清单，不把发布委托给公网注册中心。

纯远程 MCP 可以只上传 `manifest.json`，没有 zip。

### 4.4 `kind=skill` 纯技能

zip 根目录：

```
manifest.json
SKILL.md
references/          可选
scripts/             可选
platform/            可选，且必须覆盖 clients 里的每一个工具
```

```json
{
  "kind": "skill",
  "skill": { "entry": "SKILL.md" },
  "invoke": {
    "form": "load_skill",
    "entry": "SKILL.md",
    "note": "由客户端按技能加载，不建立 MCP 连接。"
  }
}
```

| 规则 | id |
|---|---|
| zip 内存在 `skill.entry`，且是 `SKILL.md` | `SKILL_ENTRY_MISSING` |
| `invoke.form` 必须是 `load_skill` | `SKILL_INVOKE` |
| `clients` 中每个工具都能装技能 | `CLIENT_CAPABILITY` |
| 声明了 `platform/<client>.md` 时，文件必须在 | `PLATFORM_FILE_MISSING` |
| 技能目录里不得有密钥文件或密钥样式 | `SECRET_FILE` / `SECRET_PATTERN` |

不进包：`工程过程/`、`.models/`、`.git/`、`.DS_Store`、`SECRET.md`、`credentials.md`、`**/*key*.md`。模型权重若真要分发，单独做成 `runtime`，不要塞进技能 zip。

### 4.5 `kind=mcp+skill`

同时满足 4.3 和 4.4。`depends.mcp` 列出要先安装的**本商店已发布** MCP 产品（`id` + 版本范围）。也可以在本包内嵌一个 `mcp` 块，表示连接定义随这个包一起发布。

安装顺序固定，作者不能改：商店托管依赖 → 外部依赖检查 → `depends.skills` → `depends.mcp` → 本包的 `mcp_add` → 解压技能 → 索要密钥 → 验证。

`invoke.form` 必须是 `load_skill_then_mcp`：先加载技能，技能再去调用那些 MCP 工具。

### 4.6 `kind=runtime` 商店托管的依赖

二进制和其他文件依赖走同一套作者、版本、说明、环境、审核。区别只是不上默认货架。

```json
{
  "spec_version": 1,
  "id": "lab.officecli",
  "version": "1.4.0",
  "kind": "runtime",
  "title": "officecli",
  "summary": "本地改 Office 文件的单文件命令。",
  "description": "按系统放置到客户端的 bin 目录，技能通过命令名调用。",
  "author": { "display_name": "某团队" },
  "platforms": ["darwin-arm64", "darwin-amd64", "windows-amd64"],
  "changes": "首个发布",
  "clients": ["reasonix"],
  "runtime": {
    "bin_name": "officecli",
    "assets": [
      {
        "platform": "darwin-arm64",
        "path": "darwin-arm64/officecli",
        "sha256": "<64 hex>"
      }
    ]
  },
  "invoke": { "form": "exec_bin", "bin_name": "officecli" }
}
```

`new_version` 提交里，每个 `platforms` 条目都必须有对应 asset（`RUNTIME_ASSET`）。只准备先发一个平台时，`platforms` 里就只写这一个，其余用以后的 `add_platform` 补。`sha256` 由上传应用重新计算，与清单不一致则拒收（`CHECKSUM`）。`clients` 仍要填：这个二进制准备被哪些 AI 工具使用，且那些工具必须有 `local_bin`。

`runtime` 可以被搜索到，但只有调用方显式 `kind=runtime` 时才返回，避免和技能、MCP 混在默认货架里。

### 4.7 非商店托管的依赖

商店不保存安装包，但作者必须给出可检测的安装指南和资源地址。上传应用会检查这些字段，并请求这些地址。

```json
"depends": {
  "external": [
    {
      "name": "pandoc",
      "version_range": ">=3.0",
      "detect": ["pandoc", "--version"],
      "install_guide": "从下面的地址安装对应系统的安装包，装好后保证命令 pandoc 在 PATH 中。",
      "resources": [
        {
          "title": "Pandoc 发布页",
          "url": "https://github.com/jgm/pandoc/releases",
          "platform": "any"
        }
      ]
    }
  ]
}
```

| 检查 | 做法 | 规则 |
|---|---|---|
| 结构 | `name`、`version_range`、`detect`、`install_guide`、`resources` 都有；`detect` 是字符串数组，不是一条 shell | `DEP_GUIDE_MISSING` |
| 指南 | `install_guide` 至少 20 字，并且包含依赖名称 | `DEP_GUIDE_MISSING` |
| 地址 | 每个 `url` 必须是 `https` | `DEP_EXTERNAL_URL` |
| 可达 | `finish_submit` 对每个 URL 做 HEAD，失败再 GET，超时 10 秒；2xx/3xx 才算通过。打不开不退回、不删制品，提交留在 `uploading`，同一条可以再调一次。这次检查不占着商店的锁 | `DEP_EXTERNAL_UNREACHABLE` |
| 平台覆盖 | 产品声明的每个平台，至少有一条 `platform` 匹配或为 `any` 的 resource | `DEP_RESOURCE_GAP` |

服务器**不运行** `detect`。检测发生在用户电脑上，是安装步骤 `ensure_bin` 的事。上传时的「检测」指：指南和地址齐、地址当时打得开。

`depends.store` 里的每一项必须是已发布的 `runtime`（或同一待审核批次里、将被同时发布的 runtime）。引用未发布 id 则拒收（`DEP_STORE_UNPUBLISHED`）。  
`depends.skills` / `depends.mcp` 同样必须指向已发布且 kind 匹配的产品。

### 4.8 密钥扫描

对 manifest 全文和 zip 内文本文件：

- 文件名命中 `secret`、`credential`、`.env`、`*key*.md`（`SKILL.md` 除外）→ `SECRET_FILE`。
- 内容命中常见密钥样式（`sk-` 后接长串、`Bearer ` 后接长串、私钥块）→ `SECRET_PATTERN`。
- `secrets[]` 里出现疑似键值而不是变量名 → `SECRET_PATTERN`。

扫描是拒收，不是打码后放行。

### 4.9 `kind=knowledge` 知识包

知识包是一组笔记，不是技能，也不是 MCP。笔记可以由任何技能、任何工具或人工生产；商店不登记生产者，也不运行生产技能。上架时只检查这组笔记能不能按协议闭合：分类、标签、密级、入口、链接、命名与大小。

规范以商店 MCP 下发的数据资源为准：`qilin://vocab/knowledge`（分类）、`qilin://tags/knowledge`（标签）、`qilin://metadata/knowledge`（字段与流程）、`qilin://schema/knowledge`（题头 schema），另有 `qilin://checklist/knowledge` 与 `qilin://example/knowledge`。散文说明见 `qilin://spec/knowledge`。校验器与这些资源同源。

包内只有根上的 `manifest.json` 和 `NN-*.md`（`00` 为入口，`01`–`99` 为正文）。不设子目录，不收图片、`SKILL.md`、`README.md` 或其他文件（`NOTE_FILE`）。单篇不超过 256KiB，整包不超过 8MiB。适用平台只写 `["any"]`；`depends` 四项与 `secrets` 为空（`KNOWLEDGE_SCOPE`）。

```json
"knowledge": {
  "entry": "00-导航概览.md",
  "taxonomy": {
    "business_category": "knowledge-ops",
    "business_domain": "method",
    "knowledge_kind": "guide"
  },
  "security_level": "internal",
  "scope": "work",
  "vocabulary": "kv/1",
  "topics": ["本地主题"],
  "producer": "知识分解技能"
},
"invoke": { "form": "read_notes", "entry": "00-导航概览.md" }
```

包级字段：

| 字段 | 要求 |
|---|---|
| `entry` | 必填。必须是 `00-*.md`，且那一篇的 `type` 是 `map_of_content`（`NOTE_ENTRY`） |
| `taxonomy` | 必填三段。取值必须在词表内；`business_domain` 必须属于所选 `business_category`（`KNOWLEDGE_TAXONOMY`） |
| `security_level` | 必填。`public`、`internal`、`restricted`、`confidential`（`NOTE_SECURITY`） |
| `scope` | 可选。`enterprise` 或 `work`，默认 `enterprise` |
| `vocabulary` | 可选。当前只支持 `kv/1` |
| `language`、`license` | 可选。取值见 `qilin://metadata/knowledge` |
| `topics` | 可选。最多 10 个本地主题词，供 `tags` 的 `topic/` 使用 |
| `producer` | 可选。技能 id、工具名或「人工」，只作溯源，不参与校验 |

每篇笔记开头是 YAML 题头（`NOTE_FRONTMATTER`）：

| 字段 | 要求 |
|---|---|
| `type` | `reference_note`、`guide_note`、`tutorial_note`、`reflection_note`、`map_of_content` 之一 |
| `status` | `evergreen` 或 `seedling` |
| `created`、`updated` | `YYYY-MM-DD`，`updated` 不早于 `created` |
| `tags` | 数组，1–12 个，至少一个 `topic/`。写成 `topic/`、`entity/`、`method/`、`source/` 前缀；`topic` 只认词表或本包 `topics`，`method` 与 `source` 是封闭词表，`entity` 开放 |
| `aliases` | 可省略。写了就是数组，最多 8 个；链接不指向别名 |
| `prerequisite` | 可省略。没有前置时写 `无`；有前置时写成数组 |
| `summary`、`source` | 可省略。`summary` 最多 200 字，`source` 最多 500 字 |
| `domain`、`knowledge_kind` | 可省略。`domain` 必须在包级类目下；`knowledge_kind` 必须与 `type` 对应 |

链接只认 `[[篇名]]`、`[[篇名\|显示]]`、`[[篇名#标题]]`，篇名是文件名去掉 `.md`。每一条都必须对上包里的一篇（`NOTE_LINK`）；带 `#标题` 时，目标篇必须真有这个标题。别名不算篇名。代码块与行内代码里的例子不查。没有任何入链的篇（入口除外）记 warning `NOTE_ORPHAN`，不阻断。商店不检查文章好不好、标签够不够细；那些留在生产技能里。

安装步骤与技能包相同的下载和解压，目录是 `${KNOWLEDGE_ROOT}/<id>/<version>`，然后核对入口文件在。不列工具，不写连接。环境变量 `QILIN_KNOWLEDGE_ROOT`。

### 4.10 知识树

`knowledge.taxonomy` 必填后，逻辑路径固定为 `<business_category>/<business_domain>/<knowledge_kind>/<id>/<version>`。三段不合法时不生成计划（`KNOWLEDGE_TAXONOMY`），不再退回 `_unclassified`。

`knowledge.knowledge_map` 可选。节点若指向包内不存在的笔记，且正在检查文件，则是 `KNOWLEDGE_MAP`。不写不算错误。

安装计划的 `unpack` 步骤可以带 `tree_dest`，值是上面的逻辑路径，前面仍是 `${KNOWLEDGE_ROOT}`。当前安装器只认 `dest`，必须忽略 `tree_dest`。`dest` 继续是 `${KNOWLEDGE_ROOT}/<id>/<version>`。在单独决定改安装目录之前，不要把文件解到分类路径上。

这不是图数据库，也不另做一套存储。包内仍然不能有子目录（`NOTE_FILE`）。

---

### 4.11 `kind=plugin` 插件

DSH 的 Host/Client 代码。配置式 bundle（只插 MCP 连接、没有代码）仍走 `kind=mcp`，不算插件。

| 字段 | 约定 |
|---|---|
| `plugin.tier` | `A`（零 npm 运行时依赖）、`B`（白名单纯 JS 依赖）、`C`（原生模块，当前拒绝） |
| `plugin.form` | `host` / `client` / `both` |
| `plugin.entry` | host/both 的 Host 入口，必须出现在 `plugin.files` |
| `plugin.client` | client/both 的 Client 入口，必须出现在 `plugin.files` |
| `plugin.patch` | `cordis.patch.yml`，必须存在 |
| `plugin.files` | 运行文件白名单 |
| `plugin.dsh` | DSH 版本范围，例如 `>=1.5 <2` |
| `plugin.capabilities` | 审核对照用，不是权限系统：`tools`、`client-slots`、`commands`、`services`、`network`、`credentials:read`、`credentials:write`、`filesystem:read`、`filesystem:write`、`process:spawn`、`session:read`、`session:write` |
| `plugin.external` | 需要的外部命令名；写了就要有 `process:spawn` |

包内 `package.json` 的 `version` 必须与清单一致；`scripts` 必须为空；Tier A 不允许 `dependencies` / `optionalDependencies`；Tier B 的 `dependencies` 只能是白名单里的纯 JS 包（当前白名单为空）；`peerDependencies` 只允许 `@deepseek-ai/*`；不允许 `.node` / `binding.gyp`。

`platforms` 只能是 `["any"]`；`clients` 只能是 `["deepseek-harness"]`；`invoke.form` 固定 `install_plugin`。

安装计划：`download` → `check_bin`（每个外部命令）→ `plugin_stage`（解压到 `${PLUGIN_STAGING}/<id>/<version>`，核对 `package.json`，不执行代码）→ `plugin_confirm`（打印路径与安装说明）→ `verify`。商店和参考安装器都不调用 `plugin_manager`；实际安装由用户在 DSH Plugins 里确认。

Tier A 允许 `publisher` 提交，Tier B 只接受 admin/白名单作者，Tier C 拒绝。`approve` 增加 `SELF_REVIEW`：任何人不能批准自己提交的产品（不只 plugin）。 `approve` 前必须先调 `review_plugin` 记录审核清单；没有记录返回 `PLUGIN_REVIEW_REQUIRED`。清单项见 `PLUGIN_REVIEW_ITEMS`，必须逐项为 true。

## 5. 商店 MCP

一个 MCP 服务，传输 `streamable-http`。工具按这把 KEY 的角色自动可见：匿名和 `member` 看不到上传和审核工具；`publisher` 看不到审核工具；`reviewer` 可见上传 + 审核；`admin` 全部可见。不要把令牌当成工具参数。

`initialize` 返回 `instructions`。四种角色都先被告知：打包规范不在技能正文里，先 `get_spec`，或读取资源 `qilin://spec/<name>`。匿名的说明里不出现上传工具的名字。作者还会被告知：按规范整理目录，再 `validate`，有 error 就按 `path` 和 `message` 改包，`ok` 为 true 再用本机的 `store submit` 交目录，不要在还有错误时 `begin_submit`，也不要在 `store submit` 之外再手动交一次。审核还会被告知只发布、退回或下架，不改清单。工具描述写明何时调用、什么不能做、结果是什么。商店仍不执行安装。

规范正文只在 `store/protocol.py`。`get_spec` 和 `resources/read` 返回同一段文字。与第 4 节不一致时，以校验器为准，并改这段文字，不要在技能文件里再存一份。

### 5.1 工具

公开（匿名或任意密钥，只读已发布）：

| 工具 | 作用 |
|---|---|
| `get_spec` | 参数：`name`，取 `upload`、`knowledge`、`skill`、`mcp`、`mcp+skill`、`runtime`、`plugin`。返回该规范全文。未知名字为 `SPEC_UNKNOWN`。打包之前先调。匿名也能调 |
| `search` | 参数：`query`、`kind`（可选，含 `knowledge`）、`client`（可选）、`platform`（可选）、`limit` / `offset`（可选，分页）。走 `catalog_index`，返回 id、kind、title、summary、author、**最新匹配版本**，以及 `total`、`limit`、`offset`。不把旧版本折掉 |
| `list_versions` | 参数：`id`、`client`（可选）、`platform`（可选）。返回该产品每个已发布版本的 version、changes、platforms、clients、发布时间 |
| `resolve` | 参数：`id`、`client`、`platform`、`version`（可选，精确号或范围）。按第 3.1 节返回 `candidates` 和 `selected`。技能包和工具都走这一步 |
| `describe` | 参数：`id`、`version`（必填，不接受「最新」这种省略）。返回该版本的公开清单：只含商店认识的顶层字段，`author` 只留 `display_name`。商店还不认识的顶层字段不出现在这里，审核单里仍保留 |
| `install_plan` | 参数：`id`、`version`（必填精确号）、`client`、`platform`。版本必须先由 `resolve` 或调用方选定。平台不在该版本里返回 `PLATFORM_MISMATCH`，不生成步骤 |
| `artifact` | 参数：`id`、`version`、`platform`。返回该平台制品的短期 URL 和 sha256。`any` 的技能包忽略具体平台、返回那一份 zip |

`publisher`（作者；`reviewer`/`admin` 继承）：

| 工具 | 作用 |
|---|---|
| `validate` | 只传 `manifest.json`。不入库。返回 `ok`、`rules` 和 `findings`（`rule`、`path`、`message`、`severity`）。还有 error 就改包再调，不要上传 |
| `begin_submit` | 参数：`submit_kind`（`new_version` 或 `add_platform`）、清单文本、制品字节数、本次涉及的 `platforms`。`add_platform` 还要传已发布的 `version`。通过静态校验后返回 `submission_id` 和带一次性 `ticket` 的 PUT 地址。大文件不进工具参数 |
| `finish_submit` | 参数：`submission_id`。应用核对校验和、扫密钥、请求外部依赖 URL。通过则状态变为 `pending`。只有地址当时打不开时仍是 `uploading`，带 `DEP_EXTERNAL_UNREACHABLE`，可以再调一次。其他失败为 `rejected` 并带规则 id，不进入审核队列 |
| `begin_revise` | 参数：`submission_id`、清单、制品字节数。只对待审核或已退回的提交；静态校验通过后返回带一次性 `ticket` 的 PUT 地址。修订不新开提交，保持同一个 `submission_id` |
| `finish_revise` | 参数：`submission_id`。核对修订制品并按原单替换；通过回到 `pending`，不通过回到 `rejected` |
| `list_my_submissions` | 自己的提交和状态、退回原因 |
| `get_submission` | 自己的某一条，含校验报告 |
| `withdraw` | 撤回仍为 `pending` 的提交 |

`reviewer`（审核）与 `admin`（管理员）：

| 工具 | 作用 |
|---|---|
| `list_reviews` | 参数：`status`，默认 `pending` |
| `get_review` | 清单、校验报告、zip 文件清单（不是整个压缩包内容） |
| `review_plugin` | 参数：`submission_id`、`checklist`（逐项为 true）。审核/管理员可用；不能审自己提交的插件。记录审核人与制品 sha256；`approve` 插件前必须有这条记录。 |
| `approve` | `new_version` 变为一个新的已发布版本；`add_platform` 只给该版本加上这个平台。已有平台的文件不变 |
| `reject` | 参数：`submission_id`、`reason`。作者能在 `list_my_submissions` 里看见原因 |
| `unpublish` | 已发布版本下架，须填写原因 |
| `transfer_namespace` | 仅管理员：把命名空间转给另一个作者。只改所有者记录，不改写历史提交与审计 |
| `list_contributions` | 贡献排行/明细。审核/管理员可用；不传 `account_id` 返回排行，传则返回该账号的已发布产品明细 |
| `list_audit` | 读审计记录。按 id 倒序，可按包与动作过滤；只读 |

`runtime` 的上传和审核用同一组工具，不另做一套。规范也走 `get_spec`，不另做工具。

资源与工具读的是同一份正文，匿名可见：

| URI | 对应 |
|---|---|
| `qilin://spec/upload` | 上架顺序 |
| `qilin://spec/knowledge` | 第 4.9 节 |
| `qilin://spec/skill` | 第 4.4 节 |
| `qilin://spec/mcp` | 第 4.3 节 |
| `qilin://spec/mcp+skill` | 第 4.5 节 |
| `qilin://spec/runtime` | 第 4.6 节 |

知识包另有六条数据资源，与上面的散文规范分开：

| URI | MIME | 内容 |
|---|---|---|
| `qilin://schema/knowledge` | `application/schema+json` | 笔记题头 JSON Schema |
| `qilin://vocab/knowledge` | `application/json` | 分类词表：类目、领域、形态、密级、范围 |
| `qilin://tags/knowledge` | `application/json` | 标签词表：topic、entity、method、source |
| `qilin://metadata/knowledge` | `application/json` | 字段字典与十阶段生产流程 |
| `qilin://checklist/knowledge` | `text/markdown` | 由流程生成的自检清单 |
| `qilin://example/knowledge` | `application/json` | 最小合格包（清单 + 三篇笔记） |

`resources/list` 列出这十二条。`resources/read` 的参数是 `uri`。没有这份规范时，JSON-RPC 错误码 `-32002`，不返回别的正文。数据资源与校验器读同一份文件；改词表要升 `version`，并同时更新第 12 节的规则表。

### 5.2 上传时序

AI 工具侧约定这样做，模型不自己编造 curl，也不把步骤抄进技能：

1. `get_spec`，`name` 先取 `upload`，再取要交的种类。按返回的文本整理目录。
2. 调 `validate`。`ok` 为 false 就按 `findings` 改包，再调，直到没有 error。不要上传。
3. 在栖林仓库根执行 `python -m store.cli submit <目录>`。这条命令自己做 `begin_submit`、PUT、`finish_submit`。不要再手动调一遍。
4. 钥匙放在环境变量 `QILIN_STORE_KEY`，不进命令行，不进输出，不写进笔记。地址用 `QILIN_STORE_URL`，不设则是 `http://127.0.0.1:8787`。没有钥匙时退出码 2，命令不生成钥匙。
5. 把 `pending` 或失败规则原样告诉用户。
6. 持有审核 KEY 的人在自己的客户端里调 `list_reviews` → `get_review` → `approve` 或 `reject`。作者的 KEY 不能发布。

修订一条 `pending` 或 `rejected` 的提交走另一条三段：`begin_revise`（静态校验、拿 PUT 地址）→ PUT 到 `/v1/uploads/revise/<submission_id>` → `finish_revise`。不要复用 `begin_submit`，否则会新开一条让审核员对不上。

PUT 地址一次性、短时有效、只能写这一份制品；URL 里的 `ticket` 是短期能力票据，不是用户 KEY。独立终端不需要继承 MCP 的 `Authorization`，也不需要设置 `QILIN_STORE_KEY`；ticket 用一次即失效。MCP 工具结果里不包含用户密钥。

### 5.3 审核时序

管理端不改清单字段。审核只做三选一：发布、退回、之后下架。内容不对就退回，作者改包再传。这样管理端才能保持「只上传、只审核」，没有编辑器。

退回原因会进入作者的 `get_submission`，供他的 AI 工具下一轮修改时读取。

### 5.4 知识分解技能如何交包

知识分解技能写的是笔记，不是商店里的一种运行方式。商店不调用它，也不读它的阶段说明。上架说明的范式由商店 MCP 提供，不写在 `SKILL.md`，也不写在 `references/`。曾经放在 `references/qilin-upload.md` 的那一份已经撤掉。

用户要上架时，AI 工具已经连着商店 MCP，就这样做：

1. `get_spec`，`name` 为 `upload`，再 `name` 为 `knowledge`。或 `resources/read`，URI 为 `qilin://spec/upload` 与 `qilin://spec/knowledge`。两处正文相同。
2. 按返回的文本在笔记目录根上写 `manifest.json`。`id` 由用户定，不能用技能自己的登记 id `luxi.knowledge-decomposer`。入口优先 `00-导航概览.md`。`_知识图谱总览.md` 可以留在包里，但不是默认入口。
3. 执行 `python -m store.cli submit <笔记目录>`。本地校验失败不访问网络。通过之后才走 `begin_submit`、PUT、`finish_submit`。
4. `pending` 把 `submission_id` 告诉用户。失败把 `rules` 和 `message` 原样告诉用户。没有钥匙就停，不生成钥匙。
5. 用户没说要上架，技能停在笔记目录，不自行上传。写作阶段（题头、双链、孤篇、标签）仍由技能自己做。商店不把那些写成拒收规则。

`store submit` 不限知识包。目录里是什么 kind，就按那种校验。交之前都先取对应的 `get_spec`。

---

## 6. 安装步骤

调用 `install_plan` 之前必须先有精确版本：AI 工具先 `list_versions` 或 `resolve`，可以改选候选里的另一版，然后把该版本号传进来。`install_plan` 不自己把「最新」当成省略。返回 JSON，不是 Markdown。人类可读说明在 `describe.description`、该版本的 `changes` 和外部依赖的 `install_guide` 里。AI 工具把步骤交给本地安装器执行。安装器只认识下列 `op`，遇到未知 `op` 必须停止。

| op | 含义 |
|---|---|
| `download` | `url`、`sha256`、`dest`。校验失败即停 |
| `unpack` | 解压。技能到 `${SKILLS_ROOT}/<id>/<version>`。知识包仍到 `${KNOWLEDGE_ROOT}/<id>/<version>`。步骤上可能带 `tree_dest`，当前安装器必须忽略它，只使用 `dest` |
| `place_bin` | 把 runtime 资产放到 `${BIN_DIR}`，文件名为 `bin_name` |
| `mcp_add` | 写入一条抽象连接：id、transport、command/args 或 url、密钥变量名。不写具体客户端的配置文件路径 |
| `ensure_bin` | 在本机执行 `detect`。成功才继续。失败则把 `install_guide` 和 `resources` 展示出来并停止，不尝试自动安装 |
| `ensure_product` | 已弃用。依赖在计划里直接展开成具体步骤，不再发出这个标记；安装器遇到旧计划只打印，不重复安装 |
| `need_secret` | 向用户要这个变量的值，写入客户端自己的密钥存储。商店不接收这个值 |
| `verify` | 技能：入口文件存在。MCP：连接并列出工具。插件：暂存目录里的 entry/patch 存在。两者都有则都做。这一步带 `transport`，以及 `url` 或 `command` / `args`，不带密钥的值 |
| `check_bin` | 插件声明的外部命令是否在 PATH。失败即停，不自动安装 |
| `plugin_stage` | 插件专用。解压到 `${PLUGIN_STAGING}/<id>/<version>`，核对 `package.json` 的 version 与 entry/client/patch；**不执行代码** |
| `plugin_confirm` | 打印暂存路径与「在 DSH Plugins 里安装」的说明，返回 0；不调用 `plugin_manager` |

生成顺序：

1. `depends.store`：对每个依赖按 `platform` 做 `resolve`，选中版本后 `download` 该平台资产 + `place_bin`。该版本没有这个平台 → 计划失败，规则 `PLATFORM_MISMATCH`。
2. `depends.external`：每项一条 `ensure_bin`。
3. `depends.skills`、`depends.mcp`：每项递归展开成具体步骤（检测环，`DEP_CYCLE`），同一 `(id, version)` 只展开一次，不发出 `ensure_product` 标记。
4. 本包若有 MCP：`mcp_add`。
5. 本包若有技能：`download` + `unpack`。
6. `secrets`：每项一条 `need_secret`。
7. 插件：`download` → `check_bin` → `plugin_stage` → `plugin_confirm` → `verify`。商店只做暂存与静态核对。
8. 一条 `verify`。MCP 先 `initialize`，再 `tools/list`。连不上、超时，或结果里没有工具列表，这一步失败，后面的步骤不执行。商店服务器不跑这一步，只把它写进安装说明。

客户端安装器负责把 `${SKILLS_ROOT}`、`${BIN_DIR}` 和 `mcp_add` 落到自己的真实路径。商店不为每个 AI 工具写一份路径。

已下架的版本不生成计划。范围依赖若解析不到已发布版本，步骤失败并写明缺哪一个 id。

---

## 7. 如何改已有技能

目标是让校验器对 zip 返回零规则，而不是改写技能正文的业务步骤。编排类保持 `kind=skill`，不要改成 MCP。

对每一个准备上架的技能目录：

1. 选定 `id` 和严格 semver，并写 `changes`。原 frontmatter 里的文字版本号只留在正文，不作为商店版本。纯提示词技能的 `platforms` 写 `["any"]`；脚本只在某些系统能跑，就写具体平台 id。以后改行为就发新版本，不要覆盖已发布版本。补一个二进制平台用 `add_platform`，不要为此升技能包版本。
2. 在根上写 `manifest.json`，`kind=skill`，`invoke.form=load_skill`。
3. `clients` 只填真实跑得起来的工具。目录里已有 `platform/reasonix-code.md` 的，v1 填 `reasonix`，并在 `platform/reasonix.md` 留适配说明，或在清单里把平台文件名写成实际文件名（实现时 `platform` 映射表固定：`reasonix` → `platform/reasonix.md` 或 `platform/reasonix-code.md`，两名都接受）。
4. `description` 写清交付物和不做什么。搜索用的 `summary` 从现有 frontmatter 的 `description` 收成一句。
5. 删掉密钥文件。密钥改为 `secrets[].name`。脚本只读环境变量。禁止再写「把密钥告诉我，我帮你存进 memory.md」。
6. 本机命令逐个登记。商店愿意托管并审核的（例如自研单文件二进制）做成 `runtime`，技能用 `depends.store` 引用。商店不托管的（pandoc、ffmpeg、rclone）做成 `depends.external`，写 `detect`、安装指南、https 地址。
7. 技能依赖别的技能或 MCP 时写入 `depends.skills` / `depends.mcp`，不要只在正文里点名。
8. 删除或排除 `工程过程/`、`.models/`、`.DS_Store`。大文件走 `runtime`。
9. 本地运行 `store validate <dir>`，直到没有规则 id，再让 AI 工具用用户密钥 `begin_submit` / `finish_submit`。

数据查询类若仍是「本地脚本 + 远程 HTTP」，登记为 `skill`，密钥进 `secrets`。若已经有独立的远程 MCP 服务，登记为 `mcp`，`invoke.tools` 写清工具名。由作者选 kind，商店不根据目录名猜测。

改文件类：技能包是 `skill` 或 `mcp+skill`；二进制是 `runtime`。不要把二进制打进技能 zip 冒充无依赖。

---

## 8. 如何注册 MCP

1. `kind=mcp` 或 `mcp+skill`。
2. 写清 `transport` 和连接。内部地址可以使用内网 https；上传时的可达性检查从商店服务器发起，内网地址必须在商店服务器上打得开，否则 `DEP_EXTERNAL_UNREACHABLE` 同类的连接检查失败（规则 `MCP_URL_UNREACHABLE`，只对 http 传输做 HEAD）。
3. `invoke` 至少列出工具名和一句话，或明确 `form=list_tools` 且审核时管理端能连上并看到工具（`approve` 前可选跑一次列出工具，失败则不允许发布，规则 `MCP_LIST_FAILED`）。v1 把这次连接检查放在 `approve`，不放在作者上传：作者上传时服务器未必持有该 MCP 的业务密钥。
4. 业务密钥放在 `secrets`，不要放进 `url` 的查询串。`url` 带 `key=`、`token=` 直接 `SECRET_PATTERN`。
5. `clients` 按传输方式填写，并满足第 4.2 节的能力表。
6. 没有技能文件就不要选 `mcp+skill`。

---

## 9. 数据模型

v1 一个进程、一个 SQLite、一个本地制品目录。不要先上数据库集群。

- `accounts`：id、username、display_name（与昵称相同，供旧读法）、real_name、nickname、department、job_title、role（`member` | `publisher` | `reviewer` | `admin`）、password_salt、password_hash、key_salt、key_hash、key_prefix、key_plain、disabled_at。内网策略下 `key_plain` 保留 KEY 明文；口令仍只存哈希。备份与数据库文件必须按密钥材料保护
- `sessions`：门户登录会话的哈希和过期时间。会话不是 KEY
- `principals`：只留给测试夹具。提交上的 `publisher_id` 是账号 id；若用的是枢纽令牌，则是令牌的 `sub`
- `submissions`：id、package_id、version、submit_kind（`new_version` | `add_platform`）、platforms、kind、status、publisher_id、manifest_json、artifact_path、artifact_sha256、validation_report_json、review_reason、reviewed_by、created_at、reviewed_at
- `revisions`：submission_id、manifest_json、byte_size、artifact_path、created_at。修订上传的暂存区；`finish_revise` 成功或失败后删除
- `releases`：package_id、version、manifest_json、published_at、unpublished_at、unpublish_reason。主键 `(package_id, version)`。下架后整版离开搜索，但行还在
- `release_platforms`：package_id、version、platform、artifact_path、sha256、submission_id、published_at。主键 `(package_id, version, platform)`。已有主键不可更新（`PLATFORM_TAKEN` / `VERSION_IMMUTABLE`）
- `package_owners`：namespace、publisher_id、created_at、updated_at。主键 `namespace`。首个在该前缀下发布商品的发布者成为所有者；管理员可转移。启动时按已发布提交回填。
- `catalog_index`：package_id 主键、version、kind、title、summary、description、clients、platforms、manifest_json、updated_at。每个包只放最新可见版本；发布与下架时增量维护，启动时重建。`search` 走这张表，不再解析全部清单。
- `catalog_fts`：FTS5 trigram 虚表（package_id、title、summary、description），与 `catalog_index` 同步维护。3 个字符以上的查询走 `MATCH`，短词回退 `LIKE`；SQLite 没编 FTS5 时整体回退 `LIKE`。
- `client_caps`：client_id、caps（JSON 数组）
- `audit`：actor、action、package_id、version、channel、at。只写不改；admin 用 `list_audit` 读，按 id 倒序，可按包与动作过滤

制品路径按 sha256 存放，提交记录指向它。发布不复制文件。

---

## 10. 开发方案

一个 Python 3.11 项目，分四个包，共用同一个校验器。选型理由：技能和校验脚本已经是 Python；v1 要先把拒收规则做对，不先做门户。

```
store/
  spec/store.schema.json      清单 JSON Schema
  validator/                   纯函数，无网络之外的副作用；URL 检查可关闭
  cli/                         store keys / store validate / store approve
  server/                      FastAPI：上传 PUT、制品 GET、审核
  mcp/                         商店 MCP，只调用 server 的同一套函数
  installer/                   参考安装器，执行 install_plan
```

门户是同一套上架单的人读界面，不是另一套数据。页面、字段和审核结论见第 13 节。开发顺序见第 16 节。

### 阶段 0 — 规格冻结

交付：本文档 + `store.schema.json` + 规则 id 枚举。  
验收：评审时不增加本文档之外的 kind、op、工具。

### 阶段 1 — 校验器与 CLI

交付：`store validate <目录或 manifest>`，退出码 0 表示可上传。  
验收用例：

- 版本号不是 semver → `SEMVER`
- `kind=skill` 且 `clients` 含 `cursor`（当前能力表无 `skill_dir`）→ `CLIENT_CAPABILITY`
- zip 含 `SECRET.md` 或 `sk-` 长串 → `SECRET_FILE` 或 `SECRET_PATTERN`
- 外部依赖 URL 不是 https、或 HEAD/GET 失败 → `DEP_EXTERNAL_URL` / `DEP_EXTERNAL_UNREACHABLE`
- 外部依赖没有指南 → `DEP_GUIDE_MISSING`
- `runtime` 少了一个 arch 的文件或 sha256 不符 → `RUNTIME_ASSET` / `CHECKSUM`
- 引用未发布的 runtime → `DEP_STORE_UNPUBLISHED`
- 合法的最小技能包、最小远程 MCP、最小 runtime 均通过

此阶段没有服务器。用它来改技能、准备 MCP 清单。

### 阶段 2 — 上传与审核

交付：SQLite、制品目录、用户密钥和管理密钥、`begin_submit` / PUT / `finish_submit`、`approve` / `reject` / `unpublish`。先做 CLI 和 HTTP，MCP 可晚一步。  
验收：

- 用户密钥调用审核接口得到 `ROLE_FORBIDDEN`
- 管理密钥不能用作者身份去改 `author.publisher_id`
- 校验失败的提交状态为 `rejected`，不出现在审核队列
- 通过的提交为 `pending`，`search` 看不到
- `approve` 一个 `new_version` 之后 `list_versions` 能看到它，旧版本仍在；对已发布版本再次 `new_version` 同一号得到 `VERSION_IMMUTABLE`
- `add_platform` 给已发布版本补一个平台后，`resolve` 用该平台能选中这一版；重复补同一平台得到 `PLATFORM_TAKEN`；`add_platform` 若改了 summary 或 invoke 得到 `PLATFORM_SCOPE`
- `reject` 的原因作者能读到；同一 version 在未发布前可以重新 PUT
- 审计表有提交、发布、退回、下架记录

### 阶段 3 — 商店 MCP

交付：第 5 节全部工具，Bearer 鉴权，匿名 / 用户 / 管理三种可见集。  
验收：用一个 MCP 客户端分别连三次：

- 无密钥：`search` 可用，`begin_submit` 不可见或调用即 `ROLE_FORBIDDEN`
- 用户密钥：能走完上传时序，不能 `approve`
- 管理密钥：能发布；`resolve` 在同时存在 1.0.0 和 1.1.0、且只有 1.0.0 含 `windows-amd64` 时，对该平台返回的候选不含 1.1.0，`selected` 为 1.0.0 且 `reason` 为 `highest_match_platform`
- `install_plan` 不传精确版本则调用失败；传入选定版本后，三种 kind 的 `op` 序列与第 6 节一致
- 工具 schema 中不存在密钥参数；响应中不出现 Bearer 值

### 阶段 4 — 参考安装器

交付：`store-install plan.json`，只执行第 6 节的 op。  
验收：

- 未知 op 非零退出
- `ensure_bin` 在命令不存在时打印 `install_guide` 和 URL 后退出，且没有下载陌生安装包
- sha256 不符时不解压
- 对一个 `skill`、一个 `mcp`、一个带 `runtime` 的 `mcp+skill` 各做一次干跑（`--dry-run` 打印将执行的 op）

AI 工具的接法：先 `resolve`（必要时改选 `candidates` 里的版本），再 `install_plan`，把返回的 JSON 交给 `store-install`。不要让模型自己拼安装命令，也不要省略版本号。Reasonix 若要原生支持，实现同一组 op 即可，不必先改商店。

### 阶段 5 — 用真实包试上架

至少各走通一条，作为开发完成的定义，而不是新功能。样例按规范另备，不在本工程里改技能仓库里的现有目录：

| 样例 | kind | 要证明的点 |
|---|---|---|
| 一个编排技能（公文或 QC） | `skill` | 去密钥、平台文件、`clients=["reasonix"]`，安装后入口在技能目录 |
| 一个远程或 stdio MCP | `mcp` | `invoke` 齐全，`mcp_add` 能被安装器写到测试配置 |
| 一个带二进制的改文件技能 | `skill` + `runtime` | 二进制走审核，技能只引用；缺的 arch 无法发布 |
| 一个依赖 pandoc 或 ffmpeg 的技能 | `skill` + `external` | 上传时 URL 检查生效；本机没有命令时安装器停在指南 |

做不到上表的，不算商店可用。门户、多实例、公网注册中心同步，都不在这五段里。

### 模块与接口

HTTP（给 PUT/GET，避免大文件进 MCP）：

- `POST /v1/submissions` 用户密钥 → `{ submission_id, upload_url, expires_at }`
- `PUT /v1/uploads/{token}` 上传制品
- `POST /v1/submissions/{id}/finish` 用户密钥
- `GET /v1/artifacts/{id}/{version}` 仅已发布，或 `artifact` 工具返回的短期签名 URL
- `POST /v1/releases/{id}/{version}/approve|reject|unpublish` 管理密钥

MCP 工具是这些操作的薄封装，校验函数只有一份，CLI、HTTP、MCP 都调用它。

### 配置

- 制品目录与数据库路径由 `store serve --blobs`、`--db` 指定，默认是本目录下的 `qilin-blobs` 与 `qilin.db`。旧版文档里的 `STORE_BLOB_DIR`、`STORE_DB` 并未实现，不要按那两个名字配置
- `QILIN_PUBLIC_BASE` 或 `store serve --public-base` 是对外地址，用于生成下载 URL、上传 URL 和 `mcp_url`。反代部署必须显式配置：不配置时按 `X-Forwarded-Proto` 加 `Host` 推断，再退回本地监听地址，而请求头可以伪造
- `QILIN_HUB_ORIGIN` 枢纽地址。也可写在 `store serve --hub-origin`
- `QILIN_HUB_AUTHORIZE` 枢纽授权页地址，可含 `{state}`、`{redirect_uri}`、`{app_id}` 占位。也可写在 `store serve --hub-authorize`。配了它，`/auth/start` 才可用，回调的 `state` 才能与商店下发的 Cookie 对上；不配则跳转只能由枢纽发起，回调记 `STATE_UNBOUND`
- `QILIN_APP_ID` 默认 `lucy-qilin`
- `QILIN_LOG_LEVEL` 或 `store serve --log-level` 调日志级别，默认 `WARNING`，输出到 stderr。不记访问日志，也不记口令和 KEY
- 公钥缓存放在数据库同目录的 `jwks-cache.json`
- 服务器不配置任何产品业务密钥，也不配置栖林自己的签发密钥

上传与运维相关的固定值写在代码里，不做成配置项：正文上限 `_MAX_BODY`（比制品上限 `_MAX_BYTES` 多出 base64 膨胀量和 1MiB 余量）、解压合计上限 `MAX_UNPACKED`、残留上传的存活时间 `_UPLOAD_TTL_HOURS`（72 小时）。要改需改代码，改之前先确认内存与磁盘的代价。

### 测试

校验器用固定夹具，不访问网络；URL 检查的测试使用本地 http 假服务器。审核流用临时目录和临时数据库。不把真实技能仓库里的密钥文件当作测试夹具。

---

## 11. 实现时不要自行加的东西

- 不要在 `approve` 里修改清单。
- 不要把外部依赖自动 apt 安装。
- 不要为编排技能生成「调用一次就跑完全部阶段」的工具。
- 不要把管理密钥的能力合并进用户密钥的「高级模式」。
- 不要在栖林里签发或分配密钥。角色只认枢纽令牌里的 `app_roles`。
- 不要在 v1 做语义搜索。`search` 先做名称、summary、description 的包含匹配，以及 kind、client、platform 过滤。
- 不要在 `install_plan` 里省略版本号，也不要在最新版缺少当前平台时静默改装另一个版本。选择只发生在 `resolve`。

---

## 12. 规则 id 一览

实现和改造时以这张表为错误码，不另起一套中文句子当机器判断。

`SPEC_VERSION` `SPEC_UNKNOWN` `KIND` `SEMVER` `AUTHOR_MISMATCH` `TITLE` `SUMMARY` `DESCRIPTION`  
`CLIENT_UNKNOWN` `CLIENT_CAPABILITY` `CLIENT_MISMATCH`  
`PLATFORM_UNKNOWN` `PLATFORM_MIXED` `PLATFORM_TOO_BROAD` `PLATFORM_MISMATCH` `PLATFORM_TAKEN` `PLATFORM_SCOPE`  
`VERSION_UNAVAILABLE` `VERSION_REQUIRED`  
`MCP_TRANSPORT` `MCP_CONNECT` `MCP_INVOKE` `MCP_URL_UNREACHABLE` `MCP_LIST_FAILED`  
`SKILL_ENTRY_MISSING` `SKILL_INVOKE` `PLATFORM_FILE_MISSING`  
`NOTE_ENTRY` `NOTE_LINK` `NOTE_FRONTMATTER` `NOTE_FILE` `NOTE_SECURITY` `NOTE_ORPHAN` `KNOWLEDGE_SCOPE` `KNOWLEDGE_TAXONOMY` `KNOWLEDGE_MAP`  
`SECRET_FILE` `SECRET_PATTERN` `CHECKSUM`  
`DEP_GUIDE_MISSING` `DEP_EXTERNAL_URL` `DEP_EXTERNAL_UNREACHABLE` `DEP_RESOURCE_GAP`  
`RATE_LIMITED` `PLUGIN_MANIFEST` `PLUGIN_ENTRY` `PLUGIN_SCRIPT` `PLUGIN_DEPENDENCY` `PLUGIN_CAPABILITY` `PLUGIN_DSH_VERSION` `PLUGIN_ARTIFACT` `PLUGIN_TIER` `PLUGIN_REVIEW_REQUIRED` `SELF_REVIEW`  
`DEP_STORE_UNPUBLISHED` `DEP_CYCLE` `RUNTIME_ASSET`  
`VERSION_IMMUTABLE` `ROLE_FORBIDDEN` `IDENTITY_INVALID` `IDENTITY_UNCONFIGURED`  
`USERNAME_INVALID` `USERNAME_TAKEN` `NAME_INVALID` `KEY_INVALID` `KEY_MISMATCH` `KEY_TAKEN` `PASSWORD_INVALID` `PASSWORD_MISMATCH` `ACCOUNT_DISABLED` `LAST_ADMIN`  
`FRONTMATTER_CONFLICT` `ID_FORMAT`

所有规则都是 error 级；没有「表外警告 + 人工确认」这一档。`ID_FORMAT` 只检查 `id` 是不是小写的 `命名空间.名字`，不检查它在不在任何名单里。

`KEY_MISMATCH`、`MCP_LIST_FAILED`、`MCP_URL_UNREACHABLE` 为 v1 预留，当前代码尚未产出。`KEY_MISMATCH` 用来表示「同一账号在两处凭据不一致」这类需要人工核对的场景；后两者按第 4.3 节第 2、3 条放在 `approve` 阶段做连接检查，v1 尚未实现，保留 id 以免后续新增时再改一次码表。

---

## 13. 同一份上架单

手工上传、手工修订、MCP 上传、MCP 修订，读写的是同一份 JSON，字段名与第 4 节 `manifest.json` 一致，一个不多，一个不少。门户不设「备注给审核员」之类只在页面上存在的作者字段。审核员看到的就是这份上架单，外加系统算出来的校验报告。

上架单分组，门户表单、MCP 参数、审核页三处锁死为这七组：

| 组 | 字段 |
|---|---|
| 身份 | `id` `version` `kind` `submit_kind` `author.display_name` |
| 阐述 | `title` `summary` `description` `changes` |
| 适用 | `platforms` `clients` |
| 调用 | `invoke`，以及 `kind` 需要的 `mcp` / `skill` / `runtime` |
| 依赖 | `depends.store` `depends.skills` `depends.mcp` `depends.external` |
| 密钥名 | `secrets`（只有名字和说明） |
| 制品 | zip 或单文件；远程 MCP 允许没有制品 |

系统附加，作者不能写，门户和 `get_review` / `get_submission` 都返回：

- `validation.rules[]`：规则 id 和字段路径
- `validation.urls[]`：每个外部地址的检查结果
- `validation.checksum`
- `validation.files[]`：制品内文件清单
- `diff`：若该 `id` 已有发布版，与上一已发布版本的字段差异。没有上一版则为空

人工过关只有两个按钮：发布、退回。退回必须填写原因，原因写入 `review_reason`，作者在「我的提交」和 `get_submission` 里看到同一句。审核员不能改字段。要改，退回，作者在门户或 MCP 里修订后再交。

修订按状态分流，两边相同：

| 当前状态 | 门户按钮 | MCP | 实际动作 |
|---|---|---|---|
| 无，或已发布 | 上架新版本 | `begin_submit` + `submit_kind=new_version` | 新版本。表单用上一已发布版预填，`version` 和 `changes` 必须改 |
| 已发布，只补平台 | 追加平台 | `submit_kind=add_platform` | 只增加平台制品。其余字段与已发布清单逐字相同，否则 `PLATFORM_SCOPE` |
| `pending` 或 `rejected` | 修订此提交 | `begin_revise` + PUT + `finish_revise` | 整份上架单替换，重新跑校验，状态回到待审。保持同一个 `submission_id`，不新开一条让审核员对不上 |

预填不是第三份数据。`GET /v1/releases/{id}/{version}` 与 MCP `describe` 返回同一 JSON，门户把它填进表单。

---

## 14. 既有技能改造契约

这份契约由商店 MCP 的 `get_spec`（`name` 为 `skill` 或 `upload`）下发。本工程不改造技能目录，也不登记每个目录改到哪一步。用户连上商店 MCP 之后，用自己的 AI 工具按规范改、再上传。

改造原则是商店迁就技能的运行方式，不要求为了上架去改阶段、拆 MCP、改 `references/` 的读法。

### 14.1 不许动的

- `SKILL.md` 仍是 harness 的入口。商店不读取它来代替 `manifest.json`，也不要求把正文搬进清单。
- `references/` 的按需读取、阶段挂起、任务目录落在用户工作区，都保持原样。
- `platform/` 里现有文件名保持原样。Reasonix 适配继续叫 `platform/reasonix-code.md` 也可以。清单用 `skill.platform_files` 声明实际文件名，校验器按声明去找（`PLATFORM_FILE_MISSING`）。不要为了商店把文件改名。
- 脚本的参数和调用方式不改。依赖写进 `depends`，不把脚本重写成 MCP 工具。

### 14.2 只新增的

每个准备上架的目录根上增加 `manifest.json`。用 `.storeignore` 排除 `工程过程/`、`.models/`、`.git/`、`.DS_Store`。不把排除规则写进技能正文。

`SKILL.md` 的 frontmatter 继续留给 harness。两边都有的信息不得矛盾（`FRONTMATTER_CONFLICT`）：

| frontmatter | 上架单 | 冲突时 |
|---|---|---|
| `name` | `title` 可不同；`id` 由清单决定，不按目录名套 | `name` 改了也不改已发布 `id` |
| `description` | `summary` 必须是同一作用的缩短，不得改成另一件事 | 拒收 |
| `version` 能解析为 semver | `version` 必须相同 | 拒收 |
| `version` 是说明文字 | 商店版本从 `1.0.0` 起，`changes` 写明原文字 | 不拒，但 `changes` 必填 |
| `dependency` / `allowed-tools` | 必须出现在 `depends` 或 `clients` 的说明里 | 漏了拒收 |

已发布的 `id` 永久不变。目录文件夹改名不影响 `id`。同一 `id` 的新版本只能由首个发布者提交，admin 也不能代发（`ROLE_FORBIDDEN`）；新 `id` 不需要登记，也不会因为「不在名单里」被警告。

### 14.3 阻断上架的，和可以后补的

阻断，不改就不能过校验：

- 包内密钥文件和密钥样式。已知位置：`gs-smart-stock-picking/SECRET.md`、`STT-BUILDER-V1/.siliconflow-key.md`、`R2-BUCKET-OPS/references/credentials.md`。改为 `secrets[].name`，脚本只读环境变量。禁止再把密钥写进 `memory.md`。
- 没有严格 semver、没有 `changes`、没有 `platforms`、没有 `clients`。
- 外部命令没有 `detect`、安装指南和当时打得开的 https 地址。
- `clients` 含当前能力表不支持的工具。编排技能在补上技能目录能力之前，只填 `reasonix`。

可以后补，不阻断第一次上架：

- 把国信、国投查询收成远程 MCP。在那之前以 `kind=skill` 上架。
- 把 `officecli` 收成 `runtime`。在那之前 `slide-master` 用 `depends.external` 写明二进制来源；一旦 `runtime` 发布，下一版技能改为 `depends.store`，不在同一版里两处都写。
- 模型权重做成 `runtime`。`STT-BUILDER-V1` 第一次上架不得把 `.models/` 打进 zip。

### 14.4 登记表（历史材料）

下表是 2026-11-11 起草时的一批目录到 `id` 的示例对照。**2026-11-16 起不再作为校验输入**：商品身份由清单里的 `id` 决定，目录名只是文件夹；同一 `id` 与命名空间归首个发布者。保留此表只作历史对照，不要按它分配 id，也不要因为某个目录不在表里就拒收。`kind` 是第一次上架的种类，不是最终形态。

| 目录 | id | 首次 kind | platforms | 上架前必须处理 |
|---|---|---|---|---|
| GOV-Antigravity-V3 | `luxi.gov-writer` | skill | `any` | 无密钥；`clients` 只写 `reasonix` |
| QC-Antigravity-V3 | `luxi.qc-cluster` | skill | `any` | 同上 |
| PAPER-Antigravity-V4 | `luxi.paper-workshop` | skill | `any` | 同上 |
| TECH-Antigravity_V3 | `luxi.tech-orchestrator` | skill | `any` | `pdfminer` 写入 `depends.external` |
| WEB-SEARCH-V1 | `luxi.web-search` | skill | `any` | 搜索后端地址写入外部依赖或说明 |
| KNOWLEDGE-DECOMPOSER | `luxi.knowledge-decomposer` | skill | `any` | `pandoc` 外部依赖；声明会写用户知识库 |
| chenmen-write | `luxi.chenmen-write` | skill | `any` | `officecli` 先外部依赖，后改 `depends.store` |
| GOV-DOC-META-V1 | `luxi.gov-doc-meta` | skill | `any` | `pandoc` 外部依赖 |
| slide-master | `luxi.slide-master` | skill | 按实际能跑的平台列 | `pandoc` 外部依赖；`officecli` 规则同上 |
| pandoc-slide-compiler | `luxi.pandoc-slides` | skill | `any` | `pandoc` 外部依赖 |
| web-slide-forge | `luxi.web-slide-forge` | skill | `any` | 供应商脚本留在包内，不另做 runtime |
| notes-to-static-site | `luxi.notes-site` | skill | `any` | 无特殊阻断 |
| STT-BUILDER-V1 | `luxi.stt-builder` | skill | 具体平台 | 去掉密钥文件；`ffmpeg` 外部依赖；`.models/` 不进包 |
| EDGE-TTS-V1 | `luxi.edge-tts` | skill | `any` | `edge-tts` 作为 Python 外部依赖 |
| PADDLE-OCR | `luxi.paddle-ocr` | skill | `any` | 令牌只留变量名 |
| VISION-LAYOUT-RESTORE | `luxi.vision-layout` | skill | `any` | 同上 |
| R2-BUCKET-OPS | `luxi.r2-bucket` | skill | 具体平台 | 去掉 `credentials.md`；`rclone` 外部依赖 |
| gs-economy-query | `luxi.gs-economy` | skill | `any` | 密钥只走环境变量 |
| gs-etf-filter | `luxi.gs-etf` | skill | `any` | 同上 |
| gs-fund-compare | `luxi.gs-fund` | skill | `any` | 同上 |
| gs-smart-stock-picking | `luxi.gs-stock-pick` | skill | `any` | 先删除 `SECRET.md` |
| gs-stock-financial-query | `luxi.gs-financial` | skill | `any` | 密钥只走环境变量 |
| gs-stock-market-query | `luxi.gs-market` | skill | `any` | 同上 |
| sdicsc-crowding-degree | `luxi.sdicsc-crowding` | skill | `any` | 密钥只走环境变量 |
| sdicsc-fund-query-compare | `luxi.sdicsc-fund` | skill | `any` | 停止把密钥写入 `memory.md` |
| sdicsc-market-query | `luxi.sdicsc-market` | skill | `any` | 同上 |
| sdicsc-smart-stock-selection | `luxi.sdicsc-stock` | skill | `any` | 同上 |

已发布的 `luxi.pandoc-slide-compiler@1.0.0` 保持原 `id`，不按本表改成 `luxi.pandoc-slides`。

新技能不需要先补表。清单里写合法 `id` 与 `version` 就是新商品；与商店里已有 `id` 重叠时按第 3 节判版本与归属。

Python 依赖不进商店镜像。写法固定为 `depends.external`：`detect` 用 `python3 -c` 检查导入，`install_guide` 写清安装命令，`resources` 给文档或内部源的 https 地址。

---

## 15. 栖林门户

商店的产品名是栖林（QiLin），枢纽·一市，口号「百智栖林，成荫润木」。视觉遵循鹿溪设计范式 v1.1.1：`data-product="qilin"`，accent `#0D5E42` / `#F1C40F`，默认深色工作台、comfortable 密度。primary 按钮仍是鹿溪绿，标题金只做落点和状态面，文字用 `--text-warn` 等等安全色。顶栏产品方标与实验室符号标并排等大。工作台顶栏高 56，双标取 32；营销页才用 48 或 64。深色顶栏上的实验室符号标用烫金文件 `luxi-lab-gold.svg`。不使用写实主标。

信息架构是工作台，不是营销页：

```
顶栏 56：方标 + 栖林 + 口号，右侧主题、登录和当前身份。顶栏不提供签发
左栏 240：货架、运行时、我的提交、审核、审计、贡献、用户、用户管理、连接
主区：列表或一张上架单
```

九个主区：

1. **货架**。导语：「货架陈列已经发布的技能、MCP、知识包与插件。运行时、依赖工具请前往 运行时 菜单。下载使用请选择适合的版本。」说明：「本商店的产品，均需要登录后，才能在页面进行下载。如用 AI 工具进行相关产品的浏览、下载、使用，请在 AI 工具中连接本商店提供的 MCP 服务，使用说明请查看 连接 菜单。」先是搜索、排序和分类（全部、技能、MCP、技能与 MCP、知识包、插件），再是在架数量；每页 12 条，可加载更多。卡片用完整的一句话说明；下面是产品 id、平台、AI 工具和作者。点进详情用 `list_versions`，由人选定版本再看 `describe`。运行时不进默认货架。
2. **详情**。标题下先是一句话和整段说明，再是七组上架单。调用、依赖、密钥名写成句子，不把 JSON 倒在页面上。版本表的「适用平台」是作者写进这一版的；`any` 显示为「任意平台」。知识包另列一组：分类三段、入口、密级、范围、词表、语言与许可、生产溯源、本地主题。表下面的「装到这台电脑」不是创建时保存的，只为生成安装说明。适用 `any` 时，这里才列出六种具体电脑，并写明第一项不是产品数据。主按钮只有一个：「安装说明」展示 `install_plan` 的步骤，不在门户里执行安装。
3. **用户**。未登录时这一页只注册。登录是顶栏弹出的登录框：用户名和口令两个输入，不列出已有的人，也不公开账号名单。登录后这一页只剩资料和修改口令，两块分开。注册成功会换成新会话，所以已登录时不再显示注册。没有「把密钥发给某人」的按钮。
4. **用户管理**。单独一页，只给管理员。未登录、作者、审核只看到不能管理。管理员看到名单，可以改角色、停用、给别人重置口令，也可以把某个命名空间转给另一个作者。新口令随机生成，只显示一次。不能重置自己的，不能看见原来的口令或 KEY。
5. **连接**。四个标签页：DSH 只读、DSH 发布、通用只读、通用发布。只读不带 KEY；发布配置在页面持有 KEY 明文时直接写入，不再用环境变量或占位符。登录只保留会话、不保留 KEY 明文，所以发布标签默认置灰；页面用「重新生成并显示发布配置」显式生成一把新 KEY（旧 KEY 失效），并同时列出 KEY 明文。四个配置的 serverName 固定为 `qilin-store` / `qilin-publish`，同一客户端只装一个，发布配置已包含只读工具。口令不能代替 KEY。连上之后，上架规范用 `get_spec` 取。
6. **上架 / 修订**。同一张七组表单。每组中文标题下保留字段名，不另做一套作者字段。换 `kind` 后收起这一类型用不到的组：纯技能不展开 MCP 连接项，纯 MCP 不展开制品（远程 MCP 没有制品），运行时不展开技能入口。MCP 按传输方式只展开 `url` 或 `command`。`add_platform` 只出现在运行时。技能、MCP 和运行时可以声明依赖和密钥名，没有就留空。知识包这两项留空数组，页面上不出现。身份、阐述、适用里规格要求的字段标成必填，空着不能提交。待审核显示「修订此提交」；已发布显示「上架新版本」和「追加平台」。提交前先走与 MCP `validate` 相同的接口，规则 id 标在字段上。页面仍然提交同一份上架单。
7. **审核**。左列待审，右列同一份上架单加校验报告和与上一版的差异。主按钮「发布」；「退回」是 danger，必须填原因。空队列有下一步：回到货架。
8. **审计**。只给审核。按 id 倒序列出操作者、动作、产品、版本、通道与时间；可按动作与产品过滤，一次 50 条，「加载更多」翻页。只读，不提供改状态的按钮。
9. **贡献**。只给审核。按已发布产品统计贡献者：昵称、姓名、部门、用户名、产品数、版本数、最近发布时间；点「明细」看该账号的产品/版本/类型/发布时间。数据来自提交时的账号快照；公开货架仍只暴露 `author.display_name`。

页面必须有加载、空、错误三态。发布和退回失败要回滚按钮状态并说明原因。焦点环可见，`Esc` 关闭抽屉。

页面在 `qilin-portal.html`，由 `store serve` 和接口一起给出，引用 `LUCY-DESIGN` 的 `tokens.css` 与栖林方标。顶栏是登录和当前身份，不是粘贴令牌的输入框。

---

## 16. 开发计划

仍是一个 Python 3.11 进程。门户是这进程的页面，不是另一个后端。

| 阶段 | 交付 | 完成标准 |
|---|---|---|
| 0 规格 | 本文档 + `store.schema.json` | 字段以第 4、13 节为准，不再加作者字段 |
| 1 校验器 | `store validate` | 第 10 节阶段 1 的用例，外加 `FRONTMATTER_CONFLICT`、`ID_FORMAT` |
| 2 上架单 API | 提交、替换、预填、审核、发布 | 同一 JSON 经 HTTP 写入后，商店认识的公开字段在 `describe` 与审核详情中一致。审核详情另保留 `author.publisher_id` 和商店还不认识的顶层字段，`describe` 不返回这两类 |
| 3 商店 MCP | 第 5 节工具，全部调用阶段 2 的函数 | MCP `begin_submit` 与门户提交在库里不可区分，除 `audit.channel` 记 `mcp` 或 `portal` |
| 4 栖林门户 | 货架、详情、上架/修订、审核 | 表单字段名与清单一致；待审修订不新开提交；审核员改不了字段；退回原因作者可见 |
| 5 安装器 | `store-install` | 只执行第 6 节的 op；版本号必填 |
| 6 上传验收 | 一份按 `get_spec` 整理的技能目录 | `store submit` 进入 `pending`。既有技能目录的改造不在本工程 |

阶段 3 的身份是商店为每个用户生成的一把 KEY，不是人自己设置的，也不是 `store serve` 打印出来的。配置了枢纽时，长令牌仍按 `lucy-qilin` 的 `publisher` / `reviewer` / `admin` 验签，其余为 `member`。

阶段 4 不提前做视觉探索。页面按 `qilin-portal.html` 的结构和 tokens 实现。阶段 6 只验收上传路径：符合规范的目录能交到待审核。既有技能怎么改，由用户连上商店 MCP 之后自己做，不记入本工程。校验不通过就退回，不改规则迁就旧包。

---

## 17. 审计与后续

2026-11-11 对已经做出的门户和用户模块做了一次审计。结论：商店的边界没有走偏，用户模块的页面组织不合理，已经按下面第 1 条改掉。其余按顺序做，不把阶段 6 提前。

### 17.1 用户模块里不合理的，和已经改的

点昵称再填口令，不是登录。它把谁有账号公开在页面上，也让登录和注册挤在同一屏。已登录时旁边还能再注册，而注册会换成新会话，审核会把自己登出。

现在的分法：

| 在哪 | 只做这件事 |
|---|---|
| 顶栏登录框 | 用户名、口令。取消或 Esc 关掉。框里可以去注册 |
| 用户页，未登录 | 只注册。必填、选填和口令规则留在这里 |
| 用户页，已登录 | 只改资料，和修改口令。两块分开。不再显示注册 |
| 用户管理 | 只给审核。改角色、停用，给别人重置口令。新口令随机生成，只显示一次。不能重置自己的 |
| 连接 | 只给 AI 工具准备商店 MCP。和登录无关 |

`GET /v1/accounts` 不再对匿名开放。名单只给审核。

丢了口令的第二条路是审核重置：商店生成一把新口令，只给审核看一次，由审核交给对方。审核不能看见原来的口令，不能自己编新口令，也不能重置自己的。没有自助找回。

### 17.2 门户其余页面

上架单仍是那七组，但不再整页摊开英文清单名。每组一个中文标题，字段名留在中文旁边，不另做一套作者字段。换类型后收起用不到的组：纯技能不展开 MCP 连接项，纯 MCP 收起制品，运行时不展开技能入口，追加平台只给运行时。依赖和密钥名不随技能、MCP、运行时收起。知识包不声明这两项，页面上不出现，提交时仍是空数组。

详情和货架这一轮不再改结构。适用平台和「装到这台电脑」已经分开。

安装说明继续只展示，不在门户里执行。未登录时不请求安装计划，提示先登录，或改走连接菜单里的商店 MCP。

### 17.3 商店行为上还缺的

按这个顺序，做完一项再进下一项：

1. 纯技能、纯 MCP 的上架表单按类型收起无关组。已做。页面仍然提交同一份上架单。
2. `install_plan` 里 MCP 的 `verify` 要连接并列出工具。已做。连不上、超时或没有工具列表就停，不假装成功。商店服务器仍不执行安装。
3. `finish_submit` 的外部地址检查不再占着商店的锁。打不开留在 `uploading`，规则 `DEP_EXTERNAL_UNREACHABLE`，同一条可以再交。已做。其他失败仍退回。
4. `add_platform` 目前只允许运行时。技能包一个版本一份 zip，这条保持。远程 MCP 若按平台分资产，再单开，不顺手放宽技能包。
5. 枢纽登录跳转（`/auth/callback`）和 10 分钟令牌的续期。已做。有刷新令牌就续上，没有就停在这次访问令牌的过期时间。日常仍是栖林口令加商店 KEY。跳转会话不产生 KEY。`state` 的绑定见第 10 项。
6. 既有技能目录的改造不在本工程。规范在 `get_spec`。本工程的验收是一份符合规范的技能目录经 `store submit` 进入 `pending`。已做。不改校验规则去迁就旧包。
7. 知识包。已做第一版：链接在包内闭合，题头按约定字段。文章好不好不由商店判断。上架规范由 `get_spec` 下发。技能文件里不再写上架步骤。交目录仍用 `store submit`。
8. 安全修复、门户目录上传、审核时看文件和差异。已做。压缩包拒绝绝对路径和 `..`，跳过 macOS 与版本库尾巴，并限制解压体积。请求体超过 32MiB 拒收。`runtime.bin_name` 不能带路径。门户可以选择技能目录或笔记目录，清单只用来填表，不进制品。审核页可以点开文本预览，并看与上一已发布版本的统一差异。知识树只预留逻辑路径和 `tree_dest`，安装目录没有改。既有技能目录仍不在本工程改造。
9. 部署与运维口径。已做。KEY 认证先用 `key_prefix` 索引定位再比对，旧行在首次成功后回填指纹；停在 `uploading` 超过 72 小时的提交连同制品一起清掉；对外地址可由 `--public-base` / `QILIN_PUBLIC_BASE` 固定，未配置时用 `X-Forwarded-Proto` 加 `Host` 推断并校验形状；正文上限与制品上限分开，覆盖 base64 膨胀；制品缺失报 `VERSION_UNAVAILABLE`；鉴权失败与未预料异常写入 stderr，级别由 `--log-level` / `QILIN_LOG_LEVEL` 控制。仍未做：请求体流式处理、日志轮转。
10. 跳转登录的 `state` 绑定。已做。配了 `--hub-authorize` 时由商店签发一次性 state 并写 Cookie，回调比对通过才换令牌；没配时回调仍可用但记 `STATE_UNBOUND`。门户在 `hub_start` 为真时显示「实验室账号登录」。仍未做：鉴权移出全局锁，实际并发度仍为 1。
11. 公开清单与审核清单分开。已做。`describe` 只返回商店认识的顶层字段，`author` 只留 `display_name`；审核单保留全文，包括还不认识的顶层字段。嵌套块内部的未知子字段仍会出现在详情里。
12. 校验器的类型守卫。已做。`Report.guard` 在类型不符时记一条 finding 并返回默认值，不把畸形字段抛成异常。类型和内容绑在同一句里的判断没有拆成两条。仍未做：鉴权移出全局锁。
13. 登记表不再充当集市资产名单。已做（2026-11-16）：删除 `DIRECTORY_IDS`/`REGISTERED_IDS`、按目录名强改 id、`ID_UNREGISTERED` 与发布确认门；新增 `ID_FORMAT`；同一 `id` 归首个发布者，`submit` 与 `approve` 都校验。
14. 插件品类（`kind=plugin`）与 8 项审核清单。已做（2026-11-17）：支持 Tier A/B 纯 JS 插件校验、门户表单与目录选择、审核页 8 项清单记录（`review_plugin`），安装计划只暂存（`plugin_stage` / `plugin_confirm`），不在商店执行代码。
15. 四角色模型、贡献链路与内网 KEY 策略。已做（2026-11-17）：角色分为 `member` / `publisher` / `reviewer` / `admin`，全品类禁止自审（`SELF_REVIEW`）；提交时写入账号快照，审核与管理员可看贡献排行与明细；内网持久化 `key_plain`，连接页四标签直接嵌入 KEY。
16. 协议闭环与深度硬化。已做（2026-11-18）：HTTP 补齐 `withdraw`、`unpublish` 与三段式 `submissions/begin`、`finish`，门户可直接完成修订/撤回/下架，CLI `store submit` 支持 `--add-platform`；`catalog_index` 保留真实 `published_at`；单包查询与安装计划按需加载；已发布制品独立归档至 `blobs/releases/`；备份恢复补路径守卫与 WAL 残留清理；引入 `PRAGMA user_version` 版本迁移。

不做的仍然有效：不自建网关，不做产品密钥保管，不在商店服务器上执行安装，不把编排技能拆成一个技能一个 MCP 服务。
