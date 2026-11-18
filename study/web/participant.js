(() => {
  "use strict";
  const {el, api: requestApi, identifier, notice, button, field, copyText} = window.Creative;
  const workspace = document.getElementById("workspace");
  const idNode = document.getElementById("participant-id");
  const exitButton = document.getElementById("exit");
  const pauseButton = document.getElementById("pause");
  const endpoint = location.pathname + "api/";
  const mode = location.pathname.split("/")[2];
  const hash = new URLSearchParams(location.hash.slice(1));
  let activeInvite = hash.get("invite") || "";
  try {
    activeInvite ||= sessionStorage.getItem("invite_" + mode) || "";
    if (activeInvite) sessionStorage.setItem("invite_" + mode, activeInvite);
  } catch {}
  if (hash.has("invite")) history.replaceState(null, "", location.pathname);
  const scope = () => /^I([0-9a-f]{24})_[0-9a-f]{64}$/.exec(activeInvite)?.[1] || "shared";
  const cached = (key, value) => {
    try {
      const name = key === "invite" ? "invite_" + mode : key + "_" + mode + "_" + scope();
      if (value !== undefined) sessionStorage.setItem(name, value);
      return sessionStorage.getItem(name);
    } catch { return null; }
  };
  let joinNonce = cached("join") || identifier();
  cached("join", joinNonce);
  const api = (path, body, options = {}) => requestApi(path, body,
    {...options, headers: {...(options.headers || {}), ...(activeInvite ? {"X-Study-Invitation": activeInvite} : {})}});
  const terminal = new Set(["done", "incomplete", "withdrawn", "restarted"]);
  let state = null;
  let editor = null;
  let saveTimer = null;
  let savePromise = null;
  let actionBusy = false;
  let requestState = false;
  let busyLanding = false;
  let lastRead = 0;
  let serverDelta = 0;
  let timerNode = null;
  let saveNotice = null;
  let temporaryDialog = null;
  let pauseDialog = null;
  let restartTimer = null;

  const card = (className, ...children) => el("section", {class: "card stack " + className}, children);
  const p = (value, className = "") => el("p", {class: className}, value);
  const label = (text) => el("span", {class: "kicker"}, text);
  const row = (...children) => el("div", {class: "button-row"}, children);
  const heading = (title, subtitle) => el("div", {class: "stack-sm"}, el("h1", {}, title), subtitle && p(subtitle, "lede"));

  function showDialog(title, content, buttons, cancel) {
    closeDialog();
    const dialog = el("dialog", {"aria-label": title},
      el("div", {class: "stack"}, el("h2", {}, title), content, row(buttons)));
    if (cancel) dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      cancel();
    });
    workspace.append(dialog);
    temporaryDialog = dialog;
    dialog.showModal();
    return dialog;
  }

  function closeDialog() {
    if (!temporaryDialog) return;
    temporaryDialog.close();
    temporaryDialog.remove();
    temporaryDialog = null;
  }

  function setSave(text, failed = false) {
    if (!saveNotice) return;
    saveNotice.textContent = text;
    saveNotice.classList.toggle("error", failed);
  }

  function applyState(view, force = false) {
    const old = state;
    serverDelta = view.now * 1000 - Date.now();
    const changed = !old || old.seq !== view.seq || old.started !== view.started ||
      JSON.stringify(old.pause) !== JSON.stringify(view.pause);
    const imageChanged = view.phase === "main_image" && JSON.stringify(old?.image) !== JSON.stringify(view.image);
    const restoreChanged = view.phase === "main_edit" && old?.restore_step !== view.restore_step;
    const changedDraft = editor && !editor.dirty && old?.edit_version !== view.edit_version;
    if (!force && !changed && !imageChanged && !restoreChanged && !changedDraft) {
      if (editor?.dirty && old?.edit_version !== view.edit_version) {
        setSave("服务器上已有其他页面保存的版本。请保留当前文字并刷新核对。", true);
        return;
      }
      state = view;
      return;
    }
    if (old && changed && editor?.dirty && old.deadline && Date.now() + serverDelta >= old.deadline * 1000) {
      notice("计时结束。服务器已封存截止时收到的稿件。", "warn");
    }
    state = view;
    render();
  }

  function refresh() {
    if (requestState || actionBusy || savePromise || !state || terminal.has(state.phase)) return;
    requestState = true;
    lastRead = Date.now();
    const reading = state;
    api(endpoint + "state").then((view) => {
      if (state === reading && !actionBusy && !savePromise) applyState(view);
    }).catch((error) => {
      if (error.status === 401) notice("会话已失效。请刷新页面，用恢复码继续。", "error");
      else if (!editor?.dirty) setSave(error.message, true);
    }).finally(() => { requestState = false; });
  }

  function stageRequest(op, extra = {}, settings = {}) {
    return api(endpoint + "action", {op, command: identifier(), seq: state.seq, ...extra}, settings);
  }

  function draftForm(phase, work) {
    const inputs = {};
    const form = el("form", {class: "stack"});
    function input(name, title, value, maximum, multiline = false, help = "") {
      const node = el(multiline ? "textarea" : "input", {
        name, maxlength: maximum, autocomplete: "off", class: multiline ? "draft" : undefined,
        rows: multiline ? 6 : undefined, type: multiline ? undefined : "text",
      });
      node.value = value || "";
      inputs[name] = node;
      form.append(field(title, node, help));
    }
    if (phase === "timing_ideas") {
      input("idea1", "构思一", work.idea1, 1200, true, "简要说明画面主题和表达方式。");
      input("idea2", "构思二", work.idea2, 1200, true, "尝试采用不同的表达方向。");
    } else {
      input("title", phase === "timing_final" ? "一句主题语" : "方案标题", work.title, 100);
      input("concept", phase === "timing_final" ? "视觉提案" : "画面构思", work.concept, 1800, true,
        phase === "timing_final" ? "建议约 150 字，写出画面如何呈现主题。" : "描述画面主体、叙事和表现方式。");
      if (phase !== "timing_final") {
        for (let i = 0; i < 3; i++) {
          input("element" + i, "关键元素 " + (i + 1) + (i === 2 ? "（可选）" : ""), work.elements?.[i], 120);
        }
      }
    }
    function collect() {
      const values = Object.fromEntries(Object.entries(inputs).map(([key, node]) => [key, node.value.trim()]));
      if (phase === "timing_ideas") return values;
      const result = {title: values.title, concept: values.concept};
      if (phase !== "timing_final") {
        result.elements = [values.element0, values.element1, values.element2];
        while (result.elements.length && result.elements.at(-1) === "") result.elements.pop();
      }
      return result;
    }
    saveNotice = p("输入后自动保存", "saved");
    saveNotice.setAttribute("role", "status");
    const setLocked = (locked) => Object.values(inputs).forEach((node) => { node.readOnly = locked; });
    editor = {collect, setLocked, dirty: false, saved: JSON.stringify(collect())};
    form.addEventListener("input", () => {
      editor.dirty = JSON.stringify(collect()) !== editor.saved;
      setSave(editor.dirty ? "等待保存…" : "已保存");
      clearTimeout(saveTimer);
      if (editor.dirty) saveTimer = setTimeout(flush, 450);
    });
    form.append(row(el("button", {class: "button primary", type: "submit"}, "提交本阶段"), saveNotice));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      act("submit", {content: collect()});
    });
    return form;
  }

  function flush(keepalive = false, force = false) {
    if (savePromise) return savePromise;
    if (state?.pause || !editor?.dirty || (actionBusy && !force)) return Promise.resolve(true);
    clearTimeout(saveTimer);
    const current = editor;
    const seq = state.seq;
    const content = current.collect();
    setSave("正在保存…");
    savePromise = stageRequest("save", {content, edit_version: state.edit_version}, {keepalive}).then((view) => {
      if (view.seq !== seq) {
        applyState(view);
        return false;
      }
      current.saved = JSON.stringify(content);
      current.dirty = JSON.stringify(current.collect()) !== current.saved;
      state = view;
      serverDelta = view.now * 1000 - Date.now();
      setSave(current.dirty ? "等待保存…" : "已保存");
      if (current.dirty) saveTimer = setTimeout(flush, 450);
      return true;
    }).catch((error) => {
      setSave(error.message, true);
      if (error.code === "stale_phase") {
        setTimeout(refresh, 0);
      } else if (error.code !== "stale_edit" && error.status !== 401) {
        saveTimer = setTimeout(flush, 2500);
      }
      return false;
    }).finally(() => { savePromise = null; });
    return savePromise;
  }

  async function act(op, extra = {}) {
    if (actionBusy || state?.pause) return;
    actionBusy = true;
    const seq = state.seq;
    const currentEditor = editor;
    if (op !== "reference") currentEditor?.setLocked(true);
    if (op !== "reference") notice("");
    const focused = document.activeElement;
    const trigger = focused instanceof HTMLButtonElement ? focused :
      (op === "submit" ? workspace.querySelector('button[type="submit"]') : null);
    const caption = trigger?.textContent;
    if (trigger) { trigger.disabled = true; if (op !== "reference") trigger.textContent = "正在处理…"; }
    if (op !== "reference") workspace.setAttribute("aria-busy", "true");
    try {
      if (savePromise) await savePromise;
      if (state.seq !== seq) return;
      if ((op === "restore_open" || op === "restore_confirm") && editor?.dirty) {
        if (!await flush(false, true)) throw new Error("当前修改还未保存。请等连接恢复后再恢复原稿。");
      }
      const view = await stageRequest(op, {...extra, edit_version: state.edit_version});
      if (op === "reference") {
        applyState(view);
      } else {
        applyState(view, true);
        if (op === "restore_confirm" || (op === "restore_open" && !view.restore_step)) {
          notice("原稿已载入，恢复前的工作稿已保存。");
        }
      }
    } catch (error) {
      notice(error.message, "error");
      if (temporaryDialog?.open) {
        temporaryDialog.querySelector(".dialog-error")?.remove();
        const alert = p(error.message, "notice error inset dialog-error");
        alert.setAttribute("role", "alert");
        temporaryDialog.firstElementChild.append(alert);
      }
      if (error.code === "stale_phase") setTimeout(refresh, 0);
    } finally {
      if (trigger) { trigger.disabled = false; trigger.textContent = caption; }
      workspace.setAttribute("aria-busy", "false");
      currentEditor?.setLocked(false);
      actionBusy = false;
      if (editor?.dirty) saveTimer = setTimeout(flush, 350);
    }
  }

  function maskTask(masked) {
    document.body.classList.toggle("participant-paused", masked);
    document.querySelectorAll(".participant-shell, .topbar, .participant-foot").forEach((node) => { node.inert = masked; });
  }

  function closePause() {
    clearInterval(restartTimer);
    restartTimer = null;
    if (pauseDialog) { pauseDialog.close(); pauseDialog.remove(); pauseDialog = null; }
    maskTask(false);
  }

  function showPause(content, buttons, title = "实验已暂停") {
    closePause();
    closeDialog();
    maskTask(true);
    pauseDialog = el("dialog", {class: "pause-dialog", "aria-label": title},
      el("div", {class: "stack"}, el("h2", {}, title), content, row(buttons)));
    pauseDialog.addEventListener("cancel", (event) => event.preventDefault());
    document.body.append(pauseDialog);
    pauseDialog.showModal();
  }

  function currentLink(token = activeInvite) {
    return location.origin + location.pathname + (token ? "#invite=" + encodeURIComponent(token) : "");
  }

  function pauseMenu() {
    const saved = state.pause.archived;
    const remaining = state.pause.remaining;
    showPause(el("div", {class: "stack-sm"},
      p(saved ? "进度已存档在服务器，现在可以关闭页面。再次打开原邀请链接，点击继续实验即可。" :
        "当前输入已保存在服务器。暂停期间不能查看任务内容，也不消耗剩余时间。"),
      p("参与编号 " + state.participant_code, "mono"),
      remaining != null && p("本步骤剩余 " + Math.ceil(remaining) + " 秒", "mono"),
      saved && button("复制继续链接", async () => {
        notice(await copyText(currentLink()) ? "继续链接已复制。" : "请保存原邀请链接。");
        const status = pauseDialog?.querySelector(".copy-status");
        if (status) status.textContent = "继续链接已复制，可关闭页面。";
      }), saved && p("", "copy-status hint")),
    [button("继续实验", () => control("continue"), "primary"),
      button("存档并退出", () => control("archive_exit")),
      button("我要从头开始", () => control("restart_prepare"), "danger", {disabled: !state.restart_allowed})],
    saved ? "进度已存档，可关闭页面" : "实验已暂停");
  }

  function restartConfirmation() {
    const challenge = state.restart_challenge;
    const confirm = button("请等待 10 秒", () => control("restart_confirm", {challenge: challenge.token}), "danger", {disabled: true});
    confirm.dataset.restartConfirm = "1";
    showPause(el("div", {class: "stack-sm"},
      p("确认后将开始一份空白的新任务，并自动领取新的参与编号和个人链接。刚才的作答不会带入新任务。"),
      p("旧记录将封存，研究人员仍可查看；新旧编号不会相互覆盖。此操作无法撤销。", "notice warn inset")),
    [confirm, button("取消", () => control("restart_cancel"))], "确认从头开始");
    // Both this UI and the server enforce the full waiting interval.
    const receivedAt = performance.now();
    const minimum = Math.max(0, challenge.ready_at * 1000 - Date.now() - serverDelta);
    const update = () => {
      const seconds = Math.max(0, Math.ceil((minimum - (performance.now() - receivedAt)) / 1000));
      confirm.disabled = seconds > 0 || actionBusy;
      confirm.textContent = seconds > 0 ? "确认从头开始（" + seconds + " 秒）" : "确认从头开始";
    };
    update();
    restartTimer = setInterval(update, 100);
  }

  function collectPageDraft() {
    const values = {};
    workspace.querySelectorAll("input[name], select[name], textarea[name]").forEach((node) => {
      if (!["radio", "checkbox"].includes(node.type) || node.checked) values[node.name] = node.value;
    });
    return Object.fromEntries(Object.entries(values).filter(([key]) =>
      ["intent", "experience", "changed", "reason", "refinement"].includes(key)));
  }

  async function control(op, extra = {}) {
    if (actionBusy || !state || terminal.has(state.phase)) return;
    actionBusy = true;
    clearTimeout(saveTimer);
    const oldEditor = editor;
    const pageDraft = collectPageDraft();
    oldEditor?.setLocked(true);
    if (op === "pause") showPause(p("正在保存当前输入并请求暂停。服务器确认前，计时仍在继续。"), [], "正在暂停…");
    const lockedButtons = [...(pauseDialog?.querySelectorAll("button") || [])].map((node) => [node, node.disabled]);
    lockedButtons.forEach(([node]) => { node.disabled = true; });
    try {
      if (savePromise) await savePromise;
      const payload = op === "pause" ? {page_draft: pageDraft,
        ...(oldEditor ? {content: oldEditor.collect()} : {})} : extra;
      const view = await stageRequest(op, {...payload, edit_version: state.edit_version});
      if (view.restart_invitation) {
        const invitation = view.restart_invitation;
        activeInvite = invitation.token;
        cached("invite", activeInvite);
        joinNonce = cached("join") || identifier(); cached("join", joinNonce);
        closePause();
        state = null;
        // Persist the new link visibly in the address bar until entry has authenticated it.
        location.assign(currentLink(activeInvite));
        location.reload();
        return;
      }
      if (view.continuation_invitation) {
        activeInvite = view.continuation_invitation.token;
        cached("invite", activeInvite);
      }
      applyState(view, true);
    } catch (error) {
      if (op === "pause") {
        showPause(p(error.message + " 暂停尚未确认，计时可能仍在继续。当前输入仍保留在此页面。"),
          [button("重试暂停", () => control("pause")), button("核对服务器状态", async () => {
            try { applyState(await api(endpoint + "state"), true); } catch (e) { notice(e.message, "error"); }
          })], "请检查连接");
      } else {
        pauseDialog?.querySelector(".pause-error")?.remove();
        pauseDialog?.firstElementChild.append(p(error.message, "pause-error notice error inset"));
      }
    } finally {
      actionBusy = false;
      oldEditor?.setLocked(false);
      lockedButtons.forEach(([node, disabled]) => { if (node.isConnected) node.disabled = disabled; });
    }
  }

  function previewDraft(draft) {
    const elements = draft.elements?.filter(Boolean) || [];
    return el("div", {class: "stack-sm"},
      el("strong", {}, draft.title || "无标题"),
      p(draft.concept || "暂无正文", "readable"),
      elements.length && el("ul", {}, elements.map((element) => el("li", {}, element))));
  }

  function reference(labelText, name, content) {
    const detail = el("details", {class: "panel"}, el("summary", {}, labelText), content);
    detail.addEventListener("toggle", () => {
      if (detail.open && !actionBusy) act("reference", {reference: name});
    });
    return detail;
  }

  function briefCard() {
    return card("brief", label("任务简报"), el("h2", {}, state.brief.title), p(state.brief.brief, "readable"));
  }

  function stageHeader(title, description) {
    const timing = state.phase.startsWith("timing_");
    const steps = el("div", {class: "steps", "aria-label": "实验进度"},
      el("span", {class: "step" + (!timing ? " active" : "")}, "任务一　视觉构思"),
      el("span", {class: "step" + (timing ? " active" : "")}, "任务二　新简报"));
    const text = el("div", {class: "stack-sm"}, steps, heading(title, description));
    let timer = null;
    if (state.deadline) {
      timerNode = el("strong", {class: "mono"}, "—:—");
      timer = el("div", {class: "timer"}, el("small", {}, "剩余时间"), timerNode);
    }
    return el("div", {class: "section-top"}, text, timer);
  }

  function radios(items, name) {
    return el("div", {class: "stack-sm"}, items.map((item) =>
      el("label", {class: "choice option"},
        el("input", {type: "radio", name, value: item.id, required: true}),
        el("span", {class: "stack-sm"}, el("strong", {}, item.label), item.detail && p(item.detail, "hint")))));
  }

  function choicePhase(first) {
    const intent = radios(state.intentions, "intent");
    const experience = el("select", {name: "experience", required: true},
      el("option", {value: ""}, "请选择"), el("option", {value: "none"}, "没有相关经历"),
      el("option", {value: "some"}, "偶尔参与设计或创意项目"),
      el("option", {value: "regular"}, "经常参与设计或创意项目"),
      el("option", {value: "skip"}, "不填写"));
    const form = el("form", {class: "stack"}, el("h2", {}, "选择优先创意意图"), intent,
      first && field("你参与视觉设计或创意项目的经历", experience),
      el("div", {}, el("button", {type: "submit", class: "button primary"}, "保存选择并继续")));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      act("choose", {intent: data.get("intent"), ...(first ? {experience: data.get("experience")} : {})});
    });
    const description = first ? "阅读简报，并选出你最希望在方案中体现的方向。" : "请根据这份新简报单独选择优先意图。";
    return [stageHeader("阅读任务简报", description), el("div", {class: "two-columns"}, card("", form),
      el("div", {class: "stack read-first"}, briefCard(), first && p("建议阅读约 " + Math.round(state.duration / 60 * 10) / 10 + " 分钟。", "hint")))];
  }

  const instructions = {
    main_draft: ["独立构思", "请写出自己的初始方案，至少填写两个不同的关键视觉元素。"],
    main_edit: ["编辑视觉方案", "请以工作稿为起点完成定稿。你可以查看参考稿、自由编辑或恢复原稿。"],
    timing_suggestion: ["阅读一条 AI 建议", "请阅读完整建议，计时结束后页面会进入下一步。"],
    timing_ideas: ["写下两条构思", "为这份简报写下两个可以继续发展的创意方向。"],
    timing_final: ["完成视觉提案", "提交一句主题语和约 150 字的视觉提案。"],
  };

  function timedPhase() {
    const phase = state.phase;
    const [title, description] = instructions[phase];
    if (state.started == null) {
      return [stageHeader(title, description), el("div", {class: "two-columns"},
        card("", el("h2", {}, "准备开始"), p("本阶段限时 " + state.duration + " 秒。点击开始后计时；需要中断时，请点击暂停或按 ESC。直接离开或刷新不会自动暂停。", "lede"),
          state.chosen && p("你选择的优先意图：" + state.chosen),
          row(button("开始本阶段", () => act("start"), "primary"))), briefCard())];
    }
    if (phase === "timing_suggestion") {
      return [stageHeader(title, description), el("div", {class: "two-columns"},
        card("", label("AI 建议"), p(state.suggestion, "readable"), p("请保留页面开启，等待计时结束。", "hint")),
        briefCard())];
    }
    const controls = [];
    if (phase === "main_edit") {
      controls.push(card("", el("h2", {}, "参考与版本"),
        reference("我的原稿 · " + state.own.title, "own", previewDraft(state.own)),
        reference("AI 草稿 · " + state.ai.title, "ai", previewDraft(state.ai)),
        row(button("恢复我的原稿", () => act("restore_open")))));
    } else if (phase === "timing_final") {
      controls.push(card("", el("h2", {}, "回看构思"),
        reference("查看 AI 建议", "suggestion", p(state.suggestion, "readable")),
        reference("查看我的两条构思", "ideas",
          el("div", {class: "stack-sm"}, p(state.ideas.idea1 || "未填写", "readable"), p(state.ideas.idea2 || "未填写", "readable")))));
    }
    return [stageHeader(title, description), el("div", {class: "two-columns"},
      card("", draftForm(phase, state.work || {})), el("aside", {class: "stack"}, briefCard(), controls))];
  }

  function restoreDialog() {
    const cancel = () => act("restore_cancel");
    if (state.restore_step === 1) {
      showDialog("历史版本", el("div", {class: "stack-sm"},
        p("选择要恢复的版本。", "subtle"), el("div", {class: "panel"}, previewDraft(state.own))),
      [button("选择本人原稿", () => act("restore_select"), "primary"), button("取消", cancel)], cancel);
    } else {
      showDialog("确认恢复原稿", p("本人原稿将载入编辑区。当前工作稿会保留在版本记录中。"),
        [button("确认恢复", () => act("restore_confirm"), "primary"), button("取消", cancel)], cancel);
    }
  }

  function postPhase() {
    const options = [{id: "yes", label: "有意改变了方向"}, {id: "no", label: "仍沿着原先方向"}, {id: "unsure", label: "不确定"}];
    const reason = el("textarea", {name: "reason", maxlength: 1500, required: true, rows: 4});
    const form = el("form", {class: "stack"}, el("h2", {}, "你是否主动改变了原先的优先意图？"),
      p("你选择的优先意图：" + state.chosen, "subtle"), radios(options, "changed"),
      field("简要说明原因", reason), row(el("button", {type: "submit", class: "button primary"}, "保存并进入任务二")));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      act("post", {changed: data.get("changed"), reason: reason.value});
    });
    return [stageHeader("回顾你的设计", "文字定稿已经封存。请简短回顾刚才的决策。"), card("", form)];
  }

  function imagePhase() {
    const image = state.image;
    const generating = image.status === "generating";
    const refinement = el("textarea", {name: "refinement", rows: 3, maxlength: 500, disabled: generating});
    const files = image.rounds.flatMap((round) => round.files);
    const buttons = files.map((file, i) => el("div", {class: "panel stack-sm"},
      el("img", {src: endpoint + "image/" + file + (state.access_scope ? "?access=" + state.access_scope : ""), alt: "候选图 " + (i + 1)}),
      button("提交候选图 " + (i + 1), () => act("image_select", {choice: file}), "secondary", {disabled: generating})));
    return [stageHeader("选择最终图像", "用已封存的文字方案生成候选图，再选择一张提交。"),
      el("div", {class: "two-columns"}, card("", generating && p("正在生成候选图，请保持页面开启。", "notice inset"),
        image.status === "failed" && p("刚才的生成未成功。可以重试、选用已有图像或跳过。", "notice warn inset"),
        el("div", {class: "image-grid"}, buttons),
        image.rounds.length === 1 && field("第二轮补充要求（可选）", refinement),
        row(image.rounds.length < 2 && button(image.rounds.length ? "生成第二轮" : "生成两张候选图",
          () => act("image_generate", {refinement: refinement.value}), "primary", {disabled: generating}),
        button("跳过图像", () => act("image_skip"), "secondary", {disabled: generating}))),
      card("brief", label("已封存文字定稿"), previewDraft(state.final)))];
  }

  function render() {
    closeDialog();
    closePause();
    clearTimeout(saveTimer);
    editor = null;
    timerNode = null;
    saveNotice = null;
    idNode.hidden = false;
    idNode.textContent = "版本 " + (state.release_code || state.version_code) + " · " + (state.participant_code || state.id);
    exitButton.hidden = terminal.has(state.phase);
    pauseButton.hidden = terminal.has(state.phase);
    document.title = state.title + " · 受试者端";
    let nodes;
    if (terminal.has(state.phase)) {
      const titles = {done: "实验已提交", withdrawn: "已退出实验", incomplete: "本次实验未完成", restarted: "原任务已存档"};
      const descriptions = {
        done: "感谢参与。你的任务记录已经保存，可以关闭页面。",
        withdrawn: "退出状态已经记录。需要联系研究人员时，请提供匿名编号。",
        incomplete: "独立构思阶段在截止前未收到完整原稿。需要协助时，请提供匿名编号。",
        restarted: "你已选择从头开始，这份链接对应的旧记录已封存。请使用重开时生成的新链接继续。",
      };
      nodes = [card("intro", label("任务结束"), heading(titles[state.phase], descriptions[state.phase]),
        p(state.participant_code || state.id, "mono key-line"), p(state.contact, "hint readable"),
        state.phase === "incomplete" && row(button("检查恢复状态", initialize, "primary")),
        p("如需重新做一次，请向研究人员索取新的个人邀请链接；同一份邀请始终对应同一条记录。", "hint"))];
    } else if (state.phase === "main_brief" || state.phase === "timing_intro") {
      nodes = choicePhase(state.phase === "main_brief");
    } else if (state.phase === "main_post") {
      nodes = postPhase();
    } else if (state.phase === "main_image") {
      nodes = imagePhase();
    } else {
      nodes = timedPhase();
    }
    workspace.replaceChildren(...nodes);
    for (const [name, value] of Object.entries(state.page_draft || {})) {
      workspace.querySelectorAll("[name]").forEach((node) => {
        if (node.name !== name) return;
        if (node.type === "radio") node.checked = node.value === value;
        else node.value = value;
      });
    }
    if (state.pause) {
      if (state.restart_challenge) restartConfirmation(); else pauseMenu();
    } else if (state.phase === "main_edit" && state.restore_step) restoreDialog();
    updateTimer();
  }

  function updateTimer() {
    if (!timerNode || state?.pause || !state?.deadline) return;
    const seconds = Math.max(0, Math.ceil((state.deadline * 1000 - Date.now() - serverDelta) / 1000));
    timerNode.textContent = String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
    timerNode.parentElement.classList.toggle("ending", seconds <= 20);
    if (seconds === 0 && Date.now() - lastRead > 1000) refresh();
  }

  function saveCredentials(credentials) {
    const details = el("div", {class: "stack-sm"},
      p("如果关闭页面、更换设备或浏览器，可以用这两项信息恢复进度。请仅自己保存。", "lede"),
      p("参与者编号　" + (credentials.participant_code || credentials.id), "mono key-line"),
      p("恢复码　" + credentials.code, "mono key-line"));
    showDialog("请保存你的编号和恢复码", details,
      [button("我已保存，进入任务", closeDialog, "primary"),
        button("复制编号和恢复码", async () => {
          const ok = await copyText((credentials.participant_code || credentials.id) + "\n" + credentials.code);
          details.append(p(ok ? "已复制。" : "请手动复制。", "hint"));
        })], () => {});
  }

  function landing(view) {
    const key = el("input", {name: "invite", required: true, maxlength: 96, autocomplete: "off", spellcheck: "false"});
    key.value = activeInvite;
    if (view.reserved_code) key.type = "hidden";
    const consent = el("input", {type: "checkbox", name: "consent", required: true});
    const adult = el("input", {type: "checkbox", name: "adult", required: true});
    const form = el("form", {class: "stack"}, el("h2", {}, "开始前请确认"),
      p(view.consent || "请使用研究人员发送的邀请链接。", "readable subtle"),
      view.adult_only && el("label", {class: "choice"}, adult, "我已满 18 周岁。"),
      el("label", {class: "choice"}, consent, "我已阅读知情说明，自愿参与这项研究。"),
      view.reserved_code ? el("div", {class: "panel stack-sm"},
        p("对应参与编号 " + view.reserved_code, "mono"), key) : field("邀请口令", key),
      row(el("button", {type: "submit", class: "button primary"}, "同意并领取参与者编号")));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busyLanding) return;
      busyLanding = true;
      const submit = form.querySelector("button");
      submit.disabled = true;
      notice("");
      try {
        if (key.value.trim() !== activeInvite) {
          activeInvite = key.value.trim();
          cached("invite", activeInvite);
          joinNonce = cached("join") || identifier();
          cached("join", joinNonce);
        }
        const answer = await api(endpoint + "join", {invite: key.value.trim(), nonce: joinNonce,
          consent: consent.checked, adult: adult.checked});
        applyState(answer.view, true);
        if (answer.resume) saveCredentials(answer.resume);
      } catch (error) {
        notice(error.message, "error");
      } finally {
        submit.disabled = false;
        busyLanding = false;
      }
    });
    const resumeId = el("input", {name: "id", maxlength: 64, required: true, autocomplete: "off", placeholder: "S0-V0001-P000001 或原 P-… 编号"});
    const resumeCode = el("input", {name: "code", maxlength: 24, required: true, autocomplete: "off"});
    const resume = el("form", {class: "stack-sm"}, field("参与者编号", resumeId), field("恢复码", resumeCode),
      row(el("button", {type: "submit", class: "button secondary"}, "恢复进度")));
    resume.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busyLanding) return;
      busyLanding = true;
      try {
        applyState(await api(endpoint + "resume", {id: resumeId.value.trim(), code: resumeCode.value.trim()}), true);
      } catch (error) {
        notice(error.message, "error");
      } finally { busyLanding = false; }
    });
    const side = card("brief", el("h2", {}, "你将完成两项创意任务"),
      el("ol", {}, el("li", {}, "构思并编辑一份视觉方案。"), el("li", {}, "根据新简报写下两条构思和最终提案。")),
      p("请预留约 20 分钟，准备连续完成。已保存的内容可通过匿名编号和恢复码找回。", "subtle"),
      el("details", {class: "panel"}, el("summary", {}, "我已有编号，恢复进度"), resume),
      view.contact && p(view.contact, "hint readable"));
    workspace.replaceChildren(
      el("div", {class: "section-top"}, el("div", {class: "stack-sm"}, label(mode === "pilot" ? "研究预试" : "研究邀请"),
        heading(view.title, "请根据页面提示，完成两项简短的视觉创意活动。"),
        view.version_code && p("实验版本 " + (view.release_code || view.version_code), "mono subtle"))),
      el("div", {class: "two-columns"}, view.open ? card("intro", form) :
        card("", el("h2", {}, "当前未开放新报名"), p("已有参与者可使用恢复码继续。", "lede")), side));
  }

  exitButton.addEventListener("click", () => {
    if (!state || terminal.has(state.phase)) return;
    const reason = el("textarea", {maxlength: 500, rows: 3});
    showDialog("确认退出本次实验", el("div", {class: "stack-sm"},
      p("退出后本次任务结束。已提交的信息按知情说明处理。"), field("退出原因（可选）", reason)),
      [button("继续参与", closeDialog), button("确认退出", () => act("withdraw", {reason: reason.value}), "danger")], closeDialog);
  });
  pauseButton.addEventListener("click", () => control("pause"));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !state || terminal.has(state.phase) || event.isComposing) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!state.pause && !pauseDialog && !temporaryDialog?.getAttribute("aria-label")?.includes("恢复码")) control("pause");
  }, true);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) flush(true);
    else refresh();
  });
  window.addEventListener("online", () => { flush(); refresh(); });
  window.addEventListener("beforeunload", (event) => {
    if (editor?.dirty) {
      event.preventDefault();
      event.returnValue = true;
    }
  });
  setInterval(() => {
    updateTimer();
    if (state?.phase === "main_image" && state.image.status === "generating" && Date.now() - lastRead > 2200) refresh();
    if (Date.now() - lastRead > 12000) refresh();
  }, 250);

  async function initialize() {
    try {
      const answer = await api(endpoint + "entry", {invite: activeInvite});
      if (answer.view) applyState(answer.view, true);
      else landing(answer.landing);
    } catch (error) {
      workspace.replaceChildren(card("", el("h2", {}, "暂时无法进入实验"), p(error.message),
        row(button("重新连接", initialize, "primary"))));
    }
  }
  initialize();
})();
