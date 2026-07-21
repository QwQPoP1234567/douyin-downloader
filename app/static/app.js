const $ = (selector) => document.querySelector(selector);

const statusLabels = {
  idle: "等待检查", scanning: "扫描中", needs_verification: "需要人工验证",
  error: "扫描失败", pending: "等待下载", downloading: "下载中",
  downloaded: "已下载", failed: "下载失败"
};
const remoteLabels = {
  active: "线上可见",
  unconfirmed_missing: "本次未出现，待复核",
  removed_or_private: "已删除或转为私密"
};
let pendingDeleteCreatorId = null;
let pendingContinueCreatorId = null;
const previewState = {token: null, page: 1, pageSize: 30, totalPages: 0, items: [], timer: null};
const routes = new Set(["dashboard", "add", "creators", "videos", "pending", "downloads", "settings", "logs", "player"]);
let currentRoute = "dashboard";
let refreshInFlight = false;
let refreshTimer = null;
const creatorState = {page: 1, pageSize: 20, totalPages: 0, editingId: null, hasActiveJobs: false};
const videoLibraryState = {page: 1, pageSize: 30, totalPages: 0, items: [], selected: new Set(), creatorId: null};
const pendingState = {page: 1, pageSize: 30, totalPages: 0, items: [], selected: new Set(), creatorId: null};
const downloadState = {page: 1, pageSize: 30, totalPages: 0, creatorId: null, status: null};
const logState = {page: 1, pageSize: 50, totalPages: 0, level: null};
const playerState = {
  currentId: null, context: null, assets: [], imageIndex: 0,
  switching: false, wheelLocked: false, returnScrollY: 0,
  zoom: 1, panX: 0, panY: 0, dragging: false, dragStartX: 0, dragStartY: 0,
  dragOriginX: 0, dragOriginY: 0, imageTimer: null,
  rightHoldTimer: null, rightHoldActive: false, rightHoldOriginalRate: 1,
  rightHoldWasPaused: false
};
const downloadStatusLabels = {queued: "等待中", running: "下载中", pausing: "正在暂停", paused: "已暂停", cancelling: "正在取消", completed: "已完成", failed: "失败", cancelled: "已取消"};
let pendingDeleteVideoId = null;
const policyLabels = {
  manual_selected_only: "仅手动选择",
  selected_then_auto_new: "选中历史 + 自动新增",
  all_history_then_auto_new: "全部历史 + 自动新增",
  new_only_auto: "仅自动新增",
  metadata_only: "只收集信息",
  new_pending_confirmation: "新增待确认"
};
const policyDescriptions = {
  manual_selected_only: "只处理本次明确勾选的历史作品；以后扫描到新作品时不会自动下载。",
  selected_then_auto_new: "本次下载已勾选的历史作品；确认添加后扫描到的新作品会自动下载。",
  all_history_then_auto_new: "本次获取到的历史作品全部进入下载队列；以后发现的新作品也自动下载。",
  new_only_auto: "本次历史作品只保存信息、不下载；确认添加后发现的新作品自动下载。",
  metadata_only: "历史和后续作品都只保存标题、链接等信息，不会自动创建下载任务。",
  new_pending_confirmation: "历史作品按本次选择处理；以后发现的新作品进入待确认页面，由你决定是否下载。"
};

function updatePolicyDescription(select, target) {
  target.textContent = policyDescriptions[select.value] || "";
}

function saveListState() {
  try {
    localStorage.setItem("douyinListState", JSON.stringify({
      creators: {page: creatorState.page, pageSize: creatorState.pageSize},
      videos: {page: videoLibraryState.page, pageSize: videoLibraryState.pageSize, creatorId: videoLibraryState.creatorId, keyword: $("#videoKeyword").value, status: $("#videoStatusFilter").value, type: $("#videoTypeFilter").value, sort: $("#videoSort").value},
      pending: {page: pendingState.page, pageSize: pendingState.pageSize, creatorId: pendingState.creatorId, keyword: $("#pendingKeyword").value, type: $("#pendingType").value, sort: $("#pendingSort").value},
      downloads: {page: downloadState.page, pageSize: downloadState.pageSize, creatorId: downloadState.creatorId, status: downloadState.status},
      logs: {page: logState.page, pageSize: logState.pageSize, level: logState.level},
      preview: {token: previewState.token, page: previewState.page, pageSize: previewState.pageSize, keyword: $("#previewKeyword").value, type: $("#previewType").value, sort: $("#previewSort").value, continueLimit: Number($("#previewContinueLimit").value) || 100}
    }));
  } catch (_) {}
}

function restoreListState() {
  try {
    const saved = JSON.parse(localStorage.getItem("douyinListState") || "null");
    if (!saved) return;
    Object.assign(creatorState, saved.creators || {});
    Object.assign(videoLibraryState, saved.videos || {});
    Object.assign(pendingState, saved.pending || {});
    Object.assign(downloadState, saved.downloads || {});
    Object.assign(logState, saved.logs || {});
    Object.assign(previewState, saved.preview || {});
    $("#creatorPageSize").value = String(creatorState.pageSize);
    $("#videoKeyword").value = saved.videos?.keyword || "";
    $("#videoStatusFilter").value = saved.videos?.status ?? "downloaded";
    $("#videoTypeFilter").value = saved.videos?.type || "";
    $("#videoSort").value = saved.videos?.sort || "newest";
    $("#videoPageSize").value = String(videoLibraryState.pageSize);
    $("#pendingKeyword").value = saved.pending?.keyword || "";
    $("#pendingType").value = saved.pending?.type || "";
    $("#pendingSort").value = saved.pending?.sort || "newest";
    $("#pendingPageSize").value = String(pendingState.pageSize);
    $("#downloadStatusFilter").value = downloadState.status || "";
    $("#downloadPageSize").value = String(downloadState.pageSize);
    $("#logsLevel").value = logState.level || "";
    $("#logsPageSize").value = String(logState.pageSize);
    $("#previewKeyword").value = saved.preview?.keyword || "";
    $("#previewType").value = saved.preview?.type || "";
    $("#previewSort").value = saved.preview?.sort || "desc";
    $("#previewPageSize").value = String(previewState.pageSize);
    $("#previewContinueLimit").value = String(saved.preview?.continueLimit || 100);
  } catch (_) {}
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

function fallbackCover(image) {
  const fallback = image.dataset.fallback;
  if (fallback && !image.dataset.fallbackUsed) {
    image.dataset.fallbackUsed = "true";
    image.src = fallback;
  } else {
    image.hidden = true;
  }
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  try {
    const response = await fetch(path, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options
    });
    if (!response.ok) {
      let detail = `请求失败 (${response.status})`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return await response.json();
  } catch (error) {
    $("#requestFeedback").classList.add("error");
    $("#requestFeedbackText").textContent = error.message || "请求失败";
    $("#requestRetry").hidden = false;
    $("#requestFeedback").hidden = false;
    throw error;
  }
}

function timeText(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false});
}

function bytesText(value) {
  const size = Number(value || 0);
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const power = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / Math.pow(1024, power)).toFixed(power ? 1 : 0)} ${units[power]}`;
}

async function loadStatus() {
  const data = await api("/api/status");
  const badge = $("#loginBadge");
  badge.className = `badge ${data.verification_required ? "error" : data.logged_in ? "success" : data.browser_running ? "warning" : "neutral"}`;
  badge.textContent = data.verification_required ? "需要人工完成验证码" : data.logged_in ? "账号已登录" : data.browser_running ? "等待扫码 / 验证" : "浏览器未启动";
  const accountText = data.verification_required ? "需要验证" : data.logged_in ? "已登录" : data.browser_running ? "等待登录" : "未启动";
  $("#dashboardLogin").textContent = accountText;
  $("#dashboardCreators").textContent = data.creator_count;
  $("#dashboardScans").textContent = data.active_scans;
  $("#dashboardStorage").textContent = data.download_dir;
  $("#dashboardStorage").title = data.download_dir;
  $("#sidebarLoginText").textContent = accountText;
  $("#sidebarLoginDot").className = `status-dot ${data.verification_required ? "error" : data.logged_in ? "success" : data.browser_running ? "warning" : ""}`;
  $("#loginHint").textContent = data.verification_required
    ? "抖音正在要求安全验证，请切换到已打开的 Chrome 窗口手动完成。"
    : data.logged_in
    ? `登录会话有效。下载目录：${data.download_dir}`
    : "点击后会打开独立浏览器。请扫码登录；遇到验证码也在这个窗口手动完成。";
}

function navigate(route, {updateHash = true} = {}) {
  const nextRoute = routes.has(route) ? route : "dashboard";
  currentRoute = nextRoute;
  document.body.classList.toggle("player-active", nextRoute === "player");
  document.querySelectorAll("[data-view]").forEach(element => {
    const functionalPreviewHidden = element.id === "previewPanel" && !previewState.token;
    element.hidden = element.dataset.view !== nextRoute || functionalPreviewHidden;
  });
  document.querySelectorAll(".nav-item").forEach(button => {
    const active = button.dataset.route === nextRoute;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  if (updateHash && location.hash !== `#${nextRoute}`) history.replaceState(null, "", `#${nextRoute}`);
  window.scrollTo({top: 0, behavior: "smooth"});
  refreshVisible().catch(error => toast(error.message));
  scheduleVisibleRefresh();
}

