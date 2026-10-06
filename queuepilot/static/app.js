"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  const state = {
    apiKey: "",
    filter: "",
    offset: 0,
    limit: 20,
    selectedId: null,
    selectedJob: null,
    events: [],
    provider: "mock",
    stats: null,
    refreshBusy: false,
    refreshAgain: false,
    submitting: false,
    actionBusy: false,
    connectionVersion: 0,
  };
  const labels = {
    queued: "排队中",
    running: "运行中",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "已失败",
    cancelled: "已取消",
  };
  const eventLabels = {
    submitted: "任务已提交",
    started: "开始执行",
    created: "任务已创建",
    queued: "任务已排队",
    job_created: "任务已创建",
    claimed: "Worker 已认领",
    running: "开始执行",
    attempt_started: "开始执行",
    retry_wait: "等待自动重试",
    retry_scheduled: "已安排重试",
    succeeded: "执行成功",
    failed: "执行失败",
    attempt_failed: "本次尝试失败",
    cancelled: "任务已取消",
    retried: "手动重新排队",
    retry_requested: "手动重新排队",
    manual_retry: "手动重新排队",
    lease_expired: "租约到期，恢复任务",
    lease_recovered: "租约到期，恢复任务",
  };
  const demoText = "产品团队决定将耗时的文本摘要放到后台执行。用户提交内容后立即得到任务 ID，不必等待模型返回。每一次执行都会保存状态和事件；遇到临时错误时，系统按照退避策略自动重试。服务重启后，未完成的任务仍可从 SQLite 中恢复。这个项目用于学习可靠后端的事务、幂等与租约设计。";
  let toastTimer;
  let pollTimer;
  let pollVersion = 0;

  function write(id, text) {
    get(id).textContent = String(text ?? "");
  }

  function toast(message, isError = false) {
    clearTimeout(toastTimer);
    const node = get("toast");
    node.textContent = message;
    node.classList.toggle("error", isError);
    node.hidden = false;
    toastTimer = setTimeout(() => { node.hidden = true; }, 4500);
  }

  function connection(message, error = false) {
    get("connection").classList.toggle("error", error);
    get("connection").classList.toggle("online", !error);
    write("connection-text", error ? "连接需检查" : "服务在线");
    get("connection-error").hidden = !message;
    write("connection-error", message);
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    const headers = { Accept: "application/json", ...options.headers };
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    try {
      const response = await fetch(path, { ...options, headers, signal: controller.signal, cache: "no-store" });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        let message = `请求失败（${response.status}），请稍后重试。`;
        if (response.status === 401) message = "业务接口需要正确的访问密钥。请在上方填写 X-API-Key 后连接。";
        if (response.status === 404) message = "任务不存在，可能已切换数据库。请刷新列表。";
        if (response.status === 409) message = "操作与当前状态冲突，或幂等键已经用于不同请求。请刷新后检查。";
        if (response.status === 422) message = "输入不符合要求。请检查文本长度、尝试次数和模拟失败次数。";
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      if (body === null) throw new Error("服务响应格式异常，请检查后端状态。");
      return { data: body, status: response.status };
    } catch (error) {
      if (error.name === "AbortError") throw new Error("服务响应超时，请检查服务是否正常运行。");
      if (error instanceof TypeError) throw new Error("无法连接服务。请确认 QueuePilot 已启动，然后刷新。");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function asDate(value) {
    return new Date(typeof value === "number" ? value * 1000 : value);
  }

  function formatDate(value, withDate = true) {
    if (!value) return "—";
    const date = asDate(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString("zh-CN", withDate ? {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    } : { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function age(value) {
    const elapsed = Math.max(0, Math.floor((Date.now() - asDate(value).getTime()) / 1000));
    if (!Number.isFinite(elapsed)) return "—";
    if (elapsed < 60) return "刚刚";
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)} 分钟前`;
    if (elapsed < 86400) return `${Math.floor(elapsed / 3600)} 小时前`;
    return formatDate(value);
  }

  function statusBadge(status, node = document.createElement("span")) {
    const known = Object.hasOwn(labels, status);
    node.className = `status-badge${known ? ` ${status}` : ""}`;
    node.textContent = labels[status] || String(status);
    return node;
  }

  function renderStats(stats) {
    state.stats = stats;
    state.provider = stats.provider;
    const counts = stats.counts;
    write("stat-total", stats.total);
    write("stat-waiting", counts.queued + counts.retry_wait);
    write("stat-running", counts.running);
    write("stat-succeeded", counts.succeeded);
    write("stat-failed", counts.failed);
    write("stat-cancelled", counts.cancelled);
    write("attempt-count", `累计执行 ${stats.total_attempts} 次`);
    const mock = stats.provider === "mock";
    write("provider-label", mock ? "mock · 离线摘要" : "Ollama · 本地模型");
    write("worker-label", `SQLite 持久化 · 内置 Worker ${stats.worker_enabled ? "已启用" : "已关闭"}`);
    get("simulate-failures").disabled = !mock;
    if (!mock) get("simulate-failures").value = "0";
    write("simulation-hint", mock ? "mock 模式可模拟失败，观察自动重试。" : "Ollama 模式调用本地模型，不支持模拟失败。");
    write("demo-note", mock ? "重试与失败示例使用确定性 mock，不调用大模型。" : "当前为 Ollama 模式；成功示例会调用配置的本地模型。");
    updateButtons();
  }

  function renderJobs(data) {
    const body = get("jobs-body");
    const fragment = document.createDocumentFragment();
    for (const job of data.items) {
      const row = document.createElement("tr");
      row.classList.toggle("selected", job.id === state.selectedId);
      row.addEventListener("click", () => selectJob(job.id));
      const content = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "job-row-button";
      button.setAttribute("aria-label", `查看任务 ${job.id}`);
      const id = document.createElement("span");
      id.className = "job-row-id";
      id.textContent = job.id.slice(0, 8);
      const preview = document.createElement("span");
      preview.className = "job-row-preview";
      preview.textContent = job.text;
      button.append(id, preview);
      content.append(button);
      const status = document.createElement("td");
      status.append(statusBadge(job.status));
      const attempt = document.createElement("td");
      attempt.className = "job-attempt";
      attempt.textContent = `${job.attempt} / ${job.max_attempts}`;
      const time = document.createElement("td");
      time.className = "job-age";
      time.textContent = age(job.created_at);
      time.title = formatDate(job.created_at);
      row.append(content, status, attempt, time);
      fragment.append(row);
    }
    body.replaceChildren(fragment);
    const empty = get("jobs-empty");
    empty.hidden = data.items.length > 0;
    empty.querySelector("strong").textContent = state.filter ? "当前状态下暂无任务" : "从第一个任务开始";
    empty.querySelector("p").textContent = state.filter ? "切换状态筛选，或提交一个新的示例任务。" : "提交文本，或点击左侧示例体验完整流程。";
    write("list-total", data.total);
    const pages = Math.max(1, Math.ceil(data.total / state.limit));
    write("page-info", `${Math.floor(state.offset / state.limit) + 1} / ${pages}`);
    get("previous-page").disabled = state.offset === 0;
    get("next-page").disabled = state.offset + state.limit >= data.total;
  }

  function renderDetails(job) {
    state.selectedJob = job;
    get("details-empty").hidden = true;
    get("details-content").hidden = false;
    document.querySelector(".detail-hint").textContent = "实时追踪执行";
    write("selected-job-id", job.id);
    write("selected-job-meta", `本轮尝试 ${job.attempt} / ${job.max_attempts} · 累计执行 ${job.total_attempts} 次`);
    statusBadge(job.status, get("selected-job-status"));
    const result = get("job-result");
    result.classList.toggle("is-success", job.status === "succeeded");
    result.classList.toggle("is-pending", job.status !== "succeeded");
    const waiting = {
      queued: "任务已入队，等待 Worker 认领。",
      running: "Worker 正在处理文本，结果会自动显示。",
      retry_wait: "本次执行未成功，系统将在退避等待后自动重试。",
      failed: "任务已耗尽本轮尝试次数。可手动重新排队，执行历史会保留。",
      cancelled: "任务已在执行前取消。",
    };
    result.textContent = job.result?.summary || waiting[job.status] || "暂无结果。";
    write("result-provider", job.result ? `${job.result.provider} · 输入 ${job.result.characters} 字符` : "");
    get("job-error").hidden = !job.error;
    write("job-error", job.error ? `最近错误：${typeof job.error === "string" ? job.error : JSON.stringify(job.error)}` : "");
    write("job-source", job.text);
    write("job-created", formatDate(job.created_at));
    write("job-updated", formatDate(job.updated_at));
    get("job-available-row").hidden = job.status !== "retry_wait";
    write("job-available", formatDate(job.available_at));
    get("cancel-job").hidden = !["queued", "retry_wait"].includes(job.status);
    get("retry-job").hidden = job.status !== "failed";
    updateButtons();
  }

  function renderEvents() {
    const list = get("events-list");
    const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 35;
    const fragment = document.createDocumentFragment();
    for (const event of state.events) {
      const item = document.createElement("li");
      if (event.type.includes("succeed")) item.className = "event-success";
      if (event.type.includes("fail") || event.type.includes("expired")) item.className = "event-error";
      const title = document.createElement("span");
      title.className = "event-title";
      title.textContent = eventLabels[event.type] || event.type;
      if (event.attempt > 0) {
        const attempt = document.createElement("span");
        attempt.className = "event-attempt";
        attempt.textContent = `第 ${event.attempt} 次`;
        title.append(attempt);
      }
      const time = document.createElement("time");
      time.className = "event-time";
      time.textContent = formatDate(event.created_at);
      time.dateTime = asDate(event.created_at).toISOString();
      item.append(title, time);
      if (event.detail && (typeof event.detail !== "object" || Object.keys(event.detail).length > 0)) {
        const detail = document.createElement("p");
        detail.className = "event-detail";
        detail.textContent = typeof event.detail === "string" ? event.detail : JSON.stringify(event.detail, null, 2);
        item.append(detail);
      }
      fragment.append(item);
    }
    list.replaceChildren(fragment);
    get("events-empty").hidden = state.events.length > 0;
    write("event-count", `${state.events.length} 条事件`);
    if (atEnd) list.scrollTop = list.scrollHeight;
  }

  async function refreshDetails() {
    const id = state.selectedId;
    if (!id) return;
    const version = state.connectionVersion;
    const after = state.events.at(-1)?.id ?? 0;
    const [job, events] = await Promise.all([
      request(`/api/jobs/${encodeURIComponent(id)}`),
      request(`/api/jobs/${encodeURIComponent(id)}/events?after=${after}`),
    ]);
    if (id !== state.selectedId || version !== state.connectionVersion) return;
    renderDetails(job.data);
    if (events.data.items.length > 0) {
      const existing = new Set(state.events.map((event) => event.id));
      state.events.push(...events.data.items.filter((event) => !existing.has(event.id)));
      state.events.sort((left, right) => left.id - right.id);
      renderEvents();
    }
  }

  async function refresh() {
    if (state.refreshBusy) {
      state.refreshAgain = true;
      return;
    }
    state.refreshBusy = true;
    get("refresh-button").classList.add("refreshing");
    const filter = state.filter;
    const offset = state.offset;
    const version = state.connectionVersion;
    try {
      const query = new URLSearchParams({ limit: String(state.limit), offset: String(offset) });
      if (filter) query.set("status", filter);
      const [stats, jobs] = await Promise.all([request("/api/stats"), request(`/api/jobs?${query}`)]);
      if (version !== state.connectionVersion) return;
      renderStats(stats.data);
      if (filter === state.filter && offset === state.offset) {
        if (jobs.data.items.length === 0 && offset > 0) {
          state.offset = Math.max(0, Math.ceil(jobs.data.total / state.limit) - 1) * state.limit;
          state.refreshAgain = true;
        } else {
          renderJobs(jobs.data);
        }
      }
      await refreshDetails();
      if (version !== state.connectionVersion) return;
      connection("");
      write("updated-time", `更新于 ${formatDate(new Date().toISOString(), false)} · 2 秒轮询`);
    } catch (error) {
      if (version === state.connectionVersion) connection(error.message, true);
    } finally {
      state.refreshBusy = false;
      get("refresh-button").classList.remove("refreshing");
      if (state.refreshAgain) {
        state.refreshAgain = false;
        void refresh();
      }
    }
  }

  function selectJob(id) {
    if (id !== state.selectedId) {
      state.selectedId = id;
      state.selectedJob = null;
      state.events = [];
      get("details-content").hidden = true;
      get("details-empty").hidden = false;
      get("details-empty").querySelector("strong").textContent = "正在读取任务";
      get("details-empty").querySelector("p").textContent = "加载执行结果与事件时间线……";
      renderEvents();
    }
    void refresh();
  }

  function updateButtons() {
    get("job-form").setAttribute("aria-busy", String(state.submitting));
    get("submit-button").disabled = state.submitting;
    get("submit-button").querySelector("span").textContent = state.submitting ? "正在提交…" : "提交任务";
    get("demo-success").disabled = state.submitting;
    get("demo-retry").disabled = state.submitting || state.provider !== "mock";
    get("demo-failed").disabled = state.submitting || state.provider !== "mock";
    get("cancel-job").disabled = state.actionBusy;
    get("retry-job").disabled = state.actionBusy;
  }

  async function submitJob(payload, idempotencyKey = "") {
    if (state.submitting) return;
    state.submitting = true;
    updateButtons();
    try {
      const headers = { "Content-Type": "application/json" };
      if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
      const response = await request("/api/jobs", { method: "POST", headers, body: JSON.stringify(payload) });
      state.filter = "";
      state.offset = 0;
      get("status-filter").value = "";
      selectJob(response.data.id);
      toast(response.status === 200 ? "幂等命中：已返回之前创建的任务。" : "任务已提交，后台执行进度会自动更新。");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.submitting = false;
      updateButtons();
    }
  }

  async function jobAction(action) {
    if (!state.selectedId || state.actionBusy) return;
    const id = state.selectedId;
    state.actionBusy = true;
    updateButtons();
    try {
      const response = await request(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      if (id === state.selectedId) renderDetails(response.data);
      toast(action === "cancel" ? "任务已取消。" : "任务已重新排队，本轮尝试归零，执行历史已保留。");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.actionBusy = false;
      updateButtons();
      void refresh();
    }
  }

  get("connection-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.apiKey = get("api-key").value.trim();
    state.connectionVersion += 1;
    state.selectedId = null;
    state.selectedJob = null;
    state.events = [];
    state.offset = 0;
    get("details-content").hidden = true;
    get("details-empty").hidden = false;
    get("details-empty").querySelector("strong").textContent = "每次尝试，都有记录";
    get("details-empty").querySelector("p").textContent = "点击任务，查看结果、错误与完整执行时间线。";
    void refresh();
  });
  get("job-text").addEventListener("input", () => {
    get("job-text-error").hidden = true;
    get("job-text").removeAttribute("aria-invalid");
    write("character-count", `${get("job-text").value.length.toLocaleString("zh-CN")} / 20,000`);
  });
  get("job-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = get("job-text").value.trim();
    if (!text) { get("job-text-error").textContent = "请输入至少一个非空白字符。"; get("job-text-error").hidden = false; get("job-text").setAttribute("aria-invalid", "true"); get("job-text").focus(); return; }
    void submitJob({
      text,
      max_attempts: Number(get("max-attempts").value),
      simulate_failures: state.provider === "mock" ? Number(get("simulate-failures").value) : 0,
    }, get("idempotency-key").value.trim());
  });
  get("demo-success").addEventListener("click", () => { void submitJob({ text: demoText, max_attempts: 3, simulate_failures: 0 }); });
  get("demo-retry").addEventListener("click", () => { void submitJob({ text: demoText, max_attempts: 3, simulate_failures: 2 }); });
  get("demo-failed").addEventListener("click", () => { void submitJob({ text: demoText, max_attempts: 3, simulate_failures: 3 }); });
  get("status-filter").addEventListener("change", () => {
    state.filter = get("status-filter").value;
    state.offset = 0;
    void refresh();
  });
  get("refresh-button").addEventListener("click", () => { void refresh(); });
  get("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); void refresh(); });
  get("next-page").addEventListener("click", () => { state.offset += state.limit; void refresh(); });
  get("cancel-job").addEventListener("click", () => { void jobAction("cancel"); });
  get("retry-job").addEventListener("click", () => { void jobAction("retry"); });
  get("copy-id").addEventListener("click", async () => {
    if (!state.selectedId) return;
    try { await navigator.clipboard.writeText(state.selectedId); toast("任务 ID 已复制。"); }
    catch { toast("浏览器未允许复制，可在详情中直接选中任务 ID。", true); }
  });

  async function poll(version = pollVersion) {
    if (!document.hidden) await refresh();
    if (version === pollVersion) pollTimer = setTimeout(() => { void poll(version); }, 2000);
  }
  document.addEventListener("visibilitychange", () => {
    clearTimeout(pollTimer);
    pollVersion += 1;
    if (!document.hidden) void poll();
  });
  window.addEventListener("pagehide", () => { pollVersion += 1; clearTimeout(pollTimer); clearTimeout(toastTimer); });
  void poll();
})();
