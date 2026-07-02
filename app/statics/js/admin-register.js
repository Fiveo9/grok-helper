(function () {
  'use strict';

  const API = '/admin/register';
  const POLL_MS = 5000;
  const terminalStatuses = new Set(['completed', 'failed', 'stopped', 'partial']);
  const stoppableStatuses = new Set(['queued', 'starting', 'running']);
  let adminKeyValue = '';
  let pollTimer = null;
  let pollingActive = false;
  let isPollingTasks = false;
  let selectedTaskId = null;
  let currentDefaults = {};
  let cloudMailDomainDraft = '';
  let eventsBound = false;
  let fieldCounter = 0;
  // Pagination state
  let currentPage = 1;
  let pageSize = 5;
  let totalPages = 1;
  let totalTasks = 0;
  const fieldNonce = Math.random().toString(36).slice(2, 10);
  const payloadKeyByControlName = new Map();
  const autofillSafePayloadKeys = new Set([
    'temp_mail_admin_email',
    'temp_mail_admin_password',
    'temp_mail_domain',
    'temp_mail_site_password',
    'api_token',
  ]);
  const nonLoginSecretPayloadKeys = new Set([
    'temp_mail_admin_password',
    'temp_mail_site_password',
    'api_token',
  ]);

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const text = (value, fallback = '-') => {
    const stringValue = String(value ?? '').trim();
    return stringValue || fallback;
  };

  const renderDetailValue = (value) => Array.isArray(value)
    ? (value.map((item) => esc(String(item ?? '').trim())).filter(Boolean).join('<br>') || '-')
    : esc(text(value));

  const isCloudMailProvider = (value) => String(value || '').trim().toLowerCase() === 'cloudmail';

  const domainFieldValue = (value) => Array.isArray(value) ? value.join('\n') : String(value ?? '');

  const api = async (path, options = {}) => {
    const headers = {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(adminKeyValue ? { Authorization: adminKeyValue } : {}),
      ...(options.headers || {}),
    };
    const response = await fetch(`${API}${path}`, { ...options, headers, cache: 'no-store' });
    let data = null;
    try {
      data = await response.json();
    } catch {}
    if (!response.ok) {
      if (response.status === 401) {
        adminKey.clear();
        const next = encodeURIComponent(location.pathname + location.search);
        location.href = `/admin/login?next=${next}`;
      }
      const detail = data?.detail || data?.message || `HTTP ${response.status}`;
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return data || {};
  };

  const statusLabel = (status) => ({
    queued: '排队中',
    starting: '启动中',
    running: '运行中',
    stopping: '停止中',
    completed: '已完成',
    partial: '部分完成',
    failed: '失败',
    stopped: '已停止',
  }[status] || status || '-');

  const statusClass = (status) => `status-badge status-${String(status || '').replace(/[^a-z0-9_-]/gi, '')}`;

  const phaseFallback = (status) => ({
    queued: '等待调度',
    starting: '准备启动',
  }[status] || '');

  const fieldValue = (data, key, fallback = '') => data?.[key] ?? fallback ?? '';

  const controlNameFor = (payloadKey) => {
    if (!autofillSafePayloadKeys.has(payloadKey)) return payloadKey;
    const controlName = `rcf_${fieldNonce}_${++fieldCounter}`;
    payloadKeyByControlName.set(controlName, payloadKey);
    return controlName;
  };

  const payloadKeyFor = (controlName) => payloadKeyByControlName.get(controlName) || controlName;

  const payloadElement = (form, payloadKey, role = '') => Array.from(form.elements || []).find((element) => {
    if (role && element.dataset?.registerRole === role) return true;
    return element.name && payloadKeyFor(element.name) === payloadKey;
  });

  const renderTempMailDomainField = (provider, value) => `
    <div id="register-temp-mail-domain-field">
      ${isCloudMailProvider(provider)
        ? textareaField('temp_mail_domain', '邮箱域名', domainFieldValue(value), 'example.com', false, { role: 'scope' })
        : inputField('temp_mail_domain', '邮箱域名', domainFieldValue(value), 'example.com', 'text', false, { role: 'scope' })}
    </div>`;

  const renderSettingsForm = (settings = {}, defaults = {}) => {
    currentDefaults = defaults || {};
    const apiDefaults = currentDefaults.api || {};
    const form = $('register-settings-form');
    if (!form) return;
    const provider = fieldValue(settings, 'temp_mail_provider', currentDefaults.temp_mail_provider);
    const domainValue = fieldValue(settings, 'temp_mail_domain', currentDefaults.temp_mail_domain);
    cloudMailDomainDraft = domainFieldValue(domainValue);
    form.innerHTML = [
      inputField('proxy', '请求代理', fieldValue(settings, 'proxy', currentDefaults.proxy), 'http://127.0.0.1:7890'),
      inputField('browser_proxy', '浏览器代理', fieldValue(settings, 'browser_proxy', currentDefaults.browser_proxy), 'http://127.0.0.1:7890'),
      selectField('temp_mail_provider', '邮箱服务商', provider, [
        ['cloudmail', 'cloudmail'],
        ['duckmail', 'duckmail'],
        ['ahem', 'ahem'],
        ['generic', 'generic'],
      ]),
      inputField('temp_mail_api_base', '邮箱 API', fieldValue(settings, 'temp_mail_api_base', currentDefaults.temp_mail_api_base), 'https://example.com'),
      inputField('temp_mail_admin_email', '邮箱管理员', fieldValue(settings, 'temp_mail_admin_email', currentDefaults.temp_mail_admin_email), 'admin@example.com', 'text', false, { role: 'contact' }),
      inputField('temp_mail_admin_password', '邮箱密码', '', '已有值则留空不修改', 'password'),
      renderTempMailDomainField(provider, domainValue),
      inputField('temp_mail_site_password', '站点密码', '', '已有值则留空不修改', 'password'),
      inputField('api_endpoint', 'Token Sink', fieldValue(settings, 'api_endpoint', apiDefaults.endpoint), 'http://127.0.0.1:8000/admin/api/tokens', 'text', true),
      inputField('api_token', 'Token Sink Key', '', '已有值则留空不修改', 'password'),
      checkboxField('api_append', '追加写入 token', fieldValue(settings, 'api_append', apiDefaults.append ?? true)),
      '<div class="form-field wide"><button type="submit" class="page-action-btn page-action-btn-primary">保存默认设置</button></div>',
    ].join('');
    form.dataset.tempMailProvider = String(provider || '');
  };

  const renderCreateForm = () => {
    const form = $('register-create-form');
    if (!form) return;
    const count = currentDefaults?.run?.count || 50;
    form.innerHTML = [
      inputField('name', '任务名称', `register-${new Date().toISOString().slice(0, 10)}`, 'register-batch'),
      inputField('count', '注册数量', count, '例如 50', 'number'),
      inputField('proxy', '覆盖请求代理', '', '留空沿用系统默认值'),
      inputField('browser_proxy', '覆盖浏览器代理', '', '留空沿用系统默认值'),
      inputField('temp_mail_domain', '覆盖邮箱域名', '', '留空沿用系统默认值', 'text', false, { role: 'scope' }),
      inputField('api_token', '覆盖 Sink Key', '', '留空沿用系统默认值', 'password'),
      checkboxField('api_append', '追加写入 token', true),
      '<div class="form-field wide"><label for="create-notes">备注</label><textarea id="create-notes" name="notes" placeholder="可选"></textarea></div>',
      '<div class="form-field wide"><button type="submit" class="page-action-btn page-action-btn-primary">创建任务</button></div>',
    ].join('');
  };

  const inputField = (name, label, value, placeholder = '', type = 'text', wide = false, options = {}) => {
    const controlName = controlNameFor(name);
    const safeId = `register-${controlName}`;
    const isAutofillSafe = autofillSafePayloadKeys.has(name);
    const isNonLoginSecret = nonLoginSecretPayloadKeys.has(name);
    const inputType = isNonLoginSecret ? 'text' : type;
    const roleAttr = options.role ? ` data-register-role="${esc(options.role)}"` : '';
    const safeAttrs = isAutofillSafe ? ' autocomplete="one-time-code" readonly data-unlock-on-focus="true"' : ' autocomplete="off"';
    const secretAttrs = isNonLoginSecret ? ' data-secret-field="true" style="-webkit-text-security:disc"' : '';
    return `
    <div class="form-field${wide ? ' wide' : ''}">
      <label for="${esc(safeId)}">${esc(label)}</label>
      <input id="${esc(safeId)}" name="${esc(controlName)}" type="${esc(inputType)}" value="${esc(value)}" placeholder="${esc(placeholder)}"${safeAttrs}${roleAttr}${secretAttrs}>
    </div>`;
  };

  const selectField = (name, label, value, options, wide = false) => {
    const stringValue = String(value ?? '');
    const normalizedOptions = options.some(([optionValue]) => String(optionValue) === stringValue)
      ? options
      : (stringValue ? [[stringValue, stringValue], ...options] : options);
    return `
    <div class="form-field${wide ? ' wide' : ''}">
      <label for="register-${esc(name)}">${esc(label)}</label>
      <select id="register-${esc(name)}" name="${esc(name)}">${normalizedOptions.map(([optionValue, optionLabel]) => `<option value="${esc(optionValue)}" ${String(optionValue) === stringValue ? 'selected' : ''}>${esc(optionLabel)}</option>`).join('')}</select>
    </div>`;
  };

  const textareaField = (name, label, value, placeholder = '', wide = false, options = {}) => {
    const controlName = controlNameFor(name);
    const safeId = `register-${controlName}`;
    const isAutofillSafe = autofillSafePayloadKeys.has(name);
    const roleAttr = options.role ? ` data-register-role="${esc(options.role)}"` : '';
    const safeAttrs = isAutofillSafe ? ' autocomplete="one-time-code" readonly data-unlock-on-focus="true"' : '';
    return `
    <div class="form-field${wide ? ' wide' : ''}">
      <label for="${esc(safeId)}">${esc(label)}</label>
      <textarea id="${esc(safeId)}" name="${esc(controlName)}" placeholder="${esc(placeholder)}"${safeAttrs}${roleAttr}>${esc(value)}</textarea>
    </div>`;
  };

  const checkboxField = (name, label, checked) => `
    <div class="form-field">
      <label>${esc(label)}</label>
      <span class="check-row"><input name="${esc(name)}" type="checkbox" ${checked ? 'checked' : ''}> 启用</span>
    </div>`;

  const formPayload = (form, includeEmpty = true) => {
    const data = new FormData(form);
    const payload = {};
    for (const [key, value] of data.entries()) {
      const payloadKey = payloadKeyFor(key);
      if (payloadKey === 'api_append') continue;
      const trimmed = String(value).trim();
      if (includeEmpty || trimmed) payload[payloadKey] = trimmed;
    }
    if (form.elements.count) payload.count = Math.max(1, Number(payload.count || 1));
    payload.api_append = Boolean(form.elements.api_append?.checked);
    if (form.id === 'register-settings-form') {
      const provider = String(payload.temp_mail_provider || '').trim().toLowerCase();
      if (provider === 'cloudmail' && Object.prototype.hasOwnProperty.call(payload, 'temp_mail_domain')) {
        const domains = String(payload.temp_mail_domain || '')
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean);
        payload.temp_mail_domain = domains;
      }
    }
    return payload;
  };

  const loadMetaAndSettings = async () => {
    const [meta, settingsData] = await Promise.all([api('/meta'), api('/settings')]);
    renderSettingsForm(settingsData.settings || meta.settings || {}, settingsData.defaults || meta.defaults || {});
    renderCreateForm();
  };

  const loadHealth = async () => {
    const grid = $('register-health-grid');
    const checked = $('register-health-checked');
    if (grid) grid.innerHTML = '<div class="health-card muted">检查中...</div>';
    const data = await api('/health');
    if (checked) checked.textContent = data.checked_at ? `检查于 ${data.checked_at}` : '已检查';
    if (!grid) return;
    const items = data.items || [];
    grid.innerHTML = items.length ? items.map((item) => `
      <div class="health-card">
        <div class="health-top">
          <div class="health-label" title="${esc(item.label)}">${esc(item.label)}</div>
          <span class="health-pill ${item.ok ? 'ok' : 'bad'}">${item.ok ? '正常' : '异常'}</span>
        </div>
        <div class="health-summary">${esc(item.summary)}</div>
        <div class="health-detail">${esc(item.detail || item.target || '')}</div>
      </div>`).join('') : '<div class="health-card muted">暂无健康检查结果</div>';
  };

  const loadTasks = async () => {
    if (isPollingTasks) return;
    isPollingTasks = true;
    try {
      const data = await api(`/tasks?page=${currentPage}&page_size=${pageSize}`);
      totalPages = data.total_pages || 1;
      totalTasks = data.total || 0;
      renderTasks(data.tasks || []);
      const selected = selectedTaskId && (data.tasks || []).find((task) => task.id === selectedTaskId);
      if (selected) await loadDetail(selectedTaskId, false);
    } finally {
      isPollingTasks = false;
    }
  };

  const renderTasks = (tasks) => {
    const list = $('register-task-list');
    const count = $('register-task-count');
    if (count) count.textContent = `${totalTasks} 个任务`;
    if (!list) return;
    if (!tasks.length) {
      list.innerHTML = '<tr><td colspan="6" class="table-empty">暂无任务</td></tr>';
      renderPagination();
      return;
    }
    list.innerHTML = tasks.map((task) => {
      const progress = `${task.completed_count || 0}/${task.target_count || 0}`;
      const failures = task.failed_count ? `，失败 ${task.failed_count}` : '';
      const canStop = stoppableStatuses.has(task.status);
      const canDelete = terminalStatuses.has(task.status);
      return `
        <tr data-task-id="${esc(task.id)}">
          <td><div class="task-name">${esc(task.name)}</div><div class="task-sub">#${esc(task.id)} ${esc(task.current_phase || phaseFallback(task.status))}</div></td>
          <td><span class="${esc(statusClass(task.status))}">${esc(statusLabel(task.status))}</span></td>
          <td>${esc(progress)}${esc(failures)}</td>
          <td>${esc(text(task.last_email))}</td>
          <td><div>${esc(text(task.created_at))}</div><div class="task-sub">${esc(text(task.finished_at || task.started_at, '未开始'))}</div></td>
          <td><div class="task-actions">
            <button type="button" class="link-btn" data-action="detail">详情</button>
            <button type="button" class="link-btn" data-action="logs">日志</button>
            <button type="button" class="link-btn danger" data-action="stop" ${canStop ? '' : 'disabled'}>停止</button>
            <button type="button" class="link-btn danger" data-action="delete" ${canDelete ? '' : 'disabled'}>删除</button>
          </div></td>
        </tr>`;
    }).join('');
    renderPagination();
  };

  const renderPagination = () => {
    let container = $('register-pagination');
    if (!container) {
      container = document.createElement('div');
      container.id = 'register-pagination';
      container.className = 'pagination-wrap';
      const tableCard = $('register-task-list')?.closest('.table-card');
      if (tableCard) tableCard.after(container);
    }
    const sizeOptions = [5, 10, 20].map(s =>
      `<option value="${s}" ${s === pageSize ? 'selected' : ''}>${s}条/页</option>`
    ).join('');
    const pageButtons = [];
    for (let i = 1; i <= totalPages; i++) {
      pageButtons.push(`<button type="button" class="page-btn ${i === currentPage ? 'active' : ''}" data-page="${i}">${i}</button>`);
    }
    container.innerHTML = `
      <div class="pagination-left">
        <select id="register-page-size" class="page-size-select">${sizeOptions}</select>
      </div>
      <div class="pagination-right">
        <button type="button" class="page-btn" data-page="prev" ${currentPage <= 1 ? 'disabled' : ''}>‹</button>
        ${pageButtons.join('')}
        <button type="button" class="page-btn" data-page="next" ${currentPage >= totalPages ? 'disabled' : ''}>›</button>
      </div>
    `;
  };

  const loadDetail = async (taskId, includeLogs = true) => {
    selectedTaskId = Number(taskId);
    const data = await api(`/tasks/${selectedTaskId}`);
    renderDetail(data.task || {});
    if (includeLogs) await loadLogs(selectedTaskId);
  };

  const renderDetail = (task) => {
    const title = $('register-detail-title');
    const detail = $('register-task-detail');
    if (title) title.textContent = task.id ? `${task.name} #${task.id}` : '未选择任务';
    if (!detail) return;
    if (!task.id) {
      detail.innerHTML = '<div class="muted">选择任务后显示概要。</div>';
      return;
    }
    const config = task.config || {};
    const apiConf = config.api || {};
    const items = [
      ['状态', statusLabel(task.status)],
      ['目标数量', task.target_count],
      ['完成/失败', `${task.completed_count || 0}/${task.failed_count || 0}`],
      ['当前轮次', task.current_round || 0],
      ['最近邮箱', text(task.last_email)],
      ['最近错误', text(task.last_error)],
      ['请求代理', text(config.proxy)],
      ['浏览器代理', text(config.browser_proxy)],
      ['邮箱域名', config.temp_mail_domain],
      ['Token Sink', text(apiConf.endpoint)],
      ['PID', text(task.pid)],
      ['退出码', text(task.exit_code)],
    ];
    detail.innerHTML = items.map(([label, value]) => `
      <div class="detail-item">
        <div class="detail-label">${esc(label)}</div>
        <div class="detail-value">${renderDetailValue(value)}</div>
      </div>`).join('');
  };

  const loadLogs = async (taskId) => {
    const box = $('register-task-logs');
    if (!box) return;
    const data = await api(`/tasks/${Number(taskId)}/logs?limit=300`);
    const lines = data.lines || [];
    box.textContent = lines.length ? lines.join('\n') : '暂无日志。';
    box.scrollTop = box.scrollHeight;
  };

  const saveSettings = async (event) => {
    event.preventDefault();
    const notice = $('register-settings-notice');
    try {
      const data = await api('/settings', { method: 'POST', body: JSON.stringify(formPayload(event.target, true)) });
      renderSettingsForm(data.settings || {}, data.defaults || {});
      renderCreateForm();
      if (notice) notice.textContent = '默认设置已保存。';
    } catch (error) {
      if (notice) notice.textContent = `保存失败：${error.message}`;
    }
  };

  const createTask = async (event) => {
    event.preventDefault();
    const notice = $('register-create-notice');
    try {
      const data = await api('/tasks', { method: 'POST', body: JSON.stringify(formPayload(event.target, false)) });
      selectedTaskId = data.task?.id || null;
      if (notice) notice.textContent = '任务已创建。';
      renderCreateForm();
      await loadTasks();
      if (selectedTaskId) await loadDetail(selectedTaskId);
    } catch (error) {
      if (notice) notice.textContent = `创建失败：${error.message}`;
    }
  };

  const handleTaskAction = async (event) => {
    const button = event.target.closest('button[data-action]');
    const row = event.target.closest('tr[data-task-id]');
    if (!button || !row || button.disabled) return;
    const taskId = Number(row.dataset.taskId);
    const actionLabel = {
      detail: '查看详情',
      logs: '查看日志',
      stop: '停止任务',
      delete: '删除任务',
    }[button.dataset.action] || '执行操作';
    try {
      if (button.dataset.action === 'detail') await loadDetail(taskId, false);
      if (button.dataset.action === 'logs') await loadDetail(taskId, true);
      if (button.dataset.action === 'stop') await api(`/tasks/${taskId}/stop`, { method: 'POST' });
      if (button.dataset.action === 'delete') {
        const taskName = row.querySelector('.task-name')?.textContent || `#${taskId}`;
        if (!confirm(`确定删除任务 ${taskName}？此操作会删除任务文件和日志。`)) return;
        await api(`/tasks/${taskId}`, { method: 'DELETE' });
      }
      if (button.dataset.action === 'stop' || button.dataset.action === 'delete') await loadTasks();
      if (button.dataset.action === 'delete' && selectedTaskId === taskId) {
        selectedTaskId = null;
        renderDetail({});
        const box = $('register-task-logs');
        if (box) box.textContent = '选择任务后显示日志。';
      }
    } catch (error) {
      const box = $('register-task-logs');
      if (box) box.textContent = `任务 #${taskId} ${actionLabel}操作失败：${error.message}`;
    }
  };

  const bindEvents = () => {
    if (eventsBound) return;
    eventsBound = true;
    document.addEventListener('submit', (event) => {
      if (event.target?.id === 'register-settings-form') saveSettings(event);
      if (event.target?.id === 'register-create-form') createTask(event);
    });
    document.addEventListener('change', (event) => {
      if (event.target?.name !== 'temp_mail_provider' || event.target.form?.id !== 'register-settings-form') return;
      const form = event.target.form;
      const previousProvider = form.dataset.tempMailProvider || '';
      const domainField = payloadElement(form, 'temp_mail_domain', 'scope');
      const domainWrap = $('register-temp-mail-domain-field');
      if (!domainWrap) return;
      const currentDomainValue = domainField?.value || '';
      if (isCloudMailProvider(previousProvider)) {
        cloudMailDomainDraft = currentDomainValue || cloudMailDomainDraft;
      }
      const nextProvider = event.target.value;
      const nextDomainValue = isCloudMailProvider(nextProvider)
        ? (isCloudMailProvider(previousProvider) ? cloudMailDomainDraft : currentDomainValue)
        : currentDomainValue;
      if (isCloudMailProvider(nextProvider)) cloudMailDomainDraft = nextDomainValue;
      domainWrap.outerHTML = renderTempMailDomainField(nextProvider, nextDomainValue);
      form.dataset.tempMailProvider = String(nextProvider || '');
    });
    document.addEventListener('input', (event) => {
      if (event.target?.dataset?.registerRole === 'scope' && event.target.form?.id === 'register-settings-form') {
        const provider = event.target.form.dataset.tempMailProvider || payloadElement(event.target.form, 'temp_mail_provider')?.value || '';
        if (isCloudMailProvider(provider)) cloudMailDomainDraft = event.target.value || '';
      }
    });
    document.addEventListener('focusin', (event) => {
      if (event.target?.dataset?.unlockOnFocus === 'true') event.target.removeAttribute('readonly');
    });
    $('register-task-list')?.addEventListener('click', handleTaskAction);
    $('register-refresh-health')?.addEventListener('click', () => loadHealth().catch(showError));
    $('register-refresh-tasks')?.addEventListener('click', () => loadTasks().catch(showError));
    // Pagination events (delegated)
    document.addEventListener('click', (event) => {
      const btn = event.target.closest('.page-btn[data-page]');
      if (!btn || btn.disabled) return;
      const val = btn.dataset.page;
      if (val === 'prev') currentPage = Math.max(1, currentPage - 1);
      else if (val === 'next') currentPage = Math.min(totalPages, currentPage + 1);
      else currentPage = Number(val) || 1;
      loadTasks().catch(showError);
    });
    document.addEventListener('change', (event) => {
      if (event.target?.id !== 'register-page-size') return;
      pageSize = Number(event.target.value) || 5;
      currentPage = 1;
      loadTasks().catch(showError);
    });
  };

  const showError = (error) => {
    const box = $('register-task-logs');
    if (box) box.textContent = `请求失败：${error.message}`;
  };

  const stopPolling = () => {
    pollingActive = false;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  };

  const pollTasks = async () => {
    if (!pollingActive) return;
    try {
      await loadTasks();
    } catch {}
    if (pollingActive) pollTimer = setTimeout(pollTasks, POLL_MS);
  };

  window.initRegisterConsole = async function initRegisterConsole(key) {
    adminKeyValue = key;
    stopPolling();
    bindEvents();
    renderDetail({});
    try {
      await loadMetaAndSettings();
      await Promise.all([loadHealth(), loadTasks()]);
    } catch (error) {
      showError(error);
    }
    pollingActive = true;
    pollTimer = setTimeout(pollTasks, POLL_MS);
    window.addEventListener('beforeunload', stopPolling, { once: true });
  };
})();
