/**
 * QwenPaw 生图助手
 * 工作流驱动右侧栏：主模型 → 工作流预设 → LoRA → 提示词 → 动态参数
 */
(function () {
  'use strict';
  var Q = window.QwenPaw;
  if (!Q || !Q.host) return;
  var React = Q.host.React, antd = Q.host.antd, I = Q.host.antdIcons, h = React.createElement;
  var Button = antd.Button, Input = antd.Input, InputNumber = antd.InputNumber, Select = antd.Select,
    Slider = antd.Slider, Switch = antd.Switch, Tag = antd.Tag, Typography = antd.Typography,
    Alert = antd.Alert, Divider = antd.Divider, Modal = antd.Modal, Rate = antd.Rate,
    message = antd.message, Empty = antd.Empty;
  var pid = 'qwenpaw-image-gen';
  var FRONTEND_VERSION = '';  // 从后端读取，不再硬编码
  var versionWarning = { current: '' };

  // 启动时从后端读取版本号（唯一定义源是 plugin.json）
  function fetchVersion() {
    return req('/version?_=' + Date.now()).then(function (v) {
      if (v && v.version) FRONTEND_VERSION = v.version;
    }).catch(function () {});
  }
  fetchVersion();

  function req(p, o) {
    o = o || {};
    o.headers = Object.assign({ 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' }, o.headers || {});
    return Q.host.fetch('/image-gen' + p, o).then(function (r) {
      return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || '请求失败'); return b; });
    });
  }
  function iurl(id) { return '/api/image-gen/images/' + id + '/file'; }
  function isOn() { return localStorage.getItem(pid + '-enabled') !== '0'; }
  function setOn(v) { localStorage.setItem(pid + '-enabled', v ? '1' : '0'); }
  var listeners = [];
  function onToggle(fn) { listeners.push(fn); }
  function emitToggle(v) { listeners.forEach(function (f) { try { f(v); } catch(e) {} }); }

  function kvToOptions(arr) { return (arr || []).map(function (x) { return { value: x, label: x }; }); }
  function schemaDefault(schema) {
    var out = {};
    Object.keys(schema || {}).forEach(function (k) { out[k] = schema[k].default; });
    if (out.seed === undefined) out.seed = -1;
    return out;
  }

  function Section(props) {
    return h('div', { style: { padding: '10px 10px', borderBottom: '1px solid var(--border-color-split)' } },
      h('div', { style: { fontSize: 12, fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        h('span', null, props.title), props.extra || null),
      props.children
    );
  }

  function ParamControl(props) {
    var k = props.name, def = props.def || {}, value = props.value, setValue = props.setValue;
    var label = def.label || k;
    if (def.type === 'select') {
      return h('div', { style: { marginBottom: 8 } },
        h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 3 } }, label),
        h(Select, { size: 'small', value: value, onChange: function (v) { setValue(k, v); }, style: { width: '100%' }, options: kvToOptions(def.options || []) })
      );
    }
    return h('div', { style: { marginBottom: 8 } },
      h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 3 } },
        h('span', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)' } }, label),
        h(InputNumber, {
          size: 'small', value: value, min: def.min, max: def.max, step: def.step || 1,
          onChange: function (v) { setValue(k, v); }, style: { width: 88 }
        })
      ),
      (def.min !== undefined && def.max !== undefined && def.max - def.min <= 4096) ? h(Slider, {
        value: typeof value === 'number' ? value : def.default,
        min: def.min, max: def.max, step: def.step || 1,
        onChange: function (v) { setValue(k, v); }
      }) : null
    );
  }

  // Error Boundary：防止插件崩溃导致 QwenPaw 整体白屏
  function ErrorBoundary(props) {
    var s = React.useState(null);
    var err = s[0], setErr = s[1];
    if (err) {
      return h('div', { style: { padding: 16, color: '#ff4d4f' } },
        h('b', null, '⚠ 生图助手出错了'),
        h('p', { style: { fontSize: 12, marginTop: 8 } }, String(err.message || err)),
        h(Button, { size: 'small', onClick: function () { setErr(null); }, style: { marginTop: 8 } }, '重试')
      );
    }
    return React.createElement(ErrorBoundaryInner, { onError: setErr, children: props.children });
  }
  function ErrorBoundaryInner(props) {
    var s = React.useState(false);
    React.useEffect(function () {
      var prev = window.onerror;
      window.onerror = function (msg) { props.onError(new Error(msg)); return true; };
      return function () { window.onerror = prev; };
    }, []);
    if (s[0]) return null;
    return props.children;
  }

  function GenPanel() {
    var s = React.useState;
    var state = s(null), model = s(''), tab = s('gen'), imgs = s([]), preview = s(null), busy = s(false), scanning = s(false), galleryLoading = s(false), category = s('未分类'), categories = s(['未分类']), batchMode = s(false), selectedIds = s([]), batchBusy = s(false);
    var galleryRequest = React.useRef(0);
    var scanFailed = s(false);  // 扫描失败时置 true，让「复制 AI 提示词」按钮切换为找 ComfyUI 的提示词
    var versionMismatch = s('');
    var prompt = s(''), neg = s(''), loras = s([]), params = s({}), workflowPreset = s(0);

    function load(m, presetId) {
      var qs = new URLSearchParams();
      if (m) qs.set('model_name', m);
      if (presetId) qs.set('workflow_preset_id', presetId);
      req('/workflow-state' + (qs.toString() ? '?' + qs.toString() : '')).then(function (d) {
        state[1](d);
        var chosen = d.selected_model || '';
        model[1](chosen);
        workflowPreset[1](Number(d.selected_preset_id || presetId || 0));
        params[1](schemaDefault(d.params_schema || {}));
      }).catch(function (e) { message.error(e.message); });
    }
    function loadImages(targetCategory) {
      var selected = targetCategory === undefined ? category[0] : targetCategory;
      var requestId = ++galleryRequest.current;
      galleryLoading[1](true); imgs[1]([]);
      req('/images?category=' + encodeURIComponent(selected) + '&_=' + Date.now()).then(function (d) {
        // 忽略较早请求的迟到响应，防止切换分类后又被旧分类结果覆盖。
        if (requestId === galleryRequest.current) imgs[1](d.items || []);
      }).catch(function (e) { if (requestId === galleryRequest.current) message.error(e.message || '图库读取失败'); })
        .then(function () { if (requestId === galleryRequest.current) galleryLoading[1](false); });
    }
    function switchCategory(v) { category[1](v); selectedIds[1]([]); loadImages(v); }
    function toggleSelected(id) { selectedIds[1](function (old) { return old.indexOf(id) >= 0 ? old.filter(function (x) { return x !== id; }) : old.concat([id]); }); }
    function exitBatchMode() { batchMode[1](false); selectedIds[1]([]); }
    function batchMove(target) {
      if (!selectedIds[0].length) return;
      batchBusy[1](true);
      req('/images/batch/category?category=' + encodeURIComponent(target), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: selectedIds[0] }) })
        .then(function (r) { message.success('已移动 ' + (r.moved || 0) + ' 张图片到「' + target + '」'); exitBatchMode(); loadImages(category[0]); })
        .catch(function (e) { message.error(e.message || '批量换分类失败'); })
        .then(function () { batchBusy[1](false); });
    }
    function batchDelete() {
      var count = selectedIds[0].length; if (!count) return;
      if (!window.confirm('确定从图库删除 ' + count + ' 张图片吗？图片文件也会移入系统回收站。')) return;
      batchBusy[1](true);
      req('/images/batch/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: selectedIds[0] }) })
        .then(function (r) { message.success('已删除 ' + (r.deleted || 0) + ' 张图片'); exitBatchMode(); preview[1](null); loadImages(category[0]); })
        .catch(function (e) { message.error(e.message || '批量删除失败'); })
        .then(function () { batchBusy[1](false); });
    }
    function loadCategories() { req('/gallery/categories?_=' + Date.now()).then(function (d) { categories[1](d.categories || ['未分类']); }).catch(function () {}); }
    function scanGallery() { galleryLoading[1](true); req('/gallery/scan', { method: 'POST' }).then(function (d) { message.success('已扫描 ' + (d.added || 0) + ' 张图片'); loadCategories(); loadImages(); }).catch(function (e) { galleryLoading[1](false); message.error(e.message); }); }
    React.useEffect(function () {
      load();
      loadCategories();
      loadImages();
      req('/version?_=' + Date.now()).then(function (v) {
        if (v && v.version && FRONTEND_VERSION && v.version !== FRONTEND_VERSION) {
          var msg = '版本不一致：前端 v' + FRONTEND_VERSION + ' / 后端 v' + v.version + '。请完全退出并重启 QwenPaw Desktop。';
          versionMismatch[1](msg);
          if (!versionWarning.current) { versionWarning.current = msg; message.warning(msg, 8); }
        }
      }).catch(function () {});
    }, []);

    function setParam(k, v) { params[1](Object.assign({}, params[0], (function(){ var o={}; o[k]=v; return o; })())); }
    function addLora() { loras[1]([].concat(loras[0], [{ name: '', enabled: true, strength_model: 0.6, strength_clip: 0.6 }])); }
    function updateLora(i, patch) { var a = loras[0].slice(); a[i] = Object.assign({}, a[i], patch); loras[1](a); }
    function delLora(i) { var a = loras[0].slice(); a.splice(i, 1); loras[1](a); }
    function applyPreset(id) {
      workflowPreset[1](id || 0);
      load(model[0], id || 0);
      if (id && model[0]) {
        req('/workflows/apply-preset/' + id + '?model_name=' + encodeURIComponent(model[0]), { method: 'POST' })
          .then(function () { message.success('已切换并绑定工作流'); })
          .catch(function (e) { message.warning(e.message); });
      }
    }
    function saveCurrentWorkflow() {
      var name = window.prompt('工作流名称', (model[0] ? model[0].split(/[\\/]/).pop().replace(/\.[^.]+$/, '') + ' 工作流' : '自定义工作流'));
      if (!name) return;
      var savedSchema = JSON.parse(JSON.stringify(Object.keys(schema).length ? schema : {}));
      Object.keys(savedSchema).forEach(function (k) { if (params[0][k] !== undefined) savedSchema[k].default = params[0][k]; });
      req('/presets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        name: name,
        description: '从生图助手面板保存的工作流参数预设',
        model_type: 'custom',
        workflow_json: {},
        params_schema: savedSchema,
        sort_order: 1000
      }) }).then(function (p) {
        message.success('工作流已保存');
        load(model[0], p.id);
      }).catch(function (e) { message.error(e.message); });
    }

    function autoBindWorkflow() {
      scanning[1](true);
      // 一键适配：全自动，不需要传模型名
      req('/workflows/one-click-setup', { method: 'POST' }).then(function (r) {
        scanning[1](false);
        scanFailed[1](false);
        var s = r.summary || {};
        message.success(r.message + ' | ' + (s.total_models || 0) + ' 个模型、' + (s.loras || 0) + ' 个 LoRA、' + (s.samplers || 0) + ' 个采样器');
        // 刷新面板状态，选中自动绑定的模型
        load(r.selected_model, 0);
      }).catch(function (e) {
        scanning[1](false);
        // 区分"找不到 ComfyUI"和"其他错误"
        if (e.message && e.message.indexOf('未找到') >= 0) {
          scanFailed[1](true);
          message.warning(e.message);
        } else {
          message.error('一键适配失败：' + e.message);
        }
      });
    }

    function requestAIFindComfyUI() {
      // 扫描失败时的兜底提示词：让 AI 通过 skill 手动配置 ComfyUI 连接
      var text = [
        '小琪，我的生图插件自动扫描没找到 ComfyUI，请帮我手动配置。',
        '',
        '我的 ComfyUI 可能：',
        '· 用了非标准端口（不在 8000~9000 范围）',
        '· 装在二级目录，端口自动分配到了意料之外的地方',
        '· 或者还没启动',
        '',
        '请帮我：',
        '1. 先确认 ComfyUI 是否已启动 —— 如果没启动，告诉我怎么启动',
        '2. 如果启动了，帮我找出它实际在哪个端口上监听（可以看 ComfyUI 启动窗口的输出，通常有 "To see the GUI go to: http://127.0.0.1:XXXX" 这行）',
        '3. 找到端口后，用 PATCH /image-gen/config/comfyui_api_url 手动设置，例如：',
        '   curl -X PATCH http://127.0.0.1:14999/image-gen/config/comfyui_api_url -H "Content-Type: application/json" -d \'{"value":"http://127.0.0.1:9188"}\'',
        '4. 设置完后，调用 GET /image-gen/status 确认连接成功',
        '5. 连接成功后告诉我，我回插件点「一键自动适配」继续绑定工作流',
        '',
        'ComfyUI 启动窗口的输出示例：',
        '  Total VRAM 6144 MB, total RAM 16384 MB',
        '  To see the GUI go to: http://127.0.0.1:9188',
        '（看最后一行的端口号就行）'
      ].join('\n');
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
          .then(function () { message.success('已复制 ComfyUI 手动配置提示词，请粘贴到主聊天框发送。'); })
          .catch(function () { message.info('请手动复制提示词到主聊天框发送。'); });
      } else {
        message.info('当前环境不支持自动复制，请手动复制提示词到主聊天框发送。');
      }
    }

    function requestAIWorkflow() {
      var text = [
        '小琪，请为我的 ComfyUI 主模型创建并绑定一个生图工作流。',
        '',
        '【目标模型】' + (model[0] || '当前选中的模型'),
        '【插件任务】',
        '1. 扫描 ComfyUI 是否运行，并读取当前可用节点、checkpoint、LoRA、VAE。',
        '2. 判断这个模型适合的工作流类型（SDXL / Flux / Illustrious / Pony / 其他）。',
        '3. 创建或选择一个能稳定运行的 ComfyUI API workflow。',
        '4. 把可调参数整理成 params_schema，同步给生图助手插件：steps、cfg、sampler、scheduler、width、height、seed、batch_size、denoise 等，按实际工作流节点暴露，不要假参数。',
        '5. 如果工作流支持 LoRA，请暴露 LoRA 节点，并支持 strength_model 和 strength_clip。',
        '6. 最后调用插件后端的 /image-gen/workflows/bind 完成模型与工作流绑定。',
        '',
        '要求：没有真实节点就不要编造；优先做一个最小可运行工作流，确认能跑后再扩展高级功能。'
      ].join('\n');
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
          .then(function () { message.success('已复制工作流创建提示词，请粘贴到主聊天框发送。'); })
          .catch(function () { message.info('请手动复制提示词到主聊天框发送。'); });
      } else {
        message.info('当前环境不支持自动复制，请手动复制提示词到主聊天框发送。');
      }
    }
    function doGen() {
      if (!state[0] || !state[0].has_workflow) return message.warning('请先绑定工作流');
      if (!prompt[0].trim()) return message.warning('先写提示词～');
      if (prompt[0].length > 2000) return message.warning('提示词太长了，最多2000字符');
      if (neg[0].length > 1000) return message.warning('负向提示词太长了，最多1000字符');
      busy[1](true);
      var p = Object.assign({}, params[0]);
      req('/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        prompt: prompt[0], negative_prompt: neg[0], model_name: model[0],
        steps: p.steps || 20, cfg: p.cfg || 7, seed: p.seed === undefined ? -1 : p.seed,
        width: p.width || 1024, height: p.height || 1024, batch_size: p.batch_size || 1, category: category[0],
        sampler_name: p.sampler_name || 'euler', scheduler: p.scheduler || 'normal', denoise: p.denoise === undefined ? 1 : p.denoise,
        loras: loras[0].filter(function (x) { return x.enabled && x.name; })
      }) }).then(function (r) {
        busy[1](false);
        if (r.success) { message.success('生图成功'); loadImages(); preview[1](r.gallery_id); tab[1]('gallery'); }
        else message.error(r.error || '生图失败');
      }).catch(function (e) { busy[1](false); message.error(e.message); });
    }

    var d = state[0] || {};
    var status = d.status || {};
    var hasWorkflow = !!d.has_workflow;
    var binding = d.binding || null;
    var schema = d.params_schema || {};
    var loraOptions = kvToOptions(d.loras || []);

    return h(React.Fragment, null,
      h('div', { style: { padding: '10px 12px', borderBottom: '1px solid var(--border-color-split)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        h('div', null, h('div', { style: { fontWeight: 700 } }, '✨ 生图助手'), h('div', { style: { fontSize: 10, color: 'var(--ant-color-text-secondary)' } }, '确定性适配面板 · v' + FRONTEND_VERSION)),
        h('div', { style: { display: 'flex', gap: 4 } },
          h(Button, { type: 'text', size: 'small', icon: I && I.ReloadOutlined ? h(I.ReloadOutlined) : null, onClick: function () { message.loading('正在刷新扫描...'); load(model[0], workflowPreset[0]); }, title: '刷新模型/采样器/调度器' }, '刷新'),
          h(Button, { type: 'text', size: 'small', icon: I && I.CloseOutlined ? h(I.CloseOutlined) : null, onClick: function () { setOn(false); emitToggle(false); toggleUI(false); } })
        )
      ),
      h('div', { style: { display: 'flex', borderBottom: '1px solid var(--border-color-split)' } },
        h(Button, { type: tab[0] === 'gen' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0 }, onClick: function () { tab[1]('gen'); } }, '工作流'),
        h(Button, { type: tab[0] === 'gallery' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0 }, onClick: function () { tab[1]('gallery'); loadCategories(); loadImages(); } }, '图库(' + (imgs[0] || []).length + ')')
      ),
      versionMismatch[0] ? h(Alert, { type: 'warning', showIcon: true, message: '缓存版本不一致', description: versionMismatch[0], style: { margin: 10 } }) : null,
      tab[0] === 'gen' ? h('div', { style: { flex: 1, overflowY: 'auto' } },
        h(Section, { title: '1. 主模型', extra: h(Tag, { color: status.connected ? 'green' : 'red' }, status.connected ? 'ComfyUI 已连' : '未连接') },
          h(Select, { size: 'small', value: model[0] || undefined, placeholder: '选择主模型', style: { width: '100%' },
            options: (d.models || []).map(function (m) { return { value: m.name, label: (m.has_workflow ? '✓ ' : '○ ') + m.name }; }),
            onChange: function (v) { model[1](v); load(v, workflowPreset[0]); }
          }),
          h('div', { style: { marginTop: 6, fontSize: 11, color: 'var(--ant-color-text-secondary)' } },
            hasWorkflow ? ('已绑定：' + (binding.workflow_name || binding.workflow_id)) : '此模型还没有绑定工作流')
        ),
        h(Section, { title: '2. 工作流切换', extra: h(Button, { size: 'small', onClick: saveCurrentWorkflow }, '保存当前') },
          h(Select, { size: 'small', value: workflowPreset[0] || undefined, placeholder: '选择默认/自定义工作流', style: { width: '100%' },
            options: (d.workflow_presets || []).map(function (p) { return { value: p.id, label: p.name + (p.model_type ? ' · ' + p.model_type : '') }; }),
            onChange: function (v) { applyPreset(v); }
          }),
          h('div', { style: { marginTop: 6, fontSize: 11, color: 'var(--ant-color-text-secondary)' } },
            binding ? ((binding.workflow_name || binding.workflow_id) + ' · 可直接生图') : '没有可用工作流')
        ),
        !hasWorkflow ? h(Section, { title: '等待工作流' },
          h(Alert, { type: 'info', showIcon: true, message: '点击「一键自动适配」即可开始', description: '插件会自动找到 ComfyUI、扫描模型和 LoRA、选择最优模型并绑定工作流，无需手动操作。' }),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 10 } },
            h(Button, { type: 'primary', size: 'small', onClick: autoBindWorkflow, loading: scanning[0], disabled: scanning[0], block: true }, scanning[0] ? '扫描中...' : '一键自动适配'),
            h(Button, { size: 'small', onClick: scanFailed[0] ? requestAIFindComfyUI : requestAIWorkflow, block: true }, scanFailed[0] ? '复制 AI 提示词（找 ComfyUI）' : '复制 AI 提示词')
          )
        ) : h(React.Fragment, null,
          h(Section, { title: '3. LoRA', extra: binding && Number(binding.supports_lora) ? h(Button, { size: 'small', onClick: addLora }, '+ 添加') : null },
            binding && Number(binding.supports_lora) ? (loras[0].length ? loras[0].map(function (x, i) {
              return h('div', { key: i, style: { padding: 6, marginBottom: 6, border: '1px solid var(--border-color-split)', borderRadius: 6 } },
                h('div', { style: { display: 'flex', gap: 4, alignItems: 'center', marginBottom: 6 } },
                  h(Switch, { size: 'small', checked: x.enabled, onChange: function (v) { updateLora(i, { enabled: v }); } }),
                  h(Select, { size: 'small', value: x.name || undefined, placeholder: '选择 LoRA', options: loraOptions, style: { flex: 1 }, onChange: function (v) { updateLora(i, { name: v }); } }),
                  h(Button, { size: 'small', danger: true, onClick: function () { delLora(i); } }, '删')
                ),
                h('div', { style: { display: 'flex', gap: 6 } },
                  h('div', { style: { flex: 1 } }, h('div', { style: { fontSize: 10 } }, '模型强度'), h(Slider, { min: -2, max: 2, step: 0.05, value: x.strength_model, onChange: function (v) { updateLora(i, { strength_model: v }); } }), h(InputNumber, { size: 'small', value: x.strength_model, step: 0.05, onChange: function (v) { updateLora(i, { strength_model: v }); }, style: { width: '100%' } })),
                  h('div', { style: { flex: 1 } }, h('div', { style: { fontSize: 10 } }, 'CLIP强度'), h(Slider, { min: -2, max: 2, step: 0.05, value: x.strength_clip, onChange: function (v) { updateLora(i, { strength_clip: v }); } }), h(InputNumber, { size: 'small', value: x.strength_clip, step: 0.05, onChange: function (v) { updateLora(i, { strength_clip: v }); }, style: { width: '100%' } }))
                )
              );
            }) : h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: '未添加 LoRA' })) : h(Alert, { type: 'warning', message: '当前工作流未暴露 LoRA 节点' })
          ),
          h(Section, { title: '4. 提示词' },
            h(Input.TextArea, { rows: 3, value: prompt[0], placeholder: '正向提示词...', maxLength: 2000, onChange: function (e) { prompt[1](e.target.value); }, style: { marginBottom: 6, fontSize: 12 } }),
            binding && Number(binding.supports_negative_prompt) ? h(Input.TextArea, { rows: 2, value: neg[0], placeholder: '负向提示词...', maxLength: 1000, onChange: function (e) { neg[1](e.target.value); }, style: { fontSize: 12 } }) : null
          ),
          h(Section, { title: '5. 工作流参数' },
            Object.keys(schema).length ? Object.keys(schema).map(function (k) { return h(ParamControl, { key: k, name: k, def: schema[k], value: params[0][k], setValue: setParam }); }) : h(Alert, { type: 'warning', message: '工作流没有暴露可调参数' }),
            h(Button, { type: 'primary', block: true, loading: busy[0], disabled: busy[0] || !prompt[0].trim(), onClick: doGen }, '✨ 按当前工作流生图')
          ),
          h(Section, { title: '6. 调试信息' },
            h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 8 } },
              '提示词会原样发送到 ComfyUI，不会被AI改写。如果生成结果与预期不符，请检查：'
            ),
            h('ul', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', margin: 0, paddingLeft: 16 } },
              h('li', null, '提示词是否包含特殊字符导致解析错误'),
              h('li', null, '模型是否支持你使用的标签（如 LoRA 触发词）'),
              h('li', null, 'CFG 值是否过高导致过度拟合')
            ),
            h('div', { style: { marginTop: 8, padding: '8px', background: 'var(--ant-color-bg-layout)', borderRadius: 4, fontSize: 11 } },
              h('div', { style: { fontWeight: 700, marginBottom: 4 } }, '最后一次发送的提示词：'),
              h('div', { style: { wordBreak: 'break-all', maxHeight: 80, overflowY: 'auto', whiteSpace: 'pre-wrap' } }, prompt[0] || '（空）')
            ),
            h('div', { style: { marginTop: 8, padding: '8px', background: 'var(--ant-color-bg-layout)', borderRadius: 4, fontSize: 11 } },
              h('div', { style: { fontWeight: 700, marginBottom: 4 } }, 'ComfyUI 画布同步说明：'),
              h('div', { style: { color: 'var(--ant-color-text-secondary)' } },
                '本插件通过 API 提交工作流 JSON 到 ComfyUI 执行，但不会改变 ComfyUI 画布显示。'
              ),
              h('div', { style: { color: 'var(--ant-color-text-secondary)', marginTop: 4 } },
                '如需在 ComfyUI 画布中查看/编辑工作流，请手动在 ComfyUI 中加载对应的 workflow JSON。'
              )
            )
          )
        )
      ) : null,
      tab[0] === 'gallery' ? h(React.Fragment, null,
        h('div', { style: { padding: 8, display: 'flex', gap: 6, borderBottom: '1px solid var(--border-color-split)' } },
          h(Select, { size: 'small', value: category[0], style: { flex: 1 }, options: [{ value: '', label: '全部分类' }].concat(categories[0].map(function (x) { return { value: x, label: x }; })), disabled: galleryLoading[0] || batchMode[0], onChange: switchCategory }),
          h(Button, { size: 'small', onClick: scanGallery, disabled: batchMode[0] }, '扫描'),
          h(Button, { size: 'small', onClick: function () { var n = window.prompt('新建分类名称'); if (!n || !n.trim()) return; req('/gallery/categories/create?name=' + encodeURIComponent(n.trim()), { method: 'POST' }).then(function () { loadCategories(); switchCategory(n.trim()); }).catch(function (e) { message.error(e.message); }); }, disabled: batchMode[0] }, '+ 分类'),
          h(Button, { size: 'small', type: batchMode[0] ? 'primary' : 'default', onClick: function () { batchMode[0] ? exitBatchMode() : batchMode[1](true); } }, batchMode[0] ? '取消' : '批量')
        ),
        batchMode[0] ? h('div', { style: { padding: '6px 8px', display: 'flex', gap: 6, alignItems: 'center', borderBottom: '1px solid var(--border-color-split)', background: 'var(--ant-color-fill-quaternary)' } },
          h('span', { style: { fontSize: 12, whiteSpace: 'nowrap' } }, '已选 ' + selectedIds[0].length + ' 张'),
          h(Button, { size: 'small', disabled: batchBusy[0] || !(imgs[0] || []).length, onClick: function () { selectedIds[1](selectedIds[0].length === imgs[0].length ? [] : imgs[0].map(function (x) { return x.id; })); } }, selectedIds[0].length === imgs[0].length && imgs[0].length ? '取消全选' : '全选当前页'),
          h(Select, { size: 'small', placeholder: '换分类', disabled: batchBusy[0] || !selectedIds[0].length, style: { flex: 1, minWidth: 88 }, options: categories[0].map(function (x) { return { value: x, label: x }; }), onChange: batchMove }),
          h(Button, { size: 'small', danger: true, disabled: batchBusy[0] || !selectedIds[0].length, onClick: batchDelete }, '删除')
        ) : null,
        h('div', { style: { flex: 1, overflowY: 'auto', padding: 8, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, alignContent: 'start' } },
          galleryLoading[0] ? h('div', { style: { gridColumn: '1/-1', paddingTop: 40, textAlign: 'center', color: 'var(--ant-color-text-secondary)' } }, '正在加载「' + (category[0] || '全部分类') + '」…') :
          (imgs[0] || []).length ? imgs[0].map(function (img) {
            var checked = selectedIds[0].indexOf(img.id) >= 0;
            return h('div', { key: img.id, onClick: function () { batchMode[0] ? toggleSelected(img.id) : preview[1](img.id); }, style: { position: 'relative', aspectRatio: '1/1', border: checked ? '2px solid var(--ant-color-primary)' : '1px solid var(--border-color-split)', borderRadius: 6, overflow: 'hidden', cursor: 'pointer', boxSizing: 'border-box' } },
              h('img', { src: iurl(img.id), style: { width: '100%', height: '100%', objectFit: 'cover' } }),
              batchMode[0] ? h('div', { style: { position: 'absolute', top: 5, left: 5, width: 18, height: 18, borderRadius: 10, background: checked ? 'var(--ant-color-primary)' : 'rgba(0,0,0,.5)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, fontWeight: 700 } }, checked ? '✓' : '') : null);
          }) : h('div', { style: { gridColumn: '1/-1', paddingTop: 40 } }, h(Empty, { description: '还没有图片' }))
        )
      ) : null,
      preview[0] ? (function () { var img = (imgs[0] || []).find(function (x) { return x.id === preview[0]; }); if (!img) return null;
        function copyText(t) { if (!t) return; if (navigator.clipboard) { navigator.clipboard.writeText(t).then(function () { message.success('已复制'); }) .catch(function () { message.info(t); }); } else { message.info(t); } }
        function InfoRow(label, value, copyable) {
          if (value === undefined || value === null || value === '') value = '—';
          var display = String(value);
          if (display.length > 120) display = display.substring(0, 120) + '...';
          return h('div', { style: { marginBottom: 6 } },
            h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginBottom: 2 } }, label),
            h('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 6 } },
              h('div', { style: { flex: 1, fontSize: 12, lineHeight: '18px', wordBreak: 'break-all', background: 'var(--ant-color-fill-secondary)', padding: '4px 8px', borderRadius: 4, fontFamily: 'monospace', maxHeight: 80, overflow: 'auto' } }, display),
              copyable ? h(Button, { size: 'small', type: 'text', style: { flexShrink: 0, fontSize: 11 }, onClick: function () { copyText(String(value)); } }, '复制') : null
            )
          );
        }
        var recipeText = (img.prompt || '') + (img.negative_prompt ? '\nNegative: ' + img.negative_prompt : '') + '\nModel: ' + (img.model_name || '') + (img.lora_name ? '\nLoRA: ' + img.lora_name : '') + '\nSteps: ' + (img.steps || 20) + '  CFG: ' + (img.cfg || 7) + '  Seed: ' + (img.seed || -1) + '  Size: ' + (img.width || 1024) + 'x' + (img.height || 1024);
        return h(Modal, { open: true, footer: null, width: 560, onCancel: function () { preview[1](null); },
          styles: { body: { padding: '12px 16px', maxHeight: '80vh', overflowY: 'auto' } } },
          h('img', { src: iurl(img.id), style: { width: '100%', borderRadius: 8, marginBottom: 12 } }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 } },
            h(Rate, { value: img.rating || 0, onChange: function (v) { req('/images/' + img.id + '/rating', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating: v }) }).then(function () { loadImages(category[0]); }).catch(function (e) { message.error(e.message || '评分保存失败'); }); } }),
            h(Button, { size: 'small', onClick: function () { copyText(recipeText); } }, '复制全部参数')
          ),
          h('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginBottom: 10 } },
            h('span', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)' } }, '所属分类'),
            h(Select, { size: 'small', value: img.category || '未分类', options: categories[0].map(function (x) { return { value: x, label: x }; }), style: { flex: 1 }, onChange: function (v) { req('/images/' + img.id + '/category?category=' + encodeURIComponent(v), { method: 'POST' }).then(function () { message.success('已移动到「' + v + '」'); preview[1](null); loadImages(category[0]); }).catch(function (e) { message.error(e.message); }); } })
          ),
          h(Divider, { style: { margin: '4px 0 10px' } }),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 16px' } },
            InfoRow('模型', img.model_name, true),
            InfoRow('LoRA', img.lora_name || '', true),
            InfoRow('尺寸', (img.width || 1024) + ' × ' + (img.height || 1024), false),
            InfoRow('文件大小', img.file_size ? (img.file_size / 1024).toFixed(1) + ' KB' : '', false),
            InfoRow('Steps', img.steps, false),
            InfoRow('CFG', img.cfg, false),
            InfoRow('Seed', img.seed, false),
            InfoRow('生成时间', img.created_at || img.generated_at, false)
          ),
          h(Divider, { style: { margin: '8px 0 10px' } }),
          InfoRow('正向提示词', img.prompt, true),
          InfoRow('反向提示词', img.negative_prompt || '', true)
        );
      })() : null
    );
  }

  function toggleUI(show) {
    var btn = document.getElementById(pid + '-btn');
    var panel = document.getElementById(pid + '-panel');
    if (btn) btn.style.display = show ? '' : 'none';
    if (panel && !show) {
      panel.classList.remove('open');
      if (btn) btn.classList.remove('open');
    }
  }

  function injectUI() {
    if (document.getElementById(pid + '-style')) return;
    var style = document.createElement('style');
    style.id = pid + '-style';
    style.textContent = [
      '#' + pid + '-btn{position:fixed;right:0;top:50%;z-index:999;transform:translateY(-50%);width:22px;height:50px;border:none;background:var(--ant-primary-color,#8EA7FF);color:#fff;border-radius:4px 0 0 4px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:right .3s;box-shadow:-2px 0 8px rgba(0,0,0,.1)}',
      '#' + pid + '-btn.open{right:360px}',
      '#' + pid + '-panel{position:fixed;top:0;right:0;width:360px;height:100vh;z-index:998;background:var(--ant-color-bg-container,#fff);border-left:1px solid var(--border-color-split,#e8e8e8);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s;box-shadow:-4px 0 20px rgba(0,0,0,.08)}',
      '#' + pid + '-panel.open{transform:translateX(0)}'
    ].join('\n');
    document.head.appendChild(style);
    var btn = document.createElement('button');
    btn.id = pid + '-btn'; btn.textContent = '✨'; btn.title = '生图助手'; btn.style.display = isOn() ? '' : 'none';
    var panel = document.createElement('div'); panel.id = pid + '-panel';
    document.body.appendChild(btn); document.body.appendChild(panel);
    btn.onclick = function () {
      panel.classList.toggle('open'); btn.classList.toggle('open');
    };
    var RD = window.ReactDOM || Q.host.ReactDOM;
    if (RD && RD.createRoot) RD.createRoot(panel).render(h(ErrorBoundary, null, h(GenPanel))); else if (RD) RD.render(h(ErrorBoundary, null, h(GenPanel)), panel);
    onToggle(toggleUI);
  }
  if (document.readyState === 'complete') injectUI(); else window.addEventListener('load', injectUI);

  if (Q.menu && Q.route) {
    function RepairPanel() {
      var repairing = React.useState(false);
      var step1 = React.useState(false);   // 第一次确认弹窗
      var step2 = React.useState(false);   // 第二次最终确认弹窗
      var result = React.useState(null);

      function doRepair() {
        repairing[1](true);
        req('/repair', { method: 'POST' }).then(function (r) {
          result[1](r);
          repairing[1](false);
          step2[1](false);
          message.success(r.message || '已恢复出厂设置');
          scanFailed[1](false);
          setTimeout(function () { window.location.reload(); }, 3000);
        }).catch(function (e) {
          repairing[1](false);
          message.error('修复失败：' + e.message);
        });
      }

      return h('div', { style: { padding: 40, maxWidth: 600 } },
        h('h2', null, '🛠️ 生图助手 · 设置'),
        h('p', { style: { color: 'var(--ant-color-text-secondary)', marginBottom: 20 } }, '版本：v' + FRONTEND_VERSION),
        h(Divider, null),
        h('h3', { style: { color: 'var(--ant-color-error)' } }, '⚠️ 强制修复'),
        h('p', null, '强制修复会清空所有数据，恢复到刚安装插件时的状态：'),
        h('ul', { style: { marginBottom: 20 } },
          h('li', null, '❌ 删除所有工作流绑定'),
          h('li', null, '❌ 删除所有工作流预设'),
          h('li', null, '❌ 删除所有图库图片（含文件）'),
          h('li', null, '❌ 重置 ComfyUI 连接配置'),
          h('li', null, '❌ 清除所有生图配方')
        ),
        h('p', { style: { color: 'var(--ant-color-error)', fontWeight: 700, fontSize: 14 } }, '⚠️ 此操作不可撤销！数据将永久丢失！'),
        h(Button, {
          type: 'primary', danger: true,
          onClick: function () { step1[1](true); },
          loading: repairing[0], disabled: repairing[0]
        }, '强制修复'),
        result[0] ? h(Alert, {
          type: 'success', showIcon: true,
          message: '修复完成',
          description: result[0].message + '（3秒后自动刷新页面）',
          style: { marginTop: 20 }
        }) : null,

        // ── 第一步确认弹窗 ──
        h(Modal, {
          title: '⚠️ 第一步确认：了解后果',
          open: step1[0],
          onOk: function () { step1[1](false); step2[1](true); },
          onCancel: function () { step1[1](false); },
          okText: '我已知晓后果，继续',
          okButtonProps: { danger: true },
          cancelText: '取消',
          width: 520
        },
          h('p', { style: { fontSize: 14, marginBottom: 12 } }, '强制修复将执行以下操作：'),
          h('ul', { style: { lineHeight: 2 } },
            h('li', null, '🗑️ 删除所有工作流绑定（模型与工作流的关联）'),
            h('li', null, '🗑️ 删除所有工作流预设（自定义保存的预设）'),
            h('li', null, '🗑️ 删除所有图库图片及记录（图片文件一并删除）'),
            h('li', null, '🗑️ 重置 ComfyUI 连接配置（恢复默认端口 8188）'),
            h('li', null, '🗑️ 清除所有生图配方（保存的参数组合）')
          ),
          h('p', { style: { color: 'var(--ant-color-error)', fontWeight: 700, marginTop: 16, fontSize: 14 } },
            '⚠️ 以上所有数据将被永久删除，无法恢复！'),
          h('p', { style: { color: 'var(--ant-color-text-secondary)', marginTop: 8 } },
            '点击「我已知晓后果，继续」进入最终确认。')
        ),

        // ── 第二步最终确认弹窗 ──
        h(Modal, {
          title: '🔴 第二步：最终确认',
          open: step2[0],
          onOk: doRepair,
          onCancel: function () { step2[1](false); },
          confirmLoading: repairing[0],
          okText: '确认修复，删除所有数据',
          okButtonProps: { danger: true },
          cancelText: '取消',
          width: 520
        },
          h('p', { style: { fontSize: 14, fontWeight: 700, color: 'red' } },
            '这是最后一次确认机会。'),
          h('p', { style: { marginTop: 12 } },
            '点击「确认修复，删除所有数据」后：'),
          h('ul', { style: { lineHeight: 2 } },
            h('li', null, '所有绑定、预设、图库、配方将立即删除'),
            h('li', null, '图片文件将被彻底清除'),
            h('li', null, '配置将恢复为默认值'),
            h('li', null, '页面将在 3 秒后自动刷新')
          ),
          h('p', { style: { color: 'red', fontWeight: 700, marginTop: 16, fontSize: 14 } },
            '🔴 此操作不可撤销！请确认你确实要删除所有数据。')
        )
      );
    }
    Q.route.add(pid, { id: pid + '.settings', path: '/image-gen-settings', component: RepairPanel });
    function MenuSwitch() {
      var checked = React.useState(isOn());
      return h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' } },
        h('span', null, '✨ 生图助手'),
        h(Switch, { size: 'small', checked: checked[0], onClick: function(e){e.stopPropagation();e.preventDefault();}, onChange: function(v,e){ if(e){e.stopPropagation();e.preventDefault();} checked[1](v); setOn(v); emitToggle(v); } })
      );
    }
    Q.menu.add(pid, { id: pid + '.menu', label: h(MenuSwitch), route: pid + '.settings', location: 'primary.agentScoped', order: 66 });
  }
})();
