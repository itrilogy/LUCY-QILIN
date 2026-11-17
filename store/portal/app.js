const CONCRETE = ["darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64", "windows-arm64", "windows-amd64"];
    const KIND_LABEL = { skill: "技能", mcp: "MCP", "mcp+skill": "技能与 MCP", runtime: "运行时", knowledge: "知识包", plugin: "插件" };
    const ROLE_LABEL = { admin: "审核", publisher: "作者", member: "未授权" };
    const SECURITY_LABEL = { public: "公开", internal: "内部", restricted: "受限", confidential: "机密" };
    const SCOPE_LABEL = { enterprise: "企业", work: "工作产出" };
    const PLUGIN_REVIEW_LABELS = {
      source_readable: "源码可读，没有混淆/压缩到不可读",
      no_install_scripts: "没有 install / postinstall 等生命周期脚本",
      dependencies_reviewed: "依赖已核对，符合 Tier 与白名单",
      capabilities_checked: "capabilities 声明与实际代码一致",
      external_commands_checked: "外部命令已核对，且声明了 process:spawn",
      lifecycle_cleanup: "注册与监听有清理，不写 DOM / 应用根",
      no_secrets: "包内没有密钥、隐私数据或未声明上传",
      dsh_range_checked: "DSH 版本范围与目标版本相符",
    };
    const state = { role: "anonymous", userId: "", mode: "new", submissionId: null, base: null, packageId: null, kind: "skill", shelfKind: "", shelfSort: "updated", shelfOffset: 0, shelfTotal: 0, issued: null };

    function show(id) {
      document.querySelectorAll(".view").forEach((view) => view.classList.toggle("on", view.id === id));
      if (id !== "detail") saveHash("#" + id);
      document.querySelectorAll(".nav button").forEach((button) => {
        button.setAttribute("aria-current", button.dataset.view === id ? "page" : "false");
      });
      if (id === "shelf") loadCatalog("shelf");
      if (id === "runtime") loadCatalog("runtime");
      if (id === "mine") loadMine();
      if (id === "review") loadReviews();
      if (id === "audit") loadAudit(true);
      if (id !== "admin-users") clearReset();
      if (id === "admin-users" && state.role === "admin") loadManage();
      if (id === "connect") renderConnect();
    }

    document.body.addEventListener("click", (event) => {
      const button = event.target.closest(".nav button[data-view], #review-back");
      if (button && button.dataset.view) show(button.dataset.view);
    });

    function signedIn() { return state.role && state.role !== "anonymous"; }

    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      if (options.signal) {
        if (options.signal.aborted) controller.abort();
        else options.signal.addEventListener("abort", () => controller.abort(), { once: true });
      }
      let response;
      try {
        response = await fetch(path, { ...options, headers, credentials: "same-origin", signal: controller.signal });
      } catch (caught) {
        if (caught && caught.name === "AbortError") {
          const error = new Error("请求超时或被取消。");
          error.name = "AbortError";
          throw error;
        }
        throw caught;
      } finally {
        clearTimeout(timer);
      }
      const text = await response.text();
      let body = {};
      if (text) {
        try { body = JSON.parse(text); } catch { body = { message: "响应不是 JSON" }; }
      }
      if (!response.ok) {
        const error = new Error(body.message || "请求失败");
        error.rule = body.rule || "";
        throw error;
      }
      return body;
    }

    function text(tag, value, className) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      node.textContent = value == null ? "" : String(value);
      return node;
    }

    function setState(id, message, isError) {
      const node = document.getElementById(id);
      node.textContent = message;
      node.classList.toggle("error", Boolean(isError));
    }

    function setStateAction(id, message, label, handler, isError) {
      const node = document.getElementById(id);
      node.replaceChildren(text("span", message));
      node.classList.toggle("error", Boolean(isError));
      if (!label) return;
      const button = text("button", label, "btn");
      button.type = "button";
      button.addEventListener("click", handler);
      node.append(" ", button);
    }

    async function refreshWho() {
      const who = document.getElementById("who");
      try {
        const me = await api("/v1/me");
        state.role = me.role || "anonymous";
        state.userId = me.id || "";
        const named = state.role !== "anonymous";
        who.textContent = named ? `${ROLE_LABEL[me.role] || me.role} · ${me.name}` : "未登录";
        document.getElementById("login-open").classList.toggle("hidden", named);
        document.getElementById("logout").classList.toggle("hidden", !named);
        document.getElementById("users-out").classList.toggle("hidden", named && Boolean(me.username));
        document.getElementById("profile-panel").classList.toggle("hidden", !named || !me.username);
        const gate = document.getElementById("admin-gate");
        const board = document.getElementById("admin-board");
        board.classList.toggle("hidden", me.role !== "admin");
        gate.classList.toggle("hidden", me.role === "admin");
        document.getElementById("nav-audit").classList.toggle("hidden", me.role !== "admin");
        document.getElementById("nav-review").classList.toggle("hidden", me.role !== "admin");
        if (me.role === "publisher") gate.textContent = "作者不能管理用户。";
        if (!named) gate.textContent = "先用审核的口令登录。";
        if (me.username) {
          document.getElementById("profile-username").value = me.username;
          document.getElementById("profile-real-name").value = me.real_name || "";
          document.getElementById("profile-nickname").value = me.nickname || me.name || "";
          document.getElementById("profile-department").value = me.department || "";
          document.getElementById("profile-title").value = me.job_title || "";
        }
        state.mcpUrl = me.mcp_url || "";
      } catch (error) {
        state.role = "anonymous";
        who.textContent = error.message || "未登录";
      }
    }

    async function loadIdentity() {
      try {
        const info = await api("/v1/identity");
        if (info.mode !== "hub" || !info.login) return;
        const note = document.getElementById("identity-note");
        if (note.dataset.hub) return;
        note.dataset.hub = "1";
        note.textContent += " 从实验室账号进来的，登录会自动续上，不用把一串令牌贴进来。";
        // 只有商店配了授权页，才由商店签发 state 并带人去授权。
        // 没配就说明这条入口不存在，不显示按钮，免得点出一个必然失败的跳转。
        if (info.hub_start) {
          document.getElementById("hub-login").classList.remove("hidden");
        }
      } catch { /* 静态说明仍可用 */ }
    }

    document.getElementById("login-open").addEventListener("click", () => openLogin());
    // 交给浏览器整页跳转：state 由商店在同一个响应里写进 Cookie，
    // 用 fetch 拿不到这次跳转，也就无法完成这一次登录。
    document.getElementById("hub-login").addEventListener("click", () => {
      window.location.assign("/auth/start");
    });
    document.getElementById("users-login").addEventListener("click", () => openLogin());
    document.getElementById("goto-register").addEventListener("click", () => {
      document.getElementById("login-box").close();
      show("users");
    });
    document.getElementById("login-cancel").addEventListener("click", () => document.getElementById("login-box").close());
    document.getElementById("logout").addEventListener("click", async () => {
      await api("/v1/logout", { method: "POST", body: "{}" });
      state.issued = null;
      await refreshWho();
      show("shelf");
    });

    document.getElementById("theme").addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      document.getElementById("theme").textContent = next === "dark" ? "浅色" : "深色";
      sessionStorage.setItem("qilin-theme", next);
    });

    function setShelfKind(kind) {
      state.shelfKind = kind || "";
      document.querySelectorAll("#kind-chips button").forEach((item) => {
        item.setAttribute("aria-pressed", item.dataset.kind === state.shelfKind ? "true" : "false");
      });
    }

    function saveHash(value) {
      if (location.hash !== value) history.replaceState(null, "", value);
    }

    function applyHash() {
      const raw = decodeURIComponent(location.hash.slice(1));
      if (!raw) { show("shelf"); return; }
      const [path, query] = raw.split("?");
      if (path.startsWith("detail/")) {
        openDetail(decodeURIComponent(path.slice("detail/".length)), false);
        return;
      }
      if (path === "shelf" && query) {
        const params = new URLSearchParams(query);
        setShelfKind(params.get("kind") || "");
        state.shelfSort = params.get("sort") || "updated";
        document.getElementById("sort").value = state.shelfSort;
        document.getElementById("query").value = params.get("q") || "";
      }
      show(path || "shelf");
    }

    function card(item, shelf) {
      const article = document.createElement("article");
      article.className = "card goods";
      const tags = document.createElement("div");
      tags.className = "tags";
      tags.append(
        text("span", KIND_LABEL[item.kind] || item.kind, "tag"),
        text("span", item.version, "tag gold"),
      );
      const author = item.author && item.author.display_name ? item.author.display_name : "";
      const meta = [platformList(item.platforms).replaceAll(", ", " · "), (item.clients || []).join(" · "), author].filter(Boolean).join(" · ");
      const row = document.createElement("div");
      row.className = "row";
      const open = text("button", "查看版本", "btn primary");
      open.type = "button";
      open.addEventListener("click", () => openDetail(item.id, shelf === "runtime"));
      row.append(open);
      article.append(
        tags,
        text("h3", item.title || item.id),
        text("p", item.summary || "", "summary"),
        text("p", item.id, "sku"),
        text("p", meta, "meta"),
        row,
      );
      return article;
    }

    function skeletonCard() {
      const article = document.createElement("article");
      article.className = "card goods skeleton";
      article.append(
        text("div", "", "bar title"),
        text("div", "", "bar"),
        text("div", "", "bar"),
        text("div", "", "bar short"),
      );
      return article;
    }

    async function loadCatalog(which, append = false) {
      const shelf = which === "shelf";
      catalogAbort?.abort();
      catalogAbort = new AbortController();
      const seq = ++catalogSeq;
      const kind = shelf ? state.shelfKind : "runtime";
      const grid = document.getElementById(which + "-grid");
      const stateId = which + "-state";
      const count = document.getElementById("shelf-count");
      const more = document.getElementById("shelf-more");
      if (!append) {
        grid.replaceChildren();
        grid.append(skeletonCard(), skeletonCard());
        if (shelf) {
          state.shelfOffset = 0;
          state.shelfTotal = 0;
        }
      }
      setState(stateId, append ? "正在读取更多。" : (shelf ? "正在读取货架。" : "正在读取运行时。"), false);
      if (count && shelf) count.textContent = "";
      const query = shelf ? document.getElementById("query").value.trim() : "";
      const params = new URLSearchParams();
      if (kind) params.set("kind", kind);
      if (query) params.set("query", query);
      if (shelf) {
        params.set("sort", state.shelfSort);
        params.set("limit", "12");
        params.set("offset", String(state.shelfOffset));
      }
      try {
        const data = await api("/v1/catalog" + (params.toString() ? "?" + params : ""), { signal: catalogAbort.signal });
        if (seq !== catalogSeq) return;
        const items = data.items || [];
        if (shelf) {
          const total = Number.isFinite(data.total) ? data.total : items.length;
          state.shelfTotal = total;
          state.shelfOffset += items.length;
          const hashParams = new URLSearchParams();
          if (kind) hashParams.set("kind", kind);
          if (query) hashParams.set("q", query);
          if (state.shelfSort !== "updated") hashParams.set("sort", state.shelfSort);
          saveHash("#shelf" + (hashParams.toString() ? "?" + hashParams.toString() : ""));
          count.textContent = `在架 ${total}`;
          more.classList.toggle("hidden", state.shelfOffset >= total);
        }
        if (!items.length && !append) {
          grid.replaceChildren();
          if (shelf) {
            setStateAction(stateId, "货架上还没有这类已发布的产品。", "清空筛选", () => {
              document.getElementById("query").value = "";
              setShelfKind("");
              loadCatalog("shelf");
            });
            more.classList.add("hidden");
          } else {
            setState(stateId, "还没有已发布的运行时。", false);
          }
          return;
        }
        if (items.length) setState(stateId, "", false);
        if (!append) grid.replaceChildren();
        items.forEach((item) => grid.append(card(item, which)));
      } catch (error) {
        if (error.name === "AbortError" || seq !== catalogSeq) return;
        grid.replaceChildren();
        setStateAction(stateId, error.message, "重试", () => loadCatalog(which), true);
      }
    }

    document.getElementById("query").addEventListener("change", () => loadCatalog("shelf"));
    document.getElementById("query").addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      loadCatalog("shelf");
    });
    document.getElementById("search").addEventListener("click", () => loadCatalog("shelf"));
    document.getElementById("sort").addEventListener("change", () => {
      state.shelfSort = document.getElementById("sort").value || "updated";
      loadCatalog("shelf");
    });
    document.getElementById("shelf-more").addEventListener("click", () => loadCatalog("shelf", true));
    document.getElementById("kind-chips").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-kind]");
      if (!button) return;
      setShelfKind(button.dataset.kind);
      loadCatalog("shelf");
    });

    async function openDetail(packageId, isRuntime) {
      state.packageId = packageId;
      saveHash("#detail/" + encodeURIComponent(packageId));
      state.kind = isRuntime ? "runtime" : "skill";
      show("detail");
      document.getElementById("detail-sheet").replaceChildren(text("p", "正在读取。", "state"));
      try {
        const listed = await api("/v1/packages/" + encodeURIComponent(packageId) + "/versions");
        const versions = listed.versions || [];
        if (!versions.length) {
          document.getElementById("detail-title").textContent = packageId;
          document.getElementById("detail-sheet").replaceChildren(text("p", "没有已发布版本。", "state"));
          return;
        }
        const rows = document.getElementById("version-rows");
        rows.replaceChildren();
        const versionSelect = document.getElementById("plan-version");
        versionSelect.replaceChildren();
        versions.forEach((item) => {
          const tr = document.createElement("tr");
          [item.version, item.changes, platformList(item.platforms), (item.clients || []).join(", ")]
            .forEach((value) => tr.append(text("td", value)));
          rows.append(tr);
          versionSelect.append(new Option(item.version, item.version));
        });
        versionSelect.onchange = () => loadDescribed(packageId, versionSelect.value, versions);
        await loadDescribed(packageId, versions[versions.length - 1].version, versions);
        versionSelect.value = versions[versions.length - 1].version;
      } catch (error) {
        document.getElementById("detail-sheet").replaceChildren(text("p", error.message, "state error"));
      }
    }

    async function loadDescribed(packageId, version, versions) {
      const manifest = await api("/v1/packages/" + encodeURIComponent(packageId) + "/" + encodeURIComponent(version));
      state.base = manifest;
      state.kind = manifest.kind;
      document.getElementById("detail-title").textContent = manifest.title || packageId;
      document.getElementById("detail-lead").textContent = manifest.summary || packageId;
      document.getElementById("detail-prose").textContent = manifest.description || "";
      document.getElementById("add-platform").classList.toggle("hidden", manifest.kind !== "runtime");
      fillChoices(manifest, versions.find((item) => item.version === version));
      document.getElementById("detail-sheet").replaceChildren(sheet(manifest, null));
    }

    function fillChoices(manifest, versionRow) {
      const clients = document.getElementById("plan-client");
      const platforms = document.getElementById("plan-platform");
      clients.replaceChildren();
      platforms.replaceChildren();
      (manifest.clients || []).forEach((item) => clients.append(new Option(item, item)));
      const declared = (versionRow && versionRow.platforms) || manifest.platforms || [];
      const broad = declared.includes("any");
      const choices = broad ? CONCRETE : declared;
      choices.forEach((item) => platforms.append(new Option(item, item)));
      document.getElementById("plan-note").textContent = broad
        ? "上表「任意平台」为作者写入这一版的适用平台。下方选择本机平台，只用于生成安装说明，不改产品。列表第一项不是创建时保存的数据。页面下载须先登录。"
        : "下方只列出这一版已经发布的平台。请选择要安装的电脑。此选择不改这一版的适用平台。页面下载须先登录。";
    }

    function platformList(list) {
      return (list || []).map((item) => (item === "any" ? "任意平台" : item)).join(", ");
    }

    function platformText(list) {
      if (!list || !list.length) return "未写";
      if (list.length === 1 && list[0] === "any") return "任意平台。不挑操作系统，也不表示只适用于某一种电脑。";
      return list.join("、");
    }

    function sheet(manifest, submitKind) {
      const root = document.createElement("div");
      const author = (manifest.author || {}).display_name || "";
      const groups = [
        ["身份", [
          ["产品", manifest.id || ""],
          ["版本", manifest.version || ""],
          ["类型", KIND_LABEL[manifest.kind] || manifest.kind || ""],
          ["作者", author],
          ["提交", submitKind || ""],
        ]],
        ["这一版在说什么", [
          ["一句话", manifest.summary || ""],
          ["说明", manifest.description || ""],
          ["相对上一版", manifest.changes || ""],
        ]],
        ["适用", [
          ["平台", platformText(manifest.platforms)],
          ["AI 工具", (manifest.clients || []).join("、") || "未写"],
        ]],
        ["怎么调用", [
          ["方式", invokeText(manifest)],
          ["连接", connectText(manifest)],
        ]],
        ["要先准备什么", [["依赖", dependText(manifest)]]],
        ["产品密钥", [["名字", secretText(manifest)]]],
      ];
      const knowledge = manifest.knowledge;
      if (knowledge) {
        const taxonomy = knowledge.taxonomy || {};
        groups.push(["知识包", [
          ["分类", [taxonomy.business_category, taxonomy.business_domain, taxonomy.knowledge_kind].filter(Boolean).join(" / ")],
          ["入口", knowledge.entry || ""],
          ["密级", SECURITY_LABEL[knowledge.security_level] || knowledge.security_level || ""],
          ["范围", SCOPE_LABEL[knowledge.scope] || knowledge.scope || ""],
          ["词表", knowledge.vocabulary || ""],
          ["语言 / 许可", [knowledge.language, knowledge.license].filter(Boolean).join(" / ")],
          ["生产溯源", knowledge.producer || ""],
          ["本地主题", (knowledge.topics || []).join("、")],
        ]]);
      }
      const plugin = manifest.plugin;
      if (plugin) {
        groups.push(["插件", [
          ["分级", plugin.tier || ""],
          ["形态", plugin.form || ""],
          ["入口", plugin.entry || plugin.client || ""],
          ["补丁", plugin.patch || ""],
          ["DSH 版本", plugin.dsh || ""],
          ["能力", (plugin.capabilities || []).join("、")],
          ["外部命令", (plugin.external || []).join("、")],
        ]]);
      }
      groups.forEach(([title, pairs]) => {
        const group = document.createElement("div");
        group.className = "group";
        group.append(text("h2", title));
        const list = document.createElement("dl");
        list.className = "kv";
        pairs.forEach(([name, value]) => {
          if (!value) return;
          list.append(text("dt", name), text("dd", value));
        });
        group.append(list);
        root.append(group);
      });
      return root;
    }

    function invokeText(manifest) {
      const form = (manifest.invoke || {}).form || "";
      const entry = (manifest.skill || {}).entry || (manifest.invoke || {}).entry || "SKILL.md";
      if (form === "load_skill") return `按技能加载，入口是 ${entry}。不建立连接。`;
      if (form === "load_skill_then_mcp") return `先加载技能 ${entry}，再调用这个包里的 MCP。`;
      if (form === "list_tools") return "连上之后，向服务器要工具列表。";
      if (form === "documented") return "工具名已经写在清单里。";
      if (form === "exec_bin") return `执行 ${(manifest.runtime || {}).bin_name || "这个二进制"}。`;
      if (form === "install_plugin") return "在 DSH 的 Plugins 里安装；商店只暂存并核对文件，不执行插件代码。";
      return form || "没有调用说明。";
    }

    function connectText(manifest) {
      const mcp = manifest.mcp;
      if (!mcp) return "";
      if (mcp.transport === "stdio") return `本机命令：${mcp.command || ""} ${(mcp.args || []).join(" ")}`.trim();
      if (mcp.url) return `远程地址：${mcp.url}`;
      return "";
    }

    function dependText(manifest) {
      const depends = manifest.depends || {};
      const lines = [];
      (depends.store || []).forEach((item) => lines.push(`商店里的运行时 ${item.id} ${item.version || ""}`.trim()));
      (depends.skills || []).forEach((item) => lines.push(`先装技能 ${item.id} ${item.version || ""}`.trim()));
      (depends.mcp || []).forEach((item) => lines.push(`先装 MCP ${item.id} ${item.version || ""}`.trim()));
      (depends.external || []).forEach((item) => lines.push(`本机要有 ${item.name}。${item.install_guide || ""}`.trim()));
      return lines.join("\n") || "没有要先安装的东西。";
    }

    function secretText(manifest) {
      const secrets = manifest.secrets || [];
      if (!secrets.length) return "这份产品不向你要密钥。";
      return secrets.map((item) => `${item.name}：${item.description || "用途未写"}`).join("\n");
    }

    let auditOffset = 0;
    async function loadAudit(reset) {
      if (state.role !== "admin") {
        setState("audit-state", "用审核账号登录后才能看审计。", false);
        document.getElementById("audit-rows").replaceChildren();
        document.getElementById("audit-more").classList.add("hidden");
        return;
      }
      if (reset) {
        auditOffset = 0;
        document.getElementById("audit-rows").replaceChildren();
      }
      const action = document.getElementById("audit-action").value.trim();
      const packageId = document.getElementById("audit-package").value.trim();
      const params = new URLSearchParams({ limit: "50", offset: String(auditOffset) });
      if (action) params.set("action", action);
      if (packageId) params.set("package_id", packageId);
      setState("audit-state", "正在读取。", false);
      try {
        const page = await api("/v1/audit?" + params.toString());
        const rows = document.getElementById("audit-rows");
        (page.items || []).forEach((item) => {
          const tr = document.createElement("tr");
          [item.id, item.actor_name || item.actor, item.action, item.package_id || "", item.version || "", item.channel || "", item.at || ""]
            .forEach((value) => tr.append(text("td", value)));
          rows.append(tr);
        });
        auditOffset += (page.items || []).length;
        setState("audit-state", `共 ${page.total} 条，已显示 ${auditOffset} 条。`, false);
        document.getElementById("audit-more").classList.toggle("hidden", auditOffset >= page.total);
      } catch (error) {
        setState("audit-state", error.message, true);
      }
    }

    document.getElementById("audit-query").addEventListener("click", () => loadAudit(true));
    document.getElementById("audit-export").addEventListener("click", () => {
      const params = new URLSearchParams();
      const action = document.getElementById("audit-action").value.trim();
      const packageId = document.getElementById("audit-package").value.trim();
      if (action) params.set("action", action);
      if (packageId) params.set("package_id", packageId);
      window.location.assign("/v1/audit.csv" + (params.toString() ? "?" + params.toString() : ""));
    });
    document.getElementById("audit-more").addEventListener("click", () => loadAudit(false));
    document.getElementById("namespace-transfer").addEventListener("click", async () => {
      const namespace = document.getElementById("namespace-name").value.trim();
      const account = document.getElementById("namespace-owner").value;
      if (!namespace || !account) {
        setState("namespace-state", "命名空间和接收者都要填。", true);
        return;
      }
      try {
        await api("/v1/namespaces/" + encodeURIComponent(namespace) + "/transfer", {
          method: "POST",
          body: JSON.stringify({ account_id: account }),
        });
        setState("namespace-state", `已把 ${namespace} 转给所选账号。`, false);
      } catch (error) {
        setState("namespace-state", error.message, true);
      }
    });

    document.getElementById("back-shelf").addEventListener("click", () => {
      show(state.kind === "runtime" ? "runtime" : "shelf");
    });

    document.getElementById("show-plan").addEventListener("click", async () => {
      const button = document.getElementById("show-plan");
      const body = document.getElementById("plan-body");
      document.getElementById("plan-drawer").showModal();
      if (!signedIn()) {
        body.textContent = "请先登录。本商店的产品，均需要登录后，才能在页面进行下载。如用 AI 工具浏览、下载、使用，请查看 连接 菜单。";
        return;
      }
      button.disabled = true;
      body.textContent = "正在生成安装说明。";
      try {
        const plan = await api("/v1/install-plan", {
          method: "POST",
          body: JSON.stringify({
            id: state.packageId,
            version: document.getElementById("plan-version").value,
            client: document.getElementById("plan-client").value,
            platform: document.getElementById("plan-platform").value,
          }),
        });
        body.textContent = (plan.steps || []).map((step) => JSON.stringify(step)).join("\n");
      } catch (error) {
        body.textContent = (error.rule ? error.rule + " " : "") + error.message;
      } finally {
        button.disabled = false;
      }
    });
    document.getElementById("close-plan").addEventListener("click", () => document.getElementById("plan-drawer").close());

    document.getElementById("new-product").addEventListener("click", () => openForm("new", null, null));
    document.getElementById("new-version").addEventListener("click", () => openForm("new_version", null, state.base));
    document.getElementById("add-platform").addEventListener("click", () => openForm("add_platform", null, state.base));

    let skillExtras = {};
    let noteFiles = null;
    let pluginFiles = null;
    let catalogSeq = 0;
    let catalogAbort = null;
    let readingFiles = false;

    function openForm(mode, submissionId, manifest) {
      skillExtras = {};
      noteFiles = null;
      state.mode = mode;
      state.submissionId = submissionId;
      state.base = manifest;
      const titles = {
        new: "上架新产品",
        new_version: "上架新版本",
        add_platform: "追加平台",
        revise: "修订此提交",
      };
      document.getElementById("form-title").textContent = titles[mode];
      document.getElementById("form-submit").textContent = mode === "revise" ? "修订此提交" : "提交待审核";
      fillForm(manifest, mode);
      document.getElementById("form-error").hidden = true;
      document.getElementById("form-rules").replaceChildren();
      show("form");
      document.getElementById("f-version").focus();
    }

    function fillForm(manifest, mode) {
      const form = document.getElementById("sheet");
      form.reset();
      pluginFiles = null;
      const source = manifest || {};
      document.getElementById("f-id").value = source.id || "";
      document.getElementById("f-version").value = mode === "new_version" ? "" : (source.version || "");
      document.getElementById("f-kind").value = source.kind || "skill";
      document.getElementById("f-submit-kind").value = mode === "add_platform" ? "add_platform" : "new_version";
      document.getElementById("f-author").value = (source.author || {}).display_name || "";
      document.getElementById("f-title").value = source.title || "";
      document.getElementById("f-summary").value = source.summary || "";
      document.getElementById("f-description").value = source.description || "";
      document.getElementById("f-changes").value = mode === "new_version" ? "" : (source.changes || "");
      document.getElementById("f-platforms").value = mode === "add_platform" ? "" : (source.platforms || []).join(",");
      document.getElementById("f-clients").value = (source.clients || []).join(",");
      const mcp = source.mcp || {};
      document.getElementById("f-transport").value = mcp.transport || "streamable-http";
      document.getElementById("f-url").value = mcp.url || "";
      document.getElementById("f-command").value = mcp.command || "";
      document.getElementById("f-args").value = (mcp.args || []).join(" ");
      const invoke = source.invoke || {};
      if (invoke.form === "list_tools" || invoke.form === "documented") {
        document.getElementById("f-invoke").value = invoke.form;
      }
      const plugin = source.plugin || {};
      document.getElementById("f-plugin-tier").value = plugin.tier || "A";
      document.getElementById("f-plugin-form").value = plugin.form || "both";
      document.getElementById("f-plugin-entry").value = plugin.entry || "index.js";
      document.getElementById("f-plugin-client").value = plugin.client || "client.js";
      document.getElementById("f-plugin-patch").value = plugin.patch || "cordis.patch.yml";
      document.getElementById("f-plugin-dsh").value = plugin.dsh || ">=1.5 <2";
      document.getElementById("f-plugin-files").value = (plugin.files || []).join(",");
      document.getElementById("f-plugin-caps").value = (plugin.capabilities || []).join(",");
      document.getElementById("f-plugin-external").value = (plugin.external || []).join(",");
      document.getElementById("f-bin").value = (source.runtime || {}).bin_name || "";
      const depends = source.depends || {};
      document.getElementById("f-store").value = lines(depends.store);
      document.getElementById("f-skills").value = lines(depends.skills);
      document.getElementById("f-mcp-deps").value = lines(depends.mcp);
      const external = (depends.external || [])[0] || {};
      document.getElementById("f-ext-name").value = external.name || "";
      document.getElementById("f-ext-range").value = external.version_range || "";
      document.getElementById("f-ext-detect").value = (external.detect || []).join(" ");
      document.getElementById("f-ext-guide").value = external.install_guide || "";
      document.getElementById("f-ext-url").value = ((external.resources || [])[0] || {}).url || "";
      document.getElementById("f-secrets").value = (source.secrets || []).map((item) => `${item.name} ${item.description || ""}`).join("\n");
      const lock = mode === "add_platform";
      ["f-id", "f-version", "f-kind", "f-submit-kind", "f-author", "f-title", "f-summary", "f-description", "f-changes", "f-clients"]
        .forEach((id) => { document.getElementById(id).readOnly = lock || (mode === "new_version" && (id === "f-id" || id === "f-kind")); });
      document.getElementById("f-kind").disabled = lock || mode === "new_version" || mode === "revise";
      document.getElementById("f-submit-kind").disabled = mode !== "new";
      syncKind();
    }

    function lines(items) {
      return (items || []).map((item) => `${item.id} ${item.version || item.version_range || "*"}`).join("\n");
    }

    function syncKind() {
      const kind = document.getElementById("f-kind").value;
      const skill = kind === "skill" || kind === "mcp+skill";
      const mcp = kind === "mcp" || kind === "mcp+skill";
      const runtime = kind === "runtime";
      const knowledge = kind === "knowledge";
      const plugin = kind === "plugin";
      const invokeNotes = {
        skill: "这是一份技能，从 SKILL.md 读起。不用填连接地址。",
        mcp: "远程服务只填地址，本机服务只填命令和参数。没有要交的文件。",
        "mcp+skill": "先填怎么连接，再交 SKILL.md。",
        runtime: "这是要随版本发布的命令文件，按平台各交一份。",
        knowledge: "这是一组笔记。从入口那一篇读起，链接只能指向包里的篇名。",
        plugin: "这是 DSH 插件。填清单字段并选插件目录；package.json 必须在根上。",
      };
      document.getElementById("invoke-note").textContent = invokeNotes[kind];
      document.getElementById("knowledge-fields").classList.toggle("hidden", !knowledge);
      document.getElementById("mcp-fields").classList.toggle("hidden", !mcp);
      document.getElementById("mcp-invoke").classList.toggle("hidden", kind !== "mcp");
      document.getElementById("runtime-fields").classList.toggle("hidden", !runtime);
      document.getElementById("plugin-fields").classList.toggle("hidden", !plugin);
      document.getElementById("plugin-file").classList.toggle("hidden", !plugin);
      document.getElementById("skill-invoke").classList.toggle("hidden", !skill);
      document.getElementById("skill-invoke").textContent = kind === "mcp+skill"
        ? "先读 SKILL.md，再连接 MCP。"
        : "从 SKILL.md 读起。";
      document.getElementById("skill-file").classList.toggle("hidden", !skill);
      document.getElementById("knowledge-file").classList.toggle("hidden", !knowledge);
      document.getElementById("runtime-file").classList.toggle("hidden", !runtime);
      document.getElementById("depends-group").classList.toggle("hidden", knowledge || plugin);
      document.getElementById("secrets-group").classList.toggle("hidden", knowledge);
      document.getElementById("artifact-group").classList.toggle("hidden", !skill && !runtime && !knowledge && !plugin);
      if (plugin) {
        const platforms = document.getElementById("f-platforms");
        const clients = document.getElementById("f-clients");
        platforms.value = "any";
        clients.value = "deepseek-harness";
      }
      const submitKind = document.getElementById("f-submit-kind");
      submitKind.querySelector('option[value="add_platform"]').hidden = !runtime;
      if (!runtime && submitKind.value === "add_platform" && !submitKind.disabled) {
        submitKind.value = "new_version";
      }
      syncTransport();
    }

    function syncTransport() {
      const stdio = document.getElementById("f-transport").value === "stdio";
      document.getElementById("mcp-http").classList.toggle("hidden", stdio);
      document.getElementById("mcp-stdio").classList.toggle("hidden", !stdio);
    }
    document.getElementById("f-kind").addEventListener("change", syncKind);
    document.getElementById("f-transport").addEventListener("change", syncTransport);
    document.getElementById("f-skill-dir").addEventListener("change", (event) => {
      ingestSkillFiles(event.target.files);
    });
    document.getElementById("f-skill-files").addEventListener("change", (event) => {
      ingestSkillFiles(event.target.files);
    });
    document.getElementById("f-notes-dir").addEventListener("change", (event) => {
      ingestNoteFiles(event.target.files);
    });
    document.getElementById("f-notes").addEventListener("change", (event) => {
      ingestNoteFiles(event.target.files);
    });
    document.getElementById("f-plugin-dir").addEventListener("change", (event) => {
      ingestPluginFiles(event.target.files);
    });

    function isIgnoredClientPath(rel) {
      const clean = String(rel || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
      if (!clean || clean === ".") return false;
      const parts = clean.split("/").filter((part) => part && part !== ".");
      if (!parts.length) return false;
      const dirs = new Set(["__MACOSX", ".git", ".svn", ".hg", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]);
      const names = new Set([".DS_Store", "Thumbs.db", "desktop.ini", ".storeignore"]);
      for (const part of parts.slice(0, -1)) {
        if (dirs.has(part) || part.startsWith("._")) return true;
      }
      const name = parts[parts.length - 1];
      return names.has(name) || dirs.has(name) || name.startsWith("._");
    }

    function relativeFromPicker(file) {
      if (!file.webkitRelativePath) return file.name;
      const parts = file.webkitRelativePath.split("/").filter(Boolean);
      return parts.slice(1).join("/") || file.name;
    }

    function fillFromManifest(data) {
      if (!data || typeof data !== "object") return;
      const set = (id, value) => {
        const el = document.getElementById(id);
        if (!el || value == null || value === "") return;
        el.value = String(value);
      };
      set("f-id", data.id);
      set("f-version", data.version);
      set("f-title", data.title);
      set("f-summary", data.summary);
      set("f-description", data.description);
      set("f-changes", data.changes);
      if (Array.isArray(data.platforms)) set("f-platforms", data.platforms.join(","));
      if (Array.isArray(data.clients)) set("f-clients", data.clients.join(","));
      if (data.author && data.author.display_name) set("f-author", data.author.display_name);
      const plugin = data.plugin || {};
      set("f-plugin-tier", plugin.tier);
      set("f-plugin-form", plugin.form);
      set("f-plugin-entry", plugin.entry);
      set("f-plugin-client", plugin.client);
      set("f-plugin-patch", plugin.patch);
      set("f-plugin-dsh", plugin.dsh);
      if (Array.isArray(plugin.files)) set("f-plugin-files", plugin.files.join(","));
      if (Array.isArray(plugin.capabilities)) set("f-plugin-caps", plugin.capabilities.join(","));
      if (Array.isArray(plugin.external)) set("f-plugin-external", plugin.external.join(","));
      const kind = document.getElementById("f-kind");
      if (data.kind && kind && !kind.disabled && kind.querySelector(`option[value="${data.kind}"]`)) {
        kind.value = data.kind;
        syncKind();
      }
    }

    async function ingestSkillFiles(fileList) {
      readingFiles = true;
      const incoming = {};
      let count = 0;
      try {
        for (const file of fileList) {
          const rel = relativeFromPicker(file);
          if (!rel || isIgnoredClientPath(rel)) continue;
          if (rel === "manifest.json") {
            try {
              fillFromManifest(JSON.parse(await file.text()));
            } catch (error) {
              /* 清单读不出来就不填，也不放进制品 */
            }
            continue;
          }
          if (rel === "SKILL.md") {
            document.getElementById("f-skill").value = await file.text();
            count += 1;
            continue;
          }
          incoming[rel] = await b64(await file.arrayBuffer());
          count += 1;
        }
        skillExtras = incoming;
        const note = document.getElementById("skill-dir-note");
        note.textContent = count
          ? `已读入 ${count} 个文件。清单会填进表单，不随制品提交。系统尾巴文件不提交。`
          : "清单会填进表单，不随制品提交。系统尾巴文件不提交。";
      } finally {
        readingFiles = false;
      }
    }

    async function ingestNoteFiles(fileList) {
      readingFiles = true;
      const incoming = {};
      try {
        for (const file of fileList) {
          const rel = relativeFromPicker(file);
          if (!rel || isIgnoredClientPath(rel) || rel.includes("/")) continue;
          if (!rel.toLowerCase().endsWith(".md")) continue;
          incoming[rel] = await b64(await file.arrayBuffer());
        }
        noteFiles = incoming;
        const count = Object.keys(incoming).length;
        document.getElementById("notes-dir-note").textContent =
          `已保留根上 ${count} 篇 Markdown。系统尾巴文件会被丢掉。子目录里的文件不进入知识包。`;
      } finally {
        readingFiles = false;
      }
    }

    async function ingestPluginFiles(fileList) {
      readingFiles = true;
      const incoming = {};
      let manifestData = null;
      try {
        for (const file of fileList) {
          const rel = relativeFromPicker(file);
          if (!rel || isIgnoredClientPath(rel)) continue;
          if (rel === "manifest.json") {
            try { manifestData = JSON.parse(await file.text()); } catch { /* 只填表 */ }
            continue;
          }
          incoming[rel] = await b64(await file.arrayBuffer());
        }
        pluginFiles = incoming;
        const names = Object.keys(incoming).sort();
        const filesInput = document.getElementById("f-plugin-files");
        if (names.length && !filesInput.value.trim()) filesInput.value = names.join(",");
        document.getElementById("plugin-dir-note").textContent = names.length
          ? `已读入 ${names.length} 个文件。根上必须有 package.json；manifest.json 只填表，不随制品提交。`
          : "还没有读入插件文件。根上必须有 package.json。";
        if (manifestData) fillFromManifest(manifestData);
      } finally {
        readingFiles = false;
      }
    }

    function splitList(value) {
      return value.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean);
    }

    function depLines(value) {
      return value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
        const [id, version] = line.split(/\s+/, 2);
        return { id, version: version || "*" };
      });
    }

    async function manifestFromForm() {
      const kind = document.getElementById("f-kind").value;
      const mode = state.mode;
      if (readingFiles) throw new Error("正在读取所选文件，请稍候再提交");
      if (mode === "add_platform" && state.base) {
        const copy = JSON.parse(JSON.stringify(state.base));
        const platform = document.getElementById("f-platforms").value.trim();
        const file = document.getElementById("f-runtime-file").files[0];
        if (!file) throw Object.assign(new Error("追加平台需要上传这个平台的文件"), { rule: "RUNTIME_ASSET" });
        const digest = await sha256(await file.arrayBuffer());
        copy.platforms = [platform];
        copy.runtime = copy.runtime || { bin_name: document.getElementById("f-bin").value.trim() };
        copy.runtime.assets = [{ platform, path: `${platform}/${file.name}`, sha256: digest }];
        return { manifest: copy, files: { [`${platform}/${file.name}`]: await b64(await file.arrayBuffer()) } };
      }
      const manifest = {
        spec_version: 1,
        id: document.getElementById("f-id").value.trim(),
        version: document.getElementById("f-version").value.trim(),
        kind,
        title: document.getElementById("f-title").value.trim(),
        summary: document.getElementById("f-summary").value.trim(),
        description: document.getElementById("f-description").value.trim(),
        changes: document.getElementById("f-changes").value.trim(),
        author: { display_name: document.getElementById("f-author").value.trim() },
        platforms: splitList(document.getElementById("f-platforms").value),
        clients: splitList(document.getElementById("f-clients").value),
        depends: {
          store: depLines(document.getElementById("f-store").value),
          skills: depLines(document.getElementById("f-skills").value),
          mcp: depLines(document.getElementById("f-mcp-deps").value),
          external: [],
        },
        secrets: document.getElementById("f-secrets").value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
          const [name, ...rest] = line.split(/\s+/);
          return { name, description: rest.join(" ") };
        }),
      };
      if (kind === "plugin") {
        const files = pluginFiles || {};
        if (!files["package.json"]) throw new Error("插件目录根上要有 package.json。请先选择插件目录。");
        const form = document.getElementById("f-plugin-form").value;
        const plugin = {
          tier: document.getElementById("f-plugin-tier").value,
          form,
          entry: document.getElementById("f-plugin-entry").value.trim() || undefined,
          client: document.getElementById("f-plugin-client").value.trim() || undefined,
          patch: document.getElementById("f-plugin-patch").value.trim() || "cordis.patch.yml",
          files: splitList(document.getElementById("f-plugin-files").value),
          dsh: document.getElementById("f-plugin-dsh").value.trim(),
          capabilities: splitList(document.getElementById("f-plugin-caps").value),
          external: splitList(document.getElementById("f-plugin-external").value),
        };
        if (!plugin.files.length) plugin.files = Object.keys(files).sort();
        if (!plugin.entry) delete plugin.entry;
        if (!plugin.client) delete plugin.client;
        manifest.plugin = plugin;
        manifest.invoke = { form: "install_plugin" };
        Object.assign(files, pluginFiles);
        return { manifest, files };
      }
      const extName = document.getElementById("f-ext-name").value.trim();
      if (extName) {
        manifest.depends.external.push({
          name: extName,
          version_range: document.getElementById("f-ext-range").value.trim(),
          detect: document.getElementById("f-ext-detect").value.trim().split(/\s+/),
          install_guide: document.getElementById("f-ext-guide").value.trim(),
          resources: [{
            title: extName,
            url: document.getElementById("f-ext-url").value.trim(),
            platform: "any",
          }],
        });
      }
      const files = {};
      if (kind === "skill" || kind === "mcp+skill") {
        manifest.skill = { entry: "SKILL.md" };
        manifest.invoke = {
          form: kind === "mcp+skill" ? "load_skill_then_mcp" : "load_skill",
          entry: "SKILL.md",
        };
        const skill = document.getElementById("f-skill").value;
        if (skill) files["SKILL.md"] = b64text(skill);
        Object.assign(files, skillExtras);
      }
      if (kind === "mcp" || kind === "mcp+skill") {
        const transport = document.getElementById("f-transport").value;
        manifest.mcp = { transport };
        if (transport === "stdio") {
          manifest.mcp.command = document.getElementById("f-command").value.trim();
          manifest.mcp.args = document.getElementById("f-args").value.trim().split(/\s+/).filter(Boolean);
        } else {
          manifest.mcp.url = document.getElementById("f-url").value.trim();
        }
        if (kind === "mcp") manifest.invoke = { form: document.getElementById("f-invoke").value };
      }
      if (kind === "knowledge") {
        const entry = document.getElementById("f-entry").value.trim();
        manifest.knowledge = { entry };
        manifest.invoke = { form: "read_notes", entry };
        manifest.depends = { store: [], external: [], skills: [], mcp: [] };
        manifest.secrets = [];
        if (noteFiles) {
          Object.assign(files, noteFiles);
        } else {
          const notes = document.getElementById("f-notes").files;
          for (const file of notes) {
            if (isIgnoredClientPath(file.name)) continue;
            files[file.name] = await b64(await file.arrayBuffer());
          }
        }
      }
      if (kind === "runtime") {
        const file = document.getElementById("f-runtime-file").files[0];
        const bin = document.getElementById("f-bin").value.trim();
        manifest.runtime = { bin_name: bin, assets: [] };
        manifest.invoke = { form: "exec_bin", bin_name: bin };
        if (file) {
          const platform = (manifest.platforms || [])[0];
          const digest = await sha256(await file.arrayBuffer());
          const path = `${platform}/${file.name}`;
          manifest.runtime.assets = [{ platform, path, sha256: digest }];
          files[path] = await b64(await file.arrayBuffer());
        }
      }
      return { manifest, files };
    }

    function b64text(value) {
      const bytes = new TextEncoder().encode(value);
      let binary = "";
      bytes.forEach((item) => { binary += String.fromCharCode(item); });
      return btoa(binary);
    }

    async function b64(buffer) {
      const bytes = new Uint8Array(buffer);
      let binary = "";
      bytes.forEach((item) => { binary += String.fromCharCode(item); });
      return btoa(binary);
    }

    async function sha256(buffer) {
      const digest = await crypto.subtle.digest("SHA-256", buffer);
      return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
    }

    document.getElementById("sheet").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = document.getElementById("form-submit");
      const error = document.getElementById("form-error");
      const rules = document.getElementById("form-rules");
      button.disabled = true;
      error.hidden = true;
      rules.replaceChildren();
      let missing = firstEmpty([
        ["f-id", "id"],
        ["f-version", "version"],
        ["f-author", "author.display_name"],
        ["f-title", "title"],
        ["f-summary", "summary"],
        ["f-description", "description"],
        ["f-changes", "changes"],
        ["f-platforms", "platforms"],
        ["f-clients", "clients"],
      ]);
      if (!missing && document.getElementById("f-kind").value === "plugin") {
        missing = firstEmpty([["f-plugin-patch", "plugin.patch"], ["f-plugin-dsh", "plugin.dsh"]]);
      }
      if (missing) {
        button.disabled = false;
        error.hidden = false;
        error.textContent = missing + "是必填。";
        return;
      }
      try {
        const built = await manifestFromForm();
        const checked = await api("/v1/validate", { method: "POST", body: JSON.stringify({ manifest: built.manifest }) });
        const failures = (checked.findings || []).filter((item) => item.severity === "error");
        if (failures.length) {
          showFindings(rules, failures);
          return;
        }
        const path = state.mode === "revise"
          ? `/v1/submissions/${state.submissionId}/revise`
          : "/v1/submissions";
        const result = await api(path, {
          method: "POST",
          body: JSON.stringify({
            manifest: built.manifest,
            files: built.files,
            submit_kind: document.getElementById("f-submit-kind").value,
          }),
        });
        if (result.status !== "pending") {
          showFindings(rules, (result.rules || []).map((rule) => ({ rule, path: "", message: "" })));
          error.hidden = false;
          error.textContent = "校验没有通过，没有进入审核队列。";
          return;
        }
        show("mine");
      } catch (caught) {
        error.hidden = false;
        error.textContent = (caught.rule ? caught.rule + " " : "") + caught.message;
      } finally {
        button.disabled = false;
      }
    });

    function showFindings(container, findings) {
      container.replaceChildren();
      findings.forEach((item) => {
        container.append(text("div", `${item.rule}  ${item.path}  ${item.message}`.trim(), "rule"));
      });
    }

    async function loadMine() {
      const list = document.getElementById("mine-list");
      list.replaceChildren();
      if (!signedIn()) {
        setState("mine-state", "先登录。作者用自己创建的 KEY。", false);
        return;
      }
      setState("mine-state", "正在读取提交。", false);
      try {
        const data = await api("/v1/mine");
        const items = data.items || [];
        if (!items.length) {
          setState("mine-state", "还没有提交。", false);
          return;
        }
        setState("mine-state", "", false);
        for (const item of items) {
          const block = document.createElement("article");
          block.className = "card";
          block.classList.add("stack");
          block.append(text("h3", `${item.package_id} · ${item.version}`));
          block.append(text("p", item.status, "foot"));
          if (item.review_reason) block.append(text("p", "退回原因：" + item.review_reason));
          if (item.status === "pending" || item.status === "rejected") {
            const button = text("button", "修订此提交", "btn");
            button.type = "button";
            button.addEventListener("click", () => revise(item.id));
            block.append(button);
          }
          list.append(block);
        }
      } catch (error) {
        setState("mine-state", error.message, true);
      }
    }

    async function revise(submissionId) {
      const detail = await api("/v1/submissions/" + submissionId);
      openForm("revise", submissionId, detail.manifest);
      if (detail.review_reason) {
        const error = document.getElementById("form-error");
        error.hidden = false;
        error.textContent = "上次退回：" + detail.review_reason;
      }
      showFindings(document.getElementById("form-rules"), detail.findings || []);
    }

    async function loadReviews() {
      const queue = document.getElementById("review-queue");
      const detail = document.getElementById("review-detail");
      queue.replaceChildren();
      detail.replaceChildren(text("p", "选左边的一条。", "state"));
      document.getElementById("review-back").classList.add("hidden");
      if (state.role !== "admin") {
        setState("review-state", "先用审核的 KEY 登录。作者看不到这一页。", false);
        return;
      }
      setState("review-state", "正在读取审核队列。", false);
      try {
        const data = await api("/v1/reviews");
        const items = data.items || [];
        if (!items.length) {
          setState("review-state", "没有待审核的提交。", false);
          document.getElementById("review-back").classList.remove("hidden");
          return;
        }
        setState("review-state", "", false);
        items.forEach((item) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "list-btn";
          button.append(text("strong", item.title || item.package_id));
          button.append(document.createElement("br"));
          button.append(text("span", `${item.status} · ${item.submit_kind} · ${item.package_id}`, "foot"));
          button.addEventListener("click", () => openReview(item.id, button));
          queue.append(button);
        });
      } catch (error) {
        setState("review-state", error.message, true);
      }
    }

    function renderDiff(patch) {
      const pre = document.createElement("pre");
      pre.className = "diff-view";
      String(patch).split("\n").forEach((line) => {
        const row = document.createElement("div");
        if (line.startsWith("+") && !line.startsWith("+++")) row.className = "diff-add";
        else if (line.startsWith("-") && !line.startsWith("---")) row.className = "diff-del";
        else if (line.startsWith("@@")) row.className = "diff-hunk";
        row.textContent = line;
        pre.append(row);
      });
      return pre;
    }

    async function openReview(submissionId, button) {
      document.querySelectorAll("#review-queue .list-btn").forEach((item) => item.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
      const detail = document.getElementById("review-detail");
      detail.replaceChildren(text("p", "正在读取。", "state"));
      try {
        const sheetData = await api("/v1/reviews/" + submissionId);
        detail.replaceChildren();
        const head = document.createElement("div");
        head.className = "group";
        const tags = document.createElement("div");
        tags.className = "tags";
        tags.append(text("span", "待审核", "tag gold"));
        head.append(tags);
        detail.append(head, sheet(sheetData.manifest, sheetData.submit_kind));
        const checks = document.createElement("div");
        checks.className = "group";
        checks.append(text("h2", "校验"));
        if (!(sheetData.findings || []).length) checks.append(text("p", "没有规则。"));
        (sheetData.findings || []).forEach((item) => checks.append(text("div", `${item.rule}  ${item.path}  ${item.message}`, "rule")));
        const diff = document.createElement("div");
        diff.className = "group";
        diff.append(text("h2", "与上一版"));
        if (!(sheetData.diff || []).length) diff.append(text("p", "无已发布版本。", "diff"));
        else diff.append(text("pre", (sheetData.diff || []).map((item) => `${item.path}\n- ${JSON.stringify(item.before)}\n+ ${JSON.stringify(item.after)}`).join("\n\n"), "diff"));
        let reviewReady = true;
        let publishButton = null;
        let pluginReviewGroup = null;
        if (sheetData.manifest.kind === "plugin") {
          reviewReady = Boolean(sheetData.plugin_review);
          pluginReviewGroup = document.createElement("div");
          pluginReviewGroup.className = "group";
          pluginReviewGroup.append(text("h2", "插件审核清单"));
          pluginReviewGroup.append(text("p", "逐项确认后才能发布。记录会保存审核人与制品 sha256；修订后要重新审。", "foot"));
          const existing = (sheetData.plugin_review || {}).checklist || {};
          const boxes = {};
          Object.entries(PLUGIN_REVIEW_LABELS).forEach(([key, label]) => {
            const row = document.createElement("label");
            row.className = "check-row";
            const box = document.createElement("input");
            box.type = "checkbox";
            box.checked = Boolean(existing[key]);
            box.dataset.item = key;
            boxes[key] = box;
            row.append(box, document.createTextNode(label));
            pluginReviewGroup.append(row);
          });
          const reviewState = text("p", "", "foot");
          if (sheetData.plugin_review) {
            reviewState.textContent = "已记录审核清单，可以发布；如重新记录会覆盖。";
          }
          const record = text("button", "记录审核", "btn primary");
          record.type = "button";
          record.addEventListener("click", async () => {
            record.disabled = true;
            reviewState.textContent = "";
            const checklist = {};
            Object.entries(boxes).forEach(([key, box]) => { if (box.checked) checklist[key] = true; });
            try {
              await api(`/v1/submissions/${submissionId}/plugin-review`, {
                method: "POST",
                body: JSON.stringify({ checklist }),
              });
              reviewReady = true;
              reviewState.textContent = "已记录审核清单，可以发布。";
              if (publishButton) publishButton.disabled = false;
            } catch (error) {
              reviewState.textContent = (error.rule ? error.rule + " " : "") + error.message;
              reviewState.classList.add("error");
            } finally {
              record.disabled = false;
            }
          });
          pluginReviewGroup.append(record, reviewState);
        }
        const actions = document.createElement("div");
        actions.className = "row";
        const publish = text("button", "发布", "btn primary");
        publish.type = "button";
        publish.disabled = !reviewReady;
        publishButton = publish;
        const reject = text("button", "退回", "btn danger");
        reject.type = "button";
        const reason = document.createElement("textarea");
        reason.placeholder = "作者和 MCP get_submission 会看到这句";
        const note = text("p", "", "field-error");
        publish.addEventListener("click", async () => {
          publish.disabled = true;
          reject.disabled = true;
          note.textContent = "";
          try {
            await api(`/v1/submissions/${submissionId}/approve`, { method: "POST" });
            loadReviews();
          } catch (error) {
            note.textContent = (error.rule ? error.rule + " " : "") + error.message;
            publish.disabled = false;
            reject.disabled = false;
          }
        });
        reject.addEventListener("click", async () => {
          if (!reason.value.trim()) { reason.focus(); return; }
          publish.disabled = true;
          reject.disabled = true;
          note.textContent = "";
          try {
            await api(`/v1/submissions/${submissionId}/reject`, {
              method: "POST",
              body: JSON.stringify({ reason: reason.value.trim() }),
            });
            loadReviews();
          } catch (error) {
            note.textContent = (error.rule ? error.rule + " " : "") + error.message;
            publish.disabled = false;
            reject.disabled = false;
          }
        });
        actions.append(publish, reject);
        detail.append(checks);
        if (pluginReviewGroup) detail.append(pluginReviewGroup);
        detail.append(diff, actions, text("label", "退回原因"), reason, note);
        const files = document.createElement("div");
        files.className = "group";
        files.append(text("h2", "制品"));
        const names = sheetData.files || [];
        if (!names.length) {
          files.append(text("p", "没有制品文件。", "diff"));
        } else {
          const list = document.createElement("div");
          list.className = "file-list";
          const preview = document.createElement("pre");
          preview.className = "diff-view";
          names.forEach((name) => {
            const fileButton = document.createElement("button");
            fileButton.type = "button";
            fileButton.className = "btn";
            fileButton.textContent = name;
            fileButton.addEventListener("click", () => {
              const body = (sheetData.files_preview || {})[name];
              preview.textContent = body == null ? "没有预览。" : body;
            });
            list.append(fileButton);
          });
          files.append(list, preview);
        }
        const fileDiffs = document.createElement("div");
        fileDiffs.className = "group";
        fileDiffs.append(text("h2", "制品差异"));
        const diffs = sheetData.file_diffs || [];
        if (!diffs.length) fileDiffs.append(text("p", "没有上一份制品，或正文没有变化。", "foot"));
        diffs.forEach((item) => {
          fileDiffs.append(text("p", `${item.status}  ${item.path}`, "foot"));
          fileDiffs.append(renderDiff(item.diff || ""));
        });
        detail.append(files, fileDiffs);
      } catch (error) {
        detail.replaceChildren(text("p", error.message, "state error"));
      }
    }

    const savedTheme = sessionStorage.getItem("qilin-theme");
    if (savedTheme) {
      document.documentElement.setAttribute("data-theme", savedTheme);
      document.getElementById("theme").textContent = savedTheme === "dark" ? "浅色" : "深色";
    }
    function openLogin() {
      setState("login-state", "", false);
      document.getElementById("login-box").showModal();
      document.getElementById("login-username").focus();
    }

    document.getElementById("login-submit").addEventListener("click", async () => {
      const missing = firstEmpty([
        ["login-username", "用户名"],
        ["login-password", "口令"],
      ]);
      if (missing) {
        setState("login-state", missing + "是必填。", true);
        return;
      }
      try {
        await api("/v1/login", {
          method: "POST",
          body: JSON.stringify({
            username: document.getElementById("login-username").value,
            password: document.getElementById("login-password").value,
          }),
        });
        document.getElementById("login-password").value = "";
        document.getElementById("login-box").close();
        setState("login-state", "", false);
        state.issued = null;
        await refreshWho();
      } catch (error) {
        setState("login-state", error.message, true);
      }
    });

    document.getElementById("reg-password").addEventListener("input", () => markPassword("reg-password", "password-checks"));
    document.getElementById("reg-username").addEventListener("input", () => markPassword("reg-password", "password-checks"));
    document.getElementById("new-password").addEventListener("input", () => markPassword("new-password", "new-password-checks"));

    document.getElementById("reg-submit").addEventListener("click", async () => {
      const required = [
        ["reg-username", "用户名"],
        ["reg-real-name", "姓名"],
        ["reg-nickname", "昵称"],
        ["reg-password", "口令"],
        ["reg-password-again", "再输入一次口令"],
      ];
      const missing = firstEmpty(required);
      if (missing) {
        setState("reg-state", missing + "是必填。", true);
        return;
      }
      const password = document.getElementById("reg-password").value;
      if (passwordProblems(password, document.getElementById("reg-username").value).length) {
        setState("reg-state", "口令还没满足上面的规则。", true);
        markPassword("reg-password", "password-checks");
        return;
      }
      if (password !== document.getElementById("reg-password-again").value) {
        setState("reg-state", "两次口令不一致。", true);
        document.getElementById("reg-password-again").setAttribute("aria-invalid", "true");
        return;
      }
      try {
        const created = await api("/v1/account", {
          method: "POST",
          body: JSON.stringify({
            username: document.getElementById("reg-username").value,
            real_name: document.getElementById("reg-real-name").value,
            nickname: document.getElementById("reg-nickname").value,
            department: document.getElementById("reg-department").value,
            job_title: document.getElementById("reg-title").value,
            password,
            password_again: document.getElementById("reg-password-again").value,
          }),
        });
        state.issued = { key: created.key, mcp: created.mcp };
        ["reg-password", "reg-password-again"].forEach((id) => { document.getElementById(id).value = ""; });
        setState("reg-state", "已创建。KEY 在「连接」里，只这一次。", false);
        await refreshWho();
        show("connect");
      } catch (error) {
        setState("reg-state", error.message, true);
      }
    });

    document.getElementById("save-profile").addEventListener("click", async () => {
      const missing = firstEmpty([
        ["profile-real-name", "姓名"],
        ["profile-nickname", "昵称"],
      ]);
      if (missing) {
        setState("profile-state", missing + "是必填。", true);
        return;
      }
      try {
        await api("/v1/account/profile", {
          method: "POST",
          body: JSON.stringify({
            real_name: document.getElementById("profile-real-name").value,
            nickname: document.getElementById("profile-nickname").value,
            department: document.getElementById("profile-department").value,
            job_title: document.getElementById("profile-title").value,
          }),
        });
        setState("profile-state", "资料已保存。", false);
        await refreshWho();
      } catch (error) {
        setState("profile-state", error.message, true);
      }
    });

    document.getElementById("change-password").addEventListener("click", async () => {
      const next = document.getElementById("new-password").value;
      const missing = firstEmpty([
        ["old-password", "原来的口令"],
        ["new-password", "新口令"],
        ["new-password-again", "再输入一次新口令"],
      ]);
      if (missing) {
        setState("password-state", missing + "是必填。", true);
        return;
      }
      if (passwordProblems(next, document.getElementById("profile-username").value).length) {
        setState("password-state", "新口令还没满足上面的规则。", true);
        markPassword("new-password", "new-password-checks");
        return;
      }
      if (next !== document.getElementById("new-password-again").value) {
        setState("password-state", "两次新口令不一致。", true);
        return;
      }
      try {
        await api("/v1/account/password", {
          method: "POST",
          body: JSON.stringify({
            old_password: document.getElementById("old-password").value,
            new_password: next,
          }),
        });
        ["old-password", "new-password", "new-password-again"].forEach((id) => {
          document.getElementById(id).value = "";
        });
        markPassword("new-password", "new-password-checks");
        setState("password-state", "口令已修改。商店 MCP 的 KEY 没变。", false);
      } catch (error) {
        setState("password-state", error.message, true);
      }
    });

    function firstEmpty(pairs) {
      let missing = "";
      pairs.forEach(([id, label]) => {
        const input = document.getElementById(id);
        const empty = !input.value.trim();
        input.setAttribute("aria-invalid", empty ? "true" : "false");
        if (empty && !missing) {
          missing = label;
          input.focus();
        }
      });
      return missing;
    }

    function passwordProblems(value, username) {
      const problems = [];
      if (value !== value.trim() || value.length < 10 || value.length > 64) problems.push("len");
      if (!/[a-z]/.test(value)) problems.push("lower");
      if (!/[A-Z]/.test(value)) problems.push("upper");
      if (!/[0-9]/.test(value)) problems.push("digit");
      if (!username.trim() || value.toLowerCase().includes(username.trim().toLowerCase())) problems.push("user");
      return problems;
    }

    function markPassword(inputId, listId) {
      const value = document.getElementById(inputId).value;
      const username = inputId === "new-password"
        ? document.getElementById("profile-username").value
        : document.getElementById("reg-username").value;
      const problems = new Set(passwordProblems(value, username));
      document.querySelectorAll("#" + listId + " li").forEach((item) => {
        const met = value && !problems.has(item.dataset.rule);
        item.classList.toggle("met", met);
        item.classList.toggle("unmet", Boolean(value) && !met);
      });
    }

    document.getElementById("copy-snippet").addEventListener("click", async () => {
      const snippet = document.getElementById("connect-snippet").textContent;
      try {
        await navigator.clipboard.writeText(snippet);
        setState("copy-state", "已复制。贴进 AI 工具的 MCP 设置。", false);
      } catch {
        setState("copy-state", "复制没成功。请从上面的配置里手动复制。", true);
      }
    });

    document.getElementById("reissue-key").addEventListener("click", async () => {
      try {
        const issued = await api("/v1/account/key", { method: "POST", body: "{}" });
        state.issued = { key: issued.key, mcp: issued.mcp };
        setState("connect-state", "", false);
        await refreshWho();
        renderConnect();
      } catch (error) {
        setState("connect-state", error.message, true);
      }
    });

    function renderConnect() {
      const signed = signedIn();
      document.getElementById("connect-anon").classList.toggle("hidden", signed);
      const secret = document.getElementById("connect-secret");
      const later = document.getElementById("connect-later");
      if (!signed) {
        secret.classList.add("hidden");
        later.classList.add("hidden");
        return;
      }
      if (state.issued && state.issued.key) {
        secret.classList.remove("hidden");
        later.classList.add("hidden");
        document.getElementById("connect-key").textContent = state.issued.key;
        document.getElementById("connect-snippet").textContent = JSON.stringify(state.issued.mcp, null, 2);
        return;
      }
      secret.classList.add("hidden");
      later.classList.remove("hidden");
      document.getElementById("connect-url").textContent = state.mcpUrl || "";
    }

    function clearReset() {
      const panel = document.getElementById("reset-once");
      if (!panel) return;
      panel.classList.add("hidden");
      document.getElementById("reset-secret").textContent = "";
      document.getElementById("reset-note").textContent = "";
    }

    document.getElementById("copy-reset").addEventListener("click", async () => {
      const secret = document.getElementById("reset-secret").textContent;
      if (!secret) return;
      try {
        await navigator.clipboard.writeText(secret);
        setState("reset-copy-state", "已复制。请当面交给对方，离开这一页后不再显示。", false);
      } catch {
        setState("reset-copy-state", "复制没成功。请从上面手动复制。", true);
      }
    });

    async function loadManage() {
      const rows = document.getElementById("manage-rows");
      rows.replaceChildren();
      try {
        const data = await api("/v1/accounts/manage");
        const ownerSelect = document.getElementById("namespace-owner");
        ownerSelect.replaceChildren();
        (data.items || []).forEach((item) => {
          ownerSelect.append(new Option(`${item.nickname || item.username} (${item.username})`, item.id));
        });
        (data.items || []).forEach((item) => {
          const tr = document.createElement("tr");
          tr.append(
            text("td", item.nickname || item.display_name),
            text("td", item.real_name || ""),
            text("td", item.department || ""),
            text("td", item.job_title || ""),
            text("td", item.username),
          );
          const role = document.createElement("select");
          role.append(new Option("作者", "publisher"), new Option("审核", "admin"));
          role.value = item.role;
          role.addEventListener("change", async () => {
            try {
              await api("/v1/accounts/" + encodeURIComponent(item.id) + "/role", {
                method: "POST",
                body: JSON.stringify({ role: role.value }),
              });
              setState("manage-state", "角色已更新。", false);
              await refreshWho();
            } catch (error) {
              setState("manage-state", error.message, true);
              role.value = item.role;
            }
          });
          const roleCell = document.createElement("td");
          roleCell.append(role);
          const action = document.createElement("td");
          if (item.id !== state.userId) {
            const reset = text("button", "重置口令", "btn");
            reset.type = "button";
            reset.addEventListener("click", async () => {
              if (reset.textContent !== "确认重置") {
                reset.textContent = "确认重置";
                return;
              }
              try {
                const issued = await api("/v1/accounts/" + encodeURIComponent(item.id) + "/password", {
                  method: "POST",
                  body: "{}",
                });
                const panel = document.getElementById("reset-once");
                panel.classList.remove("hidden");
                document.getElementById("reset-note").textContent =
                  "已为 " + (issued.nickname || issued.username) + " 生成新口令。只出现这一次。原来的口令立即失效，商店 MCP 的 KEY 没变。";
                document.getElementById("reset-secret").textContent = issued.password;
                setState("reset-copy-state", "", false);
                reset.textContent = "重置口令";
              } catch (error) {
                setState("manage-state", error.message, true);
                reset.textContent = "重置口令";
              }
            });
            action.append(reset);
          }
          if (!item.disabled) {
            const stop = text("button", "停用", "btn");
            stop.type = "button";
            stop.addEventListener("click", async () => {
              try {
                await api("/v1/accounts/" + encodeURIComponent(item.id) + "/disable", { method: "POST", body: "{}" });
                await loadManage();
              } catch (error) {
                setState("manage-state", error.message, true);
              }
            });
            action.append(stop);
          } else {
            action.append(text("span", "已停用", "foot"));
          }
          tr.append(roleCell, action);
          rows.append(tr);
        });
        setState("manage-state", "", false);
      } catch (error) {
        setState("manage-state", error.message, true);
      }
    }

    refreshWho().then(() => { loadIdentity(); applyHash(); });
