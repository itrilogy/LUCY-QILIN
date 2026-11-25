/* 修订 / 新版本时，把表单表达不了的内容从原清单带过来（评审 P1-8）。
 *
 * 门户表单只能编辑第一条外部依赖和第一条资源地址，也不完整表达 knowledge。
 * 旧实现直接用表单结果重建清单，于是：
 *   - 第二条外部依赖被丢掉；
 *   - 版本范围 `>=1.5 <2` 被 `split(/\\s+/, 2)` 截成 `>=1.5`；
 *   - 同一依赖的其它平台资源地址丢失；
 *   - knowledge 的分类、词表、密级等字段丢失。
 * 这里只覆盖表单真正编辑的字段，其余原样保留。
 *
 * 单独成文件是为了能用 node 直接跑用例（不依赖 DOM）。浏览器里挂在
 * `window.QilinMerge` 上，Node 里走 `module.exports`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.QilinMerge = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  // 每行 "id 版本范围"。只在第一个空白处切：范围本身可能带空格（">=1.5 <2"）。
  function parseDepLines(text) {
    return String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const match = line.match(/^(\S+)(?:\s+([\s\S]*))?$/);
        const range = (match[2] || "").trim();
        return { id: match[1], version: range || "*" };
      });
  }

  function formatDepLines(items) {
    return (items || [])
      .map((item) => `${item.id} ${item.version || item.version_range || "*"}`)
      .join("\n");
  }

  /**
   * @param built    表单重建出来的清单
   * @param base     原清单（published 或待审提交）；为 null 时原样返回
   * @param extInput 表单里第一条外部依赖的原始输入 {name, version_range, detect, url}
   */
  function mergeRevised(built, base, extInput) {
    if (!base) return built;
    const merged = clone(built);
    const baseDepends = base.depends || {};
    const baseExternal = (baseDepends.external || []).map(clone);
    const edited = (merged.depends.external || [])[0] || null;
    const input = extInput || {};

    if (edited) {
      const first = baseExternal[0] || {};
      const sameName = (first.name || "") === edited.name;
      // detect 的参数可能带空格（["python3","-c","import sys"]），表单把它拍成
      // 一个字符串；输入与原来一致时就保留原数组。
      if (sameName && Array.isArray(first.detect)
          && String(input.detect || "").trim() === first.detect.join(" ")) {
        edited.detect = clone(first.detect);
      }
      // 资源地址：表单只改第一条；其它平台/镜像地址保留。
      const baseResources = (first.resources || []).map(clone);
      if (baseResources.length) {
        const urlInput = String(input.url || "").trim();
        if (urlInput !== (baseResources[0].url || "")) {
          baseResources[0].url = urlInput;
        }
        edited.resources = baseResources;
      }
      // 表单没暴露的字段（title、platform 等）以原值为底。
      merged.depends.external[0] = Object.assign({}, sameName ? first : {}, edited);
    }

    // 表单只能编辑第一条；其余原样保留。表单里没有外部依赖时（例如 knowledge
    // 分支会把依赖清空），全部保留而不是丢掉。
    const keep = baseExternal.slice(edited ? 1 : 0);
    merged.depends.external = edited ? [merged.depends.external[0], ...keep] : keep;

    // knowledge：表单只填入口，分类/词表/密级/范围等字段原样带过来。
    if (base.knowledge || merged.knowledge) {
      merged.knowledge = Object.assign(clone(base.knowledge || {}), merged.knowledge || {});
    }
    // knowledge 分支不提供引用依赖与密钥名，这三类从原清单带回来。
    if (merged.kind === "knowledge" && base.kind === "knowledge") {
      ["store", "skills", "mcp"].forEach((key) => {
        if (!(merged.depends[key] || []).length && baseDepends[key]) {
          merged.depends[key] = clone(baseDepends[key]);
        }
      });
      if (!(merged.secrets || []).length && base.secrets) {
        merged.secrets = clone(base.secrets);
      }
    }
    return merged;
  }

  return { parseDepLines, formatDepLines, mergeRevised };
});
