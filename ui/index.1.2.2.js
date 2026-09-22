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
      return r.json().catch(function () { return {}; }).then(function (b) {
        if (!r.ok) {
          var detail = b && b.detail;
          if (detail && typeof detail === 'object') detail = detail.error || detail.message || JSON.stringify(detail);
          throw new Error(detail || ('请求失败（HTTP ' + r.status + '）'));
        }
        return b;
      });
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
    var state = s(null), model = s(''), tab = s('gen'), task = s(null), review = s(null), settingsTick = s(0), upscaleProfile = s('anime_6b'), upscaleCategory = s('未分类'), upscaleProfiles = s([]), upscaleQueue = s([]), upscaleQueueLoading = s(false), imgs = s([]), preview = s(null), busy = s(false), scanning = s(false), galleryLoading = s(false), galleryHasMore = s(false), galleryTotal = s(0), category = s('未分类'), categories = s(['未分类']), batchMode = s(false), selectedIds = s([]), batchBusy = s(false), gallerySort = s('newest'), galleryModel = s(''), galleryLora = s(''), galleryMinRating = s(0), galleryFilterOptions = s({ models: [], loras: [] }), genMode = s('txt2img'), refImage = s(null), refDenoise = s(0.6), refFollowSize = s(true), pickingRef = s(false), refBusy = s(false), imageCache = s({});
    var galleryRequest = React.useRef(0);
    var scanFailed = s(false);  // 扫描失败时置 true，让「复制 AI 提示词」按钮切换为找 ComfyUI 的提示词
    var versionMismatch = s('');
    var prompt = s(''), neg = s(''), loras = s([]), params = s({}), workflowPreset = s(0), clipName = s(''), vaeName = s(''), portable = s(null);
    var promptLib = s(null), promptLibOpen = s(false), tagQuery = s(''), aiIdea = s('');
    // ── API 生图（接入第三方图像 API）────────────────────────────────────────
    var apiCfg = s(null), apiModel = s(''), apiPrompt = s(''), apiNeg = s(''), apiParams = s({}),
        apiRefs = s([]), apiRefBusy = s(false), apiTask = s(null), apiSubmitting = s(false),
        apiCat = s('未分类'), apiPickingRef = s(false), apiMsg = s(''),
        apiProvForm = s(null), apiModelForm = s(null), apiTesting = s('');

    // ── API 生图：配置 / 生成 / 进度 ────────────────────────────────────────
    function apiSchemaDefaults(m) {
      var out = {}, schema = (m && m.params) || {};
      Object.keys(schema).forEach(function (k) {
        var spec = schema[k] || {};
        if (spec.default !== undefined) out[k] = spec.default;
      });
      return out;
    }
    function apiCurrentModel() {
      var list = ((apiCfg[0] || {}).models || []);
      for (var i = 0; i < list.length; i++) { if (list[i].id === apiModel[0]) return list[i]; }
      return null;
    }
    function loadApiConfig() {
      return req('/api-image/config').then(function (d) {
        apiCfg[1](d);
        var models = d.models || [];
        if (!apiModel[0] && models.length) { apiModel[1](models[0].id); apiParams[1](apiSchemaDefaults(models[0])); }
        return d;
      }).catch(function (e) { message.error(e.message); return null; });
    }
    function apiPickModel(id) {
      apiModel[1](id);
      var list = ((apiCfg[0] || {}).models || []);
      for (var i = 0; i < list.length; i++) {
        if (list[i].id === id) { apiParams[1](apiSchemaDefaults(list[i])); return; }
      }
    }
    function apiSetParam(key, value) {
      var next = Object.assign({}, apiParams[0]); next[key] = value; apiParams[1](next);
    }
    function startApiGen() {
      if (!apiModel[0]) return message.warning('请先选择模型');
      if (!(apiPrompt[0] || '').trim()) return message.warning('请先填写提示词');
      var payload = {
        model_id: apiModel[0], prompt: apiPrompt[0], negative_prompt: apiNeg[0],
        params: apiParams[0],
        ref_image_ids: (apiRefs[0] || []).filter(function (r) { return r.image_id; }).map(function (r) { return r.image_id; }),
        ref_local_paths: (apiRefs[0] || []).filter(function (r) { return r.local_path; }).map(function (r) { return r.local_path; }),
        category: apiCat[0] || '未分类'
      };
      apiSubmitting[1](true); apiMsg[1]('提交中');
      req('/api-image/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then(function (d) { apiTask[1]({ id: d.task_id, state: 'queued', message: '排队中' }); pollApiTask(d.task_id); })
        .catch(function (e) { apiSubmitting[1](false); apiMsg[1](''); message.error(e.message || '提交失败'); });
    }
    function pollApiTask(taskId) {
      req('/api-image/tasks/' + taskId + '?_=' + Date.now()).then(function (t) {
        apiTask[1](t); apiMsg[1](t.message || '');
        if (t.state === 'queued' || t.state === 'running') {
          window.setTimeout(function () { pollApiTask(taskId); }, 1500);
          return;
        }
        apiSubmitting[1](false);
        if (t.state === 'completed') {
          message.success('生成完成，已保存到图库');
          loadImages();
          if (reviewPolicyAllows((t.gallery_ids || []).length)) openReview(t.gallery_ids || []);
        } else {
          message.error(t.message || '生成失败');
        }
      }).catch(function (e) { apiSubmitting[1](false); message.error(e.message || '读取进度失败'); });
    }
    function stopApiTask() {
      if (!apiTask[0] || !apiTask[0].id) return;
      req('/api-image/tasks/' + apiTask[0].id + '/stop', { method: 'POST' })
        .then(function () { message.info('已请求取消'); }).catch(function (e) { message.error(e.message); });
    }
    function apiAddRefFromGallery(img) {
      var cur = apiRefs[0] || [];
      if (cur.some(function (r) { return r.image_id === img.id; })) return message.info('这张已经在参考图里了');
      apiRefs[1](cur.concat([{ image_id: img.id, name: img.file_name || ('#' + img.id) }]));
      message.success('已添加参考图');
    }
    function apiUploadRef(file) {
      if (!file) return;
      var fd = new FormData(); fd.append('image', file);
      apiRefBusy[1](true);
      Q.host.fetch('/image-gen/img2img/upload', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          if (!d || !d.success) throw new Error((d && d.detail) || '上传失败');
          apiRefs[1]((apiRefs[0] || []).concat([{ local_path: d.local_path, name: d.file_name || file.name }]));
          message.success('已添加参考图');
        })
        .catch(function (e) { message.error(e.message || '上传失败'); })
        .then(function () { apiRefBusy[1](false); });
    }
    function apiRemoveRef(idx) {
      var next = (apiRefs[0] || []).slice(); next.splice(idx, 1); apiRefs[1](next);
    }
    function saveApiProvider(form) {
      if (!form) return;
      req('/api-image/providers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) })
        .then(function () { message.success('服务已保存'); apiProvForm[1](null); loadApiConfig(); })
        .catch(function (e) { message.error(e.message || '保存失败'); });
    }
    function deleteApiProvider(id, name) {
      if (!window.confirm('删除服务「' + name + '」？挂在它下面的模型条目也会一并移除。')) return;
      req('/api-image/providers/' + id, { method: 'DELETE' })
        .then(function (r) { message.success('已删除，同时移除 ' + (r.removed_models || 0) + ' 个模型条目'); loadApiConfig(); })
        .catch(function (e) { message.error(e.message || '删除失败'); });
    }
    function testApiProvider(id) {
      apiTesting[1](id);
      req('/api-image/providers/' + id + '/test', { method: 'POST' })
        .then(function (r) { r.ok ? message.success(r.message) : message.warning(r.message); })
        .catch(function (e) { message.error(e.message || '测试失败'); })
        .then(function () { apiTesting[1](''); });
    }
    function saveApiModel(form) {
      if (!form) return;
      req('/api-image/models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) })
        .then(function () { message.success('模型已保存'); apiModelForm[1](null); loadApiConfig(); })
        .catch(function (e) { message.error(e.message || '保存失败'); });
    }
    function deleteApiModel(id, label) {
      if (!window.confirm('删除模型条目「' + label + '」？')) return;
      req('/api-image/models/' + id, { method: 'DELETE' })
        .then(function () { message.success('已删除'); loadApiConfig(); })
        .catch(function (e) { message.error(e.message || '删除失败'); });
    }

    // ── API 生图：面板渲染 ─────────────────────────────────────────────────
    function renderApiParam(key, spec) {
      var label = spec.label || key;
      var value = apiParams[0][key];
      var control;
      if (spec.type === 'enum') {
        control = h(Select, {
          size: 'small', style: { width: '100%' }, value: value,
          options: (spec.options || []).map(function (o) { return { value: o, label: String(o) }; }),
          onChange: function (v) { apiSetParam(key, v); }
        });
      } else {
        control = h(InputNumber, {
          size: 'small', style: { width: '100%' }, value: value,
          min: spec.min, max: spec.max,
          onChange: function (v) { apiSetParam(key, v); }
        });
      }
      return h('div', { key: key, style: { marginBottom: 8 } },
        h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 3 } }, label),
        control);
    }

    function renderApiTab() {
      var cfg = apiCfg[0] || {}, providers = cfg.providers || [], models = cfg.models || [];
      var cur = apiCurrentModel();

      if (!providers.length || !models.length) {
        return h('div', { style: { flex: 1, overflowY: 'auto', padding: 8 } },
          h(Alert, {
            type: 'info', showIcon: true, message: '还没有配置 API 生图',
            description: 'API 生图通过第三方图像接口出图，需要先在「设置 → API 生图服务」里添加服务地址与密钥，再添加要使用的模型条目。',
            action: h(Button, { size: 'small', type: 'primary', onClick: function () { tab[1]('settings'); loadApiConfig(); } }, '去设置')
          }),
          h(Section, { title: 'API 生图能做什么' },
            h('div', { style: { fontSize: 12, lineHeight: '20px', color: 'var(--ant-color-text-secondary)' } },
              '· 接任意兼容 OpenAI 图片接口或异步媒体接口的服务\n' +
              '· 生成的图片直接进图库，分类 / 星级 / 备注 / 产物库联动全部照常\n' +
              '· 支持文生图、图生图、多张参考图、比例与分辨率、画质档、透明底等能力（取决于你配置的模型）'))
        );
      }

      var groups = providers.map(function (p) {
        return {
          label: p.name || '未命名服务',
          options: models.filter(function (m) { return m.provider_id === p.id; })
            .map(function (m) { return { value: m.id, label: m.label || m.model }; })
        };
      }).filter(function (g) { return g.options.length; });

      var paramKeys = cur && cur.params ? Object.keys(cur.params) : [];
      var refs = apiRefs[0] || [];
      var running = apiTask[0] && (apiTask[0].state === 'queued' || apiTask[0].state === 'running');

      return h('div', { style: { flex: 1, overflowY: 'auto', padding: '0 4px' } },
        h(Section, { title: '模型' },
          h(Select, {
            size: 'small', value: apiModel[0] || undefined, placeholder: '选择模型',
            style: { width: '100%' }, options: groups, onChange: apiPickModel
          }),
          cur ? h('div', { style: { marginTop: 6, fontSize: 11, color: 'var(--ant-color-text-secondary)', lineHeight: '17px' } },
            '服务：' + (cur.provider_name || '—') + '\n' +
            '协议：' + (cur.protocol === 'auto' ? '自动' : cur.protocol === 'media' ? '异步媒体' : 'OpenAI 同步') +
            '　模型名：' + cur.model) : null
        ),

        paramKeys.length ? h(Section, { title: '参数' }, paramKeys.map(function (k) {
          return renderApiParam(k, cur.params[k] || {});
        })) : null,

        h(Section, { title: '提示词' },
          h(Input.TextArea, {
            rows: 3, value: apiPrompt[0], placeholder: '描述你想生成的画面…',
            onChange: function (e) { apiPrompt[1](e.target.value); }
          }),
          h(Input.TextArea, {
            rows: 2, style: { marginTop: 6 }, value: apiNeg[0],
            placeholder: '负向提示词（可选；服务不支持独立负向字段时会自动并入正向）',
            onChange: function (e) { apiNeg[1](e.target.value); }
          })
        ),

        h(Section, {
          title: '参考图（可选）',
          extra: h('div', { style: { display: 'flex', gap: 4 } },
            h(Button, {
              size: 'small',
              onClick: function () { apiPickingRef[1](true); tab[1]('gallery'); loadImages(); message.info('在图库点任意图片即加入参考图'); }
            }, '从图库选'),
            h('label', {
              style: { fontSize: 12, cursor: 'pointer', padding: '3px 8px', border: '1px solid var(--border-color-split)', borderRadius: 4, lineHeight: '18px' }
            }, '本地上传',
              h('input', {
                type: 'file', accept: '.png,.jpg,.jpeg,.webp', style: { display: 'none' },
                onChange: function (e) { var f = e.target.files && e.target.files[0]; if (f) apiUploadRef(f); e.target.value = ''; }
              }))
          )
        },
          refs.length ? h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6 } },
            refs.map(function (r, i) {
              return h('div', {
                key: i,
                style: { position: 'relative', width: 62, height: 62, borderRadius: 6, overflow: 'hidden', border: '1px solid var(--border-color-split)', background: 'var(--ant-color-fill-quaternary)' }
              },
                r.image_id ? h('img', { src: iurl(r.image_id), style: { width: '100%', height: '100%', objectFit: 'cover' } }) : null,
                h(Button, {
                  size: 'small', danger: true,
                  style: { position: 'absolute', top: 0, right: 0, padding: '0 4px', height: 17, minWidth: 17, fontSize: 10, lineHeight: '15px' },
                  onClick: function () { apiRemoveRef(i); }
                }, '×'));
            })) : h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)' } },
            apiRefBusy[0] ? '上传中…' : '未添加参考图（是否支持取决于你配置的模型能力）')
        ),

        h(Section, { title: '保存到分类' },
          h(Select, {
            size: 'small', style: { width: '100%' }, value: apiCat[0],
            options: (categories[0] || ['未分类']).map(function (c) { return { value: c, label: c }; }),
            onChange: function (v) { apiCat[1](v); }
          })
        ),

        h('div', { style: { padding: '4px 0 16px' } },
          h(Button, {
            type: 'primary', block: true, loading: apiSubmitting[0], disabled: apiSubmitting[0],
            onClick: startApiGen
          }, apiSubmitting[0] ? '生成中…' : '开始生成'),
          apiMsg[0] ? h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginTop: 6, textAlign: 'center' } }, apiMsg[0]) : null,
          running ? h(Button, { size: 'small', danger: true, block: true, style: { marginTop: 6 }, onClick: stopApiTask },
            (cur && cur.protocol === 'media') ? '取消任务' : '停止等待') : null,
          h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginTop: 8, lineHeight: '17px' } },
            '提示：同步协议会一直等到出图（常见 1~3 分钟），此时「停止等待」只结束界面等待，服务方可能已出图并计费；异步协议可真正取消任务。生成的图片会保存到图库，可继续用星级、备注、放大等功能。')
        )
      );
    }

    // ── API 生图：设置页管理 ───────────────────────────────────────────────
    function renderApiSettings() {
      var cfg = apiCfg[0] || {}, providers = cfg.providers || [], models = cfg.models || [];
      var templates = cfg.templates || [];

      return h(React.Fragment, null,
        h(Section, {
          title: 'API 生图服务',
          extra: h(Button, { size: 'small', type: 'primary', onClick: function () { apiProvForm[1]({ id: '', name: '', base_url: '', api_key: '', enabled: true }); } }, '+ 添加')
        },
          providers.length ? providers.map(function (p) {
            return h('div', {
              key: p.id,
              style: { border: '1px solid var(--border-color-split)', borderRadius: 6, padding: '8px 10px', marginBottom: 8, background: 'var(--ant-color-fill-quaternary)' }
            },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
                h('b', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, p.name || '未命名服务'),
                p.enabled === false ? h(Tag, { color: 'default' }, '已停用') : h(Tag, { color: 'green' }, '启用')),
              h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginTop: 4, wordBreak: 'break-all' } }, p.base_url),
              h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginTop: 2 } },
                p.api_key_set ? ('密钥已配置 ' + (p.api_key_hint || '')) : '未配置密钥'),
              h('div', { style: { display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' } },
                h(Button, { size: 'small', onClick: function () { apiProvForm[1]({ id: p.id, name: p.name, base_url: p.base_url, api_key: '', enabled: p.enabled !== false }); } }, '编辑'),
                h(Button, { size: 'small', loading: apiTesting[0] === p.id, onClick: function () { testApiProvider(p.id); } }, '测试连接'),
                h(Button, { size: 'small', danger: true, onClick: function () { deleteApiProvider(p.id, p.name); } }, '删除'))
            );
          }) : h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)' } },
            '还没有服务。添加一个服务地址（如 https://api.example.com/v1）与密钥即可开始。')
        ),

        h(Section, {
          title: 'API 模型条目',
          extra: h(Button, {
            size: 'small', type: 'primary', disabled: !providers.length,
            onClick: function () { apiModelForm[1]({ id: '', provider_id: (providers[0] || {}).id || '', model: '', label: '', protocol: 'auto', capabilities: ['txt2img'], params: {}, default_negative: '', enabled: true }); }
          }, '+ 添加')
        },
          models.length ? models.map(function (m) {
            var caps = (m.capabilities || []).map(function (c) {
              var names = { txt2img: '文生图', img2img: '图生图', multi_ref: '多参考图', transparent: '透明底', group: '组图' };
              return names[c] || c;
            });
            return h('div', {
              key: m.id,
              style: { border: '1px solid var(--border-color-split)', borderRadius: 6, padding: '8px 10px', marginBottom: 8, background: 'var(--ant-color-fill-quaternary)' }
            },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
                h('b', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, m.label || m.model),
                m.enabled === false ? h(Tag, { color: 'default' }, '已停用') : null),
              h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginTop: 4 } },
                (m.provider_name || '—') + '　模型名：' + m.model),
              h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginTop: 2 } },
                '协议：' + (m.protocol === 'auto' ? '自动' : m.protocol === 'media' ? '异步媒体' : 'OpenAI 同步') +
                (caps.length ? '　能力：' + caps.join('/') : '')),
              h('div', { style: { display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' } },
                h(Button, { size: 'small', onClick: function () { apiModelForm[1](JSON.parse(JSON.stringify(m))); } }, '编辑'),
                h(Button, { size: 'small', danger: true, onClick: function () { deleteApiModel(m.id, m.label || m.model); } }, '删除'))
            );
          }) : h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)' } },
            '还没有模型条目。添加时可以先套用参数模板，再填服务方文档里的模型名。')
        ),

        h(Section, { title: '关于 API 生图' },
          h('div', { style: { fontSize: 12, lineHeight: '20px', color: 'var(--ant-color-text-secondary)' } },
            '· 服务地址与密钥只保存在本机插件数据库，不会上传到任何地方\n' +
            '· 支持两类接口：OpenAI 兼容同步接口、异步媒体接口（创建任务 + 轮询）\n' +
            '· 协议选「自动」时会先试同步接口，若服务方明确提示该模型不支持此端点，再自动改用异步接口\n' +
            '· 参数模板只描述参数结构，具体模型名请照服务方文档填写'))
      );
    }
    // 放大 / 高清修复参数
    var upscaleMode = s('fast'), upscaleScale = s(0), upscaleDenoise = s(0.35), upscaleSteps = s(15),
        upscaleCfg = s(0), upscaleSeedMode = s('source'), upscaleTile = s(512), upscaleSeam = s('None'), upscaleStarting = s(false), upscalePromptMode = s('auto'), upscaleSharpen = s(0.08), upscaleFaceDetail = s(false), upscaleIterations = s(2);

    function load(m, presetId) {
      var qs = new URLSearchParams();
      if (m) qs.set('model_name', m);
      if (presetId) qs.set('workflow_preset_id', presetId);
      // 返回 promise，并把新 schema 的默认值交回调用方（复刻时需要在此基础上回填图片参数）。
      return req('/workflow-state' + (qs.toString() ? '?' + qs.toString() : '')).then(function (d) {
        state[1](d);
        var chosen = d.selected_model || '';
        model[1](chosen);
        workflowPreset[1](Number(d.selected_preset_id || presetId || 0));
        var defaults = schemaDefault(d.params_schema || {});
        params[1](defaults);
        clipName[1](d.clip_name || '');
        vaeName[1](d.vae_name || '');
        promptLib[1](null);
        if (promptLibOpen[0]) loadPromptLib(chosen);
        return defaults;
      }).catch(function (e) { message.error(e.message); return null; });
    }
    function loadImages(targetCategory, append) {
      var selected = targetCategory === undefined ? category[0] : targetCategory;
      var offset = append ? (imgs[0] || []).length : 0;
      if (!append) { imgs[1]([]); galleryHasMore[1](false); galleryTotal[1](0); }
      var requestId = ++galleryRequest.current;
      galleryLoading[1](true);
      var qs = '/images?category=' + encodeURIComponent(selected) + '&limit=40&offset=' + offset + '&sort=' + encodeURIComponent(gallerySort[0] || 'newest') + '&_=' + Date.now();
      if (galleryModel[0]) qs += '&model_name=' + encodeURIComponent(galleryModel[0]);
      if (galleryLora[0]) qs += '&lora_name=' + encodeURIComponent(galleryLora[0]);
      if (Number(galleryMinRating[0] || 0) > 0) qs += '&min_rating=' + Number(galleryMinRating[0]);
      req(qs).then(function (d) {
        if (requestId !== galleryRequest.current) return;
        var items = d.items || [];
        imgs[1](append ? (imgs[0] || []).concat(items) : items);
        galleryHasMore[1](d.has_more === true);
        galleryTotal[1](d.total || 0);
        cacheImages(items);
      }).catch(function (e) { if (requestId === galleryRequest.current) message.error(e.message || '图库读取失败'); })
        .then(function () { if (requestId === galleryRequest.current) galleryLoading[1](false); });
    }
    function loadGalleryFilters(targetCategory) {
      var selected = targetCategory === undefined ? category[0] : targetCategory;
      req('/gallery/filters?category=' + encodeURIComponent(selected) + '&_=' + Date.now())
        .then(function (d) { galleryFilterOptions[1]({ models: (d && d.models) || [], loras: (d && d.loras) || [] }); })
        .catch(function () {});
    }
    function reloadGallery() { loadGalleryFilters(); loadImages(); }
    function switchCategory(v) { category[1](v); selectedIds[1]([]); galleryModel[1](''); galleryLora[1](''); galleryMinRating[1](0); loadGalleryFilters(v); loadImages(v); }
    function loraLines(value) { return String(value || '').split(/;\s*/).map(function (x) { return x.trim(); }).filter(Boolean); }
    function fileSizeMB(bytes) { return bytes ? (Number(bytes) / 1024 / 1024).toFixed(2) + ' MB' : '—'; }
    function fmtTime(value) {
      if (!value) return '—';
      var t = Date.parse(String(value).replace(/-/g, '/'));
      if (isNaN(t)) return String(value);
      var diff = Date.now() - t;
      if (diff < 0) diff = 0;
      var m = Math.floor(diff / 60000);
      if (m < 1) return '刚刚';
      if (m < 60) return m + ' 分钟前';
      var h = Math.floor(m / 60);
      if (h < 24) return h + ' 小时前';
      var d = Math.floor(h / 24);
      if (d < 7) return d + ' 天前';
      var dt = new Date(t);
      function p(x) { return (x < 10 ? '0' : '') + x; }
      return dt.getFullYear() + '-' + p(dt.getMonth() + 1) + '-' + p(dt.getDate()) + ' ' + p(dt.getHours()) + ':' + p(dt.getMinutes());
    }
    function galleryItems() {
      // v1.0.9：排序/筛选已下放到后端 SQL（分页一致），前端不再本地过滤排序。
      return (imgs[0] || []).slice();
    }
    function toggleSelected(id) { selectedIds[1](function (old) { return old.indexOf(id) >= 0 ? old.filter(function (x) { return x !== id; }) : old.concat([id]); }); }
    function exitBatchMode() { batchMode[1](false); selectedIds[1]([]); }
    function batchMove(target) {
      if (!selectedIds[0].length) return;
      batchBusy[1](true);
      req('/gallery/batch/category?category=' + encodeURIComponent(target), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: selectedIds[0] }) })
        .then(function (r) { message.success('已移动 ' + (r.moved || 0) + ' 张图片到「' + target + '」'); exitBatchMode(); loadImages(category[0]); })
        .catch(function (e) { message.error(e.message || '批量换分类失败'); })
        .then(function () { batchBusy[1](false); });
    }
    function batchUpscale() {
      var ids = selectedIds[0].slice();
      if (!ids.length) return message.warning('请至少选择一张图片');
      var first = (imgs[0] || []).filter(function (x) { return x.id === ids[0]; })[0];
      if (first) upscaleProfile[1](pickUpscaleProfile(first.model_name));
      req('/upscale/queue/add/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: ids }) })
        .then(function (r) { if (r.success) { message.success('已添加 ' + r.added + ' 张到待放区'); exitBatchMode(); loadUpscaleQueue(); tab[1]('upscale'); } })
        .catch(function (e) { message.error(e.message || '加入待放区失败'); });
    }
    function batchDelete() {
      var count = selectedIds[0].length; if (!count) return;
      if (!window.confirm('确定从图库删除 ' + count + ' 张图片吗？图片文件也会移入系统回收站。')) return;
      batchBusy[1](true);
      req('/gallery/batch/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: selectedIds[0] }) })
        .then(function (r) { message.success('已删除 ' + (r.deleted || 0) + ' 张图片'); exitBatchMode(); preview[1](null); loadImages(category[0]); })
        .catch(function (e) { message.error(e.message || '批量删除失败'); })
        .then(function () { batchBusy[1](false); });
    }
    function loadCategories() { req('/gallery/categories?_=' + Date.now()).then(function (d) { categories[1](d.categories || ['未分类']); }).catch(function () {}); }
    function scanGallery() { galleryLoading[1](true); req('/gallery/scan', { method: 'POST' }).then(function (d) { message.success(d.message || ('已扫描 ' + (d.added || 0) + ' 张图片')); loadCategories(); loadImages(); }).catch(function (e) { galleryLoading[1](false); message.error(e.message); }); }
    function loadUpscaleQueue() { upscaleQueueLoading[1](true); req('/upscale/queue?_=' + Date.now()).then(function (d) { upscaleQueue[1](d.items || []); }).catch(function (e) { console.warn('读取待放区失败', e); }).then(function () { upscaleQueueLoading[1](false); }); }
    function addGalleryToUpscaleQueue(img) { if (!img) return; upscaleProfile[1](pickUpscaleProfile(img.model_name)); req('/upscale/queue/add/gallery/' + img.id, { method: 'POST' }).then(function (r) { if (r.success) { message.success('已加入待放区'); loadUpscaleQueue(); tab[1]('upscale'); } }).catch(function (e) { message.error(e.message || '加入待放区失败'); }); }
    function removeFromUpscaleQueue(itemId) { req('/upscale/queue/remove/' + itemId, { method: 'POST' }).then(function (r) { if (r.success) { message.success('已移出待放区'); loadUpscaleQueue(); } }).catch(function (e) { message.error(e.message || '移出失败'); }); }
    function clearUpscaleQueue() { req('/upscale/queue/clear', { method: 'POST' }).then(function (r) { if (r.success) { upscaleQueue[1]([]); message.success('待放区已清空'); } }).catch(function (e) { message.error(e.message || '清空失败'); }); }
    function startUpscaleQueue() {
      var modeLabel = upscaleMode[0] === 'hires' ? '高清修复' : (upscaleMode[0] === 'tiled' ? '分块高清修复' : '放大');
      if (!upscaleQueue[0].length) { message.warning('待放区是空的，请先加入图片'); return; }
      if (!upscaleProfile[0]) { message.warning('请先选择放大模型'); return; }
      upscaleStarting[1](true);
      req('/upscale/queue/start?profile=' + encodeURIComponent(upscaleProfile[0]) + '&options=' + encodeURIComponent(upscaleOptionsPayload()), { method: 'POST' })
        .then(function (r) {
          if (r && r.success && r.task_id) {
            message.success('已提交 ' + r.total + ' 张' + modeLabel + '任务');
            upscaleQueue[1]([]);
            task[1]({ id: r.task_id, kind: 'upscale', state: 'queued', total: r.total, completed: 0, message: modeLabel + '任务已提交' });
            pollUpscaleTask(r.task_id);
          } else {
            // 后端返回 200 但没有任务号：通常是插件后端没重启到新版本，别静默失败。
            var detail = (r && (r.detail || r.message)) || '';
            message.error('提交失败：后端未返回任务号' + (detail ? '（' + detail + '）' : '') + '，请完全退出并重启 QwenPaw Desktop 后再试', 8);
            loadUpscaleQueue();
          }
        })
        .catch(function (e) { message.error(e.message || '提交失败'); })
        .then(function () { upscaleStarting[1](false); });
    }
    React.useEffect(function () {
      load();
      loadCategories();
      loadGalleryFilters();
      loadImages();
      req('/upscale/profiles?_=' + Date.now()).then(function(d){ var items=d.items || []; upscaleProfiles[1](items); upscaleProfile[1](defaultUpscaleProfile(items)); }).catch(function(){});
      loadUpscaleQueue();
      req('/version?_=' + Date.now()).then(function (v) {
        if (v && v.version && FRONTEND_VERSION && v.version !== FRONTEND_VERSION) {
          var msg = '版本不一致：前端 v' + FRONTEND_VERSION + ' / 后端 v' + v.version + '。请完全退出并重启 QwenPaw Desktop。';
          versionMismatch[1](msg);
          if (!versionWarning.current) { versionWarning.current = msg; message.warning(msg, 8); }
        }
      }).catch(function () {});
    }, []);

    // 把面板当前状态同步给后端，让 Agent（一句话生图）能读到用户选好的参考图 / 生成方式。
    React.useEffect(function () {
      var r = refImage[0] || {};
      var p = params[0] || {};
      var payload = {
        generation_mode: genMode[0], model_name: model[0] || '',
        loras: loras[0].filter(function (x) { return x.enabled && x.name; }).map(function (x) { return x.name; }),
        reference_image: r.image_id ? String(r.image_id) : (r.local_path || ''),
        reference_file_name: r.file_name || r.image_name || '',
        reference_width: r.width || 0, reference_height: r.height || 0,
        denoise: numOr(refDenoise[0], 0.6),
        steps: numOr(p.steps, 0), cfg: numOr(p.cfg, 0), width: numOr(p.width, 0), height: numOr(p.height, 0),
        category: category[0] || '', prompt: prompt[0] || ''
      };
      req('/panel/context', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }).catch(function () {});
    }, [genMode[0], model[0], refImage[0], refDenoise[0], refFollowSize[0], prompt[0], category[0], params[0], loras[0]]);

    // 轮询「当前进行中的任务」：Agent（一句话出图）发起的生图也能在面板看到进度。
    var fnsRef = React.useRef({});
    fnsRef.current = { loadImages: loadImages, loadCategories: loadCategories, openReview: openReview, reviewPolicyAllows: reviewPolicyAllows };
    var taskRef = React.useRef(null);
    taskRef.current = task[0];
    React.useEffect(function () {
      var timer = null;
      var polling = false;
      function pollActive(id) {
        if (polling) return;
        polling = true;
        (function step() {
          req('/tasks/' + id + '?_=' + Date.now()).then(function (t) {
            task[1](t);
            var f = fnsRef.current;
            if (t.gallery_ids && t.gallery_ids.length) f.loadImages();
            if (t.state === 'queued' || t.state === 'running') { window.setTimeout(step, 1500); return; }
            polling = false;
            f.loadImages();
            if (t.state === 'completed') { message.success(t.message); if (f.reviewPolicyAllows(t.total)) f.openReview(t.gallery_ids || []); }
            else if (t.state === 'cancelled') message.info(t.message || '已停止等待');
            else message.error((t.failures && t.failures[0]) || t.message || '生图失败');
          }).catch(function () { polling = false; });
        })();
      }
      function tick() {
        var t = taskRef.current;
        if (t && (t.state === 'queued' || t.state === 'running')) { timer = window.setTimeout(tick, 2000); return; }
        req('/active-task?_=' + Date.now()).then(function (r) {
          var active = r && r.task;
          if (active && (active.state === 'queued' || active.state === 'running')) { task[1](active); pollActive(active.id); }
        }).catch(function () {}).then(function () { timer = window.setTimeout(tick, 2000); });
      }
      timer = window.setTimeout(tick, 1500);
      return function () { if (timer) window.clearTimeout(timer); };
    }, []);

    // ComfyUI 实时步进进度：直连 ComfyUI 的 websocket，任务卡住时能看出到底走到第几步。
    var liveStep = s(null);
    var wsRef = React.useRef(null);
    var taskRunning = !!(task[0] && (task[0].state === 'queued' || task[0].state === 'running'));
    var apiUrl = (state[0] && state[0].status && state[0].status.api_url) || '';
    React.useEffect(function () {
      if (!taskRunning || !apiUrl) {
        liveStep[1](null);
        if (wsRef.current) { try { wsRef.current.close(); } catch (e) {} wsRef.current = null; }
        return;
      }
      var ws = null;
      try { ws = new WebSocket(apiUrl.replace(/^http/, 'ws') + '/ws?clientId=qwenpaw-image-gen-panel'); } catch (e) { return; }
      wsRef.current = ws;
      ws.onmessage = function (ev) {
        var msg = null; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (!msg || !msg.type) return;
        if (msg.type === 'progress' && msg.data && msg.data.max) {
          liveStep[1]({ value: msg.data.value || 0, max: msg.data.max, promptId: msg.data.prompt_id || '' });
        } else if (msg.type === 'executing' && msg.data && msg.data.node === null) {
          liveStep[1](null);
        } else if (msg.type === 'execution_error') {
          liveStep[1](null);
        }
      };
      ws.onerror = function () {};
      ws.onclose = function () { wsRef.current = null; };
      return function () { try { ws.close(); } catch (e) {} wsRef.current = null; };
    }, [taskRunning, apiUrl]);

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
      // 选了模型就适配当前模型（可重复执行，用于修复/重建绑定）；没选模型才走全自动挑选
      var current = model[0];
      var url = current
        ? '/workflows/auto-bind?model_name=' + encodeURIComponent(current)
        : '/workflows/one-click-setup';
      req(url, { method: 'POST' }).then(function (r) {
        scanning[1](false);
        scanFailed[1](false);
        if (current) {
          message.success('已自动适配当前模型：' + current + '（' + ((r && r.workflow_name) || '自动适配') + '）');
          load(current, 0);
          return;
        }
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
        'AI 助手，我的生图插件自动扫描没找到 ComfyUI，请帮我手动配置。',
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
        'AI 助手，请为我的 ComfyUI 主模型创建并绑定一个生图工作流。',
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
    function loadPortable(img) {
      if (!img) return;
      req('/images/' + img.id + '/portable').then(function (d) { portable[1](d); }).catch(function (e) { message.error(e.message || '读取可复现信息失败'); });
    }

    // ── 提示词库 ──────────────────────────────────────────────────────────
    function activeLoraNames() { return loras[0].filter(function (x) { return x.enabled && x.name; }).map(function (x) { return x.name; }); }
    function appendTag(tag, negative) {
      if (!tag) return;
      if (negative) { neg[1]((neg[0] ? neg[0].replace(/,\s*$/, '') + ', ' : '') + tag); }
      else { prompt[1]((prompt[0] ? prompt[0].replace(/,\s*$/, '') + ', ' : '') + tag); }
    }
    function loadPromptLib(targetModel) {
      var m = targetModel === undefined ? (model[0] || '') : (targetModel || '');
      req('/prompt-library?model_name=' + encodeURIComponent(m) + '&loras=' + encodeURIComponent(activeLoraNames().join(',')))
        .then(function (d) { promptLib[1](d); })
        .catch(function (e) { message.error(e.message || '提示词库加载失败'); });
    }
    function savePromptTemplate() {
      if (!prompt[0].trim()) return message.warning('先写点正向提示词再存模板');
      var name = window.prompt('模板名称', (prompt[0] || '').split(',')[0].slice(0, 20) || '我的模板');
      if (!name) return;
      req('/prompt-templates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name, prompt: prompt[0], negative_prompt: neg[0], model_name: model[0] }) })
        .then(function (r) { message.success('已保存模板'); if (promptLib[0]) promptLib[1](Object.assign({}, promptLib[0], { templates: r.items })); })
        .catch(function (e) { message.error(e.message || '保存失败'); });
    }
    function applyPromptTemplate(id) {
      var list = (promptLib[0] && promptLib[0].templates) || [];
      var t = list.filter(function (x) { return String(x.id) === String(id); })[0];
      if (!t) return;
      prompt[1](t.prompt || '');
      if (t.negative_prompt) neg[1](t.negative_prompt);
      message.success('已套用模板：' + t.name);
    }
    function deletePromptTemplate(id) {
      req('/prompt-templates/' + encodeURIComponent(id), { method: 'DELETE' })
        .then(function (r) { message.success('已删除'); if (promptLib[0]) promptLib[1](Object.assign({}, promptLib[0], { templates: r.items })); })
        .catch(function (e) { message.error(e.message || '删除失败'); });
    }
    function aiOneShot() {
      var idea = (aiIdea[0] || '').trim();
      if (!idea) return message.warning('先用一句话描述你想画什么');
      var img2img = genMode[0] === 'img2img';
      if (img2img && !refImage[0]) { message.warning('以图生图要先在「0. 生成方式」里选一张参考图'); tab[1]('gen'); return; }
      var names = activeLoraNames();
      var refArg = '';
      var dn = numOr(refDenoise[0], 0.6);
      if (img2img) {
        var r = refImage[0];
        refArg = r.image_id ? String(r.image_id) : (r.local_path || '');
      }
      var text = [
        '请帮我生成一张高质量图片。',
        '',
        '【我的想法】' + idea,
        '【当前主模型】' + (model[0] || '（未选择）'),
        names.length ? '【使用 LoRA】' + names.join(', ') : '',
        img2img ? '【生成方式】以图生图（img2img）' : '【生成方式】文生图（txt2img）',
        img2img ? '【参考图】' + (refImage[0].file_name || refImage[0].image_name || '') + '，reference_image="' + refArg + '"，denoise=' + dn : '',
        img2img ? '【参考图尺寸】' + (refImage[0].width || '?') + '×' + (refImage[0].height || '?') + '（沿用，不要自己改宽高）' : '',
        '【要求】',
        '1. 先调用 image_gen_prompt_helper 读取当前模型和 LoRA 的触发词、推荐标签。',
        '2. 把上面这句话扩写成英文提示词：画质词 → 主体 → 细节 → 服装 → 动作 → 环境 → 光照 → 风格，必须包含模型触发词。',
        img2img
          ? '3. 调用 image_gen_generate 时**必须**带上 reference_image="' + refArg + '" 和 denoise=' + dn + '（漏掉就变成文生图了），model_name 用上面这个。'
          : '3. 调用 image_gen_generate 生成（model_name 用上面这个）。',
        '4. 生成后把最终用的提示词和参数告诉我。'
      ].filter(Boolean).join('\n');
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
          .then(function () { message.success('已复制「一句话出图」指令，粘贴到聊天框发给 AI 助手即可', 6); })
          .catch(function () { message.info(text); });
      } else { message.info(text); }
    }
    function promptLibChips(words, negative) {
      return (words || []).map(function (w, i) {
        var name = (w && typeof w === 'object') ? w.name : w;
        var zh = (w && typeof w === 'object') ? w.zh : '';
        return h(Tag, { key: i, style: { marginBottom: 4, cursor: 'pointer' }, onClick: function () { appendTag(name, negative); } }, name + (zh ? ' (' + zh + ')' : ''));
      });
    }
    function aiTranslateTags(tags) {
      if (!tags || !tags.length) return;
      var text = [
        '请把这些生图提示词标签翻译成简短中文（每个 2-6 字），然后用 image_gen_save_tag_translations 保存：',
        '',
        tags.join('\n'),
        '',
        '保存格式示例：{"masterpiece": "杰作", "silver hair": "银发"}'
      ].join('\n');
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
          .then(function () { message.success('已复制翻译指令，粘贴到聊天框发给 AI 助手，翻译完点「刷新」即可', 6); })
          .catch(function () { message.info(text); });
      } else { message.info(text); }
    }
    function renderPromptLibrary() {
      var lib = promptLib[0];
      if (!lib) return h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginTop: 6 } }, '正在加载提示词库...');
      var q = (tagQuery[0] || '').toLowerCase();
      function filtered(group) {
        if (!q) return group.tags;
        return group.tags.filter(function (t) { return t.name.toLowerCase().indexOf(q) >= 0 || String(t.zh || '').toLowerCase().indexOf(q) >= 0; });
      }
      return h('div', { style: { marginTop: 8, padding: 8, border: '1px solid var(--border-color-split)', borderRadius: 6, background: 'var(--ant-color-fill-quaternary)' } },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 } },
          h('div', { style: { fontSize: 11, fontWeight: 700 } }, '提示词库'),
          h('div', { style: { display: 'flex', gap: 4, alignItems: 'center' } },
            (lib.untranslated_tags && lib.untranslated_tags.length) ? h(Button, { size: 'small', type: 'link', style: { padding: 0, height: 'auto', fontSize: 11 }, onClick: function () { aiTranslateTags(lib.untranslated_tags); } }, '🌐 让 AI 翻译(' + lib.untranslated_tags.length + ')') : null,
            h(Button, { size: 'small', type: 'text', onClick: function () { loadPromptLib(); } }, '刷新')
          )
        ),
        h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 6, wordBreak: 'break-all' } },
          '当前模型：' + (lib.model && lib.model.name ? lib.model.name : '（未选择）') +
          (lib.model && lib.model.architecture ? ' · 架构 ' + lib.model.architecture : '') +
          (lib.model && lib.model.base_model ? ' · ' + lib.model.base_model : '') +
          (lib.model && lib.model.name && lib.model.found === false ? ' ·（未找到模型文件，无法读取触发词）' : '')),
        (lib.model && lib.model.trigger_words && lib.model.trigger_words.length) ? h('div', { style: { marginBottom: 8 } },
          h('div', { style: { fontSize: 11, fontWeight: 700, marginBottom: 4 } }, '模型触发词 · ' + (lib.model.name || '')),
          h('div', null, promptLibChips(lib.model.trigger_words, false))
        ) : null,
        (lib.loras || []).filter(function (l) { return l.trigger_words && l.trigger_words.length; }).map(function (l, i) {
          return h('div', { key: 'lora' + i, style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 11, fontWeight: 700, marginBottom: 4 } }, 'LoRA 触发词 · ' + l.name),
            h('div', null, promptLibChips(l.trigger_words, false))
          );
        }),
        h(Input, { size: 'small', value: tagQuery[0], placeholder: '搜索标签（中 / 英文）', allowClear: true, onChange: function (e) { tagQuery[1](e.target.value); }, style: { marginBottom: 6, fontSize: 12 } }),
        lib.tag_groups.map(function (g) {
          var tags = filtered(g);
          if (!tags.length) return null;
          var isNeg = g.id === 'negative';
          return h('div', { key: g.id, style: { marginBottom: 6 } },
            h('div', { style: { fontSize: 11, fontWeight: 700, color: g.color || 'inherit', marginBottom: 3 } }, g.label),
            h('div', null, tags.map(function (t, i) {
              return h(Tag, { key: i, style: { marginBottom: 4, cursor: 'pointer' }, onClick: function () { appendTag(t.name, isNeg); } }, t.name + (t.zh ? ' (' + t.zh + ')' : ''));
            }))
          );
        }),
        (lib.templates && lib.templates.length) ? h('div', { style: { marginTop: 6 } },
          h('div', { style: { fontSize: 11, fontWeight: 700, marginBottom: 4 } }, '我的模板'),
          lib.templates.map(function (t) {
            return h('div', { key: t.id, style: { display: 'flex', gap: 4, alignItems: 'center', marginBottom: 4 } },
              h(Button, { size: 'small', style: { flex: 1, textAlign: 'left' }, onClick: function () { applyPromptTemplate(t.id); } }, t.name),
              h(Button, { size: 'small', danger: true, onClick: function () { deletePromptTemplate(t.id); } }, '删')
            );
          })
        ) : null
      );
    }
    function setting(key, fallback) { var raw = localStorage.getItem(pid + '-setting-' + key); return raw === null ? fallback : raw; }
    function setSetting(key, value) { localStorage.setItem(pid + '-setting-' + key, String(value)); settingsTick[1](settingsTick[0] + 1); }
    // ── 图片缓存：结果弹窗 / 详情预览按 id 取图，不再依赖图库当前分页和分类筛选 ──
    var previewFetching = React.useRef({});
    function cacheImages(list) {
      if (!list || !list.length) return;
      var next = Object.assign({}, imageCache[0]);
      var changed = false;
      list.forEach(function (x) { if (x && x.id !== undefined && x.id !== null) { next[x.id] = x; changed = true; } });
      if (changed) imageCache[1](next);
    }
    function findImage(id) {
      if (id === undefined || id === null) return null;
      return (imageCache[0] || {})[id] || (imgs[0] || []).find(function (x) { return x.id === id; }) || null;
    }
    function ensureImage(id) {
      if (findImage(id) || previewFetching.current[id]) return;
      previewFetching.current[id] = true;
      req('/images/' + id + '?_=' + Date.now()).then(function (d) { cacheImages([d]); }).catch(function () {}).then(function () { delete previewFetching.current[id]; });
    }
    function openReview(ids) {
      if (!ids || !ids.length) return;
      review[1]({ ids: ids, index: Math.max(0, ids.length - 1) });
      tab[1]('gallery');
      loadImages();
      req('/images/batch?ids=' + ids.join(',') + '&_=' + Date.now()).then(function (d) {
        var items = d.items || [];
        cacheImages(items);
        // 结果可能保存在别的分类：自动切到它所在的分类，避免「弹窗有图、图库里却看不到」。
        var cats = {};
        items.forEach(function (x) { if (x && x.category) cats[x.category] = 1; });
        var keys = Object.keys(cats);
        if (keys.length === 1 && keys[0] !== category[0]) { category[1](keys[0]); loadGalleryFilters(keys[0]); loadImages(keys[0]); }
      }).catch(function () {});
    }
    function reviewPolicyAllows(total) { var policy = setting('review_policy', 'single'); return policy === 'always' || (policy === 'single' && total === 1) || (policy === 'batch' && total > 1); }
    function pollTask(taskId) {
      req('/tasks/' + taskId + '?_=' + Date.now()).then(function (t) {
        task[1](t); if (t.gallery_ids && t.gallery_ids.length) loadImages();
        if (t.state === 'queued' || t.state === 'running') { window.setTimeout(function(){ pollTask(taskId); }, 1200); return; }
        busy[1](false); loadImages();
        if (t.state === 'completed') { message.success(t.message); if (reviewPolicyAllows(t.total)) openReview(t.gallery_ids || []); }
        else if (t.state === 'cancelled') message.info(t.message || '已停止等待');
        else message.error((t.failures && t.failures[0]) || t.message || '生图失败');
      }).catch(function(e) { busy[1](false); message.error(e.message || '读取生成进度失败'); });
    }
    // ── 放大模型智能选择：二次元/插画用 anime 模型更锐利，写实照片用通用模型 ──
    var ANIME_MODEL_RE = /illustrious|anima|noobai|pony|z-image|z_image|anime|wai|luotianyi|pixai|nai|kohaku|anything/i;
    function defaultUpscaleProfile(profiles) {
      var list = profiles || upscaleProfiles[0] || [];
      var anime = list.filter(function (x) { return x.group === 'anime'; })[0];
      return (anime || list[0] || {}).id || '';
    }
    function pickUpscaleProfile(modelName, profiles) {
      var list = profiles || upscaleProfiles[0] || [];
      if (!list.length) return '';
      var want = ANIME_MODEL_RE.test(String(modelName || '')) ? 'anime' : 'general';
      var hit = list.filter(function (x) { return x.group === want; })[0];
      return (hit || list[0]).id || '';
    }
    function upscaleOptionsPayload() {
      return JSON.stringify({
        mode: upscaleMode[0], profile: upscaleProfile[0], scale: Number(upscaleScale[0]) || 0,
        denoise: numOr(upscaleDenoise[0], 0.35), steps: numOr(upscaleSteps[0], 15),
        cfg: numOr(upscaleCfg[0], 0), seed_mode: upscaleSeedMode[0],
        tile_size: numOr(upscaleTile[0], 512), seam_fix: upscaleSeam[0],
        prompt_mode: upscalePromptMode[0],
        sharpen_alpha: numOr(upscaleSharpen[0], 0.08),
        face_detail: upscaleFaceDetail[0],
        iterations: numOr(upscaleIterations[0], 2),
        category: upscaleCategory[0]
      });
    }
    function numOr(value, fallback) { var n = Number(value); return isNaN(n) ? fallback : n; }
    function parseLoraLine(line) {
      line = String(line || '').trim();
      if (!line) return null;
      // 面板生成的记录带强度后缀；扫描/导入的记录只有文件名，也要能复刻。
      var m = line.match(/^(.*?) \(模型强度 ([^,]+), CLIP强度 ([^)]+)\)$/);
      if (m) return { name: m[1].trim(), enabled: true, strength_model: numOr(m[2], 0.6), strength_clip: numOr(m[3], 0.6) };
      return { name: line, enabled: true, strength_model: 0.6, strength_clip: 0.6 };
    }
    function bringImageToEditor(img, variant) {
      if (!img) return;
      var targetModel = img.model_name || model[0];
      var restored = {
        steps: numOr(img.steps, 20), cfg: numOr(img.cfg, 7),
        width: numOr(img.width, 1024), height: numOr(img.height, 1024),
        seed: variant ? -1 : (img.seed === undefined ? -1 : numOr(img.seed, -1))
      };
      var parsed = loraLines(img.lora_name).map(parseLoraLine).filter(Boolean);
      prompt[1](img.prompt || '');
      neg[1](img.negative_prompt || '');
      model[1](targetModel);
      // 先加载该模型的工作流状态（绑定/schema/CLIP/VAE），再在其 schema 默认值之上回填
      // 图片参数——否则 load() 会用默认值把刚带入的宽高/步数覆盖掉。
      load(targetModel, 0).then(function (defaults) {
        params[1](Object.assign({}, defaults || params[0] || {}, restored));
        loras[1](parsed);  // 没 LoRA 也清空，避免残留上一张的 LoRA
        preview[1](null); review[1](null); tab[1]('gen');
        message.success(variant ? '已带入参数并随机 Seed，可继续生成变体' : '已带入原图全部参数，可复刻生成');
      });
    }

    function pollUpscaleTask(taskId) {
      req('/tasks/' + taskId + '?_=' + Date.now()).then(function(t){ task[1](t); if(t.state === 'queued' || t.state === 'running'){ window.setTimeout(function(){pollUpscaleTask(taskId);}, 1000); return; } loadImages(); if(t.state === 'completed'){ message.success('放大完成，已保存到图库'); tab[1]('gallery'); if(reviewPolicyAllows(1)) openReview(t.gallery_ids || []); } else message.error((t.failures&&t.failures[0]) || t.message || '放大失败'); }).catch(function(e){message.error(e.message || '读取放大进度失败');});
    }
    function doUpscaleUpload(file) {
      var f = file || upscaleFile[0];
      if(!f) return message.warning('先选择一张图片'); var fd=new FormData(); fd.append('image',f);
      req('/upscale/queue/add/upload?category='+encodeURIComponent(upscaleCategory[0]), {method:'POST',body:fd}).then(function(r){ if(r.success) { message.success('已加入待放区'); upscaleFile[1](null); loadUpscaleQueue(); } }).catch(function(e){message.error(e.message || '上传失败');});
    }
    function upscaleGalleryImage(img) {
      addGalleryToUpscaleQueue(img);
    }

    // ── 以图生图：参考图准备 ──────────────────────────────────────────────
    function applyRefImage(r, extra) {
      var info = Object.assign({ image_name: r.image_name, width: r.width || 0, height: r.height || 0,
        file_name: r.file_name || r.image_name, local_path: r.local_path || '' }, extra || {});
      refImage[1](info);
      pickingRef[1](false);
      message.success('已设为参考图：' + (info.file_name || '') + (info.width ? '（' + info.width + '×' + info.height + '）' : ''));
    }
    function prepareRefFromGallery(img) {
      if (!img) return;
      refBusy[1](true);
      req('/img2img/from-gallery/' + img.id, { method: 'POST' })
        .then(function (r) { applyRefImage(r, { preview: iurl(img.id), image_id: img.id }); tab[1]('gen'); })
        .catch(function (e) { message.error(e.message || '参考图准备失败'); })
        .then(function () { refBusy[1](false); });
    }
    function uploadRefImage(file) {
      if (!file) return;
      var fd = new FormData(); fd.append('image', file);
      refBusy[1](true);
      req('/img2img/upload', { method: 'POST', body: fd })
        .then(function (r) { applyRefImage(r, { preview: URL.createObjectURL(file) }); })
        .catch(function (e) { message.error(e.message || '参考图上传失败'); })
        .then(function () { refBusy[1](false); });
    }

    // ── 工作流自检：不跑图，只校验节点/参数/模型文件是否可用 ──────────────
    function selfCheckWorkflow() {
      scanning[1](true);
      req('/workflows/self-check' + (model[0] ? '?model_name=' + encodeURIComponent(model[0]) : ''), { method: 'POST' })
        .then(function (r) {
          scanning[1](false);
          if (r && r.ok) { message.success('自检通过：' + (r.nodes || 0) + ' 个节点，节点/参数/模型文件都在（' + (r.model_type || '') + '）'); return; }
          var errs = (r && r.errors) || ['未知错误'];
          Modal.warning({
            title: '工作流自检发现问题（' + (errs.length) + ' 条）',
            width: 640,
            content: h('div', { style: { fontSize: 12, lineHeight: '20px' } },
              h('div', { style: { marginBottom: 8, color: 'var(--ant-color-text-secondary)' } }, '模型：' + (r.model_name || model[0] || '—') + ' · 类型：' + (r.model_type || '—')),
              h('ul', { style: { margin: 0, paddingLeft: 18 } }, errs.slice(0, 12).map(function (t, i) { return h('li', { key: i }, String(t)); })),
              errs.length > 12 ? h('div', { style: { marginTop: 6, color: 'var(--ant-color-text-tertiary)' } }, '还有 ' + (errs.length - 12) + ' 条，详见控制台') : null
            )
          });
          if (errs.length > 12) console.warn('自检问题：', errs);
        })
        .catch(function (e) { scanning[1](false); message.error(e.message || '自检失败'); });
    }

    function doGen() {
      if (!state[0] || !state[0].has_workflow) return message.warning('请先绑定工作流');
      if (!prompt[0].trim()) return message.warning('先写提示词～');
      if (prompt[0].length > 2000) return message.warning('提示词太长了，最多2000字符');
      if (neg[0].length > 1000) return message.warning('负向提示词太长了，最多1000字符');
      var img2img = genMode[0] === 'img2img';
      if (img2img && !refImage[0]) return message.warning('以图生图要先选一张参考图');
      var p = Object.assign({}, params[0]);
      var w = p.width || 1024, h = p.height || 1024;
      if (img2img && refFollowSize[0] && refImage[0].width && refImage[0].height) { w = refImage[0].width; h = refImage[0].height; }
      var payload = {
        prompt: prompt[0], negative_prompt: neg[0], model_name: model[0],
        steps: p.steps || 20, cfg: p.cfg || 7, seed: p.seed === undefined ? -1 : p.seed,
        width: w, height: h, batch_size: p.batch_size || 1, category: category[0],
        sampler_name: p.sampler_name || 'euler', scheduler: p.scheduler || 'normal',
        denoise: img2img ? numOr(refDenoise[0], 0.6) : (p.denoise === undefined ? 1 : p.denoise),
        clip_name: clipName[0] || '', vae_name: vaeName[0] || '',
        loras: loras[0].filter(function (x) { return x.enabled && x.name; }),
        generation_mode: genMode[0],
        source_image: img2img ? (refImage[0].image_name || '') : ''
      };
      busy[1](true);
      req('/generate/async', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) })
        .then(function(r){ task[1]({id:r.task_id,state:'queued',total:r.total,completed:0,message:'任务已提交'}); pollTask(r.task_id); })
        .catch(function(e){ busy[1](false); message.error(e.message); });
    }

    var d = state[0] || {};
    var status = d.status || {};
    var hasWorkflow = !!d.has_workflow;
    var binding = d.binding || null;
    var schema = d.params_schema || {};
    var loraOptions = kvToOptions(d.loras || []);
    var caps = d.capabilities || {};

    return h(React.Fragment, null,
      h('div', { style: { padding: '10px 12px', borderBottom: '1px solid var(--border-color-split)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        h('div', null, h('div', { style: { fontWeight: 700 } }, '✨ ComfyUI 生图助手'), h('div', { style: { fontSize: 10, color: 'var(--ant-color-text-secondary)' } }, '确定性适配面板 · v' + FRONTEND_VERSION)),
        h('div', { style: { display: 'flex', gap: 4 } },
          h(Button, { type: 'text', size: 'small', icon: I && I.ReloadOutlined ? h(I.ReloadOutlined) : null, onClick: function () { message.loading('正在刷新扫描...'); load(model[0], workflowPreset[0]); req('/upscale/profiles?_=' + Date.now()).then(function(d){ var items=d.items || []; upscaleProfiles[1](items); upscaleProfile[1](defaultUpscaleProfile(items)); }); }, title: '刷新 ComfyUI 资源' }, '刷新'),
          h(Button, { type: 'text', size: 'small', icon: I && I.CloseOutlined ? h(I.CloseOutlined) : null, onClick: function () { setOn(false); emitToggle(false); toggleUI(false); } })
        )
      ),
      h('div', { style: { display: 'flex', borderBottom: '1px solid var(--border-color-split)' } },
        h(Button, { type: tab[0] === 'gen' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0, padding: '0 2px' }, onClick: function () { pickingRef[1](false); tab[1]('gen'); } }, '工作流'),
        h(Button, { type: tab[0] === 'api' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0, padding: '0 2px' }, onClick: function () { apiPickingRef[1](false); tab[1]('api'); loadApiConfig(); loadCategories(); } }, 'API生图'),
        h(Button, { type: tab[0] === 'gallery' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0, padding: '0 2px' }, onClick: function () { tab[1]('gallery'); loadCategories(); loadImages(); } }, '图库(' + (imgs[0] || []).length + ')'),
        h(Button, { type: tab[0] === 'upscale' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0, padding: '0 2px' }, onClick: function () { tab[1]('upscale'); } }, '放大'),
        h(Button, { type: tab[0] === 'settings' ? 'primary' : 'text', size: 'small', style: { flex: 1, borderRadius: 0, padding: '0 2px' }, onClick: function () { tab[1]('settings'); loadApiConfig(); } }, '设置')
      ),
      versionMismatch[0] ? h(Alert, { type: 'warning', showIcon: true, message: '缓存版本不一致', description: versionMismatch[0], action: h(Button, { size: 'small', type: 'primary', onClick: function () { versionMismatch[1](''); location.reload(); } }, '重新加载'), style: { margin: 10 } }) : null,
      task[0] && (task[0].state === 'queued' || task[0].state === 'running') ? (function () {
        var totalN = Math.max(1, Number(task[0].total || 1));
        var doneN = Number(task[0].completed || 0);
        var pct = Math.round((doneN / totalN) * 100);
        var step = liveStep[0];
        if (totalN === 1 && step && step.max) pct = Math.max(1, Math.round((Number(step.value || 0) / step.max) * 100));
        return h('div', { style:{margin:8,padding:10,border:'1px solid var(--ant-color-primary)',borderRadius:7,background:'var(--ant-color-fill-quaternary)'} },
        h('div',{style:{fontWeight:700,fontSize:12}}, (task[0].kind === 'upscale' ? '图片放大 · ' : '生成任务 · ') + (task[0].message || '正在处理中')),
        step && step.max ? h('div',{style:{fontSize:11,color:'var(--ant-color-text-secondary)',marginTop:4}},'采样进度 '+Number(step.value||0)+' / '+step.max+' 步（'+pct+'%）') : null,
        h('div',{style:{height:6,background:'var(--ant-color-fill-secondary)',borderRadius:4,overflow:'hidden',margin:'8px 0'}},h('div',{style:{height:'100%',width:pct+'%',background:'var(--ant-color-primary)',transition:'width .25s'}})),
        h('div',{style:{display:'flex',justifyContent:'space-between',fontSize:11,color:'var(--ant-color-text-secondary)'}},h('span',null,'已完成 '+doneN+' / '+totalN+' 张'),h(Button,{size:'small',danger:true,onClick:function(){req('/tasks/'+task[0].id+'/stop',{method:'POST'}).then(function(){message.info('将在当前图片结束后停止');});}},'停止等待'))
      ); })() : null,
      tab[0] === 'api' ? renderApiTab() : null,
      tab[0] === 'gen' ? h('div', { style: { flex: 1, overflowY: 'auto' } },
        h(Section, { title: '0. 生成方式' },
          h('div', { style: { display: 'flex', gap: 4 } },
            h(Button, { size: 'small', type: genMode[0] === 'txt2img' ? 'primary' : 'default', style: { flex: 1 }, onClick: function () { genMode[1]('txt2img'); } }, '文生图'),
            h(Button, { size: 'small', type: genMode[0] === 'img2img' ? 'primary' : 'default', style: { flex: 1 }, onClick: function () { genMode[1]('img2img'); } }, '以图生图')
          ),
          genMode[0] === 'img2img' ? h('div', { style: { marginTop: 8, padding: 8, border: '1px solid var(--border-color-split)', borderRadius: 6, background: 'var(--ant-color-fill-quaternary)' } },
            h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 6 } }, '参考图（原图会先缩放到目标尺寸，再按重绘幅度重画）'),
            refImage[0] ? h('div', { style: { display: 'flex', gap: 8, alignItems: 'center' } },
              h('img', { src: refImage[0].preview || '', style: { width: 56, height: 56, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border-color-split)', flexShrink: 0 } }),
              h('div', { style: { flex: 1, minWidth: 0, fontSize: 11 } },
                h('div', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, refImage[0].file_name || refImage[0].image_name),
                h('div', { style: { color: 'var(--ant-color-text-tertiary)' } }, (refImage[0].width || '?') + ' × ' + (refImage[0].height || '?')),
                h('div', { style: { display: 'flex', alignItems: 'center', gap: 6, marginTop: 4 } },
                  h('span', { style: { fontSize: 10, color: 'var(--ant-color-text-secondary)' } }, '尺寸跟随参考图'),
                  h(Switch, { size: 'small', checked: refFollowSize[0], onChange: function (v) { refFollowSize[1](v); } })
                )
              ),
              h(Button, { size: 'small', onClick: function () { refImage[1](null); } }, '移除')
            ) : h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', padding: '6px 0' } }, refBusy[0] ? '参考图处理中...' : '还没有选参考图'),
            h('div', { style: { display: 'flex', gap: 4, marginTop: 6, alignItems: 'center' } },
              h(Button, { size: 'small', loading: refBusy[0], onClick: function () { pickingRef[1](true); tab[1]('gallery'); loadImages(); message.info('在图库点任意图片即设为参考图'); } }, '从图库选'),
              h('input', { type: 'file', accept: '.png,.jpg,.jpeg,.webp', style: { flex: 1, fontSize: 11 }, onChange: function (e) { var f = e.target.files && e.target.files[0]; if (f) { uploadRefImage(f); e.target.value = ''; } } })
            ),
            h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 } },
              h('span', { style: { fontSize: 11, whiteSpace: 'nowrap' } }, '重绘幅度'),
              h(InputNumber, { size: 'small', min: 0.1, max: 1, step: 0.05, value: refDenoise[0], onChange: function (v) { refDenoise[1](v); }, style: { flex: 1 } })
            ),
            h('div', { style: { fontSize: 10, color: 'var(--ant-color-text-tertiary)', marginTop: 4, lineHeight: '15px' } }, '0.4 轻微改动（最像原图）/ 0.6 常用 / 0.8 大改。提示词写你想改成什么，模型会保留原图构图。')
          ) : null
        ),
        h(Section, { title: '1. 主模型', extra: h(Tag, { color: status.connected ? 'green' : 'red' }, status.connected ? 'ComfyUI 已连' : '未连接') },
          h(Select, { size: 'small', value: model[0] || undefined, placeholder: '选择主模型', style: { width: '100%' },
            options: (d.models || []).map(function (m) { return { value: m.name, label: (m.has_workflow ? '✓ ' : '○ ') + m.name }; }),
            onChange: function (v) { model[1](v); load(v, workflowPreset[0]); }
          }),
          h('div', { style: { marginTop: 6, fontSize: 11, color: 'var(--ant-color-text-secondary)' } },
            hasWorkflow ? ('已绑定：' + (binding.workflow_name || binding.workflow_id)) : '此模型还没有绑定工作流')
        ),
        h(Section, { title: '2. 工作流切换', extra: h('div', { style: { display: 'flex', gap: 4 } },
          h(Button, { size: 'small', type: 'link', style: { padding: 0 }, loading: scanning[0], disabled: scanning[0], onClick: autoBindWorkflow }, '自动适配'),
          h(Button, { size: 'small', type: 'link', style: { padding: 0 }, loading: scanning[0], disabled: scanning[0], onClick: selfCheckWorkflow }, '自检'),
          h(Button, { size: 'small', onClick: saveCurrentWorkflow }, '保存当前')
        ) },
          h(Select, { size: 'small', value: workflowPreset[0] || undefined, placeholder: '选择默认/自定义工作流', style: { width: '100%' },
            options: (d.workflow_presets || []).map(function (p) { return { value: p.id, label: p.name + (p.model_type ? ' · ' + p.model_type : '') }; }),
            onChange: function (v) { applyPreset(v); }
          }),
          h('div', { style: { marginTop: 6, fontSize: 11, color: 'var(--ant-color-text-secondary)' } },
            binding ? ((binding.workflow_name || binding.workflow_id) + ' · 可直接生图') : '没有可用工作流'),
          h('div', { style: { marginTop: 4, fontSize: 10, color: 'var(--ant-color-text-tertiary)', lineHeight: '15px' } },
            '不想挑预设？点右上角「自动适配」：插件会按当前模型的类型自动建一套可运行工作流并绑定（可重复执行）。')
        ),
        !hasWorkflow ? h(Section, { title: '等待工作流' },
          h(Alert, { type: 'info', showIcon: true, message: '点击「一键自动适配」即可开始', description: '插件会自动找到 ComfyUI、扫描模型和 LoRA、选择最优模型并绑定工作流，无需手动操作。' }),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 10 } },
            h(Button, { type: 'primary', size: 'small', onClick: autoBindWorkflow, loading: scanning[0], disabled: scanning[0], block: true }, scanning[0] ? '扫描中...' : '一键自动适配'),
            h(Button, { size: 'small', onClick: scanFailed[0] ? requestAIFindComfyUI : requestAIWorkflow, block: true }, scanFailed[0] ? '复制 AI 提示词（找 ComfyUI）' : '复制 AI 提示词')
          )
        ) : h(React.Fragment, null,
          (caps.needs_clip || caps.needs_vae) ? h(Section, { title: '2.5 模型组件' },
            caps.needs_clip ? (caps.clip_mode === 'single'
              ? h('div', { style: { marginBottom: 8 } },
                  h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 3 } }, '文本编码器'),
                  h(Select, { size: 'small', value: clipName[0] || undefined, placeholder: '选择文本编码器', style: { width: '100%' }, options: kvToOptions(d.clip_options || []), onChange: function (v) { clipName[1](v); } }))
              : h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 8 } }, '文本编码器：双编码器由插件自动选择'))
              : null,
            caps.needs_vae ? h('div', { style: { marginBottom: 8 } },
              h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 3 } }, 'VAE'),
              h(Select, { size: 'small', value: vaeName[0] || undefined, placeholder: '选择 VAE', style: { width: '100%' }, options: kvToOptions(d.vae_options || []), onChange: function (v) { vaeName[1](v); } }))
              : null,
            d.upscale_recommendation ? h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)' } }, '💡 ' + d.upscale_recommendation) : null
          ) : null,
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
          h(Section, { title: '4. 提示词', extra: h('div', { style: { display: 'flex', gap: 4 } },
            h(Button, { size: 'small', onClick: function () { var open = !promptLibOpen[0]; promptLibOpen[1](open); if (open) loadPromptLib(); } }, promptLibOpen[0] ? '收起词库' : '提示词库'),
            h(Button, { size: 'small', onClick: function () { var rec = (promptLib[0] && promptLib[0].default_negative) || 'worst quality, low quality, lowres, blurry, watermark, bad anatomy, bad hands, extra digits'; if (!neg[0].trim()) neg[1](rec); else neg[1](''); } }, '推荐负面词'),
            h(Button, { size: 'small', onClick: savePromptTemplate }, '存为模板')
          ) },
            h(Input.TextArea, { autoSize: { minRows: 7, maxRows: 26 }, showCount: true, value: prompt[0], placeholder: '正向提示词...', maxLength: 2000, onChange: function (e) { prompt[1](e.target.value); }, style: { marginBottom: 6, fontSize: 12, resize: 'vertical', lineHeight: '18px' } }),
            binding && Number(binding.supports_negative_prompt) ? h(Input.TextArea, { autoSize: { minRows: 3, maxRows: 12 }, showCount: true, value: neg[0], placeholder: '负向提示词...', maxLength: 1000, onChange: function (e) { neg[1](e.target.value); }, style: { fontSize: 12, resize: 'vertical', lineHeight: '18px' } }) : null,
            promptLibOpen[0] ? renderPromptLibrary() : null,
            h('div', { style: { display: 'flex', gap: 6, marginTop: 8, alignItems: 'center' } },
              h(Input, { size: 'small', value: aiIdea[0], placeholder: '一句话出图：如「雨夜街头撑伞的银发少女」', onChange: function (e) { aiIdea[1](e.target.value); }, style: { flex: 1, fontSize: 12 }, onPressEnter: aiOneShot }),
              h(Button, { size: 'small', type: 'primary', onClick: aiOneShot }, genMode[0] === 'img2img' ? '✨ 让 AI 以图生图' : '✨ 让 AI 出图')
            )
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
      tab[0] === 'upscale' ? h('div',{style:{flex:1,overflowY:'auto',padding:'0 4px'}},
        h(Section,{title:'待放大区'},
          h(Alert,{type:upscaleProfiles[0].length?'info':'warning',showIcon:true,message:upscaleProfiles[0].length?'已读取 '+upscaleProfiles[0].length+' 个放大模型':'未检测到放大模型',description:upscaleProfiles[0].length?'从图库或本地上传图片到待放区，选好模型后统一点「开始放大」':'请在 ComfyUI 安装放大模型后点击右上角刷新',style:{marginBottom:8}}),
          h('div',{style:{display:'flex',gap:4,marginBottom:6}},
            h('input',{type:'file',accept:'.png,.jpg,.jpeg,.webp',onChange:function(e){var f=e.target.files&&e.target.files[0];if(f){upscaleFile[1](f);doUpscaleUpload(f);e.target.value='';}},style:{flex:1,fontSize:11}}),
            h(Button,{size:'small',onClick:function(){tab[1]('gallery');batchMode[1](true);message.info('选择图片后点「加入待放区」')}},h('span',{style:{fontSize:11}},'从图库选'))
          ),
          upscaleQueueLoading[0] ? h('div',{style:{fontSize:11,color:'var(--ant-color-text-tertiary)',padding:8}},'加载中...') :
          upscaleQueue[0].length ? h('div',{style:{marginBottom:8}},
            h('div',{style:{fontSize:11,color:'var(--ant-color-text-secondary)',marginBottom:4}},'待放区（共 '+upscaleQueue[0].length+' 张）'),
            h('div',{style:{maxHeight:180,overflowY:'auto',border:'1px solid var(--border-color-split)',borderRadius:4,padding:4}},
              upscaleQueue[0].map(function(item){
                var sizeStr = item.file_path ? '—' : '—';
                return h('div',{key:item.id,style:{display:'flex',alignItems:'center',gap:4,padding:'2px 0',borderBottom:'1px solid var(--border-color-split)'}},
                  h('span',{style:{flex:1,fontSize:11,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}},item.file_name || '未知文件'),
                  h('span',{style:{fontSize:10,color:'var(--ant-color-text-tertiary)',marginRight:4}},item.source === 'upload' ? '本地上传' : '图库'),
                  h(Button,{size:'small',type:'text',danger:true,onClick:function(){removeFromUpscaleQueue(item.id);}},'✕')
                );
              })
            )
          ) : h('div',{style:{fontSize:11,color:'var(--ant-color-text-tertiary)',padding:'8px 0',textAlign:'center'}},'待放区为空，从图库选择或上传图片'),
          h('div',{style:{display:'flex',gap:4,marginTop:6}},
            h(Select,{size:'small',value:upscaleMode[0],style:{flex:1},onChange:function(v){upscaleMode[1](v);upscaleDenoise[1](v==='tiled'?0.2:(v==='iterative'?0.4:0.35));},options:[
              {value:'fast',label:'快速放大（只变大，不补细节）'},
              {value:'hires',label:'高清修复（放大 + 补细节）'},
              {value:'tiled',label:'分块高清修复（超大图 / 大图）'},
              {value:'iterative',label:'迭代放大（2x→4x 渐进补细节）'}
            ]}),
            h(Select,{size:'small',value:upscaleScale[0],style:{width:96},onChange:function(v){upscaleScale[1](v);},options:[
              {value:0,label:'倍数：默认'},{value:1,label:'1×'},{value:1.5,label:'1.5×'},{value:2,label:'2×'},{value:3,label:'3×'},{value:4,label:'4×'}
            ]})
          ),
          h('div',{style:{display:'flex',gap:4,marginTop:6}},
            h(Select,{size:'small',value:upscaleProfile[0],style:{flex:1},onChange:function(v){upscaleProfile[1](v);},options:upscaleProfiles[0].map(function(x){return {value:x.id,label:x.label + ' · ' + (x.recommendation || '')};})}),
            h(Select,{size:'small',value:upscaleCategory[0],style:{width:100},onChange:function(v){upscaleCategory[1](v);},options:categories[0].map(function(x){return {value:x,label:x};})})
          ),
          upscaleMode[0] !== 'fast' ? h('div',{style:{marginTop:6,padding:8,border:'1px solid var(--border-color-split)',borderRadius:6,background:'var(--ant-color-fill-quaternary)'}},
            h('div',{style:{display:'flex',gap:6}},
              h('div',{style:{flex:1}}, h('div',{style:{fontSize:10,color:'var(--ant-color-text-secondary)',marginBottom:3}},'重绘幅度 denoise'),
                h(InputNumber,{size:'small',min:0,max:1,step:0.05,value:upscaleDenoise[0],onChange:function(v){upscaleDenoise[1](v);},style:{width:'100%'}})),
              h('div',{style:{flex:1}}, h('div',{style:{fontSize:10,color:'var(--ant-color-text-secondary)',marginBottom:3}},'步数'),
                h(InputNumber,{size:'small',min:1,max:60,step:1,value:upscaleSteps[0],onChange:function(v){upscaleSteps[1](v);},style:{width:'100%'}})),
              h('div',{style:{flex:1}}, h('div',{style:{fontSize:10,color:'var(--ant-color-text-secondary)',marginBottom:3}},'CFG（0=原图）'),
                h(InputNumber,{size:'small',min:0,max:20,step:0.5,value:upscaleCfg[0],onChange:function(v){upscaleCfg[1](v);},style:{width:'100%'}}))
            ),
            h('div',{style:{display:'flex',gap:6,alignItems:'center',marginTop:6}},
              h('span',{style:{fontSize:11}},'随机种子'),
              h(Switch,{size:'small',checked:upscaleSeedMode[0]==='random',onChange:function(v){upscaleSeedMode[1](v?'random':'source');}}),
              upscaleMode[0]==='tiled' ? h(Select,{size:'small',value:upscaleTile[0],style:{flex:1},onChange:function(v){upscaleTile[1](v);},options:[{value:512,label:'分块 512'},{value:768,label:'分块 768'},{value:1024,label:'分块 1024'}]}) : null,
              upscaleMode[0]==='iterative' ? h('div',{style:{display:'flex',alignItems:'center',gap:6,flex:1}},h('span',{style:{fontSize:11}},'迭代步数'),h(InputNumber,{size:'small',min:1,max:6,step:1,value:upscaleIterations[0],onChange:function(v){upscaleIterations[1](v);},style:{width:64}})) : null
            ),
            h('div',{style:{display:'flex',gap:6,alignItems:'center',marginTop:6}},
              h('span',{style:{fontSize:11,whiteSpace:'nowrap'}},'末尾锐化'),
              h(InputNumber,{size:'small',min:0,max:0.3,step:0.02,value:upscaleSharpen[0],onChange:function(v){upscaleSharpen[1](v);},style:{flex:1}}),
              h('span',{style:{fontSize:10,color:'var(--ant-color-text-tertiary)',whiteSpace:'nowrap'}},'0=关闭')
            ),
            h('div',{style:{display:'flex',gap:6,alignItems:'center',marginTop:6}},
              h('span',{style:{fontSize:11,whiteSpace:'nowrap'}},'脸部细化'),
              h(Switch,{size:'small',checked:upscaleFaceDetail[0],onChange:function(v){upscaleFaceDetail[1](v);}}),
              h('span',{style:{fontSize:10,color:'var(--ant-color-text-tertiary)',flex:1}},'放大后用人脸检测再修一次脸（低幅度，人像建议开）')
            ),
            h('div',{style:{display:'flex',gap:6,alignItems:'center',marginTop:6}},
              h('span',{style:{fontSize:11,whiteSpace:'nowrap'}},'放大提示词'),
              h(Select,{size:'small',value:upscalePromptMode[0],style:{flex:1},onChange:function(v){upscalePromptMode[1](v);},options:[
                {value:'auto',label:'自动（低幅度沿用原图，高幅度用画质词，推荐）'},
                {value:'quality',label:'只用画质词（最不容易多出主体）'},
                {value:'full',label:'沿用原图提示词（可能长出新角色）'}
              ]})
            ),
            h('div',{style:{fontSize:10,color:'var(--ant-color-text-tertiary)',marginTop:5,lineHeight:'15px'}},'denoise 0.15 很保守 / 0.2 分块推荐 / 0.35 强；越高越补细节，但脸和构图越容易变。整图「高清修复」会把整张图一次性送进采样器，目标超过约 300 万像素会慢到不可用（8G 显存），这种情况会自动改用分块模式。'),
            upscaleMode[0]==='tiled' ? h('div',{style:{fontSize:10,color:'var(--ant-color-warning)',marginTop:3,lineHeight:'15px'}},'分块模式会把提示词在每个分块上各执行一遍：denoise ≤ 0.25 时自动沿用原图提示词（角色 LoRA 的触发词还在，人物不会变）；denoise 越高越容易在空背景块里长出多余内容，超过 0.25 会自动换成画质词防串戏。') : null
          ) : null,
          h('div',{style:{fontSize:10,color:'var(--ant-color-text-tertiary)',marginTop:4,lineHeight:'15px'}},'二次元 / 插画用 anime_6B 更锐利，写实照片用 x4plus；放大是 4 倍，比较画质请按 100% 或缩放到适合窗口看。'),
          h('div',{style:{display:'flex',gap:4,marginTop:6}},
            h(Button,{type:'primary',block:true,loading:upscaleStarting[0],onClick:startUpscaleQueue,disabled:upscaleStarting[0] || !upscaleQueue[0].length || !upscaleProfiles[0].length},upscaleStarting[0] ? '提交中…' : '开始' + (upscaleMode[0]==='hires'?'高清修复':(upscaleMode[0]==='tiled'?'分块高清修复':'放大'))),
            h(Button,{block:true,disabled:!upscaleQueue[0].length,onClick:clearUpscaleQueue},'清空')
          )
        ),
        h(Section,{title:'说明'},h('div',{style:{fontSize:12,lineHeight:'20px',color:'var(--ant-color-text-secondary)'}},'1. 在图库中选中图片后点「加入待放区」或直接本地上传\n2. 在待放区确认要放大的图片（可移除）\n3. 选择放大模型和保存分类后点「开始放大」\n放大结果将自动保存到图库。'))
      ) : null,
      tab[0] === 'settings' ? h('div',{style:{flex:1,overflowY:'auto'}},
        h(Section,{title:'生成完成与审阅'},
          h('div',{style:{fontSize:11,color:'var(--ant-color-text-secondary)',marginBottom:5}},'生成完成后自动打开结果面板'),
          h(Select,{size:'small',value:setting('review_policy','single'),style:{width:'100%',marginBottom:10},onChange:function(v){setSetting('review_policy',v);},options:[{value:'always',label:'始终自动审阅'},{value:'single',label:'仅单张生成时审阅（推荐）'},{value:'batch',label:'仅批量生成时审阅'},{value:'never',label:'从不自动弹出'}]}),
          h('div',{style:{fontSize:11,color:'var(--ant-color-text-secondary)',marginBottom:5}},'批量审阅最多显示'),
          h(Select,{size:'small',value:Number(setting('review_limit','4')),style:{width:'100%'},onChange:function(v){setSetting('review_limit',v);},options:[{value:1,label:'1 张'},{value:2,label:'2 张'},{value:4,label:'4 张（推荐）'},{value:9,label:'9 张'},{value:99,label:'全部'}]})
        ),
        h(Section,{title:'生成中'},
          h('div',{style:{fontSize:12,lineHeight:'20px',color:'var(--ant-color-text-secondary)'}},'批量生成会按单张依次提交，因此可实时看到「已完成 / 总数」，已完成图片会自动进入图库。停止等待不会中断 ComfyUI 正在跑的当前一张。')
        ),
        h(Section,{title:'当前体验'},h('div',{style:{fontSize:12,lineHeight:'20px',color:'var(--ant-color-text-secondary)'}},'好图可在详情页一键复刻或生成变体；原模型、提示词、尺寸、LoRA 和采样参数会带回工作流。')),
        renderApiSettings()
      ) : null,
      tab[0] === 'gallery' ? h(React.Fragment, null,
        apiPickingRef[0] ? h(Alert, { type: 'info', showIcon: true, style: { margin: '8px 8px 0' }, message: '正在挑选 API 生图参考图：点任意图片即可加入，可连点多张', action: h(Button, { size: 'small', onClick: function () { apiPickingRef[1](false); tab[1]('api'); } }, '完成') }) : null,
        pickingRef[0] ? h(Alert, { type: 'info', showIcon: true, style: { margin: '8px 8px 0' }, message: '正在挑选参考图：点任意图片即可设为以图生图的参考图', action: h(Button, { size: 'small', onClick: function () { pickingRef[1](false); tab[1]('gen'); } }, '取消') }) : null,
        h('div', { style: { padding: 8, display: 'flex', gap: 6, borderBottom: '1px solid var(--border-color-split)' } },
          h(Select, { size: 'small', value: category[0], style: { flex: 1 }, options: [{ value: '', label: '全部分类' }].concat(categories[0].map(function (x) { return { value: x, label: x }; })), disabled: galleryLoading[0] || batchMode[0], onChange: switchCategory }),
          h(Button, { size: 'small', onClick: scanGallery, disabled: batchMode[0] }, '扫描'),
          h(Button, { size: 'small', onClick: function () { req('/gallery/cleanup-missing', { method: 'POST' }).then(function (d) { message.success(d.message || ('已清理 ' + (d.cleaned || 0) + ' 条无源文件记录')); reloadGallery(); }).catch(function (e) { message.error(e.message || '清理失败'); }); }, disabled: batchMode[0] }, '清理失效'),
          h(Button, { size: 'small', onClick: function () { var n = window.prompt('新建分类名称'); if (!n || !n.trim()) return; req('/gallery/categories/create?name=' + encodeURIComponent(n.trim()), { method: 'POST' }).then(function () { loadCategories(); switchCategory(n.trim()); }).catch(function (e) { message.error(e.message); }); }, disabled: batchMode[0] }, '+ 分类'),
          h(Button, { size: 'small', type: batchMode[0] ? 'primary' : 'default', onClick: function () { batchMode[0] ? exitBatchMode() : batchMode[1](true); } }, batchMode[0] ? '取消' : '批量')
        ),
        h('div', { style: { padding: '6px 8px', display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0, overflow: 'hidden', borderBottom: '1px solid var(--border-color-split)', background: 'var(--ant-color-fill-quaternary)' } },
          h('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, .82fr)', gap: 6, minWidth: 0 } },
            h(Select, { size: 'small', value: gallerySort[0], style: { width: '100%', minWidth: 0 }, options: [
              { value: 'newest', label: '排序：最新优先' }, { value: 'oldest', label: '排序：最早优先' },
              { value: 'rating_desc', label: '排序：星级高→低' }, { value: 'rating_asc', label: '排序：星级低→高' },
              { value: 'size_desc', label: '排序：文件大→小' }, { value: 'size_asc', label: '排序：文件小→大' },
              { value: 'model', label: '排序：模型名称' }, { value: 'lora', label: '排序：LoRA 名称' }
            ], onChange: function (v) { gallerySort[1](v); selectedIds[1]([]); loadImages(category[0]); }, disabled: batchMode[0] }),
            h(Select, { size: 'small', value: galleryMinRating[0], style: { width: '100%', minWidth: 0 }, options: [{ value: 0, label: '星级：全部' }, { value: 1, label: '星级：≥ 1 星' }, { value: 2, label: '星级：≥ 2 星' }, { value: 3, label: '星级：≥ 3 星' }, { value: 4, label: '星级：≥ 4 星' }, { value: 5, label: '星级：5 星' }], onChange: function (v) { galleryMinRating[1](v); selectedIds[1]([]); loadImages(category[0]); }, disabled: batchMode[0] })
          ),
          h(Select, { size: 'small', allowClear: true, showSearch: true, optionFilterProp: 'label', placeholder: '筛选模型（可搜索）', value: galleryModel[0] || undefined, style: { width: '100%', minWidth: 0 }, options: (galleryFilterOptions[0].models || []).map(function (x) { return { value: x, label: x }; }), onChange: function (v) { galleryModel[1](v || ''); selectedIds[1]([]); loadImages(category[0]); }, disabled: batchMode[0] }),
          h(Select, { size: 'small', allowClear: true, showSearch: true, optionFilterProp: 'label', placeholder: '筛选 LoRA（可搜索）', value: galleryLora[0] || undefined, style: { width: '100%', minWidth: 0 }, options: (galleryFilterOptions[0].loras || []).map(function (x) { return { value: x, label: x }; }), onChange: function (v) { galleryLora[1](v || ''); selectedIds[1]([]); loadImages(category[0]); }, disabled: batchMode[0] })
        ),
        batchMode[0] ? h('div', { style: { padding: '6px 8px', display: 'flex', gap: 6, alignItems: 'center', borderBottom: '1px solid var(--border-color-split)', background: 'var(--ant-color-fill-quaternary)' } },
          h('span', { style: { fontSize: 12, whiteSpace: 'nowrap' } }, '已选 ' + selectedIds[0].length + ' 张'),
          h(Button, { size: 'small', disabled: batchBusy[0] || !galleryItems().length, onClick: function () { selectedIds[1](selectedIds[0].length === galleryItems().length ? [] : galleryItems().map(function (x) { return x.id; })); } }, selectedIds[0].length === galleryItems().length && galleryItems().length ? '取消全选' : '全选当前页'),
          h(Select, { size: 'small', placeholder: '换分类', disabled: batchBusy[0] || !selectedIds[0].length, style: { flex: 1, minWidth: 88 }, options: categories[0].map(function (x) { return { value: x, label: x }; }), onChange: batchMove }),
          h(Button, { size: 'small', disabled: batchBusy[0] || !selectedIds[0].length, onClick: batchUpscale }, '加入待放区'),
          h(Button, { size: 'small', danger: true, disabled: batchBusy[0] || !selectedIds[0].length, onClick: batchDelete }, '删除')
        ) : null,
        h('div', { style: { flex: 1, overflowY: 'auto', padding: '4px 4px', display: 'flex', flexWrap: 'wrap', gap: 8, alignContent: 'flex-start' } },
          galleryLoading[0] && !(imgs[0]||[]).length ? h('div', { style: { width: '100%', paddingTop: 40, textAlign: 'center', color: 'var(--ant-color-text-secondary)' } }, '正在加载「' + (category[0] || '全部分类') + '」…') :
          galleryItems().length ? (function(){ var items = galleryItems(); var list = items.map(function (img) {
            var checked = selectedIds[0].indexOf(img.id) >= 0;
            return h('div', { key: img.id, onClick: function () { apiPickingRef[0] ? apiAddRefFromGallery(img) : (pickingRef[0] ? prepareRefFromGallery(img) : (batchMode[0] ? toggleSelected(img.id) : preview[1](img.id))); }, style: { position: 'relative', width: 120, height: 120, border: checked ? '2px solid var(--ant-color-primary)' : '1px solid var(--border-color-split)', borderRadius: 6, overflow: 'hidden', cursor: 'pointer', boxSizing: 'border-box', flexShrink: 0 } },
              h('img', { src: iurl(img.id), style: { width: '100%', height: '100%', objectFit: 'cover' } }),
              batchMode[0] ? h('div', { style: { position: 'absolute', top: 4, left: 4, width: 18, height: 18, borderRadius: 10, background: checked ? 'var(--ant-color-primary)' : 'rgba(0,0,0,.5)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, fontWeight: 700 } }, checked ? '✓' : '') : null);
          }); if (galleryHasMore[0] && !galleryLoading[0]) { list.push(h('div', { key: '_loadmore', style: { width: '100%', textAlign: 'center', padding: 8 } },
            h(Button, { size: 'small', onClick: function () { loadImages(category[0], true); } }, '加载更多（' + galleryTotal[0] + ' 张中的 ' + (imgs[0]||[]).length + ' 张）')
          )); } if (galleryLoading[0]) { list.push(h('div', { key: '_loading', style: { width: '100%', textAlign: 'center', padding: 8 } }, '加载中…')); } return list; })() : h('div', { style: { width: '100%', paddingTop: 40, textAlign: 'center' } }, h(Empty, { description: (imgs[0] || []).length ? '没有符合当前筛选条件的图片' : '还没有图片' }))
        )
      ) : null,
      // ── API 服务编辑弹窗 ──
      apiProvForm[0] ? (function () {
        var f = apiProvForm[0];
        function upd(k, v) { var n = Object.assign({}, f); n[k] = v; apiProvForm[1](n); }
        return h(Modal, {
          open: true, width: 470, title: f.id ? '编辑 API 服务' : '添加 API 服务',
          okText: '保存', cancelText: '取消',
          onCancel: function () { apiProvForm[1](null); },
          onOk: function () { saveApiProvider(f); }
        },
          h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)', marginBottom: 10, lineHeight: '17px' } },
            '服务地址填到 /v1 为止（例如 https://api.example.com/v1）。密钥只保存在本机插件数据库，不会上传。'),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '名称'),
            h(Input, { value: f.name, placeholder: '例如：我的图像服务', onChange: function (e) { upd('name', e.target.value); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '服务地址'),
            h(Input, { value: f.base_url, placeholder: 'https://api.example.com/v1', onChange: function (e) { upd('base_url', e.target.value); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, 'API Key' + (f.id ? '（留空表示不修改）' : '')),
            h(Input.Password, { value: f.api_key, placeholder: 'sk-...', onChange: function (e) { upd('api_key', e.target.value); } })),
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
            h('span', { style: { fontSize: 12 } }, '启用'),
            h(Switch, { checked: f.enabled !== false, onChange: function (v) { upd('enabled', v); } }))
        );
      })() : null,

      // ── API 模型条目编辑弹窗 ──
      apiModelForm[0] ? (function () {
        var f = apiModelForm[0];
        function upd(k, v) { var n = Object.assign({}, f); n[k] = v; apiModelForm[1](n); }
        var provList = (apiCfg[0] || {}).providers || [];
        var tpls = (apiCfg[0] || {}).templates || [];
        function applyTemplate(key) {
          var t = tpls.filter(function (x) { return x.key === key; })[0];
          if (!t) return;
          apiModelForm[1](Object.assign({}, f, {
            protocol: t.protocol,
            capabilities: (t.capabilities || []).slice(),
            params: JSON.parse(JSON.stringify(t.params || {}))
          }));
          message.success('已套用「' + t.label + '」，请填写服务方文档里的模型名');
        }
        return h(Modal, {
          open: true, width: 520, title: f.id ? '编辑模型条目' : '添加模型条目',
          okText: '保存', cancelText: '取消',
          onCancel: function () { apiModelForm[1](null); },
          onOk: function () { saveApiModel(f); }
        },
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '所属服务'),
            h(Select, { style: { width: '100%' }, value: f.provider_id || undefined,
              options: provList.map(function (p) { return { value: p.id, label: p.name }; }),
              onChange: function (v) { upd('provider_id', v); } })),
          !f.id ? h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '套用参数模板（可选，推荐）'),
            h(Select, { style: { width: '100%' }, placeholder: '选一个模板自动填好协议与参数',
              options: tpls.map(function (t) { return { value: t.key, label: t.label + ' — ' + t.description }; }),
              onChange: applyTemplate })) : null,
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '模型名（服务方文档里的 model 值）'),
            h(Input, { value: f.model, placeholder: '例如：your-image-model-v1', onChange: function (e) { upd('model', e.target.value); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '显示名（面板里看到的名字）'),
            h(Input, { value: f.label, placeholder: '留空则直接显示模型名', onChange: function (e) { upd('label', e.target.value); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '协议'),
            h(Select, { style: { width: '100%' }, value: f.protocol || 'auto',
              options: [
                { value: 'auto', label: '自动（先试同步，不支持则转异步）' },
                { value: 'openai', label: 'OpenAI 兼容同步' },
                { value: 'media', label: '异步媒体（创建任务 + 轮询）' }
              ],
              onChange: function (v) { upd('protocol', v); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '能力标签'),
            h(Select, { mode: 'multiple', style: { width: '100%' }, value: f.capabilities || [],
              options: [
                { value: 'txt2img', label: '文生图' },
                { value: 'img2img', label: '图生图' },
                { value: 'multi_ref', label: '多参考图' },
                { value: 'transparent', label: '透明底' },
                { value: 'group', label: '组图' }
              ],
              onChange: function (v) { upd('capabilities', v); } })),
          h('div', { style: { marginBottom: 8 } },
            h('div', { style: { fontSize: 12, marginBottom: 4 } }, '参数项：' + Object.keys(f.params || {}).length + ' 个'),
            h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', lineHeight: '17px' } },
              '参数结构由模板决定，面板会按它自动渲染控件（下拉或数字框）。')),
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
            h('span', { style: { fontSize: 12 } }, '启用'),
            h(Switch, { checked: f.enabled !== false, onChange: function (v) { upd('enabled', v); } }))
        );
      })() : null,
      review[0] ? (function(){ var ids=(review[0].ids||[]).slice(-(Number(setting('review_limit','4'))||4)); var all=review[0].ids||[]; var currentId=ids[ids.length-1]; return h(Modal,{open:true,width:720,footer:null,title:'本次生成结果 · '+all.length+' 张',onCancel:function(){review[1](null);}},
        all.length>ids.length ? h(Alert,{type:'info',showIcon:true,message:'本次共生成 '+all.length+' 张，当前展示 '+ids.length+' 张',style:{marginBottom:10}}) : null,
        h('div',{style:{display:'grid',gridTemplateColumns:'repeat(3, 1fr)',gap:8}},ids.map(function(id,i){var im=findImage(id); if(!im){ensureImage(id); return h('div',{key:id,style:{aspectRatio:'1/1',display:'flex',alignItems:'center',justifyContent:'center',border:'1px solid var(--border-color-split)',borderRadius:6,fontSize:11,color:'var(--ant-color-text-tertiary)'}},'加载中…');} return h('div',{key:id,onClick:function(){review[1]({ids:all,index:i});},style:{cursor:'pointer',border:currentId===id?'2px solid var(--ant-color-primary)':'1px solid var(--border-color-split)',borderRadius:6,overflow:'hidden'}},h('img',{src:iurl(id),style:{width:'100%',display:'block',aspectRatio:'1/1',objectFit:'cover'}}));})),
        h('div',{style:{display:'flex',gap:6,marginTop:12}},h(Button,{block:true,onClick:function(){review[1](null);tab[1]('gallery');}},'在图库查看本次全部'),h(Button,{type:'primary',block:true,onClick:function(){var im=findImage(currentId); if(im) bringImageToEditor(im,true);}},'对当前图生成变体'))
      ); })() : null,
      preview[0] ? (function () { var img = findImage(preview[0]); if (!img) { ensureImage(preview[0]); return h(Modal, { open: true, footer: null, width: 560, title: '图片详情', onCancel: function () { preview[1](null); } }, h('div', { style: { padding: 24, textAlign: 'center', color: 'var(--ant-color-text-tertiary)' } }, '正在加载图片信息…')); }
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
        function LoraInfo(value) {
          var lines = loraLines(value);
          return h('div', { style: { marginBottom: 6 } },
            h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginBottom: 2 } }, 'LoRA'),
            h('div', { style: { fontSize: 12, lineHeight: '19px', wordBreak: 'break-all', background: 'var(--ant-color-fill-secondary)', padding: '4px 8px', borderRadius: 4, fontFamily: 'monospace', maxHeight: 100, overflow: 'auto' } },
              lines.length ? lines.map(function (line, i) { return h('div', { key: i, style: { padding: i ? '3px 0 0' : 0, marginTop: i ? 3 : 0, borderTop: i ? '1px solid var(--border-color-split)' : 'none' } }, line); }) : '—'
            )
          );
        }
        var recipeText = (img.prompt || '') + (img.negative_prompt ? '\nNegative: ' + img.negative_prompt : '') + '\nModel: ' + (img.model_name || '') + (img.lora_name ? '\nLoRA: ' + img.lora_name : '') + '\nSteps: ' + (img.steps || 20) + '  CFG: ' + (img.cfg || 7) + '  Seed: ' + (img.seed || -1) + '  Size: ' + (img.width || 1024) + 'x' + (img.height || 1024);
        return h(Modal, { open: true, footer: null, width: 560, onCancel: function () { preview[1](null); },
          styles: { body: { padding: '12px 16px', maxHeight: '80vh', overflowY: 'auto' } } },
          h('img', { src: iurl(img.id), style: { width: '100%', borderRadius: 8, marginBottom: 12 } }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 } },
            h(Rate, { value: img.rating || 0, onChange: function (v) { req('/images/' + img.id + '/rating', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating: v }) }).then(function () { loadImages(category[0]); }).catch(function (e) { message.error(e.message || '评分保存失败'); }); } }),
            h('div',{style:{display:'flex',gap:4,flexWrap:'wrap',justifyContent:'flex-end'}},h(Button, { size: 'small', onClick: function () { prepareRefFromGallery(img); } }, '用作参考图'),h(Button, { size: 'small', onClick: function () { bringImageToEditor(img, false); } }, '复刻这张'),h(Button, { size: 'small', onClick: function () { bringImageToEditor(img, true); } }, '生成变体'),h(Button, { size: 'small', onClick: function () { upscaleGalleryImage(img); } }, '放大这张'),h(Button, { size: 'small', onClick: function () { copyText(recipeText); } }, '复制参数'),h(Button, { size: 'small', onClick: function () { loadPortable(img); } }, '可复现信息'))
          ),
          h('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginBottom: 10 } },
            h('span', { style: { fontSize: 11, color: 'var(--ant-color-text-secondary)' } }, '所属分类'),
            h(Select, { size: 'small', value: img.category || '未分类', options: categories[0].map(function (x) { return { value: x, label: x }; }), style: { flex: 1 }, onChange: function (v) { req('/images/' + img.id + '/category?category=' + encodeURIComponent(v), { method: 'POST' }).then(function () { message.success('已移动到「' + v + '」'); preview[1](null); loadImages(category[0]); }).catch(function (e) { message.error(e.message); }); } })
          ),
          h(Divider, { style: { margin: '4px 0 10px' } }),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 16px' } },
            InfoRow('模型', img.model_name, true),
            LoraInfo(img.lora_name),
            InfoRow('尺寸', (img.width || 1024) + ' × ' + (img.height || 1024), false),
            InfoRow('文件大小', fileSizeMB(img.file_size), false),
            InfoRow('Steps', img.steps, false),
            InfoRow('CFG', img.cfg, false),
            InfoRow('Seed', img.seed, false),
            InfoRow('生成时间', fmtTime(img.generated_at || img.created_at), false)
          ),
          h(Divider, { style: { margin: '8px 0 10px' } }),
          InfoRow('正向提示词', img.prompt, true),
          InfoRow('反向提示词', img.negative_prompt || '', true)
        );
      })() : null,
      portable[0] ? (function () {
        var pd = portable[0], pkg = pd.portable || {}, gen = pkg.generation || {}, deps = pkg.dependencies || [], missing = pd.missing_dependencies || [];
        var missingNames = {}; missing.forEach(function (m) { missingNames[m.file_name] = true; });
        function copyPkg() { var t = JSON.stringify(pkg, null, 2); if (navigator.clipboard) { navigator.clipboard.writeText(t).then(function () { message.success('已复制完整包 JSON'); }).catch(function () { message.info(t); }); } else { message.info(t); } }
        var genKeys = ['prompt','negative_prompt','model_name','lora_name','steps','cfg','seed','width','height','sampler_name','scheduler','clip_name','vae_name'];
        return h(Modal, { open: true, footer: null, width: 620, title: '可复现信息', onCancel: function () { portable[1](null); },
          styles: { body: { padding: '12px 16px', maxHeight: '80vh', overflowY: 'auto' } } },
          h(Alert, { type: pd.can_reproduce_now ? 'success' : (pd.comfy_connected ? 'warning' : 'info'), showIcon: true, message: pd.message || '', style: { marginBottom: 10 } }),
          h('div', { style: { fontSize: 11, color: 'var(--ant-color-text-tertiary)', marginBottom: 8 } },
            '来源：' + (pd.source === 'embedded' ? '图片内嵌自描述包' : '图库记录重建（旧图无内嵌包）') + (pkg._reconstructed ? ' · 部分信息' : '')),
          h('div', { style: { fontSize: 12, fontWeight: 700, margin: '8px 0 4px' } }, '生成参数'),
          h('div', { style: { fontSize: 12, lineHeight: '19px', background: 'var(--ant-color-fill-secondary)', padding: '6px 8px', borderRadius: 4, fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 170, overflow: 'auto' } },
            genKeys.filter(function (k) { return gen[k] !== undefined && gen[k] !== null && gen[k] !== ''; }).map(function (k) { return k + ': ' + (typeof gen[k] === 'object' ? JSON.stringify(gen[k]) : gen[k]); }).join('\n') || '（无）'),
          h('div', { style: { fontSize: 12, fontWeight: 700, margin: '10px 0 4px' } }, '模型依赖（' + deps.length + '）'),
          deps.length ? h('div', null, deps.map(function (dep, i) {
            var miss = !!missingNames[dep.file_name];
            return h('div', { key: i, style: { fontSize: 12, padding: '4px 8px', marginBottom: 4, borderRadius: 4, background: miss ? 'rgba(255,77,79,.12)' : 'var(--ant-color-fill-secondary)', color: miss ? '#ff4d4f' : 'inherit', wordBreak: 'break-all' } },
              (miss ? '✗ 缺失：' : '✓ ') + dep.file_name + (dep.model_type ? ' · ' + dep.model_type : ''));
          })) : h('div', { style: { fontSize: 12, color: 'var(--ant-color-text-tertiary)' } }, '（无）'),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 12 } },
            h(Button, { size: 'small', onClick: copyPkg }, '复制完整包 JSON'),
            h(Button, { size: 'small', onClick: function () { portable[1](null); } }, '关闭'))
        );
      })() : null
    );
  }

  function toggleUI(show) {
    var btn = document.getElementById(pid + '-btn');
    var panel = document.getElementById(pid + '-panel');
    var resizer = document.getElementById(pid + '-resizer');
    if (btn) btn.style.display = show ? '' : 'none';
    if (panel && !show) {
      panel.classList.remove('open');
      if (btn) btn.classList.remove('open');
      if (resizer) resizer.style.display = 'none';
    }
  }

  function injectUI() {
    if (document.getElementById(pid + '-style')) return;
    var style = document.createElement('style');
    style.id = pid + '-style';
    style.textContent = [
      '#' + pid + '-btn{position:fixed;right:0;top:50%;z-index:999;transform:translateY(-50%);width:22px;height:50px;border:none;background:var(--ant-primary-color,#8EA7FF);color:#fff;border-radius:4px 0 0 4px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:right .3s;box-shadow:-2px 0 8px rgba(0,0,0,.1)}',
      '#' + pid + '-btn.open{right:var(--' + pid + '-panel-width,360px)}',
      '#' + pid + '-panel{position:fixed;top:0;right:0;width:var(--' + pid + '-panel-width,360px);height:100vh;z-index:998;background:var(--ant-color-bg-container,#fff);border-left:1px solid var(--border-color-split,#e8e8e8);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s;box-shadow:-4px 0 20px rgba(0,0,0,.08)}',
      '#' + pid + '-panel .ant-select{min-width:0!important;max-width:100%!important}#' + pid + '-panel .ant-select-selector{min-width:0!important;overflow:hidden!important}#' + pid + '-panel .ant-select-selection-item,#' + pid + '-panel .ant-select-selection-placeholder{min-width:0!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important}',
      '#' + pid + '-panel.open{transform:translateX(0)}',
      '#' + pid + '-resizer{position:fixed;top:0;right:calc(var(--' + pid + '-panel-width,360px) - 4px);width:8px;height:100vh;cursor:col-resize;z-index:1001;background:transparent;transition:background .15s}',
      '#' + pid + '-resizer:hover,#' + pid + '-resizer.active{background:var(--ant-primary-color,#8EA7FF);opacity:.4}',
      'body.' + pid + '-resizing,body.' + pid + '-resizing *{cursor:col-resize!important;user-select:none!important}'
    ].join('\n');
    document.head.appendChild(style);
    var btn = document.createElement('button');
    btn.id = pid + '-btn'; btn.textContent = '✨'; btn.title = 'ComfyUI 生图助手'; btn.style.display = isOn() ? '' : 'none';
    var panel = document.createElement('div'); panel.id = pid + '-panel';
    // 拖动条挂在 body 上（panel 的子节点归 React 管理，不能被顶掉），定位在面板左边缘。
    var resizer = document.createElement('div'); resizer.id = pid + '-resizer'; resizer.title = '拖动调整宽度（双击复位）'; resizer.style.display = 'none';
    document.body.appendChild(btn); document.body.appendChild(panel); document.body.appendChild(resizer);

    // ── 面板宽度：可拖动，记忆到 localStorage ──────────────────────────────
    var widthVar = '--' + pid + '-panel-width';
    function clampPanelWidth(w) {
      var max = Math.max(360, (window.innerWidth || 1280) - 180);
      return Math.max(320, Math.min(max, Math.round(w)));
    }
    function applyPanelWidth(w, persist) {
      var value = clampPanelWidth(w);
      document.documentElement.style.setProperty(widthVar, value + 'px');
      if (persist !== false) { try { localStorage.setItem(pid + '-panel-width', String(value)); } catch (e) {} }
      return value;
    }
    var savedWidth = 0;
    try { savedWidth = parseInt(localStorage.getItem(pid + '-panel-width') || '', 10) || 0; } catch (e) {}
    applyPanelWidth(savedWidth || 360, false);

    var resizing = false;
    resizer.addEventListener('mousedown', function (e) {
      e.preventDefault(); e.stopPropagation();
      resizing = true;
      resizer.classList.add('active');
      document.body.classList.add(pid + '-resizing');
      panel.style.transition = 'none'; btn.style.transition = 'none';
    });
    window.addEventListener('mousemove', function (e) {
      if (!resizing) return;
      applyPanelWidth((window.innerWidth || 1280) - e.clientX);
    });
    window.addEventListener('mouseup', function () {
      if (!resizing) return;
      resizing = false;
      resizer.classList.remove('active');
      document.body.classList.remove(pid + '-resizing');
      panel.style.transition = ''; btn.style.transition = '';
    });
    resizer.addEventListener('dblclick', function (e) { e.stopPropagation(); applyPanelWidth(360); });
    window.addEventListener('resize', function () {
      var current = parseInt(getComputedStyle(document.documentElement).getPropertyValue(widthVar), 10) || 360;
      applyPanelWidth(current);
    });

    btn.onclick = function () {
      var open = !panel.classList.contains('open');
      panel.classList.toggle('open', open);
      btn.classList.toggle('open', open);
      resizer.style.display = open ? '' : 'none';
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
        h('p', { style: { color: 'var(--ant-color-error)', fontWeight: 700, fontSize: 14 } }, '⚠️ 将清理失效绑定和输出目录配置，不会删除图库原图。'),
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
          okText: '确认修复配置',
          okButtonProps: { danger: true },
          cancelText: '取消',
          width: 520
        },
          h('p', { style: { fontSize: 14, fontWeight: 700, color: 'red' } },
            '这是最后一次确认机会。'),
          h('p', { style: { marginTop: 12 } },
            '点击「确认修复配置」后：'),
          h('ul', { style: { lineHeight: 2 } },
            h('li', null, '所有绑定、预设、图库、配方将立即删除'),
            h('li', null, '图片文件将被彻底清除'),
            h('li', null, '配置将恢复为默认值'),
            h('li', null, '页面将在 3 秒后自动刷新')
          ),
          h('p', { style: { color: 'red', fontWeight: 700, marginTop: 16, fontSize: 14 } },
            '🔴 请确认要清理失效绑定并恢复配置。图库原图和图片记录会保留。')
        )
      );
    }
    Q.route.add(pid, { id: pid + '.settings', path: '/image-gen-settings', component: RepairPanel });
    function MenuSwitch() {
      var checked = React.useState(isOn());
      return h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' } },
        h('span', null, '✨ ComfyUI 生图助手'),
        h(Switch, { size: 'small', checked: checked[0], onClick: function(checked, e){ if(e){e.stopPropagation();e.preventDefault();} }, onChange: function(v,e){ if(e){e.stopPropagation();e.preventDefault();} checked[1](v); setOn(v); emitToggle(v); } })
      );
    }
    Q.menu.add(pid, { id: pid + '.menu', label: h(MenuSwitch), route: pid + '.settings', location: 'primary.agentScoped', order: 66 });
  }
})();
