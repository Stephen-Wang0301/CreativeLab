(() => {
  "use strict";

  function el(tag, attributes = {}, ...children) {
    const item = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (value === false || value == null) continue;
      if (name.startsWith("on") && typeof value === "function") {
        item.addEventListener(name.slice(2), value);
      } else if (value === true) {
        item.setAttribute(name, "");
      } else {
        item.setAttribute(name, value);
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      item.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return item;
  }

  async function api(path, body, options = {}) {
    const abort = new AbortController();
    const timeout = window.setTimeout(() => abort.abort(), options.timeout || 16000);
    const init = {credentials: "same-origin", cache: "no-store", signal: abort.signal,
      headers: {...(options.headers || {})}};
    if (body !== undefined) {
      Object.assign(init, {
        method: "POST",
        headers: {...init.headers, "Content-Type": "application/json", "X-Study-Request": "1"},
        body: JSON.stringify(body),
        keepalive: Boolean(options.keepalive),
      });
    }
    try {
      const response = await fetch(path, init);
      const result = await response.json();
      if (!response.ok) {
        throw Object.assign(new Error(result.error || "请求失败。"), {status: response.status, code: result.code});
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) {
        throw new Error("连接暂时中断。请保持页面开启并重试。");
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function identifier() {
    return [...crypto.getRandomValues(new Uint8Array(16))]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function notice(text, kind = "info") {
    const node = document.getElementById("message");
    node.textContent = text || "";
    node.className = "notice " + kind;
    node.hidden = !text;
  }

  function button(label, handler, kind = "secondary", attributes = {}) {
    return el("button", {type: "button", class: "button " + kind, onclick: handler, ...attributes}, label);
  }

  function field(label, element, help) {
    return el("label", {class: "field"}, el("span", {class: "field-label"}, label), element,
      help && el("span", {class: "hint"}, help));
  }

  function anchor(label, href, kind = "secondary") {
    return el("a", {class: "button " + kind, href}, label);
  }

  async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch {}
    }
    const area = el("textarea", {class: "clipboard-buffer", "aria-hidden": "true"});
    const dialog = document.querySelector("dialog[open]");
    (dialog || document.body).append(area);
    area.value = value;
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  }

  function localTime(value) {
    if (!value) return "—";
    return new Date(value).toLocaleString("zh-CN", {hour12: false});
  }

  function table(headers, rows) {
    return el("div", {class: "table-wrap"},
      el("table", {}, el("thead", {}, el("tr", {}, headers.map((name) => el("th", {scope: "col"}, name)))),
        el("tbody", {}, rows.map((values) => el("tr", {}, values.map((value) => el("td", {}, value ?? "—")))))));
  }

  window.Creative = {el, api, identifier, notice, button, field, anchor, copyText, localTime, table};
})();
