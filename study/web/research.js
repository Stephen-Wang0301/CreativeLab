(() => {
  "use strict";
  const {el, api, identifier, notice, button, field, anchor, copyText, localTime, table} = window.Creative;
  const root = "/research/api/";
  const workspace = document.getElementById("workspace");
  const toolbar = document.getElementById("toolbar");
  const nav = document.getElementById("research-nav");
  const logout = document.getElementById("logout");
  const connection = document.getElementById("connection");
  const modes = {pilot: "预试", formal: "正式"};
  const pages = {overview: "运行概览", protocol: "方案与招募", people: "参与者数据", analysis: "盲评与分析"};
  const imageNames = {
    pending: "尚未进入", not_configured: "插件未接入", no_complete_text: "文字未完成",
    provider_unavailable: "插件不可用", ready: "待生成", generating: "生成中", available: "待选图",
    failed: "生成失败", selected: "已选图", skipped_by_participant: "主动跳过",
  };
  const stageDurations = {
    main_brief: "阅读简报参考时长", main_draft: "独立原稿", main_edit: "编辑定稿",
    timing_suggestion: "建议阅读", timing_ideas: "两条构思", timing_final: "视觉提案",
  };
  let mode = "pilot";
  let page = "overview";
  let revision = "";
  let health = null;
  let settings = null;
  let overview = null;
  let analysis = null;
  let config = null;
  let editingInput = false;
  let draftPending = false;
  let busy = false;
  let selectedPerson = null;
  let listPosition = 0;
  const filter = {text: "", group: "", phase: ""};

  const p = (value, className = "") => el("p", {class: className}, value);
  const card = (...children) => el("section", {class: "card stack"}, children);
  const row = (...children) => el("div", {class: "button-row"}, children);
  const title = (value, note) => el("div", {class: "stack-sm"}, el("h2", {}, value), note && p(note, "hint"));
  const empty = (text) => p(text, "empty");
  const imgState = (value) => imageNames[value] || value || "—";
  const entryName = (value) => value === "early" ? "早期" : value === "late" ? "延后" : "—";
  const fnum = (value) => value == null ? "—" : Number(value).toFixed(3);
  const url = (name, withRevision = false) => root + mode + "/" + name +
    (withRevision && revision ? "?revision=" + encodeURIComponent(revision) : "");
  const capsule = (value, css = "") => el("span", {class: "pill " + css}, value);

  function select(options, value, handler) {
    const control = el("select", {}, options.map(([key, caption]) => el("option", {value: key}, caption)));
    control.value = value;
    if (handler) control.addEventListener("change", () => handler(control.value, control));
    return control;
  }

  function setConnection(connected, text) {
    connection.className = "connection" + (connected ? "" : " offline");
    connection.replaceChildren(el("span", {class: "status-dot"}), text);
  }

  function resetConfig() {
    config = structuredClone(settings.saved_draft || settings.active?.protocol || settings.draft);
    editingInput = false;
  }

  async function transact(job, success, redraw = true) {
    if (busy) return;
    busy = true;
    notice("");
    const active = document.activeElement;
    if (active instanceof HTMLButtonElement) active.disabled = true;
    try {
      const result = await job();
      if (typeof success === "function") success(result);
      else if (success) notice(success);
      if (redraw) render();
    } catch (error) {
      if (error.status === 401) login();
      notice(error.message, "error");
    } finally {
      if (active instanceof HTMLButtonElement) active.disabled = false;
      busy = false;
    }
  }

  async function load(change) {
    await transact(async () => {
      if (change) change();
      health = await api(root + "health");
      settings = await api(url("settings"));
      if (revision && !settings.revisions.some((entry) => String(entry.id) === revision)) revision = "";
      resetConfig();
      if (page === "overview" || page === "people") overview = await api(url("overview", true));
      if (page === "analysis") analysis = await api(url("analysis", true));
      setConnection(true, "本机服务已连接");
      logout.hidden = false;
    });
  }

  function login() {
    settings = null;
    health = null;
    nav.replaceChildren();
    toolbar.replaceChildren();
    logout.hidden = true;
    setConnection(false, "等待登录");
    const key = el("input", {type: "password", autocomplete: "current-password", required: true});
    const form = el("form", {class: "stack"}, el("h1", {}, "登录研究后台"),
      p("请输入研究后台访问密钥，可直接粘贴。", "subtle"),
      field("访问密钥", key), row(el("button", {class: "button primary", type: "submit"}, "登录"),
        button("显示密钥", (event) => {
          key.type = key.type === "password" ? "text" : "password";
          event.currentTarget.textContent = key.type === "password" ? "显示密钥" : "隐藏密钥";
        })));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api(root + "login", {key: key.value.trim()});
        notice("");
        await load();
      } catch (error) {
        notice(error.message, "error");
      }
    });
    workspace.replaceChildren(el("section", {class: "card login-card"}, form));
  }

  function inviteCard() {
    const net = health.network;
    const publicMode = net.access === "pilot_tunnel" || net.access === "fixed";
    const pilotOnly = net.access === "pilot_tunnel" && mode === "formal";
    const verified = net.tunnel_status === "ready" && net.public_url;
    const label = publicMode
      ? verified ? (net.access === "fixed" ? "固定公网可访问" : "临时公网可访问")
        : (net.tunnel_status === "failed" ? "公网启动失败" : "公网未就绪")
      : net.lan_url ? "仅同一局域网" : "仅本机";
    const base = publicMode ? (verified || null) : net.lan_url;
    const suffix = "/p/" + mode + "/#invite=" + encodeURIComponent(settings.invite);
    const share = base && settings.recruiting && !pilotOnly ? base + suffix : null;
    const local = settings.recruiting && !pilotOnly ? net.local_url + suffix : null;
    const reason = pilotOnly ? "临时公网入口适用于预试，正式采集请使用固定 HTTPS 入口。"
      : !settings.recruiting ? "当前暂停新报名，已有参与者可继续。"
      : publicMode ? net.tunnel_detail || "正在检查公网 HTTPS 入口。"
      : net.lan_url ? "链接适用于和实验电脑在同一局域网的设备。"
      : "当前启动方式仅供本机使用。跨网络预试请用 Start_Internet_Pilot.bat 启动。";
    const folder = () => transact(() => api(root + "folder", {}), "已请求打开本地数据目录。", false);
    return card(el("div", {class: "card-heading"}, title("邀请与数据目录"),
      capsule(label, publicMode && !verified ? "warn" : "")),
      share ? p(share, "mono local-path key-line") : empty(reason),
      row(share && button(publicMode ? "复制公网邀请链接" : "复制局域网邀请链接", async () => {
        notice(await copyText(share) ? "已复制邀请链接。" : "请手动复制卡片中的链接。");
      }, "primary"),
      local && el("a", {class: "button secondary", href: local, target: "_blank", rel: "noopener"},
        "本机打开受试者页"),
      button("打开数据目录", folder), button("刷新状态", () => load())),
      publicMode && !verified && p("如果持续连接失败，请查看 logs/cloudflared.log。", "hint"),
      net.quick && p("当前为快速演练，数据存入 quick-demo。", "hint"),
      p(health.data_path, "mono local-path subtle"));
  }

  function overviewPage() {
    const info = overview;
    if (!info) return card(empty("正在读取运行状态。"));
    const metrics = el("div", {class: "metric-grid"},
      [[info.enrolled, "已报名"], [info.assigned, "完成主实验分组"], [info.completed, "两项文字任务均完成"], [info.withdrawn, "退出"]]
        .map(([value, label]) => el("section", {class: "card metric"},
          el("span", {class: "value"}, value), el("span", {class: "label"}, label))));
    const groups = table(["分组", "主实验分配", "文字定稿", "最终图像", "早期建议", "延后建议", "时机终稿"],
      info.groups.map((g) => ["G" + g.group + " · " + g.label, g.assigned, g.text, g.image, g.early, g.late, g.timing_final]));
    const counts = (mapping, names) => Object.keys(mapping).length ?
      table(["状态", "人数"], Object.entries(mapping).map(([key, value]) => [names(key), value])) : empty("暂无记录。");
    return el("div", {class: "stack"}, metrics, inviteCard(),
      info.unsynced.length > 0 && card(p("有 " + info.unsynced.length + " 个目录等待同步。服务器会自动重试。", "notice warn inset"),
        row(button("立即同步", () => transact(() => api(root + "sync", {}), (result) => {
          notice(result.errors.length ? "仍有目录未同步，请检查本地目录权限和磁盘空间。" : "本地目录同步完成。");
        }, false)))),
      card(title("分组与完成情况", "按所选方案版本统计。"), groups),
      el("div", {class: "equal-columns"}, card(title("阶段分布"), counts(info.phases, (x) => x)),
        card(title("图像流程"), counts(info.images, imgState))));
  }

  function confirmDraftEdit(caption, persist) {
    return new Promise((resolve, reject) => {
      const finish = async (accepted) => {
        if (confirm.disabled) return;
        clearTimeout(timeout);
        clearInterval(interval);
        confirm.disabled = cancel.disabled = true;
        try {
          confirm.textContent = "正在保存…";
          resolve(accepted ? await persist() : null);
        } catch (error) {
          reject(error);
        } finally {
          dialog.close();
          dialog.remove();
        }
      };
      const confirm = button("确认（5 秒）", () => finish(true), "primary");
      const cancel = button("取消", () => finish(false), "secondary", {autofocus: true});
      const dialog = el("dialog", {"aria-labelledby": "draft-confirm-title", "aria-describedby": "draft-confirm-note"},
        el("div", {class: "stack"}, el("h2", {id: "draft-confirm-title"}, "保留这次修改？"),
          p(caption), el("p", {id: "draft-confirm-note", class: "hint"}, "倒计时结束后将自动确认，并保存到本地草稿。"),
          row(confirm, cancel)));
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
      document.body.append(dialog);
      dialog.showModal();
      const deadline = performance.now() + 5000;
      const timeout = setTimeout(() => finish(true), 5000);
      const interval = setInterval(() => {
        confirm.textContent = `确认（${Math.max(1, Math.ceil((deadline - performance.now()) / 1000))} 秒）`;
      }, 250);
    });
  }

  async function saveDraft(caption, revert, redraw = false) {
    if (settings.editor_version === undefined) {
      revert();
      notice("请重启实验服务后再保存方案草稿。", "warn");
      return;
    }
    let saved = false;
    if (!busy) {
      draftPending = true;
      await transact(async () => {
        const answer = await confirmDraftEdit(caption, () => api(url("settings"), {
          op: "save_draft", protocol: config, editor_version: settings.editor_version,
        }));
        if (!answer) return;
        settings = answer;
        saved = true;
        if (redraw) { resetConfig(); render(); }
        notice("已保存到本地草稿。");
      }, null, false);
      draftPending = false;
    }
    if (!saved) revert();
  }

  function editable(object, key, caption, maximum, type = "text", hint = "") {
    const node = el(type === "textarea" ? "textarea" : "input", {
      type: type === "textarea" ? undefined : type,
      maxlength: type === "number" ? undefined : maximum,
      min: type === "number" ? "1" : undefined, max: type === "number" ? maximum : undefined,
      rows: type === "textarea" ? 4 : undefined,
    });
    const display = () => Array.isArray(object[key]) ? object[key].join("\n") : String(object[key] ?? "");
    node.value = display();
    node.addEventListener("input", () => { editingInput = node.value !== display(); });
    node.addEventListener("change", async () => {
      editingInput = false;
      const previous = object[key];
      if (node.value === display()) return;
      object[key] = type === "number" ? (node.value === "" ? null : node.valueAsNumber) :
        Array.isArray(previous) ? node.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean) : node.value;
      await saveDraft(caption, () => { object[key] = previous; });
      node.value = display();
    });
    return field(caption, node, hint);
  }

  function removeMaterial(list, index, minimum) {
    if (list.length <= minimum) return;
    const [item] = list.splice(index, 1);
    saveDraft("移除简报「" + (item.title || "未命名简报") + "」", () => list.splice(index, 0, item), true);
  }

  function materialBrief(item, index, type) {
    const first = type === "main_briefs";
    const entries = item.intentions.map((intent, i) => el("div", {class: "panel stack-sm"},
      el("h3", {}, "优先意图 " + (i + 1)),
      el("div", {class: "material-row"}, editable(intent, "id", "意图 ID", 40), editable(intent, "label", "意图名称", 100)),
      first && editable(intent, "detail", "向参与者展示的意图说明", 400, "textarea"),
      first && editable(intent.draft, "title", "对应 AI 工作稿标题", 100),
      first && editable(intent.draft, "concept", "对应 AI 工作稿正文", 1800, "textarea"),
      first && editable(intent.draft, "elements", "工作稿关键元素", 380, "textarea", "每行一项，填写 2–3 项。")));
    const suggestions = first ? [] : item.suggestions.map((suggestion, i) => el("div", {class: "panel stack-sm"},
      el("h3", {}, "AI 建议 " + (i + 1)), editable(suggestion, "id", "建议 ID", 40),
      editable(suggestion, "text", "建议正文", 1800, "textarea"),
      editable(suggestion, "features", "预定义特有特征", 980, "textarea", "每行一项，最多 8 项。")));
    return el("details", {class: "panel stack-sm", open: index === 0},
      el("summary", {}, (index + 1) + " · " + (item.title || "未命名简报")),
      el("div", {class: "stack"}, el("div", {class: "material-row"},
        editable(item, "id", "简报 ID", 40), editable(item, "title", "简报名称", 100)),
      editable(item, "brief", "简报正文", 2000, "textarea"),
      el("div", {class: "group-layout"}, entries), el("div", {class: "group-layout"}, suggestions),
      row(button("移除此简报", () => removeMaterial(config[type], index, first ? 1 : 2), "danger",
        {disabled: config[type].length <= (first ? 1 : 2)}))));
  }

  function materialSection(type, caption, note) {
    const list = config[type];
    const add = () => {
      const sample = structuredClone(settings.draft[type][0]);
      sample.id = (type === "main_briefs" ? "main-" : "timing-") + identifier().slice(0, 8);
      sample.title = "新简报";
      list.push(sample);
      saveDraft("添加简报", () => list.pop(), true);
    };
    return card(el("div", {class: "card-heading"}, title(caption, note),
      button("添加简报", add, "secondary", {disabled: list.length >= 8})),
    list.map((item, index) => materialBrief(item, index, type)));
  }

  function protocolPage() {
    const confirmed = el("input", {type: "checkbox"});
    const adults = el("input", {type: "checkbox", checked: config.adult_only !== false});
    adults.addEventListener("change", () => {
      const previous = config.adult_only;
      config.adult_only = adults.checked;
      saveDraft("报名年龄要求", () => { config.adult_only = adults.checked = previous; });
    });
    const change = (body, message, reset = false) => transact(() => api(url("settings"), body), (answer) => {
      settings = answer;
      if (reset) revision = "";
      resetConfig();
      notice(message);
    });
    const upload = el("input", {type: "file", accept: ".json,application/json"});
    upload.addEventListener("change", async () => {
      const file = upload.files[0];
      upload.value = "";
      if (!file || busy) return;
      const previous = config;
      try {
        if (file.size > 150000) throw new Error("请选择 150 KB 以内的方案文件。");
        const value = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));
        if (busy || !upload.isConnected) return;
        config = value?.protocol || value;
        await saveDraft("用导入的 JSON 替换当前草稿", () => { config = previous; }, true);
      } catch (error) { notice(error.message, "error"); }
    });
    const download = () => {
      const object = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2)], {type: "application/json"}));
      const link = el("a", {href: object, download: "study-protocol.json"});
      link.click();
      setTimeout(() => URL.revokeObjectURL(object), 1000);
    };
    const save = () => change({op: "revision", protocol: config, confirmed: confirmed.checked,
      editor_version: settings.editor_version}, "已保存新方案版本。可开放招募。", true);
    const toggle = () => change({op: "recruiting", value: !settings.recruiting},
      settings.recruiting ? "已暂停新报名。" : "已开放新报名。");
    const rotate = () => {
      if (window.confirm("更换邀请口令会让之前发出的报名链接失效。继续更换？")) {
        change({op: "rotate_invite"}, "已更换邀请口令。");
      }
    };
    return el("div", {class: "stack"},
      card(el("div", {class: "card-heading"}, title("招募设置", settings.active ?
        "当前版本 #" + settings.active.id + " · " + settings.active.protocol.label : "正式招募须先保存一版方案。"),
      capsule(settings.recruiting ? "招募开放" : "招募暂停", settings.recruiting ? "" : "warn")),
      p("新参与者使用当前版本。已报名参与者保留其报名时的方案和材料。", "hint"),
      p(settings.image_provider ? "本次运行的图像插件：" + settings.image_provider : "当前未接入图像插件，文字任务可完整运行。", "hint"),
      row(button(settings.recruiting ? "暂停新报名" : "开放新报名", toggle, "primary", {disabled: !settings.active}),
        button("更换邀请口令", rotate))),
      card(title("实验信息", "改完一项后，离开输入框即可确认保存到本地草稿。"),
        el("div", {class: "equal-columns"}, editable(config, "title", "实验名称", 100),
          editable(config, "label", "方案版本说明", 70), editable(config, "cap", "本模式总招募上限", 5000, "number"),
          editable(config, "contact", "联系方式", 500, "textarea")),
        editable(config, "consent", "知情同意说明", 4000, "textarea"),
        el("label", {class: "choice"}, adults, "报名时确认年满 18 周岁"),
        el("div", {class: "group-layout"}, Object.entries(stageDurations).map(([key, name]) =>
          editable(config.durations_seconds, key, name + "（秒）", 3600, "number")))),
      materialSection("main_briefs", "主实验简报与工作稿", "每份简报有两项优先意图及两份对应的 AI 工作稿。"),
      materialSection("timing_briefs", "时机实验简报与建议", "每份简报有两条固定建议，各有独立的特征编码清单。"),
      card(title("保存版本", settings.recruiting ? "草稿可随时保存。暂停新报名后，可将草稿保存为新版本。" : "保存为新版本后，新报名者将使用该版本。"),
        mode === "formal" && el("label", {class: "choice"}, confirmed, "我已核对正式材料、知情说明和联系方式。"),
        row(button("保存为新版本", save, "primary", {disabled: settings.recruiting}), button("下载当前方案 JSON", download)),
        field("载入方案 JSON（可选）", upload)));
  }

  function versionDetails(bundle) {
    return el("details", {class: "panel"},
      el("summary", {}, "稿件版本 · " + bundle.versions.length),
      el("div", {class: "stack-sm scroll-panel"}, [...bundle.versions].reverse().map((version) =>
        el("details", {class: "panel"}, el("summary", {}, "#" + version.id + " · " + version.type + " · " + localTime(version.at)),
          p("父版本 " + (version.parent ?? "—") + "　恢复来源 " + (version.source ?? "—"), "hint"),
          el("pre", {}, JSON.stringify(version.contents, null, 2))))));
  }

  function draft(titleText, value) {
    return el("div", {class: "panel stack-sm"}, el("h3", {}, titleText),
      value?.title && el("strong", {}, value.title), p(value?.concept || "暂无", "readable subtle"),
      value?.elements?.filter(Boolean).length ? el("ul", {}, value.elements.filter(Boolean).map((item) => el("li", {}, item))) : null);
  }

  function personDetail(bundle) {
    const info = bundle.progress;
    const s = bundle.responses;
    const main = s.main;
    const timing = s.timing;
    const currentUrl = url("person/" + info.id);
    return el("section", {class: "card stack detail-section", id: "person-detail"},
      el("div", {class: "card-heading"}, title(info.id, "方案 #" + info.revision + " · " + localTime(info.updated_at)),
        button("收起详情", () => { selectedPerson = null; render(); })),
      row(button("打开参与者文件夹", () => transact(() => api(root + "folder", {mode, id: info.id}),
        "已请求打开参与者文件夹。", false)),
      anchor("下载此参与者 ZIP", url("raw.zip") + "?id=" + encodeURIComponent(info.id))),
      el("div", {class: "group-layout"},
        el("div", {class: "panel stack-sm"}, el("h3", {}, "主实验"),
          p("简报 " + main.brief + "　组别 " + (main.group ? "G" + main.group : "—"), "mono"),
          p("封存意图　" + (main.intent_label || "—")), p("图像　" + imgState(info.image_status))),
        el("div", {class: "panel stack-sm"}, el("h3", {}, "时机实验"),
          p("简报 " + (timing.brief || "—") + "　" + entryName(timing.entry), "mono"),
          p("封存意图　" + (timing.intent_label || "—")),
          p("建议素材 " + (timing.suggestion || "—"), "mono"))),
      el("div", {class: "group-layout"}, draft("本人原稿", main.original),
        draft("AI 初稿", main.ai), draft("主实验文字定稿", main.final), draft("时机实验视觉提案", timing.final)),
      timing.ideas && el("div", {class: "panel stack-sm"}, el("h3", {}, "两条构思"), p(timing.ideas.idea1, "readable"), p(timing.ideas.idea2, "readable")),
      main.post && el("div", {class: "panel stack-sm"}, el("h3", {}, "意图回顾"),
        p(({yes: "有意改变", no: "仍沿着原方向", unsure: "不确定"})[main.post.changed]),
        p(main.post.reason, "readable subtle")),
      s.image.selected && el("div", {class: "image-grid"}, el("img", {src: currentUrl + "/image/" + s.image.selected, alt: "选定成品"})),
      info.missing.length > 0 && p("有 " + info.missing.length + " 个阶段记录了缺失或未完成。", "notice warn inset"),
      versionDetails(bundle),
      el("details", {class: "panel"}, el("summary", {}, "事件日志 · " + bundle.events.length),
        el("div", {class: "scroll-panel"}, table(["时间", "阶段", "事件", "详情"], [...bundle.events].reverse().map((event) =>
          [localTime(event.at), event.phase, event.type, JSON.stringify(event.details)])))),
      el("details", {class: "panel"}, el("summary", {}, "评分记录 · " + bundle.ratings.length),
        el("div", {class: "scroll-panel"}, table(["任务", "评审编号", "评分", "备注"], bundle.ratings.map((r) =>
          [r.task, r.rater, JSON.stringify(r.score), r.notes || "—"])))));
  }

  function peoplePage() {
    const people = overview?.participants || [];
    const search = el("input", {type: "search", placeholder: "如 P-3A…", autocomplete: "off"});
    search.value = filter.text;
    const list = el("div", {class: "stack"});
    const renderList = () => {
      const visible = people.filter((person) =>
        person.id.toLowerCase().includes(filter.text.trim().toLowerCase()) &&
        (!filter.group || String(person.group || "unassigned") === filter.group) &&
        (!filter.phase || person.phase === filter.phase));
      const pages = Math.max(1, Math.ceil(visible.length / 30));
      listPosition = Math.min(listPosition, pages - 1);
      const slice = visible.slice(listPosition * 30, (listPosition + 1) * 30);
      const open = (pid) => transact(() => api(url("person/" + pid)), (bundle) => {
        selectedPerson = bundle;
        setTimeout(() => document.getElementById("person-detail")?.scrollIntoView({block: "start"}), 0);
      });
      list.replaceChildren(slice.length ? table(["匿名编号", "阶段", "主实验", "建议时机", "图像", "目录", "最近保存"], slice.map((person) => [
        button(person.id, () => open(person.id), "secondary", {class: "button mono"}), person.phase_name,
        person.group ? "G" + person.group : "—", entryName(person.timing_entry), imgState(person.image_status),
        person.synced ? capsule("已同步") : capsule("待同步", "warn"), localTime(person.updated),
      ])) : empty("当前筛选条件下没有参与者。"),
      row(button("上一页", () => { listPosition--; renderList(); }, "secondary", {disabled: listPosition <= 0}),
        p("第 " + (listPosition + 1) + " / " + pages + " 页 · " + visible.length + " 条", "hint"),
        button("下一页", () => { listPosition++; renderList(); }, "secondary", {disabled: listPosition >= pages - 1})));
    };
    const phases = new Map(people.map((item) => [item.phase, item.phase_name]));
    const group = select([["", "全部组别"], ["unassigned", "待分组"], ...[1, 2, 3, 4].map((n) => [String(n), "G" + n])],
      filter.group, (value) => { filter.group = value; listPosition = 0; renderList(); });
    const phase = select([["", "全部阶段"], ...phases], filter.phase,
      (value) => { filter.phase = value; listPosition = 0; renderList(); });
    search.addEventListener("input", () => { filter.text = search.value; listPosition = 0; renderList(); });
    renderList();
    return el("div", {class: "stack"}, card(el("div", {class: "card-heading"}, title("参与者记录"),
      row(people.length > 0 && anchor("下载此版本原始 ZIP", url("raw.zip", true)), button("刷新列表", () => load()))),
      el("div", {class: "group-layout"}, field("查找匿名编号", search),
        el("div", {class: "group-layout"}, field("主实验分组", group), field("实验阶段", phase))), list),
    selectedPerson && personDetail(selectedPerson));
  }

  function analysisPage() {
    const report = analysis;
    if (!report) return card(empty("正在读取评分数据。"));
    const templates = table(["评分任务", "可评分作品", "已汇总", "盲评文件"], report.coverage.map((item) => [
      item.label, item.eligible, item.rated, item.eligible ? anchor("导出盲评包", url("packet/" + item.task, true)) : "—",
    ]));
    const task = select(Object.entries(health.tasks).map(([key, value]) => [key, value.label]), "h1");
    const taskHelp = p(health.tasks.h1.instructions, "hint");
    task.addEventListener("change", () => { taskHelp.textContent = health.tasks[task.value].instructions; });
    const rater = el("input", {maxlength: 32, placeholder: "例如 R01", autocomplete: "off"});
    const upload = el("input", {type: "file", accept: ".csv,text/csv"});
    const importFile = () => transact(async () => {
      const file = upload.files[0];
      if (!file || file.size > 2_000_000) throw new Error("请选择 2 MB 以内的评分 CSV。");
      if (!rater.value.trim()) throw new Error("请填写评审编号。");
      const result = await api(url("ratings", true), {task: task.value, rater: rater.value.trim(), csv: await file.text()},
        {timeout: 45000});
      analysis = await api(url("analysis", true));
      return result;
    }, (result) => notice("已导入 " + result.imported + " 件作品的评分。"));
    const comparisons = {
      h1: "H1　早期 − 延后", h2: "H2　G3 − G4", h3: "H3　(G3 − G4) − (G1 − G2)", h4: "H4　G3 − G4",
    };
    const cells = table(["假设与对比", "各条件 N / 均值", "差值", "近似 95% 区间"], report.contrasts.map((entry) => [
      comparisons[entry.task],
      entry.cells.map((cell) => (typeof cell.cell === "number" ? "G" + cell.cell : entryName(cell.cell)) +
        " " + cell.n + " / " + fnum(cell.mean)).join("　"),
      fnum(entry.estimate), entry.interval ? entry.interval.map(fnum).join(" 至 ") : "—",
    ]));
    return el("div", {class: "stack"}, card(title("盲评材料", "按指标分开导出。作品编号和材料顺序隐藏实验组别。"), templates),
      card(title("导入评审评分", "每位评审使用独立编号。以同一编号重新导入会更新其评分。"),
        el("div", {class: "group-layout"}, field("评分任务", task), field("评审编号", rater)),
        taskHelp, field("评分模板 CSV", upload, "按盲评包中的 ratings.csv 填写并保存为 UTF-8。"),
        row(button("导入评分", importFile, "primary"))),
      card(el("div", {class: "card-heading"}, title("四项假设的初步比较"),
        settings.revisions.length > 0 && anchor("下载分析 CSV", url("analysis.csv", true))),
      p("汇总需要至少三名独立评审。H2 平票保持空缺。以下为未调整描述性对比，正式推断按预设区组分析。", "hint"), cells));
  }

  function render() {
    if (!settings || !health) return;
    nav.replaceChildren(...Object.entries(pages).map(([key, name]) => el("button", {
      type: "button", class: "nav-button" + (key === page ? " active" : ""), "aria-current": key === page ? "page" : undefined,
      onclick: () => {
        if (page !== key) load(() => { page = key; });
      },
    }, name)));
    const scope = select(Object.entries(modes), mode, (value, control) => {
      if (busy) { control.value = mode; return; }
      load(() => {
        mode = value; revision = ""; selectedPerson = null; listPosition = 0;
        filter.text = filter.group = filter.phase = "";
      });
    });
    const revisions = [["", settings.active ? "当前版本 #" + settings.active.id : "当前没有版本"],
      ...settings.revisions.map((item) => [String(item.id), "#" + item.id + " · " + item.label])];
    const version = select(revisions, revision, (value) => {
      load(() => { revision = value; selectedPerson = null; listPosition = 0; });
    });
    toolbar.replaceChildren(el("div", {class: "stack-sm"}, el("span", {class: "kicker"}, "实验工作台"),
      el("h1", {}, pages[page])),
      row(field("数据范围", scope), page !== "protocol" && field("方案版本", version)));
    const content = {overview: overviewPage, protocol: protocolPage, people: peoplePage, analysis: analysisPage};
    workspace.replaceChildren(content[page]());
  }

  logout.addEventListener("click", async () => {
    if (busy) return;
    try {
      await api(root + "logout", {});
      notice("");
      login();
    } catch (error) { notice(error.message, "error"); }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!editingInput && !draftPending) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const hash = new URLSearchParams(location.hash.slice(1));
  const initialKey = hash.get("key");
  if (initialKey) history.replaceState(null, "", location.pathname);
  async function start() {
    try {
      if (initialKey) await api(root + "login", {key: initialKey});
      await load();
    } catch (error) {
      login();
      notice(error.message, "error");
    }
  }
  setInterval(() => {
    if (page === "overview" && settings && !document.hidden && !busy) load();
  }, 10000);
  start();
})();