async function loadCreators() {
  saveListState();
  const result = await api(`/api/creators?page=${creatorState.page}&page_size=${creatorState.pageSize}`);
  const creators = result.items;
  creatorState.hasActiveJobs = creators.some(creator => Boolean(creator.active_scan_job));
  creatorState.totalPages = result.total_pages;
  $("#summary").textContent = result.total ? `${result.total} 位用户正在管理` : "尚未添加用户";
  $("#creatorPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 位`;
  $("#creatorJump").value = result.page;
  $("#creatorJump").max = Math.max(1, result.total_pages);
  $("#creatorPrev").disabled = result.page <= 1;
  $("#creatorNext").disabled = result.page >= result.total_pages;
  $("#creators").innerHTML = creators.length ? creators.map(c => {
    const tone = c.status === "needs_verification" ? "warning" : c.status === "error" ? "error" : c.status === "idle" ? "success" : "neutral";
    const schedule = c.schedule || {};
    const activeJob = c.active_scan_job;
    const progressBase = activeJob ? Number(activeJob.item_limit || activeJob.max_scrolls || 1) : 1;
    const progressValue = activeJob ? Number(activeJob.written_count || activeJob.discovered_count || activeJob.scroll_count || 0) : 0;
    const progress = Math.max(0, Math.min(100, Math.round(progressValue / progressBase * 100)));
    return `<article class="creator">
      <div class="creator-profile">
        ${c.avatar_url ? `<img class="creator-avatar" loading="lazy" src="${escapeHtml(c.avatar_url)}" alt="" onerror="this.hidden=true">` : `<div class="creator-avatar-fallback">${escapeHtml((c.nickname || "?").slice(0, 1))}</div>`}
        <div><button class="creator-name" onclick="openCreatorLibrary(${c.id})">${escapeHtml(c.nickname || "等待首次扫描")}</button>
        <div class="creator-meta">
          <span class="badge ${tone}">${escapeHtml(statusLabels[c.status] || c.status)}</span>
          <span>${escapeHtml(policyLabels[c.download_policy] || c.download_policy)}</span>
          <span>作品 ${c.total_found}</span><span>已下载 ${c.downloaded_count}</span><span>失败 ${c.failed_count || 0}</span>
          <span>上次：${escapeHtml(timeText(c.last_scan_at))}</span><span>下次：${escapeHtml(timeText(schedule.next_run_at || c.next_scan_at))}</span>
        </div>
        ${activeJob ? `<div class="creator-progress" title="任务进度 ${progress}%"><span style="width:${progress}%"></span></div><div class="row-sub">扫描进度：已发现 ${activeJob.discovered_count || 0}，已写入 ${activeJob.written_count || 0}，滚动 ${activeJob.scroll_count || 0} 次</div>` : ""}
        ${c.last_error ? `<div class="creator-error">${escapeHtml(c.last_error)}</div>` : ""}
        <div class="row-sub">${escapeHtml(c.profile_url)}</div>
        </div>
      </div>
      <div class="creator-actions">
        ${c.status === "needs_verification" ? `<button class="primary" onclick="verifyAndRetry(${c.id})">打开验证</button>` : ""}
        ${activeJob && ["queued", "running"].includes(activeJob.status) ? `<button onclick="controlScanJob(${activeJob.id}, 'pause')">暂停扫描</button>` : ""}
        ${activeJob && activeJob.status === "paused" ? `<button class="primary" onclick="controlScanJob(${activeJob.id}, 'resume')">继续扫描</button>` : ""}
        ${activeJob && ["queued", "running", "paused"].includes(activeJob.status) ? `<button class="danger" onclick="controlScanJob(${activeJob.id}, 'cancel')">取消扫描</button>` : ""}
        <button onclick="scanCreator(${c.id})">立即扫描</button>
        <button onclick="openContinueScan(${c.id}, ${Number(c.per_scan_limit || 100)})">继续历史</button>
        <button onclick="toggleCreator(${c.id}, ${c.enabled ? "false" : "true"})">${c.enabled ? "暂停" : "启用"}</button>
        <button onclick="editCreator(${c.id})">编辑</button>
        <button class="danger" onclick="removeCreator(${c.id})">删除</button>
      </div>
    </article>`;
  }).join("") : '<div class="empty">添加一个用户主页后，会立即进行首次扫描。</div>';
}

async function editCreator(id) {
  try {
    const creator = await api(`/api/creators/${id}`);
    const schedule = creator.schedule || {schedule_type: "minutes", interval_value: creator.interval_minutes || 60, timezone: "Asia/Shanghai", enabled: creator.enabled};
    creatorState.editingId = id;
    $("#editCreatorTitle").textContent = `编辑：${creator.nickname || "监控用户"}`;
    $("#editCreatorPolicy").value = creator.download_policy;
    updatePolicyDescription($("#editCreatorPolicy"), $("#editPolicyDescription"));
    $("#editScheduleType").value = schedule.schedule_type;
    $("#editIntervalValue").value = schedule.interval_value;
    $("#editDailyTime").value = (schedule.daily_time || "03:00").slice(0, 5);
    $("#editDailyTimeLabel").hidden = schedule.schedule_type !== "daily";
    $("#editScanLimit").value = creator.per_scan_limit || 100;
    $("#editCreatorEnabled").checked = Boolean(creator.enabled);
    $("#editCreatorDialog").showModal();
  } catch (error) { toast(error.message); }
}

function openCreatorLibrary(id) {
  videoLibraryState.creatorId = id;
  $("#videoCreatorFilter").value = String(id);
  videoLibraryState.page = 1;
  navigate("videos");
}

async function loadVideoCreatorOptions() {
  const result = await api("/api/creators?page=1&page_size=100");
  const current = videoLibraryState.creatorId ? String(videoLibraryState.creatorId) : "";
  $("#videoCreatorFilter").innerHTML = '<option value="">全部监控用户</option>' + result.items.map(c => `<option value="${c.id}">${escapeHtml(c.nickname || `用户 ${c.id}`)}</option>`).join("");
  $("#videoCreatorFilter").value = current;
}

function videoLibraryQuery() {
  const params = new URLSearchParams({page: videoLibraryState.page, page_size: videoLibraryState.pageSize, sort: $("#videoSort").value});
  if (videoLibraryState.creatorId) params.set("creator_id", videoLibraryState.creatorId);
  if ($("#videoKeyword").value.trim()) params.set("keyword", $("#videoKeyword").value.trim());
  if ($("#videoStatusFilter").value) params.set("status", $("#videoStatusFilter").value);
  if ($("#videoTypeFilter").value) params.set("content_type", $("#videoTypeFilter").value);
  return params;
}

function renderVideoLibrary(result) {
  videoLibraryState.items = result.items;
  videoLibraryState.totalPages = result.total_pages;
  const creatorCount = new Set(result.items.map(item => item.creator_id)).size;
  $("#videoLibrarySummary").textContent = `共 ${result.total} 条作品；当前页按 ${creatorCount} 个作者账户分组。`;
  $("#videoPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 条`;
  $("#videoJump").value = result.page;
  $("#videoJump").max = Math.max(1, result.total_pages);
  $("#videoPrev").disabled = result.page <= 1;
  $("#videoNext").disabled = result.page >= result.total_pages;
  const cardHtml = v => {
    const selected = videoLibraryState.selected.has(Number(v.id));
    const localText = v.local_file_status === "missing" ? "本地文件不存在" : v.local_file_status === "available" ? "本地文件可用" : "尚未下载";
    const playable = v.status === "downloaded" && v.local_file_exists;
    return `<article class="media-card ${selected ? "selected" : ""}">
      <div class="media-cover"><span>暂无封面</span>${v.cover_url || v.cover_path ? `<img loading="lazy" src="/api/media/videos/${v.id}/cover" data-fallback="${escapeHtml(v.cover_url || "")}" alt="" onerror="fallbackCover(this)">` : ""}${playable ? `<button class="media-play-hit" type="button" aria-label="播放 ${escapeHtml(v.description || v.aweme_id)}" onclick="openPlayer(${v.id})"></button>` : ""}<input class="media-select" type="checkbox" data-video-id="${v.id}" ${selected ? "checked" : ""}><span class="media-type">${v.content_type === "images" ? `图文 ${v.asset_count || 1} 张` : "视频"}</span></div>
      <div class="media-body">${playable ? `<button class="media-title media-title-button" type="button" title="${escapeHtml(v.description || v.aweme_id)}" onclick="openPlayer(${v.id})">${escapeHtml(v.description || v.aweme_id)}</button>` : `<div class="media-title" title="${escapeHtml(v.description || v.aweme_id)}">${escapeHtml(v.description || v.aweme_id)}</div>`}
      <div class="media-details"><span>${escapeHtml(v.creator_nickname || "未知用户")} · ${escapeHtml(timeText(v.create_time ? Number(v.create_time) * 1000 : null))}</span><span class="${v.local_file_status === "missing" ? "file-missing" : ""}">${escapeHtml(statusLabels[v.status] || v.status)} · ${localText}${v.file_size ? ` · ${bytesText(v.file_size)}` : ""}</span>${v.last_error ? `<span class="creator-error">${escapeHtml(v.last_error)}</span>` : ""}</div>
      <div class="media-actions">${v.status === "failed" ? `<button onclick="retryVideo(${v.id})">失败重试</button>` : v.status !== "downloaded" ? `<button onclick="downloadVideo(${v.id})">下载</button>` : ""}<button class="danger" onclick="removeVideo(${v.id})">删除记录</button></div></div>
    </article>`;
  };
  const groups = new Map();
  result.items.forEach(item => {
    const key = Number(item.creator_id || 0);
    if (!groups.has(key)) groups.set(key, {name: item.creator_nickname || "未知用户", items: []});
    groups.get(key).items.push(item);
  });
  $("#videoLibrary").innerHTML = result.items.length ? [...groups.entries()].map(([creatorId, group]) => `
    <section class="creator-media-group">
      <div class="creator-folder-head">
        <div class="creator-folder-icon" aria-hidden="true">📁</div>
        <div><button type="button" class="creator-folder-name" onclick="openCreatorLibrary(${creatorId})">${escapeHtml(group.name)}</button><p>当前页 ${group.items.length} 条作品</p></div>
      </div>
      <div class="media-grid">${group.items.map(cardHtml).join("")}</div>
    </section>`).join("") : '<div class="empty">当前筛选条件下没有作品</div>';
  document.querySelectorAll(".media-select").forEach(input => input.addEventListener("change", () => {
    const id = Number(input.dataset.videoId);
    if (input.checked) videoLibraryState.selected.add(id); else videoLibraryState.selected.delete(id);
    renderVideoSelectionCount();
    input.closest(".media-card").classList.toggle("selected", input.checked);
  }));
  renderVideoSelectionCount();
}

function renderVideoSelectionCount() {
  $("#videoSelectedCount").textContent = `已选择 ${videoLibraryState.selected.size} 条`;
  $("#videoBulkDownload").disabled = videoLibraryState.selected.size === 0;
  $("#videoBulkRetry").disabled = videoLibraryState.selected.size === 0;
}

async function loadVideoLibrary() {
  saveListState();
  renderVideoLibrary(await api(`/api/videos?${videoLibraryQuery()}`));
}

async function downloadVideo(id) {
  try { await api(`/api/videos/${id}/download`, {method: "POST"}); toast("已加入高优先级下载队列"); await loadVideoLibrary(); }
  catch (error) { toast(error.message); }
}

async function retryVideo(id) {
  try { await api(`/api/videos/${id}/retry`, {method: "POST"}); toast("失败作品已重新排队"); await loadVideoLibrary(); }
  catch (error) { toast(error.message); }
}

async function removeVideo(id) {
  const video = videoLibraryState.items.find(item => Number(item.id) === Number(id));
  pendingDeleteVideoId = id;
  $("#deleteVideoFiles").checked = false;
  $("#deleteVideoSummary").textContent = video ? `确认删除“${video.description || video.aweme_id}”的数据库记录？` : "确认删除这条作品记录？";
  $("#deleteVideoDialog").showModal();
}

function playbackContextQuery() {
  const params = new URLSearchParams({status: "downloaded", sort: $("#videoSort").value});
  if (videoLibraryState.creatorId) params.set("creator_id", videoLibraryState.creatorId);
  if ($("#videoKeyword").value.trim()) params.set("keyword", $("#videoKeyword").value.trim());
  if ($("#videoTypeFilter").value) params.set("content_type", $("#videoTypeFilter").value);
  return params;
}

function currentPlayerVisual() {
  return $("#playerVideo") || $("#playerMedia .player-image");
}

function applyPlayerTransform() {
  const visual = currentPlayerVisual();
  if (visual) visual.style.transform = `translate(${playerState.panX}px, ${playerState.panY}px) scale(${playerState.zoom})`;
  $("#playerZoomText").textContent = `${Math.round(playerState.zoom * 100)}%`;
  $("#playerMedia").classList.toggle("is-zoomed", playerState.zoom > 1);
}

function setPlayerZoom(value, {originX = null, originY = null} = {}) {
  const previous = playerState.zoom;
  playerState.zoom = Math.max(1, Math.min(10, Math.round(value * 4) / 4));
  if (playerState.zoom === 1) {
    playerState.panX = 0;
    playerState.panY = 0;
  } else if (originX !== null && originY !== null) {
    const media = $("#playerMedia").getBoundingClientRect();
    const ratio = (playerState.zoom - previous) / Math.max(previous, 0.01);
    playerState.panX += (media.width / 2 - originX) * ratio;
    playerState.panY += (media.height / 2 - originY) * ratio;
  }
  applyPlayerTransform();
}

function clearPlayerImageTimer() {
  clearTimeout(playerState.imageTimer);
  playerState.imageTimer = null;
}

function schedulePlayerImageAdvance() {
  clearPlayerImageTimer();
  if (!$("#playerAutoNext").checked || !playerState.assets.length || currentRoute !== "player") return;
  playerState.imageTimer = setTimeout(() => {
    if (playerState.imageIndex < playerState.assets.length - 1) changePlayerImage(1);
    else switchPlayer("next");
  }, 5000);
}

function finishRightHold({seekIfTap = false} = {}) {
  clearTimeout(playerState.rightHoldTimer);
  playerState.rightHoldTimer = null;
  const video = $("#playerVideo");
  if (playerState.rightHoldActive && video) {
    video.playbackRate = playerState.rightHoldOriginalRate;
    if (playerState.rightHoldWasPaused) video.pause();
  } else if (seekIfTap) {
    seekPlayer(5);
  }
  playerState.rightHoldActive = false;
  $("#playerAcceleration").hidden = true;
}

function startRightHold() {
  if (playerState.rightHoldTimer || playerState.rightHoldActive || !$("#playerVideo")) return;
  playerState.rightHoldTimer = setTimeout(() => {
    const video = $("#playerVideo");
    if (!video) return;
    playerState.rightHoldTimer = null;
    playerState.rightHoldActive = true;
    playerState.rightHoldOriginalRate = video.playbackRate;
    playerState.rightHoldWasPaused = video.paused;
    video.playbackRate = Math.max(2, video.playbackRate);
    if (video.paused) video.play().catch(() => {});
    $("#playerAcceleration").hidden = false;
  }, 300);
}

function releasePlayerMedia() {
  clearPlayerImageTimer();
  finishRightHold();
  const video = $("#playerVideo");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  playerState.zoom = 1;
  playerState.panX = 0;
  playerState.panY = 0;
  playerState.dragging = false;
  $("#playerZoomText").textContent = "100%";
  $("#playerMedia").classList.remove("is-zoomed", "is-dragging");
  $("#playerMedia").innerHTML = '<div class="player-empty">正在加载作品…</div>';
}

function renderPlayerImage() {
  const asset = playerState.assets[playerState.imageIndex];
  if (!asset) {
    $("#playerMedia").innerHTML = '<div class="player-empty">本地图文资源不存在</div>';
    return;
  }
  $("#playerMedia").innerHTML = `<button class="image-arrow left" type="button" aria-label="上一张" onclick="changePlayerImage(-1)" ${playerState.imageIndex <= 0 ? "disabled" : ""}>‹</button><img class="player-image" loading="lazy" src="${escapeHtml(asset.url)}" alt="图文第 ${playerState.imageIndex + 1} 张"><button class="image-arrow right" type="button" aria-label="下一张" onclick="changePlayerImage(1)" ${playerState.imageIndex >= playerState.assets.length - 1 ? "disabled" : ""}>›</button><span class="image-counter">${playerState.imageIndex + 1} / ${playerState.assets.length}</span>`;
  applyPlayerTransform();
  schedulePlayerImageAdvance();
}

function changePlayerImage(offset) {
  const nextIndex = playerState.imageIndex + offset;
  if (nextIndex < 0 || nextIndex >= playerState.assets.length) return;
  playerState.imageIndex = nextIndex;
  renderPlayerImage();
}

async function loadPlayerContext(id, {autoplay = false} = {}) {
  if (playerState.switching) return;
  playerState.switching = true;
  releasePlayerMedia();
  try {
    const context = await api(`/api/videos/${id}/playback-context?${playbackContextQuery()}`);
    const current = context.current;
    playerState.currentId = Number(current.id);
    playerState.context = context;
    playerState.assets = [];
    playerState.imageIndex = 0;
    $("#playerPosition").textContent = `${context.position} / ${context.total}`;
    $("#playerTitle").textContent = current.description || current.aweme_id;
    $("#playerCreator").textContent = `${current.creator_nickname || "未知用户"} · ${timeText(current.create_time ? Number(current.create_time) * 1000 : null)}`;
    $("#playerPrevious").disabled = !context.previous;
    $("#playerNext").disabled = !context.next;
    $("#playerTypeLabel").textContent = current.content_type === "images" ? `图文 · ${current.asset_count || 1} 张` : "本地视频";
    if (!current.local_file_exists) {
      $("#playerMedia").innerHTML = '<div class="player-empty">本地文件不存在，请返回作品库重新下载。</div>';
      return;
    }
    if (current.content_type === "images") {
      const assets = await api(`/api/media/videos/${current.id}/assets`);
      playerState.assets = assets.items;
      renderPlayerImage();
    } else {
      $("#playerMedia").innerHTML = `<video id="playerVideo" controls playsinline preload="metadata" src="/api/media/videos/${current.id}"></video>`;
      const video = $("#playerVideo");
      if (!$("#playerPersistSpeed").checked) $("#playerSpeed").value = "1";
      video.playbackRate = Number($("#playerSpeed").value);
      video.loop = !$("#playerAutoNext").checked;
      video.addEventListener("ended", () => {
        if ($("#playerAutoNext").checked) switchPlayer("next");
      });
      video.addEventListener("error", () => toast("本地视频无法播放，文件可能损坏或编码不受浏览器支持"));
      video.addEventListener("click", event => {
        if (event.offsetY < video.clientHeight - 48) togglePlayerVideo();
      });
      applyPlayerTransform();
      if (autoplay) video.play().catch(() => {});
    }
    $("#playerView").focus({preventScroll: true});
  } catch (error) {
    $("#playerMedia").innerHTML = `<div class="player-empty">${escapeHtml(error.message)}</div>`;
    toast(error.message);
  } finally {
    playerState.switching = false;
  }
}

async function openPlayer(id) {
  playerState.returnScrollY = window.scrollY;
  navigate("player");
  await loadPlayerContext(id);
}

async function switchPlayer(direction) {
  if (playerState.switching || !playerState.context) return;
  const target = playerState.context[direction];
  if (!target) {
    toast(direction === "previous" ? "已经是第一条作品" : "已经是最后一条作品");
    return;
  }
  await loadPlayerContext(Number(target.id), {autoplay: true});
}

function togglePlayerVideo() {
  const video = $("#playerVideo");
  if (!video) return;
  if (video.paused) video.play().catch(() => {}); else video.pause();
}

function seekPlayer(seconds) {
  const video = $("#playerVideo");
  if (!video || !Number.isFinite(video.duration)) return;
  video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
}

function closePlayer() {
  releasePlayerMedia();
  playerState.currentId = null;
  playerState.context = null;
  playerState.assets = [];
  navigate("videos");
  requestAnimationFrame(() => window.scrollTo({top: playerState.returnScrollY}));
}

async function loadPendingCreatorOptions() {
  const result = await api("/api/creators?page=1&page_size=100");
  $("#pendingCreatorFilter").innerHTML = '<option value="">全部监控用户</option>' + result.items.map(c => `<option value="${c.id}">${escapeHtml(c.nickname || `用户 ${c.id}`)}</option>`).join("");
  $("#pendingCreatorFilter").value = pendingState.creatorId ? String(pendingState.creatorId) : "";
}

function pendingQuery() {
  const params = new URLSearchParams({page: pendingState.page, page_size: pendingState.pageSize, sort: $("#pendingSort").value});
  if (pendingState.creatorId) params.set("creator_id", pendingState.creatorId);
  if ($("#pendingKeyword").value.trim()) params.set("keyword", $("#pendingKeyword").value.trim());
  if ($("#pendingType").value) params.set("content_type", $("#pendingType").value);
  return params;
}

function renderPendingVideos(result) {
  pendingState.items = result.items;
  pendingState.totalPages = result.total_pages;
  $("#pendingSummary").textContent = `共 ${result.total} 条作品等待确认。处理后不会影响已有本地文件。`;
  $("#pendingPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 条`;
  $("#pendingJump").value = result.page;
  $("#pendingJump").max = Math.max(1, result.total_pages);
  $("#pendingPrev").disabled = result.page <= 1;
  $("#pendingNext").disabled = result.page >= result.total_pages;
  $("#pendingVideos").innerHTML = result.items.length ? result.items.map(v => {
    const selected = pendingState.selected.has(Number(v.id));
    return `<article class="media-card ${selected ? "selected" : ""}"><div class="media-cover"><span>暂无封面</span>${v.cover_url || v.cover_path ? `<img loading="lazy" src="/api/media/videos/${v.id}/cover" data-fallback="${escapeHtml(v.cover_url || "")}" alt="" onerror="fallbackCover(this)">` : ""}<input class="pending-select media-select" type="checkbox" data-video-id="${v.id}" ${selected ? "checked" : ""}><span class="media-type">${v.content_type === "images" ? `图文 ${v.asset_count || 1} 张` : "视频"}</span></div><div class="media-body"><div class="media-title">${escapeHtml(v.description || v.aweme_id)}</div><div class="media-details"><span>${escapeHtml(v.creator_nickname || "未知用户")}</span><span>${escapeHtml(timeText(v.create_time ? Number(v.create_time) * 1000 : null))}</span></div></div></article>`;
  }).join("") : '<div class="empty">没有等待确认的作品</div>';
  document.querySelectorAll(".pending-select").forEach(input => input.addEventListener("change", () => {
    const id = Number(input.dataset.videoId);
    if (input.checked) pendingState.selected.add(id); else pendingState.selected.delete(id);
    input.closest(".media-card").classList.toggle("selected", input.checked);
    renderPendingSelectionCount();
  }));
  renderPendingSelectionCount();
}

function renderPendingSelectionCount() {
  $("#pendingSelectedCount").textContent = `已选择 ${pendingState.selected.size} 条`;
  $("#pendingDownload").disabled = pendingState.selected.size === 0;
  $("#pendingKeep").disabled = pendingState.selected.size === 0;
}

async function loadPendingVideos() {
  saveListState();
  renderPendingVideos(await api(`/api/pending-confirmations?${pendingQuery()}`));
}

async function resolvePending(action) {
  const ids = [...pendingState.selected];
  if (!ids.length) return;
  try {
    let resolved = 0;
    let queued = 0;
    for (let index = 0; index < ids.length; index += 100) {
      const result = await api("/api/pending-confirmations/resolve", {method: "POST", body: JSON.stringify({video_ids: ids.slice(index, index + 100), action})});
      resolved += result.resolved_count;
      queued += result.queued_count;
    }
    pendingState.selected.clear();
    toast(action === "download" ? `已确认 ${resolved} 条，并创建 ${queued} 个下载任务` : `已确认 ${resolved} 条，仅保留作品信息`);
    await loadPendingVideos();
  } catch (error) { toast(error.message); }
}

async function loadDownloadCreatorOptions() {
  const result = await api("/api/creators?page=1&page_size=100");
  $("#downloadCreatorFilter").innerHTML = '<option value="">全部监控用户</option>' + result.items.map(c => `<option value="${c.id}">${escapeHtml(c.nickname || `用户 ${c.id}`)}</option>`).join("");
  $("#downloadCreatorFilter").value = downloadState.creatorId ? String(downloadState.creatorId) : "";
}

function downloadJobsQuery() {
  const params = new URLSearchParams({page: downloadState.page, page_size: downloadState.pageSize});
  if (downloadState.creatorId) params.set("creator_id", downloadState.creatorId);
  if (downloadState.status) params.set("status", downloadState.status);
  return params;
}

function renderDownloadJobs(result) {
  downloadState.totalPages = result.total_pages;
  $("#downloadJobsSummary").textContent = `共 ${result.total} 个任务，NAS 默认并发为 1。`;
  $("#downloadPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 个任务`;
  $("#downloadJump").value = result.page;
  $("#downloadJump").max = Math.max(1, result.total_pages);
  $("#downloadPrev").disabled = result.page <= 1;
  $("#downloadNext").disabled = result.page >= result.total_pages;
  $("#downloadJobs").innerHTML = result.items.length ? result.items.map(job => {
    const downloaded = Number(job.bytes_downloaded || 0);
    const total = Number(job.total_bytes || 0);
    const percent = total > 0 ? Math.min(100, Math.round(downloaded / total * 100)) : job.status === "completed" ? 100 : 0;
    const tone = job.status === "completed" ? "success" : job.status === "failed" ? "error" : job.status === "paused" ? "warning" : "neutral";
    const active = ["queued", "running", "pausing", "cancelling"].includes(job.status);
    return `<article class="job-row"><div><div class="job-head"><span class="badge ${tone}">${escapeHtml(downloadStatusLabels[job.status] || job.status)}</span><strong title="${escapeHtml(job.description || job.aweme_id)}">${escapeHtml(job.description || job.aweme_id)}</strong></div><div class="job-meta"><span>${escapeHtml(job.creator_nickname || "未知用户")}</span><span>优先级 ${job.priority}</span><span>尝试 ${job.attempts}/${job.max_attempts}</span><span>${bytesText(downloaded)}${total ? ` / ${bytesText(total)}` : ""}</span><span>${percent}%</span><span>${bytesText(job.speed_bytes_per_second || 0)}/s</span><span>创建于 ${escapeHtml(timeText(job.created_at))}</span></div><div class="job-progress"><span style="width:${percent}%"></span></div>${job.failure_reason ? `<div class="job-error">${escapeHtml(job.failure_reason)}</div>` : ""}</div><div class="job-actions">${["queued", "running"].includes(job.status) ? `<button onclick="controlDownloadJob(${job.id}, 'pause')">暂停</button>` : ""}${job.status === "paused" ? `<button class="primary" onclick="controlDownloadJob(${job.id}, 'resume')">继续</button>` : ""}${active || job.status === "paused" ? `<button class="danger" onclick="controlDownloadJob(${job.id}, 'cancel')">取消</button>` : ""}${["failed", "cancelled"].includes(job.status) ? `<button onclick="controlDownloadJob(${job.id}, 'retry')">重试</button>` : ""}</div></article>`;
  }).join("") : '<div class="empty">当前筛选条件下没有下载任务</div>';
}

async function loadDownloadJobs() {
  saveListState();
  renderDownloadJobs(await api(`/api/download-jobs?${downloadJobsQuery()}`));
}

async function controlDownloadJob(id, action) {
  try { await api(`/api/download-jobs/${id}/${action}`, {method: "POST"}); toast({pause: "已请求暂停", resume: "任务已继续", cancel: "已请求取消", retry: "任务已重新排队"}[action]); await loadDownloadJobs(); }
  catch (error) { toast(error.message); }
}

function logsPageQuery() {
  const params = new URLSearchParams({page: logState.page, page_size: logState.pageSize});
  if (logState.level) params.set("level", logState.level);
  return params;
}

function renderLogsPage(result) {
  logState.totalPages = result.total_pages;
  $("#logsPageSummary").textContent = `共 ${result.total} 条事件，时间以 Asia/Shanghai 显示。`;
  $("#logsPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 条`;
  $("#logsJump").value = result.page;
  $("#logsJump").max = Math.max(1, result.total_pages);
  $("#logsPrev").disabled = result.page <= 1;
  $("#logsNext").disabled = result.page >= result.total_pages;
  $("#logsPage").innerHTML = result.items.length ? result.items.map(log => `<article class="event-row"><span class="event-level ${escapeHtml(log.level)}">${escapeHtml(log.level)}</span><div class="event-message">${escapeHtml(log.message)}</div><time class="event-time">${escapeHtml(timeText(log.created_at))}</time></article>`).join("") : '<div class="empty">当前级别没有日志</div>';
}

async function loadLogsPage() {
  saveListState();
  renderLogsPage(await api(`/api/logs?${logsPageQuery()}`));
}

async function loadVideos() {
  const result = await api("/api/videos?page=1&page_size=30");
  const videos = result.items;
  $("#videos").innerHTML = videos.length ? videos.map(v => `<div class="compact-row">
    <div class="row-main"><div class="row-title">${escapeHtml(v.description || v.aweme_id)}</div><span class="badge ${v.remote_status === "removed_or_private" ? "warning" : v.status === "downloaded" ? "success" : v.status === "failed" ? "error" : "neutral"}">${escapeHtml(v.remote_status === "removed_or_private" && v.status === "downloaded" ? "已删除/私密，但本地已下载" : v.remote_status !== "active" ? remoteLabels[v.remote_status] : statusLabels[v.status] || v.status)}</span></div>
    <div class="row-sub">${escapeHtml(v.creator_nickname || "未知用户")} · ${v.content_type === "images" ? `图文/日常 ${v.asset_count || 0} 张` : v.is_daily ? "日常视频" : "视频"} · ${escapeHtml(v.aweme_id)}${v.status === "downloading" ? ` · ${bytesText(v.bytes_downloaded)}${v.total_bytes ? ` / ${bytesText(v.total_bytes)}` : ""}` : v.file_size ? ` · ${bytesText(v.file_size)}` : ""}${v.last_error ? ` · ${escapeHtml(v.last_error)}` : ""}</div>
  </div>`).join("") : '<div class="empty">尚无视频记录</div>';
}

async function loadLogs() {
  const result = await api("/api/logs?page=1&page_size=50");
  const logs = result.items;
  $("#logs").innerHTML = logs.length ? logs.map(log => `<div class="compact-row ${escapeHtml(log.level)}">
    <div class="row-title">${escapeHtml(log.message)}</div><div class="row-sub">${escapeHtml(timeText(log.created_at))}</div>
  </div>`).join("") : '<div class="empty">暂无日志</div>';
}

async function loadSettings() {
  const settings = await api("/api/settings");
  $("#downloadDir").value = settings.download_dir;
  $("#downloadDir").disabled = Boolean(settings.download_dir_locked);
  $("#settingsSave").disabled = Boolean(settings.download_dir_locked);
  $("#settingsSave").textContent = settings.download_dir_locked ? "由容器挂载锁定" : "保存下载目录";
}

async function loadDingTalk() {
  const data = await api("/api/notifications/dingtalk");
  $("#dingEnabled").checked = data.enabled;
  $("#dingStatus").textContent = data.configured
    ? `已配置：${data.webhook_masked}${data.enabled ? "（通知已启用）" : "（通知已停用）"}`
    : "尚未配置 Webhook 和加签密钥";
}

function previewQuery() {
  const params = new URLSearchParams({page: previewState.page, page_size: previewState.pageSize, sort_order: $("#previewSort").value});
  if ($("#previewKeyword").value.trim()) params.set("keyword", $("#previewKeyword").value.trim());
  if ($("#previewType").value) params.set("content_type", $("#previewType").value);
  return params;
}

async function loadPreviewSession() {
  if (!previewState.token) return;
  saveListState();
  const data = await api(`/api/previews/${previewState.token}`);
  $("#previewTitle").textContent = data.nickname || "正在识别用户信息";
  $("#previewStatus").textContent = `${statusLabels[data.status] || data.status} · 已发现 ${data.discovered_count || 0} 条${data.last_error ? ` · ${data.last_error}` : ""}`;
  if (data.avatar_url) { $("#previewAvatar").src = data.avatar_url; $("#previewAvatar").hidden = false; }
  const scanning = ["queued", "scanning"].includes(data.status);
  $("#previewCancel").disabled = !scanning;
  $("#previewContinue").disabled = scanning || ["cancelled", "duplicate", "confirmed"].includes(data.status);
  if (scanning) {
    clearTimeout(previewState.timer);
    previewState.timer = setTimeout(async () => { try { await loadPreviewSession(); await loadPreviewVideos(); } catch (error) { toast(error.message); } }, 1500);
  } else clearTimeout(previewState.timer);
}

function renderPreviewVideos(result) {
  previewState.items = result.items;
  previewState.totalPages = result.total_pages;
  $("#selectedCount").textContent = `已选择 ${result.selection.selected_count} 条${result.selection.auto_select_new ? "（自动选择已开启）" : ""}`;
  $("#autoSelectNew").checked = result.selection.auto_select_new;
  $("#previewPageText").textContent = `第 ${result.page} / ${result.total_pages || 1} 页，共 ${result.total} 条`;
  $("#previewJump").max = Math.max(1, result.total_pages);
  $("#previewJump").value = result.page;
  $("#previewPrev").disabled = result.page <= 1;
  $("#previewNext").disabled = result.page >= result.total_pages;
  $("#previewVideos").innerHTML = result.items.length ? result.items.map(v => `<article class="preview-card ${v.selected ? "selected" : ""}">
    <label><div class="preview-cover"><span>封面不可用</span>${v.cover_url ? `<img loading="lazy" src="/api/previews/${encodeURIComponent(previewState.token)}/videos/${v.id}/cover" data-fallback="${escapeHtml(v.cover_url)}" alt="" onerror="fallbackCover(this)">` : ""}</div>
    <input class="preview-check" type="checkbox" data-aweme-id="${escapeHtml(v.aweme_id)}" ${v.selected ? "checked" : ""}></label>
    <div class="preview-info"><div class="row-title" title="${escapeHtml(v.description || v.aweme_id)}">${escapeHtml(v.description || v.aweme_id)}</div><div class="row-sub">${v.content_type === "images" ? `图文 · ${v.asset_count || 1} 张` : "视频"} · ${escapeHtml(timeText(v.create_time ? Number(v.create_time) * 1000 : null))}</div></div>
  </article>`).join("") : '<div class="empty">当前筛选条件下没有作品</div>';
  document.querySelectorAll(".preview-check").forEach(input => input.addEventListener("change", () => updatePreviewSelection(input.checked ? "select" : "deselect", [input.dataset.awemeId])));
  updatePreviewConfirmationSummary();
}

async function loadPreviewVideos() {
  if (!previewState.token) return;
  saveListState();
  renderPreviewVideos(await api(`/api/previews/${previewState.token}/videos?${previewQuery()}`));
}

async function updatePreviewSelection(action, awemeIds = [], extra = {}) {
  try {
    await api(`/api/previews/${previewState.token}/selection`, {method: "PATCH", body: JSON.stringify({action, aweme_ids: awemeIds, ...extra})});
    await loadPreviewVideos();
  } catch (error) { toast(error.message); }
}

function previewConfirmationPayload() {
  return {
    download_policy: $("#previewPolicy").value,
    immediate_download_selected: $("#previewImmediate").checked,
    schedule_type: $("#previewScheduleType").value,
    interval_value: Number($("#previewIntervalValue").value),
    daily_time: $("#previewScheduleType").value === "daily" ? $("#previewDailyTime").value : null,
    timezone: "Asia/Shanghai"
  };
}

async function updatePreviewConfirmationSummary() {
  if (!previewState.token) return;
  try {
    const data = await api(`/api/previews/${previewState.token}/confirmation-summary`, {method: "POST", body: JSON.stringify(previewConfirmationPayload())});
    $("#previewConfirmSummary").textContent = `已选择 ${data.selected_count} 条，预计创建 ${data.estimated_download_jobs} 个下载任务`;
  } catch (_) {}
}

async function refreshVisible() {
  if (document.hidden || refreshInFlight) return;
  if (currentRoute === "player") return;
  refreshInFlight = true;
  try {
    const tasks = [loadStatus()];
    if (currentRoute === "dashboard") tasks.push(loadVideos(), loadLogs());
    else if (currentRoute === "creators") tasks.push(loadCreators());
    else if (currentRoute === "videos") tasks.push(loadVideoCreatorOptions(), loadVideoLibrary());
    else if (currentRoute === "pending") tasks.push(loadPendingCreatorOptions(), loadPendingVideos());
    else if (currentRoute === "downloads") tasks.push(loadDownloadCreatorOptions(), loadDownloadJobs());
    else if (currentRoute === "settings") tasks.push(loadSettings(), loadDingTalk());
    else if (currentRoute === "logs") tasks.push(loadLogsPage());
    else if (currentRoute === "add" && previewState.token) tasks.push(loadPreviewSession(), loadPreviewVideos());
    await Promise.all(tasks);
  } finally {
    refreshInFlight = false;
  }
}

function scheduleVisibleRefresh() {
  clearTimeout(refreshTimer);
  if (document.hidden || currentRoute === "player") return;
  const interval = currentRoute === "downloads" ? 2000 : currentRoute === "creators" && creatorState.hasActiveJobs ? 2000 : 10000;
  refreshTimer = setTimeout(async () => {
    try { await refreshVisible(); } catch (error) { toast(error.message); }
    scheduleVisibleRefresh();
  }, interval);
}

async function scanCreator(id) {
  try { const result = await api(`/api/creators/${id}/scan`, {method: "POST"}); toast(result.message); await loadCreators(); }
  catch (error) { toast(error.message); }
}

async function controlScanJob(id, action) {
  try {
    await api(`/api/scan-jobs/${id}/${action}`, {method: "POST"});
    toast({pause: "已请求暂停扫描", resume: "扫描任务已继续", cancel: "已请求取消扫描"}[action]);
    await loadCreators();
  } catch (error) { toast(error.message); }
}

function openContinueScan(creatorId, defaultLimit) {
  pendingContinueCreatorId = Number(creatorId);
  $("#continueScanLimit").value = String(Math.max(20, Math.min(1000, Number(defaultLimit) || 100)));
  $("#continueScanDialog").showModal();
}

async function verifyAndRetry(id) {
  try { await api("/api/login/open", {method: "POST"}); toast("请在浏览器完成验证，完成后再点立即扫描"); }
  catch (error) { toast(error.message); }
}

async function toggleCreator(id, enabled) {
  try { await api(`/api/creators/${id}`, {method: "PATCH", body: JSON.stringify({enabled})}); await loadCreators(); }
  catch (error) { toast(error.message); }
}

async function removeCreator(id) {
  try {
    const creator = await api(`/api/creators/${id}`);
    pendingDeleteCreatorId = id;
    $("#deleteCreatorFiles").checked = false;
    $("#deleteCreatorSummary").textContent = `${creator.nickname || "该用户"}共有 ${creator.total_found || 0} 个作品，其中 ${creator.downloaded_count || 0} 个已下载。`;
    $("#deleteCreatorDialog").showModal();
  } catch (error) { toast(error.message); }
}

window.scanCreator = scanCreator;
window.controlScanJob = controlScanJob;
window.openContinueScan = openContinueScan;
window.verifyAndRetry = verifyAndRetry;
window.toggleCreator = toggleCreator;
window.removeCreator = removeCreator;
window.editCreator = editCreator;
window.openCreatorLibrary = openCreatorLibrary;
window.downloadVideo = downloadVideo;
window.retryVideo = retryVideo;
window.removeVideo = removeVideo;
window.controlDownloadJob = controlDownloadJob;
window.openPlayer = openPlayer;
window.changePlayerImage = changePlayerImage;
window.fallbackCover = fallbackCover;

$("#loginButton").addEventListener("click", async () => {
  try { const result = await api("/api/login/open", {method: "POST"}); toast(result.message); setTimeout(loadStatus, 1500); }
  catch (error) { toast(error.message); }
});
$("#requestRetry").addEventListener("click", async () => {
  $("#requestFeedback").classList.remove("error");
  $("#requestFeedback").hidden = true;
  try { await refreshVisible(); } catch (error) { toast(error.message); }
});
$("#mainNav").addEventListener("click", event => {
  const button = event.target.closest("[data-route]");
  if (button) navigate(button.dataset.route);
});
document.addEventListener("click", event => {
  const jump = event.target.closest("[data-route-jump]");
  if (jump) navigate(jump.dataset.routeJump);
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1), {updateHash: false}));
$("#creatorPageSize").addEventListener("change", async event => { creatorState.pageSize = Number(event.target.value); creatorState.page = 1; await loadCreators(); });
$("#creatorPrev").addEventListener("click", async () => { if (creatorState.page > 1) { creatorState.page--; await loadCreators(); } });
$("#creatorNext").addEventListener("click", async () => { if (creatorState.page < creatorState.totalPages) { creatorState.page++; await loadCreators(); } });
$("#creatorJumpButton").addEventListener("click", async () => { creatorState.page = Math.max(1, Math.min(Number($("#creatorJump").value) || 1, creatorState.totalPages || 1)); await loadCreators(); });
$("#videoLibraryFilters").addEventListener("submit", async event => { event.preventDefault(); videoLibraryState.creatorId = Number($("#videoCreatorFilter").value) || null; videoLibraryState.pageSize = Number($("#videoPageSize").value); videoLibraryState.page = 1; await loadVideoLibrary(); });
$("#videoLibraryRefresh").addEventListener("click", loadVideoLibrary);
$("#videoPrev").addEventListener("click", async () => { if (videoLibraryState.page > 1) { videoLibraryState.page--; await loadVideoLibrary(); } });
$("#videoNext").addEventListener("click", async () => { if (videoLibraryState.page < videoLibraryState.totalPages) { videoLibraryState.page++; await loadVideoLibrary(); } });
$("#videoJumpButton").addEventListener("click", async () => { videoLibraryState.page = Math.max(1, Math.min(Number($("#videoJump").value) || 1, videoLibraryState.totalPages || 1)); await loadVideoLibrary(); });
$("#videoSelectPage").addEventListener("click", () => { videoLibraryState.items.forEach(item => videoLibraryState.selected.add(Number(item.id))); document.querySelectorAll(".media-select").forEach(input => { input.checked = true; input.closest(".media-card").classList.add("selected"); }); renderVideoSelectionCount(); });
$("#videoClearSelection").addEventListener("click", () => { videoLibraryState.selected.clear(); document.querySelectorAll(".media-select").forEach(input => { input.checked = false; input.closest(".media-card").classList.remove("selected"); }); renderVideoSelectionCount(); });
$("#videoBulkDownload").addEventListener("click", async () => {
  const ids = [...videoLibraryState.selected];
  try { for (let index = 0; index < ids.length; index += 100) await api("/api/videos/bulk-download", {method: "POST", body: JSON.stringify({video_ids: ids.slice(index, index + 100)})}); toast(`已将 ${ids.length} 条作品加入下载队列`); videoLibraryState.selected.clear(); await loadVideoLibrary(); }
  catch (error) { toast(error.message); }
});
$("#videoBulkRetry").addEventListener("click", async () => {
  const ids = [...videoLibraryState.selected];
  try { let queued = 0; for (let index = 0; index < ids.length; index += 100) { const result = await api("/api/videos/bulk-retry", {method: "POST", body: JSON.stringify({video_ids: ids.slice(index, index + 100)})}); queued += result.queued_count; } toast(`已重新排队 ${queued} 条失败作品`); videoLibraryState.selected.clear(); await loadVideoLibrary(); }
  catch (error) { toast(error.message); }
});
$("#deleteVideoConfirm").addEventListener("click", async () => {
  if (!pendingDeleteVideoId) return;
  const button = $("#deleteVideoConfirm");
  button.disabled = true;
  try { await api(`/api/videos/${pendingDeleteVideoId}`, {method: "DELETE", body: JSON.stringify({delete_local_files: $("#deleteVideoFiles").checked})}); videoLibraryState.selected.delete(Number(pendingDeleteVideoId)); pendingDeleteVideoId = null; $("#deleteVideoDialog").close(); toast("作品记录已删除"); await loadVideoLibrary(); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#continueScanConfirm").addEventListener("click", async () => {
  if (!pendingContinueCreatorId) return;
  const button = $("#continueScanConfirm");
  const limit = Math.max(20, Math.min(1000, Number($("#continueScanLimit").value) || 100));
  $("#continueScanLimit").value = String(limit);
  button.disabled = true;
  try {
    await api(`/api/creators/${pendingContinueCreatorId}/scan/continue`, {method: "POST", body: JSON.stringify({limit})});
    $("#continueScanDialog").close();
    pendingContinueCreatorId = null;
    toast(`已开始继续获取更早的 ${limit} 条作品`);
    await loadCreators();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#playerClose").addEventListener("click", closePlayer);
$("#playerPrevious").addEventListener("click", () => switchPlayer("previous"));
$("#playerNext").addEventListener("click", () => switchPlayer("next"));
$("#playerSpeed").addEventListener("change", event => {
  const video = $("#playerVideo");
  if (video) video.playbackRate = Number(event.target.value);
  if ($("#playerPersistSpeed").checked) {
    try { localStorage.setItem("douyinPlayerSpeed", event.target.value); } catch (_) {}
  }
});
$("#playerPersistSpeed").addEventListener("change", event => {
  try {
    localStorage.setItem("douyinPlayerPersistSpeed", String(event.target.checked));
    if (event.target.checked) localStorage.setItem("douyinPlayerSpeed", $("#playerSpeed").value);
  } catch (_) {}
});
$("#playerAutoNext").addEventListener("change", event => {
  try { localStorage.setItem("douyinPlayerAutoNext", String(event.target.checked)); } catch (_) {}
  const video = $("#playerVideo");
  if (video) video.loop = !event.target.checked;
  if (playerState.assets.length) {
    if (event.target.checked) schedulePlayerImageAdvance(); else clearPlayerImageTimer();
  }
});
$("#playerZoomOut").addEventListener("click", () => setPlayerZoom(playerState.zoom - .25));
$("#playerZoomIn").addEventListener("click", () => setPlayerZoom(playerState.zoom + .25));
$("#playerZoomReset").addEventListener("click", () => setPlayerZoom(1));
$("#playerMedia").addEventListener("dblclick", event => {
  if (!event.target.matches("video, .player-image")) return;
  const rect = $("#playerMedia").getBoundingClientRect();
  setPlayerZoom(playerState.zoom > 1 ? 1 : 2, {originX: event.clientX - rect.left, originY: event.clientY - rect.top});
});
$("#playerMedia").addEventListener("pointerdown", event => {
  if (playerState.zoom <= 1 || !event.target.matches("video, .player-image")) return;
  if (event.target.matches("video") && event.offsetY >= event.target.clientHeight - 48) return;
  event.preventDefault();
  playerState.dragging = true;
  playerState.dragStartX = event.clientX;
  playerState.dragStartY = event.clientY;
  playerState.dragOriginX = playerState.panX;
  playerState.dragOriginY = playerState.panY;
  $("#playerMedia").classList.add("is-dragging");
  $("#playerMedia").setPointerCapture(event.pointerId);
});
$("#playerMedia").addEventListener("pointermove", event => {
  if (!playerState.dragging) return;
  playerState.panX = playerState.dragOriginX + event.clientX - playerState.dragStartX;
  playerState.panY = playerState.dragOriginY + event.clientY - playerState.dragStartY;
  applyPlayerTransform();
});
const finishPlayerDrag = event => {
  if (!playerState.dragging) return;
  playerState.dragging = false;
  $("#playerMedia").classList.remove("is-dragging");
  if ($("#playerMedia").hasPointerCapture(event.pointerId)) $("#playerMedia").releasePointerCapture(event.pointerId);
};
$("#playerMedia").addEventListener("pointerup", finishPlayerDrag);
$("#playerMedia").addEventListener("pointercancel", finishPlayerDrag);
$("#playerView").addEventListener("wheel", event => {
  if (currentRoute !== "player" || playerState.wheelLocked || Math.abs(event.deltaY) < 20) return;
  event.preventDefault();
  playerState.wheelLocked = true;
  switchPlayer(event.deltaY > 0 ? "next" : "previous");
  setTimeout(() => { playerState.wheelLocked = false; }, 350);
}, {passive: false});
document.addEventListener("keydown", event => {
  if (currentRoute !== "player") return;
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement?.tagName)) return;
  if (event.key === "Escape") { event.preventDefault(); closePlayer(); }
  else if (event.key === "ArrowUp") { event.preventDefault(); switchPlayer("previous"); }
  else if (event.key === "ArrowDown") { event.preventDefault(); switchPlayer("next"); }
  else if (event.key === " ") { event.preventDefault(); togglePlayerVideo(); }
  else if (event.key === "ArrowLeft") {
    event.preventDefault();
    if (playerState.assets.length) changePlayerImage(-1); else seekPlayer(-5);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    if (playerState.assets.length) {
      if (!event.repeat) changePlayerImage(1);
    } else if (!event.repeat) startRightHold();
  }
});
document.addEventListener("keyup", event => {
  if (currentRoute === "player" && event.key === "ArrowRight" && !playerState.assets.length) {
    event.preventDefault();
    finishRightHold({seekIfTap: true});
  }
});
$("#pendingFilters").addEventListener("submit", async event => { event.preventDefault(); pendingState.creatorId = Number($("#pendingCreatorFilter").value) || null; pendingState.pageSize = Number($("#pendingPageSize").value); pendingState.page = 1; await loadPendingVideos(); });
$("#pendingRefresh").addEventListener("click", loadPendingVideos);
$("#pendingPrev").addEventListener("click", async () => { if (pendingState.page > 1) { pendingState.page--; await loadPendingVideos(); } });
$("#pendingNext").addEventListener("click", async () => { if (pendingState.page < pendingState.totalPages) { pendingState.page++; await loadPendingVideos(); } });
$("#pendingJumpButton").addEventListener("click", async () => { pendingState.page = Math.max(1, Math.min(Number($("#pendingJump").value) || 1, pendingState.totalPages || 1)); await loadPendingVideos(); });
$("#pendingSelectPage").addEventListener("click", () => { pendingState.items.forEach(item => pendingState.selected.add(Number(item.id))); document.querySelectorAll(".pending-select").forEach(input => { input.checked = true; input.closest(".media-card").classList.add("selected"); }); renderPendingSelectionCount(); });
$("#pendingClear").addEventListener("click", () => { pendingState.selected.clear(); document.querySelectorAll(".pending-select").forEach(input => { input.checked = false; input.closest(".media-card").classList.remove("selected"); }); renderPendingSelectionCount(); });
$("#pendingDownload").addEventListener("click", () => resolvePending("download"));
$("#pendingKeep").addEventListener("click", () => resolvePending("keep_metadata"));
$("#downloadJobFilters").addEventListener("submit", async event => { event.preventDefault(); downloadState.creatorId = Number($("#downloadCreatorFilter").value) || null; downloadState.status = $("#downloadStatusFilter").value || null; downloadState.pageSize = Number($("#downloadPageSize").value); downloadState.page = 1; await loadDownloadJobs(); });
$("#downloadJobsRefresh").addEventListener("click", loadDownloadJobs);
$("#downloadPrev").addEventListener("click", async () => { if (downloadState.page > 1) { downloadState.page--; await loadDownloadJobs(); } });
$("#downloadNext").addEventListener("click", async () => { if (downloadState.page < downloadState.totalPages) { downloadState.page++; await loadDownloadJobs(); } });
$("#downloadJumpButton").addEventListener("click", async () => { downloadState.page = Math.max(1, Math.min(Number($("#downloadJump").value) || 1, downloadState.totalPages || 1)); await loadDownloadJobs(); });
$("#logsPageFilters").addEventListener("submit", async event => { event.preventDefault(); logState.level = $("#logsLevel").value || null; logState.pageSize = Number($("#logsPageSize").value); logState.page = 1; await loadLogsPage(); });
$("#logsPageRefresh").addEventListener("click", loadLogsPage);
$("#logsPrev").addEventListener("click", async () => { if (logState.page > 1) { logState.page--; await loadLogsPage(); } });
$("#logsNext").addEventListener("click", async () => { if (logState.page < logState.totalPages) { logState.page++; await loadLogsPage(); } });
$("#logsJumpButton").addEventListener("click", async () => { logState.page = Math.max(1, Math.min(Number($("#logsJump").value) || 1, logState.totalPages || 1)); await loadLogsPage(); });
$("#editCreatorClose").addEventListener("click", () => $("#editCreatorDialog").close());
$("#editScheduleType").addEventListener("change", event => { $("#editDailyTimeLabel").hidden = event.target.value !== "daily"; });
$("#editCreatorForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (!creatorState.editingId) return;
  const button = $("#editCreatorSave");
  button.disabled = true;
  try {
    const enabled = $("#editCreatorEnabled").checked;
    await api(`/api/creators/${creatorState.editingId}`, {
      method: "PATCH",
      body: JSON.stringify({enabled, download_policy: $("#editCreatorPolicy").value, per_scan_limit: Number($("#editScanLimit").value)})
    });
    await api(`/api/creators/${creatorState.editingId}/schedule`, {
      method: "PATCH",
      body: JSON.stringify({
        schedule_type: $("#editScheduleType").value,
        interval_value: Number($("#editIntervalValue").value),
        daily_time: $("#editScheduleType").value === "daily" ? $("#editDailyTime").value : null,
        timezone: "Asia/Shanghai",
        enabled
      })
    });
    $("#editCreatorDialog").close();
    toast("监控配置已保存并立即生效");
    await loadCreators();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#refreshButton").addEventListener("click", () => refreshVisible().catch(error => toast(error.message)));
$("#videosRefresh").addEventListener("click", loadVideos);
$("#logsRefresh").addEventListener("click", loadLogs);
$("#deleteCreatorConfirm").addEventListener("click", async () => {
  if (!pendingDeleteCreatorId) return;
  const button = $("#deleteCreatorConfirm");
  button.disabled = true;
  try {
    await api(`/api/creators/${pendingDeleteCreatorId}`, {
      method: "DELETE",
      body: JSON.stringify({delete_local_files: $("#deleteCreatorFiles").checked})
    });
    $("#deleteCreatorDialog").close();
    pendingDeleteCreatorId = null;
    toast("监控用户已删除");
    await Promise.all([loadStatus(), loadCreators()]);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#creatorForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#previewStart");
  button.disabled = true;
  try {
    const preview = await api("/api/previews", {method: "POST", body: JSON.stringify({profile_url: $("#profileUrl").value})});
    previewState.token = preview.token;
    previewState.page = 1;
    navigate("add");
    $("#previewPanel").hidden = false;
    $("#previewPanel").scrollIntoView({behavior: "smooth", block: "start"});
    toast("预览扫描已启动，正在获取最近作品");
    await Promise.all([loadPreviewSession(), loadPreviewVideos()]);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#previewFilters").addEventListener("submit", async event => { event.preventDefault(); previewState.page = 1; previewState.pageSize = Number($("#previewPageSize").value); await loadPreviewVideos(); });
$("#previewPrev").addEventListener("click", async () => { if (previewState.page > 1) { previewState.page--; await loadPreviewVideos(); } });
$("#previewNext").addEventListener("click", async () => { if (previewState.page < previewState.totalPages) { previewState.page++; await loadPreviewVideos(); } });
$("#previewJumpButton").addEventListener("click", async () => { previewState.page = Math.max(1, Math.min(Number($("#previewJump").value) || 1, previewState.totalPages || 1)); await loadPreviewVideos(); });
$("#selectPage").addEventListener("click", () => updatePreviewSelection("select", previewState.items.map(v => v.aweme_id)));
$("#clearPage").addEventListener("click", () => updatePreviewSelection("deselect", previewState.items.map(v => v.aweme_id)));
$("#selectAllPreview").addEventListener("click", () => updatePreviewSelection("select_all", [], {auto_select_new: $("#autoSelectNew").checked}));
$("#selectFilteredPreview").addEventListener("click", () => updatePreviewSelection("select_filter", [], {filter: {keyword: $("#previewKeyword").value.trim(), content_type: $("#previewType").value}, auto_select_new: $("#autoSelectNew").checked}));
$("#clearAllPreview").addEventListener("click", () => updatePreviewSelection("clear_all"));
$("#autoSelectNew").addEventListener("change", event => updatePreviewSelection("set_auto", [], {auto_select_new: event.target.checked}));
$("#previewContinue").addEventListener("click", async () => { try { const limit = Math.max(20, Math.min(1000, Number($("#previewContinueLimit").value) || 100)); $("#previewContinueLimit").value = String(limit); saveListState(); await api(`/api/previews/${previewState.token}/continue`, {method: "POST", body: JSON.stringify({limit})}); toast(`已开始继续获取更早的 ${limit} 条作品`); await loadPreviewSession(); } catch (error) { toast(error.message); } });
$("#previewCancel").addEventListener("click", async () => { try { await api(`/api/previews/${previewState.token}/cancel`, {method: "POST"}); toast("预览扫描已取消"); await loadPreviewSession(); } catch (error) { toast(error.message); } });
$("#previewScheduleType").addEventListener("change", event => { $("#previewDailyTimeLabel").hidden = event.target.value !== "daily"; updatePreviewConfirmationSummary(); });
$("#previewPolicy").addEventListener("change", () => updatePolicyDescription($("#previewPolicy"), $("#previewPolicyDescription")));
$("#editCreatorPolicy").addEventListener("change", () => updatePolicyDescription($("#editCreatorPolicy"), $("#editPolicyDescription")));
[$("#previewPolicy"), $("#previewImmediate"), $("#previewIntervalValue"), $("#previewDailyTime")].forEach(element => element.addEventListener("change", updatePreviewConfirmationSummary));
$("#previewConfirmForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#previewConfirm");
  button.disabled = true;
  try {
    const result = await api(`/api/previews/${previewState.token}/confirm`, {method: "POST", body: JSON.stringify({...previewConfirmationPayload(), idempotency_key: crypto.randomUUID()})});
    clearTimeout(previewState.timer);
    toast(`已添加监控用户，并创建 ${result.download_jobs_created} 个下载任务`);
    $("#previewPanel").hidden = true;
    $("#profileUrl").value = "";
    previewState.token = null;
    saveListState();
    openCreatorLibrary(Number(result.creator.id));
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/settings", {method: "PATCH", body: JSON.stringify({download_dir: $("#downloadDir").value})});
    toast("下载目录已保存");
    await loadStatus();
  } catch (error) { toast(error.message); }
});
$("#dingForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/notifications/dingtalk", {
      method: "PATCH",
      body: JSON.stringify({
        enabled: $("#dingEnabled").checked,
        webhook: $("#dingWebhook").value || null,
        secret: $("#dingSecret").value || null
      })
    });
    $("#dingWebhook").value = "";
    $("#dingSecret").value = "";
    toast("钉钉配置已保存");
    await loadDingTalk();
  } catch (error) { toast(error.message); }
});
$("#dingTest").addEventListener("click", async () => {
  try { await api("/api/notifications/dingtalk/test", {method: "POST"}); toast("测试通知已发送"); }
  catch (error) { toast(error.message); }
});

try {
  const persistSpeed = localStorage.getItem("douyinPlayerPersistSpeed") !== "false";
  const autoNext = localStorage.getItem("douyinPlayerAutoNext") !== "false";
  const storedSpeed = localStorage.getItem("douyinPlayerSpeed") || "1";
  $("#playerPersistSpeed").checked = persistSpeed;
  $("#playerAutoNext").checked = autoNext;
  $("#playerSpeed").value = persistSpeed && [...$("#playerSpeed").options].some(option => option.value === storedSpeed) ? storedSpeed : "1";
} catch (_) {}

restoreListState();
updatePolicyDescription($("#previewPolicy"), $("#previewPolicyDescription"));
updatePolicyDescription($("#editCreatorPolicy"), $("#editPolicyDescription"));
navigate(location.hash.slice(1) || "dashboard", {updateHash: false});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(refreshTimer);
  else {
    refreshVisible().catch(error => toast(error.message));
    scheduleVisibleRefresh();
  }
});
