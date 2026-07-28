/**
 * QwenPaw 生图助手 — v0.4.1
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
  var FRONTEND_VERSION = '0.4.1';
  var versionWarning = { current: '' };

  function req(p, o) {
    o = o || {};
    o.headers = Object.assign({ 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' }, o.headers || {});
    return Q.host.fetch('/image-gen' + p, o).then(function (r) {
      return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || '请求失败'); return b; });
    });
  }
  function iurl(id) { return '/image-gen/images/' + id + '/file'; }
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

  function GenPanel() {
    var s = React.useState;
    var state = s(null), model = s(''), tab = s('gen'), imgs = s([]), preview = s(null), busy = s(false);
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
    function loadImages() { req('/images').then(function (d) { imgs[1](d.items || []); }).catch(function () {}); }
    React.useEffect(function () {
      load();
      loadImages();
      req('/version?_=' + Date.now()).then(function (v) {
        if (v && v.version && v.version !== FRONTEND_VERSION) {
          var msg = '检测到生图助手前端缓存未更新：前端 v' + FRONTEND_VERSION + ' / 后端 v' + v.version + '。请完全退出并重启 QwenPaw Desktop；若仍旧，请清理桌面端 WebView Cache 和 Code Cache。';
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
      busy[1](true);
      var p = Object.assign({}, params[0]);
      req('/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        prompt: prompt[0], negative_prompt: neg[0], model_name: model[0],
        steps: p.steps || 20, cfg: p.cfg || 7, seed: p.seed === undefined ? -1 : p.seed,
        width: p.width || 1024, height: p.height || 1024, batch_size: p.batch_size || 1,
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
        h('div', null, h('div', { style: { fontWeight: 700 } }, '✨ 生图助手'), h('div', { style: { fontSize: 10, color: 'var(--ant-color-text-secondary)' } }, '工作流驱动面板 · v' + FRONTEND_VERSION)),
        h(Button, { type: 'text', size: 'small', icon: I && I.CloseOutlined ? h(I.CloseOutlined) : null, onClick: function () { setOn(false); emitToggle(false); toggleUI(false); } })
      ),
      h('div', { style: { display: 'flex', borderBottom: '1px solid var(--border-color-split)' } },
        h(Button, { type: tab[0] === 'gen' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0 }, onClick: function () { tab[1]('gen'); } }, '工作流'),
        h(Button, { type: tab[0] === 'gallery' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0 }, onClick: function () { tab[1]('gallery'); } }, '图库(' + (imgs[0] || []).length + ')')
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
          h(Alert, { type: 'info', showIcon: true, message: '先让 AI 为这个模型创建 ComfyUI 工作流', description: '没有工作流时，面板不会显示 LoRA、提示词和具体参数，避免出现“看似能调但实际不对应节点”的假表单。' }),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 10 } },
            h(Button, { type: 'primary', size: 'small', onClick: requestAIWorkflow, block: true }, '复制/填入 AI 工作流提示词')
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
            h(Input.TextArea, { rows: 3, value: prompt[0], placeholder: '正向提示词...', onChange: function (e) { prompt[1](e.target.value); }, style: { marginBottom: 6, fontSize: 12 } }),
            binding && Number(binding.supports_negative_prompt) ? h(Input.TextArea, { rows: 2, value: neg[0], placeholder: '负向提示词...', onChange: function (e) { neg[1](e.target.value); }, style: { fontSize: 12 } }) : null
          ),
          h(Section, { title: '5. 工作流参数' },
            Object.keys(schema).length ? Object.keys(schema).map(function (k) { return h(ParamControl, { key: k, name: k, def: schema[k], value: params[0][k], setValue: setParam }); }) : h(Alert, { type: 'warning', message: '工作流没有暴露可调参数' }),
            h(Button, { type: 'primary', block: true, loading: busy[0], disabled: busy[0] || !prompt[0].trim(), onClick: doGen }, '✨ 按当前工作流生图')
          )
        )
      ) : null,
      tab[0] === 'gallery' ? h('div', { style: { flex: 1, overflowY: 'auto', padding: 8, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, alignContent: 'start' } },
        (imgs[0] || []).length ? imgs[0].map(function (img) {
          return h('div', { key: img.id, onClick: function () { preview[1](img.id); }, style: { aspectRatio: '1/1', border: '1px solid var(--border-color-split)', borderRadius: 6, overflow: 'hidden', cursor: 'pointer' } },
            h('img', { src: iurl(img.id), style: { width: '100%', height: '100%', objectFit: 'cover' } }));
        }) : h('div', { style: { gridColumn: '1/-1', paddingTop: 40 } }, h(Empty, { description: '还没有图片' }))
      ) : null,
      preview[0] ? (function () { var img = (imgs[0] || []).find(function (x) { return x.id === preview[0]; }); return img ? h(Modal, { open: true, footer: null, width: 520, onCancel: function () { preview[1](null); } },
        h('img', { src: iurl(img.id), style: { maxWidth: '100%', borderRadius: 8 } }),
        h('div', { style: { marginTop: 8, textAlign: 'center' } }, h(Rate, { value: img.rating || 0, onChange: function (v) { req('/images/' + img.id + '/rating', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating: v }) }).then(loadImages); } }))
      ) : null; })() : null
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
    if (RD && RD.createRoot) RD.createRoot(panel).render(h(GenPanel)); else if (RD) RD.render(h(GenPanel), panel);
    onToggle(toggleUI);
  }
  if (document.readyState === 'complete') injectUI(); else window.addEventListener('load', injectUI);

  if (Q.menu && Q.route) {
    Q.route.add(pid, { id: pid + '.settings', path: '/image-gen-settings', component: function () { return h('div', { style: { padding: 40 } }, '生图助手由左侧开关控制，右侧 ✨ 按钮展开面板。'); } });
    function MenuSwitch() {
      var checked = React.useState(isOn());
      return h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' }, onClick: function(e){e.stopPropagation();e.preventDefault();} },
        h('span', null, '✨ 生图助手'),
        h(Switch, { size: 'small', checked: checked[0], onChange: function(v,e){ if(e){e.stopPropagation();e.preventDefault();} checked[1](v); setOn(v); emitToggle(v); } })
      );
    }
    Q.menu.add(pid, { id: pid + '.menu', label: h(MenuSwitch), route: pid + '.settings', location: 'primary.agentScoped', order: 66 });
  }
})();
