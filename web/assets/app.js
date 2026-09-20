/* ==========================================================================
   Agent Zero To One · 网页应用逻辑
   --------------------------------------------------------------------------
   数据来自 content.js（由 web/_build.py 从 Markdown 生成），
   正文渲染交给 assets/markdown.js，样式在 assets/app.css。
   本文件只做三件事：路由、进度、搜索。
   ========================================================================== */

(function () {
  'use strict';

  var DATA = window.AZTO;
  var MD = window.AZTO_MD;

  if (!DATA) {
    document.body.innerHTML =
      '<div style="padding:40px;font:15px/1.8 system-ui,sans-serif">' +
      '<h1>缺少 content.js</h1><p>内容文件没有找到。请在项目根目录运行：</p>' +
      '<pre style="background:#f0f2f8;padding:12px;border-radius:8px">python web/_build.py</pre></div>';
    return;
  }

  /* ------------------------------------------------------------------ 工具 */

  function $(id) { return document.getElementById(id); }

  /** 读一个 <select> 当前选中的文本（不存在时返回空串） */
  function selectValue(id) {
    var element = document.getElementById(id);
    return element ? element.value : '';
  }

  /** HTML 转义 —— 所有来自数据的文本都要过这一道 */
  function h(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /**
   * 行内 Markdown 渲染。用于来自项目文档的短文本（章节目标、摘要）——
   * 它们含 **粗体**、`代码`、[[wikilink]]，纯转义会把标记符号原样显示出来。
   * renderInline 内部自带 HTML 转义，不会引入注入风险。
   */
  function ri(text) {
    if (!text) return '';
    try { return MD.renderInline(text); } catch (err) { return h(text); }
  }

  /** 触发浏览器下载一段 JSON（导出进度、导出面试报告都用它） */
  function downloadJson(filename, payload) {
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  /** 取路径参数末尾一段：'#/chapter/05' -> '05' */
  function last(parts) { return parts[parts.length - 1]; }

  function byNo(no) {
    for (var i = 0; i < DATA.chapters.length; i++) {
      if (DATA.chapters[i].no === no) return DATA.chapters[i];
    }
    return null;
  }

  function chapterIndex(no) {
    for (var i = 0; i < DATA.chapters.length; i++) {
      if (DATA.chapters[i].no === no) return i;
    }
    return -1;
  }

  function findNote(week) {
    for (var i = 0; i < DATA.notes.length; i++) {
      if (String(DATA.notes[i].week) === String(week)) return DATA.notes[i];
    }
    return null;
  }

  function findInList(list, id) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id || list[i].id === decodeURIComponent(id)) return list[i];
    }
    return null;
  }

  function wikiKind(kind) {
    if (kind === 'topics') return { list: DATA.wiki.topics, label: '专题总结', badge: '专题' };
    if (kind === 'cheatsheets') return { list: DATA.wiki.cheatsheets, label: '速查表', badge: '速查' };
    if (kind === 'interview') return { list: DATA.wiki.interview, label: '面试题库', badge: '题库' };
    return null;
  }

  function words(text) { return Math.round((text || '').length / 1000) + 'k 字'; }

  /* -------------------------------------------------------------- 进度存储 */

  var STORE_KEY = 'azto.progress.v1';

  var Progress = {
    data: { chapters: {}, notes: {}, projects: {} },

    load: function () {
      try {
        var raw = localStorage.getItem(STORE_KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          if (parsed && parsed.chapters) this.data = parsed;
        }
      } catch (err) {
        // localStorage 被禁用（隐私模式）时静默降级为内存态
      }
      if (!this.data.chapters) this.data.chapters = {};
      if (!this.data.notes) this.data.notes = {};
      if (!this.data.projects) this.data.projects = {};
    },

    save: function () {
      try { localStorage.setItem(STORE_KEY, JSON.stringify(this.data)); } catch (err) { /* 忽略 */ }
    },

    /** 读：不存在就返回空对象，**不要**顺手写进存储（否则会存下一堆空壳条目） */
    chapter: function (no) {
      return this.data.chapters[no] || {};
    },

    /** 写：确认要改这个章节时才创建条目 */
    ensureChapter: function (no) {
      if (!this.data.chapters[no]) this.data.chapters[no] = {};
      return this.data.chapters[no];
    },

    toggleChapter: function (no, key, value) {
      var item = this.ensureChapter(no);
      item[key] = value;
      this.save();
      this.refresh();
    },

    isChapterDone: function (no) {
      var item = this.chapter(no);
      return !!(item.run && item.explain && item.modify);
    },

    toggleNote: function (week) {
      this.data.notes[week] = !this.data.notes[week];
      this.save();
      this.refresh();
    },

    toggleProject: function (id) {
      this.data.projects[id] = !this.data.projects[id];
      this.save();
      this.refresh();
    },

    /** 总进度 = 已完成条目 / 全部条目（章节三勾权重最高，因为它们是核心） */
    percent: function () {
      var done = 0, total = DATA.chapters.length * 3 + DATA.ladder.length + DATA.notes.length;
      for (var i = 0; i < DATA.chapters.length; i++) {
        var item = this.chapter(DATA.chapters[i].no);
        if (item.run) done++;
        if (item.explain) done++;
        if (item.modify) done++;
      }
      for (var j = 0; j < DATA.ladder.length; j++) {
        if (this.data.projects[DATA.ladder[j].id]) done++;
      }
      for (var k = 0; k < DATA.notes.length; k++) {
        if (this.data.notes[DATA.notes[k].week]) done++;
      }
      return total === 0 ? 0 : Math.round(done / total * 100);
    },

    chapterDoneCount: function (no) {
      var item = this.chapter(no);
      return (item.run ? 1 : 0) + (item.explain ? 1 : 0) + (item.modify ? 1 : 0);
    },

    reset: function () {
      this.data = { chapters: {}, notes: {}, projects: {} };
      this.save();
      this.refresh();
    },

    exportJson: function () {
      downloadJson('azto-progress-' + new Date().toISOString().slice(0, 10) + '.json', {
        project: 'Agent Zero To One',
        exportedAt: new Date().toISOString(),
        progress: this.data,
        percent: this.percent()
      });
    },

    importJson: function (text) {
      var parsed = JSON.parse(text);
      var incoming = parsed && parsed.progress ? parsed.progress : parsed;
      if (!incoming || typeof incoming !== 'object') throw new Error('格式不对');
      this.data = {
        chapters: incoming.chapters || {},
        notes: incoming.notes || {},
        projects: incoming.projects || {}
      };
      this.save();
      this.refresh();
    },

    /** 进度变化后，同步顶栏百分比与侧栏的小圆点 */
    refresh: function () {
      var pill = $('progressPill');
      if (pill) pill.querySelector('.progress-pill-num').textContent = this.percent() + '%';

      var links = document.querySelectorAll('.nav-chapter');
      for (var i = 0; i < links.length; i++) {
        var no = links[i].getAttribute('data-no');
        links[i].classList.toggle('done', this.isChapterDone(no));
      }

      // 页面上的章节卡若在场，同步它们的勾选状态与完成样式
      var cards = document.querySelectorAll('.chapter-card');
      for (var j = 0; j < cards.length; j++) {
        var cardNo = cards[j].getAttribute('data-no');
        var state = this.chapter(cardNo);
        var boxes = cards[j].querySelectorAll('.check input');
        var keys = ['run', 'explain', 'modify'];
        for (var b = 0; b < boxes.length && b < keys.length; b++) {
          boxes[b].checked = !!state[keys[b]];
        }
        var counter = cards[j].querySelector('.chapter-progress');
        if (counter) counter.textContent = this.chapterDoneCount(cardNo) + '/3';
        cards[j].classList.toggle('complete', this.isChapterDone(cardNo));
      }
    }
  };

  /* ------------------------------------------------------------------ 主题 */

  var Theme = {
    KEY: 'azto.theme',

    init: function () {
      var saved = null;
      try { saved = localStorage.getItem(this.KEY); } catch (err) { /* 忽略 */ }
      this.apply(saved || this.systemDefault());
    },

    systemDefault: function () {
      return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
        ? 'dark' : 'light';
    },

    apply: function (mode) {
      document.documentElement.setAttribute('data-theme', mode);
      try { localStorage.setItem(this.KEY, mode); } catch (err) { /* 忽略 */ }
    },

    toggle: function () {
      var now = document.documentElement.getAttribute('data-theme');
      this.apply(now === 'dark' ? 'light' : 'dark');
    }
  };

  /* -------------------------------------------------------------- 侧栏导航 */

  function renderNav() {
    var html = [];
    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">开始</div>');
    html.push(navLink('#/', '首页'));
    html.push(navLink('#/path', '学习路径'));
    html.push(navLink('#/plan', '12 周计划'));
    html.push(navLink('#/progress', '我的进度'));
    html.push('</div>');

    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">教程</div>');
    for (var s = 0; s < DATA.stages.length; s++) {
      var stage = DATA.stages[s];
      html.push('<div class="nav-sub">阶段' + '一二三四五'[s] + ' · ' + h(stage.name) + '</div>');
      for (var c = 0; c < stage.chapters.length; c++) {
        var chapter = byNo(stage.chapters[c]);
        if (!chapter) continue;
        html.push(
          '<a class="nav-item nav-chapter" data-no="' + h(chapter.no) + '" href="#/chapter/' + h(chapter.no) + '">' +
            '<span class="nav-no">' + h(chapter.no) + '</span>' +
            '<span class="nav-label">' + h(chapter.title) + '</span>' +
          '</a>'
        );
      }
    }
    html.push('</div>');

    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">知识库</div>');
    html.push(navLink('#/wiki/topics', '专题总结', DATA.wiki.topics.length));
    html.push(navLink('#/wiki/cheatsheets', '速查表', DATA.wiki.cheatsheets.length));
    html.push('</div>');

    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">面试准备</div>');
    html.push(navLink('#/mock', '模拟面试', DATA.mock.total));
    html.push(navLink('#/wiki/interview', '面试题库', DATA.wiki.interview.length));
    html.push(navLink('#/mock/docs', '面试流程与评分'));
    html.push(navLink('#/project', '项目实战案例',
      DATA.project ? DATA.project.stats.exercises : null));
    html.push(navLink('#/algo', '算法面试轨道',
      DATA.stats.algoProblems ? DATA.stats.algoProblems : null));
    html.push(navLink('#/judge', '在线判题', DATA.stats.algoProblems ? null : null));
    html.push('</div>');

    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">实践</div>');
    html.push(navLink('#/ladder', '项目阶梯'));
    html.push(navLink('#/notes', '周学习笔记'));
    html.push(navLink('#/config', '学习配置'));
    html.push('</div>');

    html.push('<div class="nav-group">');
    html.push('<div class="nav-group-title">其他</div>');
    html.push(navLink('#/raw', '规划与调研'));
    html.push(navLink('#/hub', '学习中枢'));
    html.push(navLink('#/about', '关于'));
    html.push('</div>');

    $('nav').innerHTML = html.join('');
  }

  function navLink(route, label, count) {
    var badge = (count == null) ? '' : ' <span class="nav-count">' + count + '</span>';
    return '<a class="nav-item" href="' + route + '">' + h(label) + badge + '</a>';
  }

  function markActiveNav(route) {
    var links = document.querySelectorAll('.nav-item');
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute('href') || '';
      links[i].classList.toggle('active', href === route ||
        (route.indexOf('#/chapter/') === 0 && href === '#/path'));
    }
  }

  /* ---------------------------------------------------------- 可复用的片段 */

  function pageHead(title, sub, actions) {
    return '<div class="page-head">' +
      '<div><h1 class="page-title">' + h(title) + '</h1>' +
      (sub ? '<p class="page-sub">' + sub + '</p>' : '') + '</div>' +
      (actions ? '<div class="page-actions">' + actions + '</div>' : '') +
      '</div>';
  }

  function checkboxes(no, state) {
    var keys = ['run', 'explain', 'modify'];
    var labels = { run: '跑通', explain: '讲清', modify: '改得动' };
    var html = '<div class="checks">';
    for (var i = 0; i < keys.length; i++) {
      html += '<label class="check">' +
        '<input type="checkbox" data-no="' + h(no) + '" data-key="' + keys[i] + '"' +
        (state[keys[i]] ? ' checked' : '') + '>' +
        '<span class="check-label">' + labels[keys[i]] + '</span></label>';
    }
    return html + '</div>';
  }

  function chapterCard(chapter) {
    var state = Progress.chapter(chapter.no);
    var done = Progress.chapterDoneCount(chapter.no);
    return '<article class="chapter-card' + (Progress.isChapterDone(chapter.no) ? ' complete' : '') +
        '" data-no="' + h(chapter.no) + '">' +
      '<div class="chapter-top">' +
        '<span class="chapter-no">' + h(chapter.no) + '</span>' +
        '<h3 class="chapter-title">' + h(chapter.title) + '</h3>' +
      '</div>' +
      '<p class="chapter-goal">' + ri(chapter.goal) + '</p>' +
      '<div class="chapter-meta">' +
        (chapter.hours ? '<span class="meta-chip">' + chapter.hours + ' 小时</span>' : '') +
        '<span class="meta-chip">前置：' + h(chapter.prereq || '无') + '</span>' +
        (chapter.code ? '<span class="meta-chip mono">' + h(chapter.code) + '</span>' : '') +
      '</div>' +
      checkboxes(chapter.no, state) +
      '<div class="chapter-foot">' +
        '<a class="btn btn-sm" href="#/chapter/' + h(chapter.no) + '">读正文</a>' +
        '<span class="chapter-progress">' + done + '/3</span>' +
      '</div>' +
    '</article>';
  }

  function cardGrid(items) {
    if (!items.length) return '<p class="empty">这里还没有内容。</p>';
    var html = '<div class="card-grid">';
    for (var i = 0; i < items.length; i++) {
      html += items[i];
    }
    return html + '</div>';
  }

  /* ------------------------------------------------------------------ 视图 */

  var views = {};

  /* 首页 ------------------------------------------------------------------ */
  views.home = function () {
    var s = DATA.stats;
    var html = [];

    html.push('<section class="hero">');
    html.push('<h1 class="hero-title">' + h(DATA.meta.subtitle) + '</h1>');
    html.push('<p class="hero-sub">' + h(DATA.meta.tagline) + '</p>');
    html.push('<div class="hero-actions">' +
      '<a class="btn btn-primary" href="#/chapter/00">从第 00 章开始</a>' +
      '<a class="btn btn-ghost" href="#/path">看学习路径</a>' +
      '<a class="btn btn-ghost" href="#/plan">12 周计划</a>' +
      '</div>');
    html.push('</section>');

    // 总进度
    var percent = Progress.percent();
    html.push('<section class="page-body">');
    html.push('<div class="progress-bar"><div class="progress-fill" style="width:' + percent + '%"></div></div>');
    html.push('<p class="page-sub" style="margin-top:8px">当前总进度 ' + percent + '% —— ' +
      '<a href="#/progress">查看明细</a></p>');
    html.push('</section>');

    // 数字
    html.push('<section class="page-body">');
    html.push('<div class="stat-grid">');
    var stats = [
      [s.chapters, '章教程'], [s.scripts, '个可运行脚本'], [s.topics, '个专题总结'],
      [s.cheatsheets, '份速查表'], [s.notes, '周学习笔记'], [s.ladder, '级项目阶梯'],
      [s.mockQuestions, '道模拟面试题'], [s.algoProblems, '道算法详解'], [s.hours + 'h', '主线学时']
    ];
    for (var i = 0; i < stats.length; i++) {
      html.push('<div class="stat-card"><span class="stat-num">' + h(stats[i][0]) + '</span>' +
        '<span class="stat-label">' + h(stats[i][1]) + '</span></div>');
    }
    html.push('</div>');
    html.push('</section>');

    // 四项目能力地图
    html.push('<section class="page-body">');
    html.push('<div class="page-head"><div><h2 class="page-title">这个项目整合了什么</h2>' +
      '<p class="page-sub">四个优质开源学习项目，各占一层，拼成一套完整的学习系统</p></div></div>');
    html.push('<div class="source-grid">');
    for (var k = 0; k < DATA.sources.length; k++) {
      var src = DATA.sources[k];
      html.push('<article class="source-card is-' + h(src.color) + '">');
      html.push('<div class="source-layer">' + h(src.layer) + '</div>');
      html.push('<h3 class="source-name">' + h(src.name) + '</h3>');
      html.push('<p class="source-solves">解决：' + ri(src.solves) + '</p>');
      html.push('<ul class="source-gives">');
      for (var g = 0; g < src.gives.length; g++) {
        html.push('<li>' + h(src.gives[g]) + '</li>');
      }
      html.push('</ul>');
      html.push('<div class="source-lands">');
      for (var l = 0; l < src.lands.length; l++) {
        html.push('<span class="land-chip">' + h(src.lands[l]) + '</span>');
      }
      html.push('</div>');
      html.push('<a class="source-link" href="' + h(src.repo) + '" target="_blank" rel="noopener">查看原项目 →</a>');
      html.push('</article>');
    }
    html.push('</div>');
    html.push('</section>');

    // 五个阶段
    html.push('<section class="page-body">');
    html.push('<div class="page-head"><div><h2 class="page-title">五个阶段</h2>' +
      '<p class="page-sub">按能力依赖链排序，每章的前置章节写在章首</p></div>' +
      '<div class="page-actions"><a class="btn btn-ghost" href="#/path">展开全部章节</a></div></div>');
    for (var st = 0; st < DATA.stages.length; st++) {
      var stage = DATA.stages[st];
      var names = [];
      for (var ch = 0; ch < stage.chapters.length; ch++) {
        var chapter = byNo(stage.chapters[ch]);
        if (chapter) names.push(chapter.no + ' ' + chapter.title);
      }
      html.push('<div class="stage-block">');
      html.push('<div class="stage-head"><div>' +
        '<h2 class="stage-name">阶段' + '一二三四五'[st] + ' · ' + h(stage.name) + '</h2>' +
        '<p class="stage-desc">' + ri(stage.desc) + '</p>' +
        '<p class="stage-desc" style="margin-top:4px">' + h(names.join('　·　')) + '</p>' +
        '</div><span class="stage-weeks">' + h(stage.weeks) + '</span></div>');
      html.push('</div>');
    }
    html.push('</section>');

    // 快速入口
    html.push('<section class="page-body">');
    html.push('<div class="tabs">' +
      '<a class="tab" href="#/wiki/topics">专题总结</a>' +
      '<a class="tab" href="#/wiki/cheatsheets">速查表</a>' +
      '<a class="tab" href="#/mock">模拟面试</a>' +
      (DATA.project ? '<a class="tab" href="#/project">项目实战案例</a>' : '') +
      '<a class="tab" href="#/wiki/interview">面试题库</a>' +
      '<a class="tab" href="#/algo">算法面试轨道</a>' +
      '<a class="tab" href="#/ladder">项目阶梯</a>' +
      '<a class="tab" href="#/hub">学习中枢</a>' +
      '</div>');
    html.push('</section>');

    // 面试准备引导（这是"学完之后"的落点）
    html.push('<section class="page-body">');
    html.push('<div class="page-head"><div><h2 class="page-title">学完之后：模拟面试</h2>' +
      '<p class="page-sub">会做不等于会讲。' + DATA.mock.total + ' 道结构化题目，' +
      '逐题自答后给参考思路、加分点与扣分点</p></div>' +
      '<div class="page-actions"><a class="btn btn-primary" href="#/mock">开始一场</a></div></div>');
    html.push('<div class="card-grid">');
    for (var m = 0; m < DATA.mock.rounds.length; m++) {
      var mockRound = DATA.mock.rounds[m];
      html.push('<a class="card" href="#/mock">' +
        '<h3 class="card-title">' + h(mockRound.name) + '</h3>' +
        '<p class="card-summary">' + mockRound.count + ' 道题</p>' +
        '<div class="card-foot"><span class="badge">' + h(mockRound.prefix) + '</span></div></a>');
    }
    html.push('</div>');
    html.push('</section>');

    // 项目实战案例：一份真实项目的标准答案（和上面"练的设施"配套）
    if (DATA.project) {
      html.push('<section class="page-body">');
      html.push('<div class="page-head"><div><h2 class="page-title">再看一份标准答案：项目实战案例</h2>' +
        '<p class="page-sub">上面那套题解决「怎么练」，这一份解决「好的回答长什么样」——' +
        '拿一个真实的 Go Agent 平台从头拆到源码行号</p></div>' +
        '<div class="page-actions"><a class="btn btn-primary" href="#/project">进入</a></div></div>');
      html.push('<div class="stat-grid">');
      var projectStats = [
        [DATA.project.stats.documents, '篇复盘文档'],
        [DATA.project.stats.lines.toLocaleString(), '行内容'],
        [DATA.project.stats.dialogue, '轮面试对话'],
        [DATA.project.stats.exercises, '道练习题']
      ];
      for (var ps = 0; ps < projectStats.length; ps++) {
        html.push('<div class="stat-card"><span class="stat-num">' +
          h(projectStats[ps][0]) + '</span><span class="stat-label">' +
          h(projectStats[ps][1]) + '</span></div>');
      }
      html.push('</div>');
      html.push('</section>');
    }

    return html.join('');
  };

  /* 学习路径 -------------------------------------------------------------- */
  views.path = function () {
    var html = [];
    html.push(pageHead('学习路径',
      DATA.stats.chapters + ' 章 · 5 个阶段 · 共 ' + DATA.stats.hours + ' 小时',
      '<button class="btn btn-ghost btn-sm" id="collapseAll">收起全部</button>'));

    html.push('<div class="page-body">');
    html.push('<p class="page-sub">每个章节卡上的三个勾代表掌握程度：' +
      '<strong>跑通</strong>代码 → <strong>讲清</strong>核心机制 → <strong>改得动</strong>一个地方并预测结果。三个都打上才算真正掌握。</p>');
    html.push('</div>');

    for (var s = 0; s < DATA.stages.length; s++) {
      var stage = DATA.stages[s];
      html.push('<section class="stage-block">');
      html.push('<div class="stage-head"><div>' +
        '<h2 class="stage-name">阶段' + '一二三四五'[s] + ' · ' + h(stage.name) + '</h2>' +
        '<p class="stage-desc">' + ri(stage.desc) + '</p></div>' +
        '<span class="stage-weeks">' + h(stage.weeks) + '</span></div>');
      var cards = [];
      for (var c = 0; c < stage.chapters.length; c++) {
        var chapter = byNo(stage.chapters[c]);
        if (chapter) cards.push(chapterCard(chapter));
      }
      html.push(cardGrid(cards));
      html.push('</section>');
    }
    return html.join('');
  };

  /* 12 周计划 ------------------------------------------------------------- */
  views.plan = function () {
    var plan = null;
    for (var i = 0; i < DATA.raw.length; i++) {
      if (DATA.raw[i].id.indexOf('02-') === 0) plan = DATA.raw[i];
    }

    var html = [];
    html.push(pageHead('12 周学习计划', '每周 6–8 小时，拆成 3–4 次，每次 1.5–2 小时'));

    html.push('<div class="page-body"><div class="week-grid">');
    for (var n = 0; n < DATA.notes.length; n++) {
      var note = DATA.notes[n];
      var chapterLabel = note.chapters.length
        ? '第 ' + note.chapters.join(' / ') + ' 章'
        : '项目与面试冲刺';
      html.push('<a class="week-card" href="#/note/' + note.week + '">' +
        '<span class="week-no">W' + note.week + '</span>' +
        '<span class="week-title">' + h(note.title) + '</span>' +
        '<span class="week-chapters">' + h(chapterLabel) + '</span>' +
        '</a>');
    }
    html.push('</div></div>');

    if (plan) {
      html.push('<div class="page-body" style="margin-top:24px">');
      html.push('<div class="page-head"><div><h2 class="page-title">完整计划</h2>' +
        '<p class="page-sub">逐日任务清单、过关标准、落后了怎么调整</p></div></div>');
      html.push('<article class="reader"><div class="reader-body">' +
        '<div class="markdown">' + MD.render(plan.markdown) + '</div></div></article>');
      html.push('</div>');
    }
    return html.join('');
  };

  /* 我的进度 -------------------------------------------------------------- */
  views.progress = function () {
    var percent = Progress.percent();
    var html = [];
    html.push(pageHead('我的进度', '进度保存在浏览器本地（localStorage），换设备请用导出/导入',
      '<button class="btn btn-ghost btn-sm" id="exportBtn">导出 JSON</button> ' +
      '<button class="btn btn-ghost btn-sm" id="importBtn">导入 JSON</button> ' +
      '<button class="btn btn-ghost btn-sm" id="resetBtn">重置</button>'));

    html.push('<div class="page-body">');
    html.push('<div class="progress-bar"><div class="progress-fill" style="width:' + percent + '%"></div></div>');
    html.push('<p class="page-sub" style="margin-top:10px">总进度 <strong>' + percent + '%</strong>　' +
      '（章节勾选 ' + Progress.chapterDoneCountTotal() + '/' + (DATA.chapters.length * 3) + '　·　' +
      '项目 ' + Progress.projectCount() + '/' + DATA.ladder.length + '　·　' +
      '周笔记 ' + Progress.noteCount() + '/' + DATA.notes.length + '）</p>');
    html.push('</div>');

    // 章节明细
    html.push('<div class="page-body"><h2 class="page-title">章节掌握度</h2>');
    html.push('<div class="table-wrap"><table><thead><tr>' +
      '<th>章节</th><th>主题</th><th>跑通</th><th>讲清</th><th>改得动</th><th>状态</th>' +
      '</tr></thead><tbody>');
    for (var i = 0; i < DATA.chapters.length; i++) {
      var chapter = DATA.chapters[i];
      var state = Progress.chapter(chapter.no);
      var done = Progress.isChapterDone(chapter.no);
      html.push('<tr><td>' + h(chapter.no) + '</td>' +
        '<td><a href="#/chapter/' + h(chapter.no) + '">' + h(chapter.title) + '</a></td>' +
        '<td>' + (state.run ? '✓' : '—') + '</td>' +
        '<td>' + (state.explain ? '✓' : '—') + '</td>' +
        '<td>' + (state.modify ? '✓' : '—') + '</td>' +
        '<td>' + (done ? '已完成' : '进行中') + '</td></tr>');
    }
    html.push('</tbody></table></div></div>');

    // 项目阶梯
    html.push('<div class="page-body"><h2 class="page-title">项目阶梯</h2><div class="table-wrap"><table>' +
      '<thead><tr><th>级别</th><th>项目</th><th>状态</th></tr></thead><tbody>');
    for (var j = 0; j < DATA.ladder.length; j++) {
      var ladder = DATA.ladder[j];
      html.push('<tr><td>' + h(ladder.id) + '</td>' +
        '<td><a href="#/ladder">' + h(ladder.name) + '</a></td>' +
        '<td>' + (Progress.data.projects[ladder.id] ? '已完成' : '未开始') + '</td></tr>');
    }
    html.push('</tbody></table></div></div>');

    // 周笔记
    html.push('<div class="page-body"><h2 class="page-title">周笔记</h2><div class="week-grid">');
    for (var k = 0; k < DATA.notes.length; k++) {
      var note = DATA.notes[k];
      var filled = !!Progress.data.notes[note.week];
      html.push('<a class="week-card" href="#/note/' + note.week + '"' +
        (filled ? ' style="border-left:3px solid var(--ok,#2f9e6e)"' : '') + '>' +
        '<span class="week-no">W' + note.week + '</span>' +
        '<span class="week-title">' + h(note.title) + '</span>' +
        '<span class="week-chapters">' + (filled ? '已填写' : '待填写') + '</span></a>');
    }
    html.push('</div></div>');

    return html.join('');
  };

  // 进度视图用到的几个小计
  Progress.chapterDoneCountTotal = function () {
    var total = 0;
    for (var i = 0; i < DATA.chapters.length; i++) {
      total += this.chapterDoneCount(DATA.chapters[i].no);
    }
    return total;
  };
  Progress.projectCount = function () {
    var total = 0;
    for (var i = 0; i < DATA.ladder.length; i++) {
      if (this.data.projects[DATA.ladder[i].id]) total++;
    }
    return total;
  };
  Progress.noteCount = function () {
    var total = 0;
    for (var i = 0; i < DATA.notes.length; i++) {
      if (this.data.notes[DATA.notes[i].week]) total++;
    }
    return total;
  };

  /* 章节阅读器 ------------------------------------------------------------ */
  views.chapter = function (parts) {
    var no = last(parts);
    var chapter = byNo(no);
    if (!chapter) return notFound('找不到第 ' + h(no) + ' 章');

    var index = chapterIndex(no);
    var prev = index > 0 ? DATA.chapters[index - 1] : null;
    var next = index < DATA.chapters.length - 1 ? DATA.chapters[index + 1] : null;
    var stageIndex = stageIndexOf(chapter.stage);
    var state = Progress.chapter(no);

    var html = [];
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb">' +
      '<a href="#/path">学习路径</a> / 阶段' + '一二三四五'[stageIndex] + ' · ' +
      h(DATA.stages[stageIndex] ? DATA.stages[stageIndex].name : '') + '</div>');
    html.push('<h1 class="reader-title">第 ' + h(chapter.no) + ' 章 · ' + h(chapter.title) + '</h1>');
    html.push('<p class="page-sub">' + ri(chapter.goal) + '</p>');
    html.push('<div class="reader-meta">' +
      (chapter.hours ? '<span class="meta-chip">' + chapter.hours + ' 小时</span>' : '') +
      (chapter.prereq ? '<span class="meta-chip">前置：' + h(chapter.prereq) + '</span>' : '') +
      (chapter.code ? '<span class="meta-chip mono">' + h(chapter.code) + '</span>' : '') +
      '</div>');
    html.push('<div class="reader-actions">');
    html.push(checkboxes(chapter.no, state));
    html.push('<button class="btn btn-sm btn-ghost" id="tocBtn">目录</button>');
    html.push('<a class="btn btn-sm btn-ghost" href="' + h('../' + chapter.file) + '" download>下载 .md</a>');
    html.push('</div>');
    html.push('</div>');

    html.push('<div class="reader-body">');
    html.push('<aside class="reader-toc" id="readerToc">' + tocHtml(chapter.markdown) + '</aside>');
    html.push('<div class="markdown" id="mdBody">' + MD.render(chapter.markdown) + '</div>');
    html.push('</div>');

    html.push('<nav class="reader-nav">');
    html.push(prev
      ? '<a class="reader-prev" href="#/chapter/' + h(prev.no) + '">' +
        '<span class="rn-dir">← 上一章</span><span class="rn-title">' + h(prev.no + ' ' + prev.title) + '</span></a>'
      : '<span class="reader-prev"></span>');
    html.push(next
      ? '<a class="reader-next" href="#/chapter/' + h(next.no) + '">' +
        '<span class="rn-dir">下一章 →</span><span class="rn-title">' + h(next.no + ' ' + next.title) + '</span></a>'
      : '<span class="reader-next"></span>');
    html.push('</nav>');
    html.push('</article>');

    return html.join('');
  };

  function stageIndexOf(stageId) {
    for (var i = 0; i < DATA.stages.length; i++) {
      if (DATA.stages[i].id === stageId) return i;
    }
    return 0;
  }

  function tocHtml(markdown) {
    var toc = MD.extractToc(markdown);
    if (!toc.length) return '';
    var html = '<div class="toc-title">本页目录</div>';
    for (var i = 0; i < toc.length; i++) {
      html += '<a class="toc-item lv' + toc[i].level + '" href="#' + h(toc[i].id) +
        '" data-anchor="' + h(toc[i].id) + '">' + h(toc[i].text) + '</a>';
    }
    return html;
  }

  /* 知识库列表 ------------------------------------------------------------ */
  views.wikiList = function (parts) {
    var kind = parts[1];
    var info = wikiKind(kind);
    if (!info) return notFound('未知的知识库分类');

    var html = [];
    html.push(pageHead(info.label, '共 ' + info.list.length + ' 份'));

    html.push('<div class="page-body"><div class="tabs">' +
      tab('#/wiki/topics', '专题总结', kind === 'topics') +
      tab('#/wiki/cheatsheets', '速查表', kind === 'cheatsheets') +
      tab('#/wiki/interview', '面试题库', kind === 'interview') +
      '</div></div>');

    var cards = [];
    for (var i = 0; i < info.list.length; i++) {
      var item = info.list[i];
      cards.push('<a class="card" href="#/wiki/' + kind + '/' + encodeURIComponent(item.id) + '">' +
        '<h3 class="card-title">' + h(item.title) + '</h3>' +
        '<p class="card-summary">' + ri(item.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">' + info.badge + '</span>' +
        '<span class="card-size">' + h(item.file) + '</span></div></a>');
    }
    html.push('<div class="page-body">' + cardGrid(cards) + '</div>');
    return html.join('');
  };

  /* 知识库条目阅读器 ------------------------------------------------------ */
  views.wikiItem = function (parts) {
    var kind = parts[1];
    var id = parts.slice(2).join('/');
    var info = wikiKind(kind);
    if (!info) return notFound('未知的知识库分类');

    var index = -1;
    for (var i = 0; i < info.list.length; i++) {
      if (info.list[i].id === decodeURIComponent(id)) index = i;
    }
    if (index < 0) return notFound('找不到这份内容：' + h(id));

    var item = info.list[index];
    var prev = index > 0 ? info.list[index - 1] : null;
    var next = index < info.list.length - 1 ? info.list[index + 1] : null;

    var html = [];
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb"><a href="#/wiki/' + kind + '">' + h(info.label) + '</a></div>');
    html.push('<h1 class="reader-title">' + h(item.title) + '</h1>');
    html.push('<p class="page-sub">' + ri(item.summary) + '</p>');
    html.push('<div class="reader-meta"><span class="meta-chip mono">' + h(item.file) + '</span></div>');
    html.push('<div class="reader-actions"><button class="btn btn-sm btn-ghost" id="tocBtn">目录</button>' +
      '<a class="btn btn-sm btn-ghost" href="' + h('../' + item.file) + '" download>下载 .md</a></div>');
    html.push('</div>');
    html.push('<div class="reader-body">');
    html.push('<aside class="reader-toc" id="readerToc">' + tocHtml(item.markdown) + '</aside>');
    html.push('<div class="markdown" id="mdBody">' + MD.render(item.markdown) + '</div>');
    html.push('</div>');
    html.push('<nav class="reader-nav">');
    html.push(prev ? '<a class="reader-prev" href="#/wiki/' + kind + '/' + encodeURIComponent(prev.id) + '">' +
      '<span class="rn-dir">← 上一篇</span><span class="rn-title">' + h(prev.title) + '</span></a>'
      : '<span class="reader-prev"></span>');
    html.push(next ? '<a class="reader-next" href="#/wiki/' + kind + '/' + encodeURIComponent(next.id) + '">' +
      '<span class="rn-dir">下一篇 →</span><span class="rn-title">' + h(next.title) + '</span></a>'
      : '<span class="reader-next"></span>');
    html.push('</nav></article>');
    return html.join('');
  };

  function tab(route, label, active) {
    return '<a class="tab' + (active ? ' active' : '') + '" href="' + route + '">' + h(label) + '</a>';
  }

  /* 项目阶梯 -------------------------------------------------------------- */
  views.ladder = function () {
    var html = [];
    html.push(pageHead('项目阶梯',
      DATA.ladder.length + ' 级 · 每一级都是一个可以拿出去给人看的作品'));

    html.push('<div class="page-body"><p class="page-sub">' +
      '判断标准不是"跑通 Demo"，而是能不能回答四个问题：' +
      '<strong>为什么这样选？效果如何度量？失败如何恢复？成本如何控制？</strong>' +
      '四个问题答不上来，这一档就没过关。</p></div>');

    html.push('<div class="page-body">');
    for (var i = 0; i < DATA.ladder.length; i++) {
      var item = DATA.ladder[i];
      var done = !!Progress.data.projects[item.id];
      html.push('<article class="ladder-item"' + (done ? ' data-done="1"' : '') + '>');
      html.push('<div class="ladder-badge">' + h(item.id) + '</div>');
      html.push('<div class="ladder-body">');
      html.push('<h3 class="ladder-title">' + h(item.name) + '</h3>');
      html.push('<p class="ladder-one">' + ri(item.one) + '</p>');
      html.push('<div class="ladder-tags">');
      for (var t = 0; t < item.skills.length; t++) {
        html.push('<span class="tag">' + h(item.skills[t]) + '</span>');
      }
      html.push('</div>');
      html.push('<p class="page-sub" style="margin:10px 0 4px">最低交付标准（' + h(item.scale) + '）：</p>');
      html.push('<ul class="ladder-criteria">');
      for (var c = 0; c < item.criteria.length; c++) {
        html.push('<li>' + h(item.criteria[c]) + '</li>');
      }
      html.push('</ul>');
      html.push('<div class="ladder-chips">');
      for (var ch = 0; ch < item.chapters.length; ch++) {
        var chapter = byNo(item.chapters[ch]);
        if (chapter) {
          html.push('<a class="chip-link" href="#/chapter/' + h(chapter.no) + '">第 ' +
            h(chapter.no) + ' 章 ' + h(chapter.title) + '</a>');
        }
      }
      html.push('</div>');
      html.push('<div class="chapter-foot" style="margin-top:12px">' +
        '<label class="check"><input type="checkbox" data-project="' + h(item.id) + '"' +
        (done ? ' checked' : '') + '><span class="check-label">这个项目我做完了</span></label>' +
        '<a class="btn btn-sm btn-ghost" href="#/chapter/' + h(item.chapters[0] || '00') + '">从对应章节开始</a>' +
        '</div>');
      html.push('</div></article>');
    }
    html.push('</div>');
    return html.join('');
  };

  /* 周笔记 ---------------------------------------------------------------- */
  views.notes = function () {
    var html = [];
    html.push(pageHead('周学习笔记', DATA.notes.length + ' 份模板 · 边学边填，12 周后这就是你自己的知识库'));
    var cards = [];
    for (var i = 0; i < DATA.notes.length; i++) {
      var note = DATA.notes[i];
      var filled = !!Progress.data.notes[note.week];
      var label = note.chapters.length ? '第 ' + note.chapters.join(' / ') + ' 章' : '项目与面试冲刺';
      cards.push('<a class="week-card" href="#/note/' + note.week + '">' +
        '<span class="week-no">W' + note.week + '</span>' +
        '<span class="week-title">' + h(note.title) + '</span>' +
        '<span class="week-chapters">' + h(label) + (filled ? ' · 已填写' : '') + '</span></a>');
    }
    html.push('<div class="page-body"><div class="week-grid">' + cards.join('') + '</div></div>');
    return html.join('');
  };

  views.note = function (parts) {
    var note = findNote(last(parts));
    if (!note) return notFound('找不到这份周笔记');

    var html = [];
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb"><a href="#/notes">周学习笔记</a> / Week ' + note.week + '</div>');
    html.push('<h1 class="reader-title">' + h(note.title) + '</h1>');
    html.push('<div class="reader-meta">' +
      (note.chapters.length ? '<span class="meta-chip">覆盖第 ' + h(note.chapters.join(' / ')) + ' 章</span>' : '') +
      '<span class="meta-chip mono">' + h(note.file) + '</span></div>');
    html.push('<div class="reader-actions">' +
      '<label class="check"><input type="checkbox" data-note="' + note.week + '"' +
      (Progress.data.notes[note.week] ? ' checked' : '') + '>' +
      '<span class="check-label">这周我学完了</span></label>' +
      '<button class="btn btn-sm btn-ghost" id="tocBtn">目录</button>' +
      '<a class="btn btn-sm btn-ghost" href="' + h('../' + note.file) + '" download>下载 .md</a></div>');
    html.push('</div>');
    html.push('<div class="reader-body">');
    html.push('<aside class="reader-toc" id="readerToc">' + tocHtml(note.markdown) + '</aside>');
    html.push('<div class="markdown" id="mdBody">' + MD.render(note.markdown) + '</div>');
    html.push('</div></article>');
    return html.join('');
  };

  /* 学习配置 -------------------------------------------------------------- */
  views.config = function () {
    var html = [pageHead('学习配置', 'AI 教练每次对话前必读、对话后必写的地方')];
    var cards = [];
    for (var i = 0; i < DATA.config.length; i++) {
      var item = DATA.config[i];
      cards.push('<a class="card" href="#/config/' + encodeURIComponent(item.id) + '">' +
        '<h3 class="card-title">' + h(item.title) + '</h3>' +
        '<p class="card-summary">' + ri(item.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">配置</span>' +
        '<span class="card-size">' + h(item.file) + '</span></div></a>');
    }
    html.push('<div class="page-body">' + cardGrid(cards) + '</div>');
    return html.join('');
  };

  views.configItem = function (parts) {
    var item = findInList(DATA.config, last(parts));
    if (!item) return notFound('找不到这份配置');
    return readerShell('学习配置', '#/config', item);
  };

  /* 规划与调研 ------------------------------------------------------------ */
  views.raw = function () {
    var html = [pageHead('规划与调研', '为什么这样排课、什么时候做什么、做到什么程度算过关')];
    var cards = [];
    for (var i = 0; i < DATA.raw.length; i++) {
      var item = DATA.raw[i];
      cards.push('<a class="card" href="#/raw/' + encodeURIComponent(item.id) + '">' +
        '<h3 class="card-title">' + h(item.title) + '</h3>' +
        '<p class="card-summary">' + ri(item.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">规划</span>' +
        '<span class="card-size">' + h(item.file) + '</span></div></a>');
    }
    html.push('<div class="page-body">' + cardGrid(cards) + '</div>');
    return html.join('');
  };

  views.rawItem = function (parts) {
    var item = findInList(DATA.raw, last(parts));
    if (!item) return notFound('找不到这份文档');
    return readerShell('规划与调研', '#/raw', item);
  };

  /* 学习中枢 / 关于 ------------------------------------------------------- */
  views.hub = function () {
    var html = [pageHead('学习中枢', 'AI 教练的身份、流程与格式规范')];
    html.push('<div class="page-body"><p class="page-sub">' +
      '把这份文档的<strong>全部内容</strong>复制到你常用 AI 助手的系统提示词里，' +
      '它就会变成你的专属 Agent 开发教练：自动读你的档案、跟你的进度、写你的笔记。</p></div>');
    html.push('<div class="page-body"><article class="reader"><div class="reader-body">' +
      '<div class="markdown">' + MD.render(DATA.hub.markdown) + '</div></div></article></div>');
    return html.join('');
  };

  views.about = function () {
    var html = [];
    html.push(pageHead('关于', '这个网页从哪里来、内容有多少、怎么用'));
    html.push('<div class="page-body">' +
      '<div class="stat-grid">' +
      '<div class="stat-card"><span class="stat-num">' + DATA.stats.lines.toLocaleString() + '</span><span class="stat-label">行项目内容</span></div>' +
      '<div class="stat-card"><span class="stat-num">' + DATA.stats.chapters + '</span><span class="stat-label">章教程</span></div>' +
      '<div class="stat-card"><span class="stat-num">' + DATA.stats.scripts + '</span><span class="stat-label">个可运行脚本</span></div>' +
      '<div class="stat-card"><span class="stat-num">' + DATA.stats.hours + '</span><span class="stat-label">预计总学时</span></div>' +
      '</div></div>');

    html.push('<div class="page-body"><h2 class="page-title">这个网页是怎么来的</h2>' +
      '<p class="page-sub">网页内容不是手写的，而是构建脚本从项目里的 Markdown 现场生成的：</p>' +
      '<pre><code class="lang-bash">python web/_build.py</code></pre>' +
      '<p class="page-sub">修改了 docs/ 或 02-Wiki/ 里的任何 Markdown 之后，重新跑一次这个命令，' +
      '网页内容就会同步更新。</p></div>');

    html.push('<div class="page-body"><article class="reader"><div class="reader-body">' +
      '<div class="markdown">' + MD.render(DATA.readme.markdown) + '</div></div></article></div>');
    return html.join('');
  };

  /* 复用的"阅读器外壳"（配置 / 规划这类单篇文档） */
  function readerShell(crumbLabel, crumbRoute, item) {
    var html = [];
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb"><a href="' + crumbRoute + '">' + h(crumbLabel) + '</a></div>');
    html.push('<h1 class="reader-title">' + h(item.title) + '</h1>');
    html.push('<p class="page-sub">' + ri(item.summary) + '</p>');
    html.push('<div class="reader-meta"><span class="meta-chip mono">' + h(item.file) + '</span></div>');
    html.push('<div class="reader-actions"><button class="btn btn-sm btn-ghost" id="tocBtn">目录</button>' +
      '<a class="btn btn-sm btn-ghost" href="' + h('../' + item.file) + '" download>下载 .md</a></div>');
    html.push('</div>');
    html.push('<div class="reader-body">');
    html.push('<aside class="reader-toc" id="readerToc">' + tocHtml(item.markdown) + '</aside>');
    html.push('<div class="markdown" id="mdBody">' + MD.render(item.markdown) + '</div>');
    html.push('</div></article>');
    return html.join('');
  }

  function notFound(message) {
    return '<div class="view"><div class="page-body"><p class="empty">' +
      h(message || '页面不存在') + '</p><p><a class="btn btn-ghost" href="#/">回到首页</a></p></div></div>';
  }

  /* ------------------------------------------------------- 模拟面试存储 */

  var MOCK_KEY = 'azto.mock.v1';

  var MockStore = {
    data: { sessions: [] },

    load: function () {
      try {
        var raw = localStorage.getItem(MOCK_KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          if (parsed && parsed.sessions) this.data = parsed;
        }
      } catch (err) { /* localStorage 不可用时降级为内存态 */ }
      if (!this.data.sessions) this.data.sessions = [];
    },

    save: function () {
      try { localStorage.setItem(MOCK_KEY, JSON.stringify(this.data)); } catch (err) { /* 忽略 */ }
    },

    add: function (record) {
      this.data.sessions.unshift(record);
      // 只留最近 50 场，避免 localStorage 膨胀
      if (this.data.sessions.length > 50) this.data.sessions.length = 50;
      this.save();
    },

    clear: function () {
      this.data.sessions = [];
      this.save();
    },

    bestByDimension: function () {
      // 每场记录里都存了 byDimension，取历史最高的一次做参照
      var best = {};
      for (var i = 0; i < this.data.sessions.length; i++) {
        var dims = this.data.sessions[i].byDimension || {};
        for (var key in dims) {
          if (!(key in best) || dims[key].rate > best[key].rate) best[key] = dims[key];
        }
      }
      return best;
    }
  };

  /* --------------------------------------------------------- 模拟面试运行器 */

  /** 场次预设：每种给一个时长和抽题方案 */
  var MOCK_SESSIONS = [
    { id: 'quick', name: '快问快答', minutes: 20,
      desc: '只跑 Agent 知识轮，检验记忆。适合每天通勤时练一遍。',
      pick: { 'Agent 知识轮': 10 } },
    { id: 'standard', name: '标准技术面', minutes: 45,
      desc: '算法 + 知识 + 项目深挖。最常见的社招一轮技术面。',
      pick: { '算法轮': 3, 'Agent 知识轮': 8, '项目深挖轮': 5 } },
    { id: 'full', name: '完整终面', minutes: 90,
      desc: '五轮全跑。适合毕设做完之后做一次完整检验。',
      pick: { '算法轮': 4, 'Agent 知识轮': 8, '项目深挖轮': 6, '系统设计轮': 4, '行为与压力轮': 4 } },
    { id: 'free', name: '自由练习', minutes: 0,
      desc: '自己挑轮次、岗位、难度和题量。',
      pick: null }
  ];

  var MockRunner = {
    run: null,          // {session, queue, index, answers, startedAt, revealed}
    timer: null,

    /** 从题库里按条件抽题 */
    build: function (options) {
      var rounds = DATA.mock.rounds;
      var queue = [];

      if (options.pick) {
        for (var i = 0; i < rounds.length; i++) {
          var want = options.pick[rounds[i].name];
          if (!want) continue;
          queue = queue.concat(this.sample(rounds[i].questions, want));
        }
      } else {
        for (var j = 0; j < rounds.length; j++) {
          if (options.round && options.round !== '全部' && rounds[j].name !== options.round) continue;
          queue = queue.concat(this.filter(rounds[j].questions, options));
        }
        if (options.count > 0 && queue.length > options.count) {
          queue = this.sample(queue, options.count);
        }
      }
      return queue;
    },

    filter: function (list, options) {
      var self = this;
      return list.filter(function (q) {
        if (options.role && options.role !== '全部' && q.role.indexOf(options.role) < 0 &&
            q.role.indexOf('全部') < 0) return false;
        if (options.level && options.level !== '全部' && q.level !== options.level) return false;
        return true;
      });
    },

    /** 洗牌后取 n 个，保证题目顺序不固定（避免记住题序而不是记住知识） */
    sample: function (list, n) {
      var copy = list.slice();
      for (var i = copy.length - 1; i > 0; i--) {
        var j = Math.floor(Math.random() * (i + 1));
        var tmp = copy[i]; copy[i] = copy[j]; copy[j] = tmp;
      }
      return copy.slice(0, Math.min(n, copy.length));
    },

    start: function (sessionId, options) {
      var session = null;
      for (var i = 0; i < MOCK_SESSIONS.length; i++) {
        if (MOCK_SESSIONS[i].id === sessionId) session = MOCK_SESSIONS[i];
      }
      if (!session) return;

      var queue = this.build({
        pick: session.pick,
        round: options && options.round,
        role: options && options.role,
        level: options && options.level,
        count: options && options.count
      });

      if (!queue.length) { toast('没有符合条件的题目，换个筛选条件试试'); return; }

      this.run = {
        session: session,
        queue: queue,
        index: 0,
        answers: {},
        revealed: {},
        startedAt: Date.now()
      };
      location.hash = '#/mock/run';
    },

    current: function () {
      return this.run && this.run.queue[this.run.index];
    },

    answer: function (questionId, score) {
      this.run.answers[questionId] = score;
    },

    finish: function () {
      var run = this.run;
      if (!run) return null;

      var byDimension = {}, byRound = {}, wrong = [];
      var total = 0;

      for (var i = 0; i < run.queue.length; i++) {
        var q = run.queue[i];
        var score = run.answers[q.id];
        if (score == null) score = 0;
        total += score;

        var dimKey = q.dimension || '未分类';
        if (!byDimension[dimKey]) byDimension[dimKey] = { got: 0, max: 0, count: 0 };
        byDimension[dimKey].got += score;
        byDimension[dimKey].max += 2;
        byDimension[dimKey].count += 1;

        var roundName = this.roundOf(q.id);
        if (!byRound[roundName]) byRound[roundName] = { got: 0, max: 0, count: 0 };
        byRound[roundName].got += score;
        byRound[roundName].max += 2;
        byRound[roundName].count += 1;

        if (score <= 1) wrong.push({ id: q.id, title: q.title, score: score, related: q.related });
      }

      for (var dim in byDimension) {
        byDimension[dim].rate = byDimension[dim].max ? byDimension[dim].got / byDimension[dim].max : 0;
      }
      for (var round in byRound) {
        byRound[round].rate = byRound[round].max ? byRound[round].got / byRound[round].max : 0;
      }

      var record = {
        date: new Date().toISOString(),
        session: run.session.name,
        sessionId: run.session.id,
        total: total,
        max: run.queue.length * 2,
        count: run.queue.length,
        durationSec: Math.round((Date.now() - run.startedAt) / 1000),
        byDimension: byDimension,
        byRound: byRound,
        wrong: wrong,
        answered: Object.keys(run.answers).length
      };
      MockStore.add(record);
      return record;
    },

    roundOf: function (questionId) {
      var prefix = questionId.charAt(0);
      for (var i = 0; i < DATA.mock.rounds.length; i++) {
        if (DATA.mock.rounds[i].prefix === prefix) return DATA.mock.rounds[i].name;
      }
      return '未分类';
    },

    reset: function () {
      this.run = null;
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
    }
  };

  /* ------------------------------------------------------------ 面试视图 */

  /** 算一场预设实际会抽多少题（视图函数里不能用 this —— 它们是当普通函数调的） */
  function mockSessionCount(session) {
    var totalCount = 0;
    for (var name in session.pick) {
      for (var i = 0; i < DATA.mock.rounds.length; i++) {
        if (DATA.mock.rounds[i].name === name) {
          totalCount += Math.min(session.pick[name], DATA.mock.rounds[i].count);
        }
      }
    }
    return totalCount;
  }

  views.mockHome = function () {
    var html = [];
    html.push(pageHead('模拟面试',
      DATA.mock.total + ' 道结构化题目 · ' + DATA.mock.rounds.length + ' 轮 · 逐题自评后给参考思路与扣分点'));

    // 开篇引导
    html.push('<div class="page-body"><p class="page-sub">' +
      '会做不等于会讲。面试是一项<b>独立技能</b> —— 很多人 Agent 用得很熟，' +
      '一被追问「为什么不用 X」就答不上来。这里不是背答案，练的是<b>被追问时怎么接</b>。' +
      '</p></div>');

    // 场次
    html.push('<div class="page-body"><h2 class="page-title">选一场开始</h2>');
    html.push('<div class="card-grid session-grid">');
    for (var i = 0; i < MOCK_SESSIONS.length; i++) {
      var session = MOCK_SESSIONS[i];
      var count = session.pick ? mockSessionCount(session) + ' 题' : '自定义题量';
      html.push('<article class="session-card" data-session="' + h(session.id) + '">' +
        '<div class="session-top"><h3 class="session-name">' + h(session.name) + '</h3>' +
        (session.minutes ? '<span class="meta-chip">' + session.minutes + ' 分钟</span>' : '') +
        '</div>' +
        '<p class="card-summary">' + h(session.desc) + '</p>' +
        '<div class="card-foot"><span class="badge">' + count + '</span>' +
        '<button class="btn btn-sm btn-primary" data-start="' + h(session.id) + '">开始</button></div>' +
        '</article>');
    }
    html.push('</div></div>');

    // 自由练习的筛选器
    html.push('<div class="page-body"><div class="panel" id="freePanel" hidden>');
    html.push('<h3 class="card-title">自由练习</h3>');
    html.push('<div class="field-row">');
    html.push('<label class="field"><span>轮次</span><select id="freeRound">' +
      '<option>全部</option>' +
      DATA.mock.rounds.map(function (r) { return '<option>' + h(r.name) + '</option>'; }).join('') +
      '</select></label>');
    html.push('<label class="field"><span>岗位</span><select id="freeRole">' +
      '<option>全部</option><option>AI应用开发</option><option>Agent工程师</option>' +
      '<option>RAG工程师</option><option>AI后端</option></select></label>');
    html.push('<label class="field"><span>难度</span><select id="freeLevel">' +
      '<option>全部</option><option>基础</option><option>进阶</option><option>困难</option></select></label>');
    html.push('<label class="field"><span>题量</span><select id="freeCount">' +
      '<option>10</option><option>15</option><option selected>20</option><option>30</option><option>50</option>' +
      '</select></label>');
    html.push('<button class="btn btn-primary" id="freeStart">开始练习</button>');
    html.push('</div></div></div>');

    // 各轮概览
    html.push('<div class="page-body"><h2 class="page-title">五轮都考什么</h2>');
    html.push('<div class="table-wrap"><table><thead><tr>' +
      '<th>轮次</th><th>题量</th><th>这轮在考什么</th></tr></thead><tbody>');
    var roundDesc = {
      '算法轮': '编码能力。能不能在 20 分钟内把一道 Medium 写对、说出复杂度',
      'Agent 知识轮': '概念理解。ReAct、工具、记忆、多智能体、协议，答得准不准',
      '项目深挖轮': '你做过什么。每个数字都要有出处，每个取舍都要有理由',
      '系统设计轮': '能不能设计。从需求到架构，从容量到成本',
      '行为与压力轮': '怎么接压力。被否定、被追问不会的东西，还能不能稳住'
    };
    for (var r = 0; r < DATA.mock.rounds.length; r++) {
      var round = DATA.mock.rounds[r];
      html.push('<tr><td>' + h(round.name) + '</td><td>' + round.count + '</td>' +
        '<td>' + h(roundDesc[round.name] || '') + '</td></tr>');
    }
    html.push('</tbody></table></div></div>');

    // 历史记录
    var sessions = MockStore.data.sessions;
    html.push('<div class="page-body"><h2 class="page-title">历史记录</h2>');
    if (!sessions.length) {
      html.push('<p class="empty">还没有记录。跑完一场就会出现在这里。</p>');
    } else {
      html.push('<div class="table-wrap"><table><thead><tr>' +
        '<th>日期</th><th>场次</th><th>得分</th><th>通过率</th><th>用时</th></tr></thead><tbody>');
      for (var s = 0; s < Math.min(sessions.length, 12); s++) {
        var item = sessions[s];
        var rate = item.max ? Math.round(item.total / item.max * 100) : 0;
        html.push('<tr><td>' + h(item.date.slice(0, 10)) + '</td><td>' + h(item.session) + '</td>' +
          '<td>' + item.total + ' / ' + item.max + '</td>' +
          '<td>' + rate + '%</td>' +
          '<td>' + Math.round(item.durationSec / 60) + ' 分钟</td></tr>');
      }
      html.push('</tbody></table></div>');
      html.push('<div class="page-actions" style="margin-top:14px">' +
        '<button class="btn btn-ghost btn-sm" id="mockExport">导出记录</button>' +
        '<button class="btn btn-ghost btn-sm" id="mockClear">清空记录</button></div>');
    }
    html.push('</div>');

    // 说明文档入口
    if (DATA.mock.documents.length) {
      html.push('<div class="page-body"><h2 class="page-title">配套文档</h2>');
      var docs = DATA.mock.documents.map(function (doc) {
        return '<a class="card" href="#/mock/docs/' + encodeURIComponent(doc.id) + '">' +
          '<h3 class="card-title">' + h(doc.title) + '</h3>' +
          '<p class="card-summary">' + h(doc.summary) + '</p>' +
          '<div class="card-foot"><span class="badge">文档</span>' +
          '<span class="card-size">' + h(doc.file) + '</span></div></a>';
      });
      html.push(cardGrid(docs));
      html.push('</div>');
    }

    return html.join('');
  };

  views.mockRun = function () {
    if (!MockRunner.run) {
      return '<div class="view"><div class="page-body">' +
        '<p class="empty">还没有开始面试。</p>' +
        '<p><a class="btn btn-primary" href="#/mock">挑一场开始</a></p></div></div>';
    }
    var run = MockRunner.run;
    var html = [];
    html.push('<div class="view runner">');
    html.push('<div class="runner-head">');
    html.push('<div class="runner-progress">' +
      '<span class="runner-session">' + h(run.session.name) + '</span>' +
      '<span class="runner-count" id="mockCount"></span>' +
      '<span class="runner-timer" id="mockTimer"></span>' +
      '</div>');
    html.push('<button class="btn btn-sm btn-ghost" id="mockQuit">结束这场</button>');
    html.push('<div class="progress-bar" style="flex:1 1 100%"><div class="progress-fill" id="mockBar"></div></div>');
    html.push('</div>');
    html.push('<div id="mockStage"></div>');
    html.push('<div id="mockReport"></div>');
    html.push('</div>');
    return html.join('');
  };

  /** 渲染当前这道题（运行器内部调用，不走路由） */
  MockRunner.render = function () {
    var run = this.run;
    var stage = document.getElementById('mockStage');
    if (!run || !stage) return;

    if (run.index >= run.queue.length) return this.renderReport();

    var question = run.queue[run.index];
    var revealed = !!run.revealed[question.id];
    var chosen = run.answers[question.id];

    var count = document.getElementById('mockCount');
    if (count) count.textContent = '第 ' + (run.index + 1) + ' / ' + run.queue.length + ' 题';
    var bar = document.getElementById('mockBar');
    if (bar) bar.style.width = Math.round((run.index) / run.queue.length * 100) + '%';

    var html = [];
    html.push('<article class="q-card">');
    html.push('<div class="q-meta">' +
      '<span class="meta-chip">' + h(question.id) + '</span>' +
      '<span class="meta-chip">' + h(question.dimension) + '</span>' +
      '<span class="meta-chip">' + h(question.level) + '</span>' +
      '<span class="meta-chip">' + h(question.role) + '</span>' +
      (question.minutes ? '<span class="meta-chip">建议 ' + h(question.minutes) + ' 分钟</span>' : '') +
      '</div>');
    html.push('<h2 class="q-prompt">' + h(question.prompt || question.title) + '</h2>');
    html.push('<p class="q-hint">先自己答一遍（开口说出来，或在纸上写要点），再展开参考思路对照。</p>');

    if (!revealed) {
      html.push('<div class="q-actions"><button class="btn btn-primary" id="mockReveal">' +
        '我答完了，看参考思路</button></div>');
    } else {
      html.push('<div class="q-reveal">');
      html.push('<div class="q-section"><div class="q-section-title">这题在考什么</div>' +
        '<p>' + h(question.probe) + '</p></div>');
      html.push(this.listSection('参考思路', question.outline, 'ok'));
      html.push(this.listSection('答到这些会加分', question.plus, 'plus'));
      html.push(this.listSection('听到这些就知道没真做过', question.minus, 'minus'));
      html.push(this.listSection('面试官会接着追问', question.followups, 'follow'));
      if (question.related) {
        html.push('<div class="q-section"><div class="q-section-title">相关章节</div>' +
          '<p class="mono">' + h(question.related) + '</p></div>');
      }
      html.push('</div>');

      html.push('<div class="rate-row"><span class="rate-label">自评这一题：</span>');
      var rates = [
        [0, '不会', '没见过 / 答不上来'],
        [1, '半懂', '想起来了一部分，但说不全'],
        [2, '掌握', '能说清机制，也能说出边界']
      ];
      for (var i = 0; i < rates.length; i++) {
        html.push('<button class="rate-btn rate-' + rates[i][0] +
          (chosen === rates[i][0] ? ' active' : '') + '" data-score="' + rates[i][0] + '">' +
          '<strong>' + rates[i][1] + '</strong><small>' + rates[i][2] + '</small></button>');
      }
      html.push('</div>');
    }

    html.push('<div class="runner-nav">');
    html.push(run.index > 0
      ? '<button class="btn btn-ghost btn-sm" id="mockPrev">← 上一题</button>'
      : '<span></span>');
    html.push('<button class="btn btn-ghost btn-sm" id="mockSkip">跳过</button>');
    html.push('<button class="btn btn-primary btn-sm" id="mockNext">' +
      (run.index === run.queue.length - 1 ? '提交并看报告' : '下一题 →') + '</button>');
    html.push('</div>');

    html.push('</article>');
    stage.innerHTML = html.join('');

    this.wire();
  };

  MockRunner.listSection = function (title, items, cls) {
    if (!items || !items.length) return '';
    var html = '<div class="q-section ' + cls + '"><div class="q-section-title">' + h(title) + '</div><ul class="q-list">';
    for (var i = 0; i < items.length; i++) {
      html += '<li>' + h(items[i]) + '</li>';
    }
    return html + '</ul></div>';
  };

  MockRunner.wire = function () {
    var self = this;
    var bind = function (id, handler) {
      var element = document.getElementById(id);
      if (element) element.addEventListener('click', handler);
    };

    bind('mockReveal', function () {
      self.run.revealed[self.current().id] = true;
      self.render();
    });

    var rateButtons = document.querySelectorAll('.rate-btn');
    for (var i = 0; i < rateButtons.length; i++) {
      rateButtons[i].addEventListener('click', function (event) {
        var target = event.currentTarget;
        self.answer(self.current().id, parseInt(target.getAttribute('data-score'), 10));
        self.render();
      });
    }

    bind('mockPrev', function () {
      if (self.run.index > 0) { self.run.index -= 1; self.render(); }
    });

    bind('mockSkip', function () {
      if (self.run.index < self.run.queue.length) { self.run.index += 1; self.render(); }
    });

    bind('mockNext', function () {
      // 没自评就默认 0 分，避免"跳过评分"导致报告失真
      var question = self.current();
      if (self.run.answers[question.id] == null) self.run.answers[question.id] = 0;
      self.run.index += 1;
      self.render();
    });
  };

  MockRunner.renderReport = function () {
    var record = this.finish();
    var stage = document.getElementById('mockReport');
    if (!record || !stage) return;

    var rate = record.max ? Math.round(record.total / record.max * 100) : 0;
    var html = [];
    html.push('<article class="report">');
    html.push('<h2 class="report-title">这一场的结果</h2>');
    html.push('<div class="report-score">' +
      '<span class="report-num">' + record.total + '<small>/' + record.max + '</small></span>' +
      '<span class="report-rate">通过率 ' + rate + '%　用时 ' +
      Math.round(record.durationSec / 60) + ' 分钟</span></div>');

    html.push('<div class="q-section"><div class="q-section-title">按维度</div>');
    for (var dim in record.byDimension) {
      var item = record.byDimension[dim];
      var percent = Math.round(item.rate * 100);
      html.push('<div class="dim-row"><span class="dim-name">' + h(dim) + '</span>' +
        '<span class="dim-bar"><i style="width:' + percent + '%"></i></span>' +
        '<span class="dim-value">' + percent + '%</span></div>');
    }
    html.push('</div>');

    html.push('<div class="q-section"><div class="q-section-title">按轮次</div>');
    for (var round in record.byRound) {
      var roundItem = record.byRound[round];
      var roundPercent = Math.round(roundItem.rate * 100);
      html.push('<div class="dim-row"><span class="dim-name">' + h(round) + '</span>' +
        '<span class="dim-bar"><i style="width:' + roundPercent + '%"></i></span>' +
        '<span class="dim-value">' + roundPercent + '%</span></div>');
    }
    html.push('</div>');

    if (record.wrong.length) {
      html.push('<div class="q-section minus"><div class="q-section-title">' +
        '这 ' + record.wrong.length + ' 题要回炉（得分 ≤ 1）</div><ul class="q-list">');
      for (var w = 0; w < record.wrong.length; w++) {
        var wrong = record.wrong[w];
        html.push('<li>' + h(wrong.id) + '　' + h(wrong.title) +
          (wrong.related ? '　<span class="mono">' + h(wrong.related) + '</span>' : '') + '</li>');
      }
      html.push('</ul></div>');
    } else {
      html.push('<div class="q-section ok"><div class="q-section-title">全场没有得分低于 2 的题</div>' +
        '<p>可以换一场更难的，或者去练项目表达。</p></div>');
    }

    html.push('<div class="q-section"><div class="q-section-title">下一步</div><ul class="q-list">' +
      '<li>得分为 0 的题，回到上面的「相关章节」重读一遍，然后<b>合上材料自己讲一遍</b></li>' +
      '<li>得分为 1 的题，说明知道但说不全——问题在表达，不在知识：练"结论 → 依据 → 边界"三句话</li>' +
      '<li>同一套题隔两周再打一次，<b>对比分数变化比单次分数更有意义</b></li>' +
      '</ul></div>');

    html.push('<div class="runner-nav">' +
      '<a class="btn btn-ghost" href="#/mock">回到面试首页</a>' +
      '<button class="btn btn-primary" id="mockExportOne">导出这份报告</button>' +
      '</div>');
    html.push('</article>');

    // 清掉题目区并停表
    var card = document.querySelector('.q-card');
    if (card) card.remove();
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    var bar = document.getElementById('mockBar');
    if (bar) bar.style.width = '100%';

    stage.innerHTML = html.join('');

    // 题目卡被移除后文档会突然变短，滚动位置会悬在空白处 —— 把视图拉回报告开头
    var reportTop = stage.getBoundingClientRect().top + window.pageYOffset - 80;
    window.scrollTo(0, Math.max(0, reportTop));

    var self = this;
    var exportBtn = document.getElementById('mockExportOne');
    if (exportBtn) exportBtn.addEventListener('click', function () {
      downloadJson('azto-mock-' + record.date.slice(0, 10) + '.json', record);
      toast('报告已导出');
    });
  };

  MockRunner.tick = function () {
    var el = document.getElementById('mockTimer');
    if (!el || !this.run) return;
    var seconds = Math.round((Date.now() - this.run.startedAt) / 1000);
    var mm = Math.floor(seconds / 60);
    var ss = seconds % 60;
    el.textContent = (mm < 10 ? '0' : '') + mm + ':' + (ss < 10 ? '0' : '') + ss;
  };

  MockRunner.mount = function () {
    var self = this;
    if (!this.run) return;
    this.render();
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(function () { self.tick(); }, 1000);
    this.tick();

    var quit = document.getElementById('mockQuit');
    if (quit) quit.addEventListener('click', function () {
      if (window.confirm('结束这一场并看报告？（未作答的题按 0 分计）')) {
        self.run.index = self.run.queue.length;
        self.render();
      }
    });
  };

  views.mockDocs = function () {
    var html = [pageHead('面试流程与评分', '面试官视角的流程脚本、评分表与反馈模板')];
    var cards = DATA.mock.documents.map(function (doc) {
      return '<a class="card" href="#/mock/docs/' + encodeURIComponent(doc.id) + '">' +
        '<h3 class="card-title">' + h(doc.title) + '</h3>' +
        '<p class="card-summary">' + h(doc.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">文档</span>' +
        '<span class="card-size">' + h(doc.file) + '</span></div></a>';
    });
    html.push('<div class="page-body">' + cardGrid(cards) + '</div>');
    return html.join('');
  };

  views.mockDoc = function (parts) {
    var id = decodeURIComponent(parts.slice(2).join('/'));
    var doc = null;
    for (var i = 0; i < DATA.mock.documents.length; i++) {
      if (DATA.mock.documents[i].id === id) doc = DATA.mock.documents[i];
    }
    if (!doc) return notFound('找不到这份文档');
    return readerShell('面试流程与评分', '#/mock/docs', doc);
  };

  /* ---------------------------------------------------------- 算法面试轨道 */

  var AlgoBundle = {
    loading: false,
    callbacks: [],

    /** content-algo.js 有 1.4MB，只在真正要看算法内容时才加载 */
    ensure: function (done) {
      if (window.AZTO_ALGO) return done();
      this.callbacks.push(done);
      if (this.loading) return;
      this.loading = true;

      var self = this;
      var script = document.createElement('script');
      script.src = 'content-algo.js';
      script.onload = function () {
        self.loading = false;
        var callbacks = self.callbacks.slice();
        self.callbacks = [];
        for (var i = 0; i < callbacks.length; i++) callbacks[i]();
      };
      script.onerror = function () {
        self.loading = false;
        self.callbacks = [];
        toast('算法轨道内容没加载成功 —— 先在项目根目录跑一次 python web/_build.py');
      };
      document.head.appendChild(script);
    }
  };

  views.algoIndex = function () {
    var stats = DATA.algo ? DATA.algo.stats : { topics: 0, problems: 0, dayNotes: 0 };

    if (!window.AZTO_ALGO) {
      var self = this;
      AlgoBundle.ensure(function () { route(); });
      return '<div class="view"><div class="page-body">' +
        '<p class="empty loading">正在加载算法轨道内容（13 个专题 + 100 道题解，约 1.4 MB）…</p>' +
        '</div></div>';
    }

    var algo = window.AZTO_ALGO;
    var html = [];
    html.push(pageHead('算法面试轨道',
      stats.topics + ' 个专题 · ' + stats.problems + ' 道 Hot 100 详解 · ' + stats.dayNotes + ' 天路线'));

    if (algo.readme) {
      html.push('<div class="page-body"><article class="reader"><div class="reader-body">' +
        '<div class="markdown">' + MD.render(algo.readme.markdown) + '</div></div></article></div>');
    }

    // 算法进度看板（这条轨道唯一的状态文件，可点进去读）
    if (algo.config && algo.config.length) {
      var board = algo.config[0];
      html.push('<div class="page-body"><div class="panel">' +
        '<h3 class="card-title">' + h(board.title) + '</h3>' +
        '<p class="page-sub">' + h(board.summary) + '</p>' +
        '<div class="page-actions" style="margin-top:12px">' +
        '<a class="btn btn-sm btn-primary" href="#/algo/config/' + encodeURIComponent(board.id) + '">打开看板</a>' +
        '</div></div></div>');
    }

    // 专题
    html.push('<div class="page-body"><h2 class="page-title">专题总结</h2>');
    var topicCards = algo.topics.map(function (topic) {
      return '<a class="card" href="#/algo/topic/' + encodeURIComponent(topic.id) + '">' +
        '<h3 class="card-title">' + h(topic.title) + '</h3>' +
        '<p class="card-summary">' + h(topic.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">专题</span>' +
        '<span class="card-size">' + h(topic.file) + '</span></div></a>';
    });
    html.push(cardGrid(topicCards));
    html.push('</div>');

    // 题目
    html.push('<div class="page-body"><h2 class="page-title">题目详解（' + algo.problems.length + ' 道）</h2>');
    html.push('<div class="problem-grid">');
    for (var i = 0; i < algo.problems.length; i++) {
      var problem = algo.problems[i];
      var numberMatch = /^(\d+)/.exec(problem.id);
      html.push('<a class="problem-chip" href="#/algo/problem/' + encodeURIComponent(problem.id) + '" ' +
        'title="' + h(problem.title) + '">' +
        '<span class="problem-no">' + h(numberMatch ? numberMatch[1] : '') + '</span>' +
        '<span class="problem-name">' + h(problem.title) + '</span></a>');
    }
    html.push('</div></div>');

    // 天笔记
    html.push('<div class="page-body"><h2 class="page-title">14 天路线笔记</h2>');
    var noteCards = algo.dayNotes.map(function (note) {
      return '<a class="card" href="#/algo/note/' + encodeURIComponent(note.id) + '">' +
        '<h3 class="card-title">' + h(note.title) + '</h3>' +
        '<p class="card-summary">' + h(note.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">笔记</span>' +
        '<span class="card-size">' + h(note.file) + '</span></div></a>';
    });
    html.push(cardGrid(noteCards));
    html.push('</div>');

    // 来源声明
    html.push('<div class="page-body"><div class="panel">' +
      '<h3 class="card-title">来源</h3>' +
      '<p class="page-sub">本轨道内容来自开源项目 ' +
      '<a href="https://github.com/mo-lx/LeetCode-BaiTiTong" target="_blank" rel="noopener">LeetCode-BaiTiTong</a>' +
      '（MIT 协议，作者 mo-lx）。正文一字未改，只把内部 wikilink 的路径前缀换成了本项目里的位置。</p>' +
      '</div></div>');

    return html.join('');
  };

  views.algoItem = function (parts) {
    if (!window.AZTO_ALGO) {
      AlgoBundle.ensure(function () { route(); });
      return '<div class="view"><div class="page-body">' +
        '<p class="empty loading">正在加载算法轨道内容…</p></div></div>';
    }

    var algo = window.AZTO_ALGO;
    var kind = parts[1];
    var id = decodeURIComponent(parts.slice(2).join('/'));
    var list = kind === 'topic' ? algo.topics
      : kind === 'problem' ? algo.problems
      : kind === 'note' ? algo.dayNotes
      : kind === 'config' ? algo.config : null;
    if (!list) return notFound('未知的算法内容分类');

    var index = -1;
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) index = i;
    }
    if (index < 0) return notFound('找不到这份内容：' + h(id));

    var item = list[index];
    var label = kind === 'topic' ? '算法专题'
      : kind === 'problem' ? '题目详解'
      : kind === 'note' ? '路线笔记' : '算法进度看板';
    var prev = index > 0 ? list[index - 1] : null;
    var next = index < list.length - 1 ? list[index + 1] : null;

    var html = [];
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb"><a href="#/algo">算法面试轨道</a> / ' + h(label) + '</div>');
    html.push('<h1 class="reader-title">' + h(item.title) + '</h1>');
    html.push('<p class="page-sub">' + h(item.summary) + '</p>');
    html.push('<div class="reader-meta"><span class="meta-chip mono">' + h(item.file) + '</span></div>');
    html.push('<div class="reader-actions"><button class="btn btn-sm btn-ghost" id="tocBtn">目录</button>' +
      '<a class="btn btn-sm btn-ghost" href="' + h('../' + item.file) + '" download>下载 .md</a></div>');
    html.push('</div>');
    html.push('<div class="reader-body">');
    html.push('<aside class="reader-toc" id="readerToc">' + tocHtml(item.markdown) + '</aside>');
    html.push('<div class="markdown" id="mdBody">' + MD.render(item.markdown) + '</div>');
    html.push('</div>');
    html.push('<nav class="reader-nav">');
    html.push(prev ? '<a class="reader-prev" href="#/algo/' + kind + '/' + encodeURIComponent(prev.id) + '">' +
      '<span class="rn-dir">← 上一篇</span><span class="rn-title">' + h(prev.title) + '</span></a>'
      : '<span class="reader-prev"></span>');
    html.push(next ? '<a class="reader-next" href="#/algo/' + kind + '/' + encodeURIComponent(next.id) + '">' +
      '<span class="rn-dir">下一篇 →</span><span class="rn-title">' + h(next.title) + '</span></a>'
      : '<span class="reader-next"></span>');
    html.push('</nav></article>');
    return html.join('');
  };

  /* ---------------------------------------------------- 在线判题（做题页） */

  var JUDGE_DEFAULT_URL = 'http://127.0.0.1:8900';
  var CODE_KEY = 'azto.code.v1';
  var LANG_KEY = 'azto.lang.v1';
  var JUDGE_URL_KEY = 'azto.judge.url';

  var Judge = {
    url: JUDGE_DEFAULT_URL,
    online: null,          // null=还没探测，true/false=结果
    problems: null,        // 服务端返回的题目数
    llm: null,
    busy: false,
    lastReport: null,
    stats: null,           // /stats 的 byProblem：每题掌握状态
    records: [],           // 当前题目的提交记录
    languages: null,       // /status 的 languages：各语言编译器是否可用
    lang: 'python',        // 当前选中的语言
    code: {},              // {problemId: {语言: 源码}}

    init: function () {
      try {
        var saved = localStorage.getItem(JUDGE_URL_KEY);
        if (saved) this.url = saved;
        var codes = localStorage.getItem(CODE_KEY);
        if (codes) this.code = JSON.parse(codes) || {};
        var lang = localStorage.getItem(LANG_KEY);
        if (lang) this.lang = lang;
      } catch (err) { /* 忽略 */ }
      this.migrate();
    },

    /** 早期版本按题存一份代码，现在按（题，语言）存。把老数据搬过去，别丢。 */
    migrate: function () {
      var changed = false;
      for (var pid in this.code) {
        if (typeof this.code[pid] === 'string') {
          this.code[pid] = { python: this.code[pid] };
          changed = true;
        }
      }
      if (changed) this.persist();
    },

    persist: function () {
      try { localStorage.setItem(CODE_KEY, JSON.stringify(this.code)); } catch (err) { /* 忽略 */ }
    },

    setLang: function (lang) {
      this.lang = lang;
      try { localStorage.setItem(LANG_KEY, lang); } catch (err) { /* 忽略 */ }
    },

    saveUrl: function (value) {
      this.url = value;
      try { localStorage.setItem(JUDGE_URL_KEY, value); } catch (err) { /* 忽略 */ }
    },

    saveCode: function (problemId, language, source) {
      if (!this.code[problemId] || typeof this.code[problemId] === 'string') {
        this.code[problemId] = {};
      }
      this.code[problemId][language] = source;
      this.persist();
    },

    getCode: function (problemId, language) {
      var entry = this.code[problemId];
      if (!entry || typeof entry === 'string') return '';
      return entry[language || this.lang] || '';
    },

    /** 某道题在哪些语言里写过 */
    writtenLangs: function (problemId) {
      var entry = this.code[problemId];
      if (!entry || typeof entry === 'string') return [];
      return Object.keys(entry).filter(function (key) { return entry[key]; });
    },

    /** 探测本地判题服务。没开时页面给出启动命令，而不是让用户干等超时。 */
    probe: function (done) {
      var self = this;
      var finished = false;
      var timer = setTimeout(function () {
        if (finished) return;
        finished = true;
        self.online = false;
        done(false);
      }, 2500);

      fetch(this.url + '/status')
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          self.online = !!data.ok;
          self.problems = data.problems;
          self.llm = data.llm;
          self.languages = data.languages || null;
          done(self.online);
        })
        .catch(function () {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          self.online = false;
          done(false);
        });
    },

    /** 拉一遍全局统计（题目列表上的绿勾、顶部进度条都靠它） */
    refreshStats: function (done) {
      var self = this;
      fetch(this.url + '/stats')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          self.stats = data.byProblem || {};
          self.overall = data;
          if (done) done(data);
        })
        .catch(function () { if (done) done(null); });
    },

    /** 拉某道题的提交记录 */
    loadRecords: function (problemId, done) {
      var self = this;
      fetch(this.url + '/submissions?limit=50&problemId=' + encodeURIComponent(problemId))
        .then(function (r) { return r.json(); })
        .then(function (data) { self.records = data.submissions || []; if (done) done(); })
        .catch(function () { self.records = []; if (done) done(); });
    },

    statusOf: function (problemId) {
      var entry = (this.stats || {})[problemId];
      return entry ? entry.status : 'untried';
    },

    post: function (path, payload) {
      return fetch(this.url + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
          return data;
        });
      });
    },

    /** 生成解题模板。Python 在这里拼，C++ 用构建时生成好的（返回类型要从用例反推）。 */
    starter: function (problem, language) {
      var lang = language || this.lang;
      var judge = problem.judge || {};
      if (lang === 'cpp') {
        return judge.cppStarter || 'class Solution {\npublic:\n    // 这道题暂时没有 C++ 模板\n};\n';
      }
      if (judge.kind === 'operations') {
        var lines = ['class ' + (judge.entry || 'Solution') + ':', '',
                     '    def __init__(self, capacity):',
                     '        # 在这里初始化你的数据结构', '        pass', ''];
        var methods = (judge.signature || '').split('需要实现：')[1];
        if (methods) {
          methods.split('、').forEach(function (name) {
            lines.push('    def ' + name + '(self, *args):');
            lines.push('        # 在这里实现 ' + name);
            lines.push('        pass');
            lines.push('');
          });
        }
        return lines.join('\n');
      }
      var signature = (judge.signature || 'def solve()');
      if (signature.indexOf('def ') !== 0) return signature + '\n    pass\n';
      var params = signature.replace(/^def\s+\w+\((.*)\)$/, '$1');
      var body = params ? params.split(',').map(function (name) {
        return '    ' + name.trim() + ' = ?';
      }).join('\n') + '\n\n' : '';
      return signature + ':\n' + body + '    # 在这里写你的解法\n    pass\n';
    },

    /** 题面：只取「题目描述」那一段 —— 题解里有完整答案，别一上来就给读者看 */
    statement: function (markdown) {
      var start = markdown.indexOf('## 题目描述');
      if (start < 0) {
        var cut = markdown.search(/\n##\s/);
        return cut < 0 ? markdown : markdown.slice(0, cut);
      }
      var rest = markdown.slice(start);
      var end = rest.indexOf('\n## ', 6);
      return end < 0 ? rest : rest.slice(0, end);
    }
  };

  Judge.init();

  function judgeHintPanel(message, detail) {
    return '<div class="panel judge-offline" id="judgeStatus">' +
      '<h3 class="card-title">' + h(message) + '</h3>' +
      '<p class="page-sub">' + detail + '</p>' +
      '<pre><code class="lang-bash">cd "05-算法面试/06-在线判题"' + '\n' +
      'python judge_server.py</code></pre>' +
      '<p class="page-sub">启动后回到这一页点「重新连接」。端口不是 8900 的话，在下面填新地址。</p>' +
      '<div class="field-row">' +
      '<label class="field"><span>判题服务地址</span>' +
      '<input id="judgeUrl" class="text-input" value="' + h(Judge.url) + '"></label>' +
      '<button class="btn btn-primary" id="judgeReconnect">重新连接</button>' +
      '</div></div>';
  }

  views.judgeIndex = function () {
    var stats = (window.AZTO_ALGO && window.AZTO_ALGO.stats) ? window.AZTO_ALGO.stats : DATA.algo.stats;

    if (!window.AZTO_ALGO) {
      AlgoBundle.ensure(function () { route(); });
      return '<div class="view"><div class="page-body">' +
        '<p class="empty loading">正在加载题库…</p></div></div>';
    }

    var html = [];
    html.push(pageHead('在线判题',
      (stats.judgeable || 0) + ' 道题可判 · ' + (stats.judgeCases || 0) + ' 个用例 · ' +
      '在本机写代码、跑用例、看判定'));

    html.push('<div class="page-body" id="judgeStatusWrap">' +
      '<div id="judgeStatus" class="panel"><p class="page-sub loading">正在探测本地判题服务…</p></div>' +
      '</div>');

    html.push('<div class="page-body" id="aiConfigWrap"></div>');
    html.push('<div class="page-body" id="judgeProgress"></div>');

    html.push('<div class="page-body"><p class="page-sub">' +
      '判题服务跑在你自己的电脑上，<b>代码不会离开本机</b>；' +
      'AI 讲评由同一个服务转发到你配置的模型，<b>密钥不会进浏览器</b>。' +
      '提交记录存在服务端，换浏览器也还在。</p></div>');

    html.push('<div class="page-body"><div id="judgeList"></div></div>');
    html.push('<div class="page-body">' +
      '<div class="page-actions"><a class="btn btn-ghost" href="#/judge/records">' +
      '查看全部提交记录 →</a></div></div>');
    return html.join('');
  };

  views.judgeProblem = function (parts) {
    var id = decodeURIComponent(parts.slice(1).join('/'));

    if (!window.AZTO_ALGO) {
      AlgoBundle.ensure(function () { route(); });
      return '<div class="view"><div class="page-body">' +
        '<p class="empty loading">正在加载题目…</p></div></div>';
    }

    var problem = null;
    for (var i = 0; i < window.AZTO_ALGO.problems.length; i++) {
      if (window.AZTO_ALGO.problems[i].id === id) problem = window.AZTO_ALGO.problems[i];
    }
    if (!problem) return notFound('找不到这道题：' + h(id));

    var judge = problem.judge;
    var html = [];
    html.push('<div class="view judge-layout">');

    // 左侧：题面（只放题目描述，不放题解）
    html.push('<aside class="judge-side">');
    html.push('<div class="judge-side-head">');
    html.push('<div class="reader-crumb">' +
      '<a href="#/judge">在线判题</a> / <a href="#/algo/problem/' + encodeURIComponent(id) + '">完整题解</a></div>');
    html.push('<h1 class="judge-title">' + h(problem.title) + '</h1>');
    if (judge && judge.level) {
      html.push('<span class="judge-badge ' + h(judge.level) + '">' + h(judge.level) + '</span>');
    }
    html.push('</div>');
    html.push('<div class="markdown judge-statement">' +
      MD.render(Judge.statement(problem.markdown)) + '</div>');
    if (judge) {
      html.push('<div class="judge-side-foot">');
      html.push('<div class="q-section-title">判题约定</div>');
      html.push('<p class="mono judge-signature">' + h(judge.signature) + '</p>');
      if (judge.hints && judge.hints.length) {
        html.push('<ul class="q-list">' + judge.hints.map(function (text) {
          return '<li>' + h(text) + '</li>';
        }).join('') + '</ul>');
      }
      if (judge.sample) {
        html.push('<div class="q-section-title" style="margin-top:12px">第一个用例</div>');
        html.push('<p class="mono judge-sample">' + h(judge.sample) + '</p>');
      }
      html.push('</div>');
    }
    html.push('</aside>');

    // 右侧：工具栏 + 编辑器 + 结果
    html.push('<section class="judge-main">');
    html.push('<div class="judge-toolbar">');
    html.push('<label class="field judge-lang-field"><span>语言</span>' +
      '<select id="judgeLang"></select></label>');
    html.push('<span class="judge-conn" id="judgeConn">检测中…</span>');
    html.push('<span class="judge-spacer"></span>');
    html.push('<button class="btn btn-sm btn-ghost" id="judgeRun">运行</button>');
    html.push('<button class="btn btn-sm btn-primary" id="judgeSubmit">提交</button>');
    html.push('<button class="btn btn-sm btn-ghost" id="judgeAsk">AI 讲评</button>');
    if (judge && judge.leetcode) {
      html.push('<a class="btn btn-sm btn-ghost" href="' + h(judge.leetcode) +
        '" target="_blank" rel="noopener">去力扣提交 ↗</a>');
    }
    html.push('<button class="btn btn-sm btn-ghost" id="judgeReset" title="恢复初始模板">重置</button>');
    html.push('</div>');

    html.push('<div class="editor">' +
      '<div class="editor-gutter" id="editorGutter">1</div>' +
      '<textarea class="editor-input" id="editorInput" spellcheck="false" ' +
      'autocomplete="off" autocapitalize="off" wrap="off"></textarea>' +
      '</div>');

    html.push('<div class="judge-result" id="judgeResult">' +
      '<p class="page-sub">点「运行」只跑第一个用例（快），点「提交」跑全部用例，' +
      'Ctrl+Enter 也能提交。</p></div>');

    html.push('<div class="judge-history" id="judgeHistory">' +
      '<div class="q-section-title">提交记录</div>' +
      '<p class="page-sub loading">正在读取…</p></div>');
    html.push('</section>');

    html.push('</div>');
    return html.join('');
  };

  views.judgeRecords = function () {
    var html = [pageHead('提交记录', '每一次提交都留着：结论、用时、内存，以及当时写的代码',
      '<button class="btn btn-ghost btn-sm" id="recordsClear">清空全部记录</button>')];
    html.push('<div class="page-body"><div id="recordsBox">' +
      '<p class="page-sub loading">正在读取…</p></div></div>');
    return html.join('');
  };

  function renderAllRecords() {
    var box = document.getElementById('recordsBox');
    if (!box) return;

    fetch(Judge.url + '/submissions?limit=200')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var rows = data.submissions || [];
        if (!rows.length) {
          box.innerHTML = '<p class="empty">还没有任何提交记录。' +
            '去 <a href="#/judge">在线判题</a> 挑一道题交一次就有了。</p>';
          return;
        }

        var html = ['<div class="table-wrap"><table><thead><tr>' +
          '<th>时间</th><th>题目</th><th>语言</th><th>结论</th><th>用例</th>' +
          '<th>用时</th><th>内存</th><th></th></tr></thead><tbody>'];
        for (var i = 0; i < rows.length; i++) {
          var row = rows[i];
          var tone = row.verdict === 'accepted' ? 'ok'
            : row.verdict === 'partial' ? 'warn' : 'bad';
          html.push('<tr>' +
            '<td class="mono">' + h((row.submitTime || '').slice(5)) + '</td>' +
            '<td><a href="#/judge/' + encodeURIComponent(row.problemId) + '">' +
              h(row.problemTitle || row.problemId) + '</a></td>' +
            '<td><span class="history-lang">' + h(row.languageLabel || '') + '</span></td>' +
            '<td class="record-' + tone + '">' + h(row.verdictLabel) + '</td>' +
            '<td class="mono">' + row.passed + '/' + row.total + '</td>' +
            '<td class="mono">' + (row.ms ? Math.round(row.ms) + ' ms' : '—') + '</td>' +
            '<td class="mono">' + (row.memoryKB ? (row.memoryKB / 1024).toFixed(1) + ' MB' : '—') + '</td>' +
            '<td><button class="btn btn-sm btn-ghost" data-restore="' + h(row.id) + '">恢复代码</button></td>' +
            '</tr>');
        }
        html.push('</tbody></table></div>');
        html.push('<p class="page-sub" style="margin-top:12px">共 ' + rows.length +
          ' 条（最多保留 500 条）。「恢复代码」会把那次提交的源码放回做题页的编辑器。</p>');
        box.innerHTML = html.join('');

        var buttons = box.querySelectorAll('[data-restore]');
        for (var k = 0; k < buttons.length; k++) {
          buttons[k].addEventListener('click', function (event) {
            var id = event.currentTarget.getAttribute('data-restore');
            fetch(Judge.url + '/submissions/' + encodeURIComponent(id))
              .then(function (r) { return r.json(); })
              .then(function (data) {
                var record = data.submission;
                if (!record) return;
                var target = record.language || 'python';
                Judge.setLang(target);
                location.hash = '#/judge/' + encodeURIComponent(record.problemId);
                setTimeout(function () {
                  var select = document.getElementById('judgeLang');
                  if (select) select.value = target;
                  var textarea = document.getElementById('editorInput');
                  if (textarea) {
                    textarea.value = record.code;
                    textarea.dispatchEvent(new Event('input', { bubbles: true }));
                    toast('已恢复 ' + record.submitTime + ' 的 ' +
                      (record.languageLabel || target) + ' 代码');
                  }
                }, 700);
              });
          });
        }
      })
      .catch(function () {
        box.innerHTML = '<p class="empty">读不到记录 —— 判题服务没在运行？</p>';
      });
  }

  /** 填语言下拉框。装不了的编译器标成不可用，用户一眼知道为什么选不了。 */
  function fillLanguageSelect(problem) {
    var select = document.getElementById('judgeLang');
    if (!select) return;

    var languages = Judge.languages || { python: { available: true, label: 'Python 3' } };
    var options = [];
    var names = Object.keys(languages);
    for (var i = 0; i < names.length; i++) {
      var info = languages[names[i]];
      var disabled = info.available ? '' : ' disabled';
      var suffix = info.available ? '' : '（未安装编译器）';
      options.push('<option value="' + h(names[i]) + '"' + disabled + '>' +
        h(info.label) + suffix + '</option>');
    }
    select.innerHTML = options.join('');

    // 当前语言不可用就退到第一个可用的
    if (!languages[Judge.lang] || !languages[Judge.lang].available) {
      for (var j = 0; j < names.length; j++) {
        if (languages[names[j]].available) { Judge.setLang(names[j]); break; }
      }
    }
    select.value = Judge.lang;

    select.addEventListener('change', function () {
      // 切换语言前，把当前编辑器里的内容存回**旧语言**的槽位
      var textarea = document.getElementById('editorInput');
      if (textarea && problem) Judge.saveCode(problem.id, Judge.lang, textarea.value);

      Judge.setLang(select.value);
      var box = document.getElementById('judgeResult');
      if (box) {
        box.innerHTML = '<p class="page-sub">已切到 ' +
          h((languages[select.value] || {}).label || select.value) +
          '。每种语言的代码分别保存，切换不会互相覆盖。</p>';
      }
      mountEditor(problem);
    });
  }

  /**
   * AI 接口配置面板。
   *
   * 密钥的处理：**页面永远拿不到已经保存的完整密钥**（服务端只回打码后的样子）。
   * 输入框留空 = 不修改，所以"改模型但不想重填密钥"是可行的。
   */
  function renderAiConfig() {
    var wrap = document.getElementById('aiConfigWrap');
    if (!wrap) return;

    if (!Judge.online) {
      wrap.innerHTML = '';
      return;
    }

    var box = document.getElementById('aiConfigBox');
    if (box) return;              // 已经渲染过，别把用户正在填的内容冲掉

    wrap.innerHTML =
      '<div class="panel ai-config" id="aiConfigBox">' +
        '<div class="ai-config-head">' +
          '<h3 class="card-title">AI 接口配置</h3>' +
          '<span class="ai-config-state" id="aiConfigState">读取中…</span>' +
        '</div>' +
        '<p class="page-sub">配好之后，「AI 讲评」就能用你自己的模型点评代码。' +
        '配置只写在本机文件里，<b>不会上传到任何地方</b>。</p>' +
        '<div class="field-row">' +
          '<label class="field"><span>服务商预设（一键填入）</span>' +
          '<select id="aiPreset"><option value="">自定义…</option></select></label>' +
        '</div>' +
        '<div class="field-row">' +
          '<label class="field field-wide"><span>API 地址</span>' +
          '<input id="aiBaseUrl" class="text-input" placeholder="https://api.deepseek.com/v1">' +
          '<small class="field-hint">大多数服务商要求以 <span class="mono">/v1</span> 结尾，漏了会报 404</small>' +
          '</label>' +
        '</div>' +
        '<div class="field-row">' +
          '<label class="field field-wide"><span>API Key</span>' +
          '<input id="aiApiKey" class="text-input" type="password" autocomplete="off" ' +
          'placeholder="sk-...">' +
          '<small class="field-hint" id="aiKeyHint">留空表示不修改已保存的密钥</small>' +
          '</label>' +
          '<button class="btn btn-ghost btn-sm" id="aiKeyToggle">显示</button>' +
        '</div>' +
        '<div class="field-row">' +
          '<label class="field field-wide"><span>模型 ID</span>' +
          '<input id="aiModel" class="text-input" placeholder="deepseek-chat">' +
          '<small class="field-hint">要和服务商匹配，写错会报 404 model not found</small>' +
          '</label>' +
        '</div>' +
        '<div class="field-row ai-config-actions">' +
          '<button class="btn btn-ghost" id="aiTest">测试连接</button>' +
          '<button class="btn btn-primary" id="aiSave">保存</button>' +
          '<span class="ai-config-msg" id="aiConfigMsg"></span>' +
        '</div>' +
        '<p class="page-sub" id="aiConfigSource"></p>' +
      '</div>';

    wireAiConfig();
    loadAiConfig();
  }

  function loadAiConfig() {
    fetch(Judge.url + '/config')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var state = document.getElementById('aiConfigState');
        if (state) {
          state.className = 'ai-config-state ' + (data.configured ? 'is-on' : 'is-off');
          state.textContent = data.configured
            ? '已配置 ' + data.model + '（密钥 ' + data.keyMasked + '）'
            : '未配置';
        }
        var baseUrl = document.getElementById('aiBaseUrl');
        var model = document.getElementById('aiModel');
        if (baseUrl && !baseUrl.value) baseUrl.value = data.baseUrl || '';
        if (model && !model.value) model.value = data.model || '';

        var hint = document.getElementById('aiKeyHint');
        if (hint && data.configured) {
          hint.textContent = '已保存 ' + data.keyMasked + '，留空表示不修改';
        }

        var source = document.getElementById('aiConfigSource');
        if (source) {
          source.innerHTML = data.source
            ? '当前生效的配置来自 <span class="mono">' + h(data.source) + '</span>。' +
              '在下面保存会写入 <span class="mono">' + h(relativePath(data.writableFile)) +
              '</span>，它的优先级更高（因为这个文件是"你刚配的"）。'
            : '还没有配置文件。保存后会写入 <span class="mono">' +
              h(relativePath(data.writableFile)) + '</span>。';
        }

        var preset = document.getElementById('aiPreset');
        if (preset && !preset.dataset.filled) {
          preset.dataset.filled = '1';
          var options = ['<option value="">自定义…</option>'];
          for (var i = 0; i < (data.presets || []).length; i++) {
            options.push('<option value="' + i + '">' + h(data.presets[i].name) + '</option>');
          }
          preset.innerHTML = options.join('');
          preset.dataset.presets = JSON.stringify(data.presets || []);
        }
      })
      .catch(function () { /* 服务没起来时静默 */ });
  }

  /** 只保留项目内的相对路径，绝对路径在页面上又长又没用 */
  function relativePath(full) {
    if (!full) return '';
    var parts = String(full).replace(/\\/g, '/').split('/');
    for (var i = 0; i < parts.length; i++) {
      // 去掉项目根之前的部分 —— 页面上只要 06-在线判题/judge.env 这种相对路径
      if (parts[i] === '05-算法面试') return parts.slice(i + 1).join('/');
    }
    return parts.slice(-2).join('/');
  }

  function wireAiConfig() {
    var preset = document.getElementById('aiPreset');
    if (preset) {
      preset.addEventListener('change', function () {
        var presets = JSON.parse(preset.dataset.presets || '[]');
        var picked = presets[parseInt(preset.value, 10)];
        if (!picked) return;
        var baseUrl = document.getElementById('aiBaseUrl');
        var model = document.getElementById('aiModel');
        if (baseUrl) baseUrl.value = picked.baseUrl;
        if (model) model.value = picked.model;
        say('已填入 ' + picked.name + ' 的地址与模型名。别忘了填 API Key，然后点「测试连接」。');
      });
    }

    var toggle = document.getElementById('aiKeyToggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var field = document.getElementById('aiApiKey');
        if (!field) return;
        var showing = field.type === 'text';
        field.type = showing ? 'password' : 'text';
        toggle.textContent = showing ? '显示' : '隐藏';
      });
    }

    var testBtn = document.getElementById('aiTest');
    if (testBtn) {
      testBtn.addEventListener('click', function () {
        say('正在测试…', 'pending');
        testBtn.disabled = true;
        Judge.post('/config/test', readAiConfig())
          .then(function (data) {
            testBtn.disabled = false;
            say(data.message || '通过', 'ok');
          })
          .catch(function (err) {
            testBtn.disabled = false;
            say(err.message || '测试失败', 'bad');
          });
      });
    }

    var saveBtn = document.getElementById('aiSave');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var payload = readAiConfig();
        if (!payload.baseUrl) { say('API 地址不能为空', 'bad'); return; }
        if (!payload.model) { say('模型 ID 不能为空', 'bad'); return; }
        say('正在保存…', 'pending');
        saveBtn.disabled = true;
        Judge.post('/config', payload)
          .then(function (data) {
            saveBtn.disabled = false;
            say(data.message || '已保存', 'ok');
            var key = document.getElementById('aiApiKey');
            if (key) key.value = '';          // 清空：密钥已进服务端，页面不留
            Judge.llm = { configured: true, model: data.model, keyMasked: data.keyMasked };
            renderJudgeStatusSwap();
            loadAiConfig();
          })
          .catch(function (err) {
            saveBtn.disabled = false;
            say(err.message || '保存失败', 'bad');
          });
      });
    }
  }

  function readAiConfig() {
    var value = function (id) {
      var element = document.getElementById(id);
      return element ? element.value.trim() : '';
    };
    return { baseUrl: value('aiBaseUrl'), apiKey: value('aiApiKey'), model: value('aiModel') };
  }

  function say(text, tone) {
    var box = document.getElementById('aiConfigMsg');
    if (!box) return;
    box.className = 'ai-config-msg ' + (tone ? 'is-' + tone : '');
    box.textContent = text;
  }

  /** 保存成功后把上游那句"未配置"换掉，不用整页重渲染 */
  function renderJudgeStatusSwap() {
    var state = document.getElementById('aiConfigState');
    var llm = Judge.llm || {};
    if (state && llm.configured) {
      state.className = 'ai-config-state is-on';
      state.textContent = '已配置 ' + llm.model + '（密钥 ' + llm.keyMasked + '）';
    }
  }

  function renderJudgeStatus(online) {
    var box = document.getElementById('judgeStatus');
    if (!box) return;

    if (!online) {
      var wrap = document.getElementById('judgeStatusWrap');
      if (wrap) wrap.innerHTML = judgeHintPanel('判题服务没在运行',
        '在项目里开一个终端，执行下面的命令，保持窗口开着：');
      var reconnect = document.getElementById('judgeReconnect');
      if (reconnect) {
        reconnect.addEventListener('click', function () {
          var field = document.getElementById('judgeUrl');
          if (field && field.value.trim()) Judge.saveUrl(field.value.trim().replace(/\/+$/, ''));
          route();
        });
      }
      return;
    }

    var llm = Judge.llm || {};
    box.className = 'panel';
    box.innerHTML =
      '<h3 class="card-title">判题服务已连接</h3>' +
      '<div class="page-sub">题库 <b>' + Judge.problems + '</b> 道题　·　大模型：' +
      (llm.configured
        ? '已配置 <b>' + h(llm.model) + '</b>（密钥 ' + h(llm.keyMasked) + '）'
        : '<b class="warn-text">未配置</b> —— 写进 <span class="mono">code/.env</span> 后重启服务，AI 讲评才可用') +
      '</div>' +
      '<div class="page-actions" style="margin-top:12px">' +
      '<a class="btn btn-sm btn-ghost" href="' + h(Judge.url) + '/" target="_blank" rel="noopener">打开服务状态页</a>' +
      '</div>';
  }

  function renderJudgeList() {
    var box = document.getElementById('judgeList');
    if (!box || !window.AZTO_ALGO) return;

    var problems = window.AZTO_ALGO.problems;
    var filter = window.__judgeFilter || 'all';

    var rows = problems.filter(function (problem) {
      if (filter === 'no') return !problem.judge;
      if (!problem.judge) return false;
      var status = Judge.statusOf(problem.id);
      if (filter === 'solved') return status === 'solved';
      if (filter === 'attempted') return status === 'attempted';
      if (filter === 'untried') return status === 'untried';
      return true;
    });

    var tabs = [['all', '全部'], ['solved', '已通过'], ['attempted', '尝试过'],
                ['untried', '未开始'], ['no', '暂不可判']];
    var html = ['<div class="tabs">'];
    for (var t = 0; t < tabs.length; t++) {
      html.push('<a class="tab' + (filter === tabs[t][0] ? ' active' : '') +
        '" href="#" data-judgefilter="' + tabs[t][0] + '">' + tabs[t][1] + '</a>');
    }
    html.push('</div>');

    html.push('<div class="problem-grid judge-grid">');
    for (var i = 0; i < rows.length; i++) {
      var problem = rows[i];
      var judge = problem.judge;
      var number = problem.id.split('-')[0];
      var name = problem.title.replace(/^\d+\.\s*/, '');
      var status = judge ? Judge.statusOf(problem.id) : 'nosupport';
      var langs = problem.judge ? Judge.writtenLangs(problem.id) : [];
      var mark = { solved: ' ✓', attempted: ' ·', untried: '', nosupport: '' }[status] || '';

      if (judge) {
        html.push('<a class="problem-chip is-' + status + '" href="#/judge/' +
          encodeURIComponent(problem.id) + '" title="' + h(problem.title + '　' + judge.signature +
            (status === 'solved' ? '　（已通过）' : status === 'attempted' ? '　（尝试过，还没通过）' : '')) + '">' +
          '<span class="problem-no">' + h(number) + '</span>' +
          '<span class="problem-name">' + h(name) + mark + '</span>' +
          '<span class="judge-badge ' + h(judge.level) + '">' + h(judge.level) + '</span></a>');
      } else {
        html.push('<a class="problem-chip is-disabled" href="#/algo/problem/' +
          encodeURIComponent(problem.id) + '" title="这道题暂时没有内置用例，可以看题解后去力扣提交">' +
          '<span class="problem-no">' + h(number) + '</span>' +
          '<span class="problem-name">' + h(name) + '</span>' +
          '<span class="judge-badge">无判题</span></a>');
      }
    }
    html.push('</div>');

    var overall = Judge.overall || { solved: 0, attempted: 0, total: problems.length };
    html.push('<p class="page-sub" style="margin-top:14px">' +
      '当前筛选 ' + rows.length + ' 道。' +
      (filter === 'no' ? '这些题暂时没有内置用例（比如需要特殊数据结构），可以看题解后去力扣提交。' : '') +
      '</p>');

    box.innerHTML = html.join('');

    // 顶部进度：已通过 / 尝试过 / 未开始
    var bar = document.getElementById('judgeProgress');
    if (bar && Judge.overall) {
      var total = Judge.overall.total || 1;
      var solvedPct = Math.round(Judge.overall.solved / total * 100);
      var triedPct = Math.round(Judge.overall.attempted / total * 100);
      bar.innerHTML = '<div class="progress-bar"><div class="progress-fill" style="width:' +
        solvedPct + '%"></div></div>' +
        '<p class="page-sub" style="margin-top:10px">' +
        '已通过 <b>' + Judge.overall.solved + '</b> / ' + Judge.overall.total + ' 道　·　' +
        '尝试过 <b>' + Judge.overall.attempted + '</b> 道　·　' +
        '累计提交 <b>' + Judge.overall.submissions + '</b> 次' +
        (triedPct > solvedPct ? '　（还有 ' + (Judge.overall.attempted - Judge.overall.solved) +
          ' 道卡着，去列表里筛「尝试过」看看）' : '') +
        '</p>';
    }

    var links = box.querySelectorAll('[data-judgefilter]');
    for (var k = 0; k < links.length; k++) {
      links[k].addEventListener('click', function (event) {
        event.preventDefault();
        window.__judgeFilter = event.currentTarget.getAttribute('data-judgefilter');
        renderJudgeList();
      });
    }
  }

  function renderRecords(problem) {
    var box = document.getElementById('judgeHistory');
    if (!box) return;

    var rows = Judge.records || [];
    if (!rows.length) {
      box.innerHTML = '<div class="q-section-title">提交记录</div>' +
        '<p class="page-sub">还没有提交过。点上面的「提交」跑一遍全部用例，记录会出现在这里。</p>';
      return;
    }

    var html = ['<div class="q-section-title">提交记录（' + rows.length + ' 次）</div>'];
    html.push('<div class="history-list">');
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var tone = row.verdict === 'accepted' ? 'ok'
        : row.verdict === 'partial' ? 'warn' : 'bad';
      html.push('<div class="history-row history-' + tone + '">' +
        '<span class="history-status">' + h(row.verdictLabel) + '</span>' +
        '<span class="history-score">' + row.passed + '/' + row.total + '</span>' +
        '<span class="history-lang">' + h(row.languageLabel || '') + '</span>' +
        '<span class="history-meta">' + (row.ms ? Math.round(row.ms) + ' ms' : '') +
        (row.memoryKB ? '　' + (row.memoryKB / 1024).toFixed(1) + ' MB' : '') + '</span>' +
        '<span class="history-time">' + h(row.submitTime || '') + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-restore="' + h(row.id) + '">恢复这段代码</button>' +
        '</div>');
    }
    html.push('</div>');
    box.innerHTML = html.join('');

    var buttons = box.querySelectorAll('[data-restore]');
    for (var k = 0; k < buttons.length; k++) {
      buttons[k].addEventListener('click', function (event) {
        restoreSubmission(event.currentTarget.getAttribute('data-restore'));
      });
    }
  }

  function restoreSubmission(submissionId) {
    fetch(Judge.url + '/submissions/' + encodeURIComponent(submissionId))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var record = data.submission;
        if (!record || !record.code) { toast('这条记录里没有源码'); return; }
        if (!window.confirm('把你现在编辑器里的代码替换成这条记录里的？')) return;

        // 记录可能是别的语言写的 —— 先把语言切过去，否则代码会填进错误的槽位
        var target = record.language || 'python';
        if (target !== Judge.lang) {
          Judge.setLang(target);
          var select = document.getElementById('judgeLang');
          if (select) select.value = target;
        }
        var textarea = document.getElementById('editorInput');
        if (!textarea) return;
        textarea.value = record.code;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        toast('已恢复 ' + record.submitTime + ' 的 ' +
          (record.languageLabel || target) + ' 代码');
      })
      .catch(function () { toast('读取记录失败'); });
  }

  function mountEditor(problem) {
    var textarea = document.getElementById('editorInput');
    var gutter = document.getElementById('editorGutter');
    if (!textarea || !gutter) return;

    fillLanguageSelect(problem);
    Judge.loadRecords(problem.id, function () { renderRecords(problem); });

    textarea.value = Judge.getCode(problem.id, Judge.lang) || Judge.starter(problem, Judge.lang);

    function syncGutter() {
      var count = textarea.value.split('\n').length;
      var out = [];
      for (var i = 1; i <= count; i++) out.push(i);
      gutter.textContent = out.join('\n');
      gutter.scrollTop = textarea.scrollTop;
    }
    function persist() { Judge.saveCode(problem.id, Judge.lang, textarea.value); }

    textarea.addEventListener('input', function () { syncGutter(); persist(); });
    textarea.addEventListener('scroll', function () { gutter.scrollTop = textarea.scrollTop; });

    // Tab 缩进 4 空格、Enter 继承缩进、Ctrl+Enter 提交。
    // 一律走 execCommand('insertText')，这样浏览器的撤销栈仍然有效。
    textarea.addEventListener('keydown', function (event) {
      if (event.key === 'Tab') {
        event.preventDefault();
        document.execCommand('insertText', false, '    ');
        return;
      }
      if (event.key === 'Enter') {
        var value = textarea.value;
        var position = textarea.selectionStart;
        var lineStart = value.lastIndexOf('\n', position - 1) + 1;
        var current = value.slice(lineStart, position);
        var indent = (current.match(/^[ \t]*/) || [''])[0];
        // Python 看冒号，C++ 看左大括号 —— 两种语言都自动缩进
        if (/[:\[{(]\s*$/.test(current)) indent += '    ';
        if (indent) {
          event.preventDefault();
          document.execCommand('insertText', false, '\n' + indent);
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        runJudge(problem, 'submit');
      }
    });

    syncGutter();

    var runBtn = document.getElementById('judgeRun');
    var submitBtn = document.getElementById('judgeSubmit');
    var askBtn = document.getElementById('judgeAsk');
    var resetBtn = document.getElementById('judgeReset');
    if (runBtn) runBtn.addEventListener('click', function () { runJudge(problem, 'run'); });
    if (submitBtn) submitBtn.addEventListener('click', function () { runJudge(problem, 'submit'); });
    if (askBtn) askBtn.addEventListener('click', function () { askAi(problem); });
    if (resetBtn) resetBtn.addEventListener('click', function () {
      if (window.confirm('恢复成初始模板？你写的代码会被覆盖。')) {
        textarea.value = Judge.starter(problem, Judge.lang);
        syncGutter();
        persist();
      }
    });
  }

  function runJudge(problem, mode) {
    if (Judge.busy) return;
    var textarea = document.getElementById('editorInput');
    var box = document.getElementById('judgeResult');
    if (!textarea || !box) return;

    if (!Judge.online) {
      box.innerHTML = '<p class="empty">判题服务没在运行 —— 先在项目里启动它（说明见题目列表页）。</p>';
      return;
    }

    Judge.busy = true;
    box.innerHTML = '<p class="page-sub loading">' +
      (mode === 'run' ? '正在跑第一个用例…' : '正在跑全部用例…') + '</p>';

    Judge.post('/judge', {
      problemId: problem.id, code: textarea.value, timeout: 6, mode: mode,
      language: Judge.lang
    })
      .then(function (report) {
        Judge.busy = false;
        Judge.lastReport = report;
        renderReport(problem, report, mode);
        // 提交之后刷新统计与本题记录 —— 题目列表的绿勾、记录列表都要跟着变
        if (mode !== 'run') {
          Judge.loadRecords(problem.id, function () { renderRecords(problem); });
        }
        Judge.refreshStats(function () { renderJudgeList(); });
      })
      .catch(function (err) {
        Judge.busy = false;
        box.innerHTML = '<div class="panel"><h3 class="card-title">判题失败</h3>' +
          '<p class="page-sub">' + h(err.message || String(err)) + '</p></div>';
      });
  }

  function caseStatusLabel(status) {
    var labels = {
      pass: '通过', fail: '答案错误', error: '运行时错误',
      timeout: '超时', syntax: '语法错误', entry: '找不到函数'
    };
    return labels[status] || status;
  }

  function briefValue(value) {
    var text;
    try { text = JSON.stringify(value); } catch (err) { text = String(value); }
    if (text === undefined) text = String(value);
    return text.length > 220 ? text.slice(0, 220) + '…' : text;
  }

  function renderReport(problem, report, mode) {
    var box = document.getElementById('judgeResult');
    if (!box) return;

    var tone = report.verdict === 'accepted' ? 'ok'
      : report.verdict === 'partial' ? 'warn' : 'bad';

    var html = [];
    html.push('<div class="verdict verdict-' + tone + '">' +
      '<span class="verdict-label">' + h(report.verdictLabel) + '</span>' +
      '<span class="verdict-score">' + report.passed + ' / ' + report.total + ' 个用例通过</span>' +
      '<span class="meta-chip">' + h(report.languageLabel || '') + '</span>' +
      '<span class="verdict-ms">合计 ' + Math.round(report.ms) + ' ms' +
      (report.memoryKB ? '　峰值内存 ' + (report.memoryKB / 1024).toFixed(1) + ' MB' : '') +
      '</span>' +
      (report.submissionId ? '<span class="verdict-id">已记入提交历史 ' + h(report.submissionId) + '</span>' : '') +
      '</div>');

    if (report.verdict === 'compile_error' && report.compileError) {
      html.push('<pre class="case-error compile-error"><code>' +
        h(report.compileError) + '</code></pre>');
      html.push('<p class="page-sub">编译没过，上面的行号已经映射到**你写的代码**' +
        '（生成文件前面还有几百行判题运行时，已经减掉了）。</p>');
      box.innerHTML = html.join('');
      return;
    }

    var results = report.results || [];
    var shown = mode === 'run' ? results.slice(0, 1) : results;

    html.push('<div class="case-list">');
    for (var i = 0; i < shown.length; i++) {
      var item = shown[i];
      var statusTone = item.status === 'pass' ? 'ok' : (item.status === 'fail' ? 'bad' : 'err');
      html.push('<div class="case case-' + statusTone + '">');
      html.push('<div class="case-head">' +
        '<span class="case-no">用例 ' + item.index + '</span>' +
        '<span class="case-status">' + h(caseStatusLabel(item.status)) + '</span>' +
        (item.note ? '<span class="case-note">' + h(item.note) + '</span>' : '') +
        (item.ms ? '<span class="case-ms">' + Math.round(item.ms) + ' ms' +
          (item.memoryKB ? '　' + (item.memoryKB / 1024).toFixed(1) + ' MB' : '') + '</span>' : '') +
        '</div>');
      if (item.status !== 'pass') {
        if (item.error) {
          html.push('<pre class="case-error"><code>' + h(item.error) + '</code></pre>');
        } else {
          html.push('<div class="case-diff">' +
            '<div><span class="diff-key">你的输出</span><code class="diff-bad">' +
            h(briefValue(item.actual)) + '</code></div>' +
            '<div><span class="diff-key">期望</span><code class="diff-ok">' +
            h(briefValue(item.expected)) + '</code></div>' +
            '</div>');
        }
      }
      html.push('</div>');
    }
    html.push('</div>');

    if (mode === 'run' && results.length > 1) {
      html.push('<p class="page-sub">「运行」只跑了第一个用例。要跑全部 ' +
        results.length + ' 个，点「提交」。</p>');
    }
    if (report.verdict === 'accepted') {
      html.push('<p class="page-sub">全部通过。' +
        (problem.judge && problem.judge.leetcode
          ? '可以去 <a href="' + h(problem.judge.leetcode) + '" target="_blank" rel="noopener">力扣</a> ' +
            '用同一份代码再提交一次，看看那边的用时分布和官方用例。'
          : '') + '</p>');
    } else {
      html.push('<p class="page-sub">想知道哪里错了？点上面「AI 讲评」。</p>');
    }

    box.innerHTML = html.join('');
  }

  function askAi(problem) {
    var box = document.getElementById('judgeResult');
    var textarea = document.getElementById('editorInput');
    if (!box || !textarea) return;

    if (!Judge.online) {
      box.innerHTML = '<p class="empty">AI 讲评也由判题服务转发，先把服务启动起来。</p>';
      return;
    }
    if (!Judge.llm || !Judge.llm.configured) {
      box.innerHTML = '<div class="panel"><h3 class="card-title">还没配置大模型</h3>' +
        '<p class="page-sub">到 <a href="#/judge">在线判题首页</a> 的「AI 接口配置」里填上' +
        ' API 地址、API Key 和模型 ID 就能用 —— 填完点「测试连接」确认通了。' +
        '不需要重启服务。</p></div>';
      return;
    }

    var judge = problem.judge || {};
    var report = Judge.lastReport;
    var lines = [];
    lines.push('题目：' + problem.title + (judge.leetcode ? '（' + judge.leetcode + '）' : ''));
    lines.push('要求的入口：' + (judge.signature || ''));
    lines.push('');
    lines.push('我的代码：');
    lines.push('```python');
    lines.push(textarea.value.trim());
    lines.push('```');
    lines.push('');
    if (report) {
      lines.push('判题结果：' + report.verdictLabel + '，' + report.passed + '/' + report.total + ' 个用例通过');
      var failed = (report.results || []).filter(function (r) { return r.status !== 'pass'; }).slice(0, 3);
      for (var i = 0; i < failed.length; i++) {
        lines.push('- 用例' + failed[i].index + (failed[i].note ? '（' + failed[i].note + '）' : '') + '：');
        if (failed[i].error) {
          lines.push('  报错：' + failed[i].error);
        } else {
          lines.push('  我的输出 ' + briefValue(failed[i].actual) + '，期望 ' + briefValue(failed[i].expected));
        }
      }
    } else {
      lines.push('判题结果：（还没跑过）');
    }
    lines.push('');
    lines.push('请按下面三点回答，用中文，直接说重点，不要复述题目：');
    lines.push('1. 我错在哪（思路上就错的话指出是哪一步；只是边界没处理的话指出是哪种边界）');
    lines.push('2. 正确的思路是什么（先给方向，不要直接给完整代码）');
    lines.push('3. 如果我的解法复杂度不达标，说明该用什么方法、为什么');

    box.innerHTML = '<p class="page-sub loading">正在请模型讲评…</p>';

    Judge.post('/chat', {
      messages: [
        { role: 'system', content: '你是一位算法面试教练。学员刚在本地判题器上提交了一道题，' +
          '你要给出精准、可执行的反馈。不要直接给完整代码，先给方向；不要复述题目；不要客套。' },
        { role: 'user', content: lines.join('\n') }
      ],
      temperature: 0.3,
      max_tokens: 1200
    }).then(function (data) {
      box.innerHTML = '<div class="ai-review">' +
        '<div class="ai-review-head">AI 讲评' +
        (data.model ? '<span class="meta-chip">' + h(data.model) + '</span>' : '') + '</div>' +
        '<div class="markdown">' + MD.render(data.content || '（模型没有返回内容）') + '</div>' +
        '</div>' +
        (Judge.lastReport ? '<p class="page-sub"><a href="#" id="judgeBack">← 回到判题结果</a></p>' : '');
      var back = document.getElementById('judgeBack');
      if (back) back.addEventListener('click', function (event) {
        event.preventDefault();
        renderReport(problem, Judge.lastReport, 'submit');
      });
    }).catch(function (err) {
      box.innerHTML = '<div class="panel"><h3 class="card-title">AI 讲评失败</h3>' +
        '<p class="page-sub">' + h(err.message || String(err)) + '</p></div>';
    });
  }

  /** 判题相关视图的挂载钩子，由 wireViewEvents 调用 */
  function mountJudge(parts) {
    if (parts[0] !== 'judge') return false;

    if (parts[1] === 'records') {
      Judge.probe(function (online) {
        if (!online) {
          var box = document.getElementById('recordsBox');
          if (box) {
            box.outerHTML = judgeHintPanel('判题服务没在运行',
              '提交记录存在服务端，要先把它启动起来才能读：');
            var reconnect = document.getElementById('judgeReconnect');
            if (reconnect) reconnect.addEventListener('click', function () {
              var field = document.getElementById('judgeUrl');
              if (field && field.value.trim()) Judge.saveUrl(field.value.trim().replace(/\/+$/, ''));
              route();
            });
          }
          return;
        }
        renderAllRecords();
      });
      var clearBtn = document.getElementById('recordsClear');
      if (clearBtn) clearBtn.addEventListener('click', function () {
        if (!window.confirm('清空全部提交记录？这个操作不可撤销。')) return;
        Judge.post('/submissions/clear', {}).then(function () {
          toast('记录已清空');
          renderAllRecords();
        }).catch(function () { toast('清空失败'); });
      });
      return true;
    }

    if (parts.length < 2) {
      Judge.probe(function (online) {
        renderJudgeStatus(online);
        renderAiConfig();
        renderJudgeList();
        if (online) Judge.refreshStats(function () {
          renderJudgeList();
        });
      });
      return true;
    }
    Judge.probe(function (online) {
      var conn = document.getElementById('judgeConn');
      if (conn) {
        conn.textContent = online ? '判题服务已连接' : '判题服务未启动';
        conn.className = 'judge-conn ' + (online ? 'is-on' : 'is-off');
      }
    });
    if (window.AZTO_ALGO) {
      var id = decodeURIComponent(parts.slice(1).join('/'));
      for (var i = 0; i < window.AZTO_ALGO.problems.length; i++) {
        if (window.AZTO_ALGO.problems[i].id === id) mountEditor(window.AZTO_ALGO.problems[i]);
      }
    }
    return true;
  }

  /* -------------------------------------------------------- 项目实战案例轨道 */

  /**
   * content-project.js 有 800KB，只在真正要看这块内容时才加载。
   * 和 AlgoBundle 一个套路，只是包更大、内容更聚焦。
   */
  var ProjectBundle = {
    loading: false,
    callbacks: [],

    ensure: function (done) {
      if (window.AZTO_PROJECT) return done();
      this.callbacks.push(done);
      if (this.loading) return;
      this.loading = true;

      var self = this;
      var script = document.createElement('script');
      script.src = 'content-project.js';
      script.onload = function () {
        self.loading = false;
        var callbacks = self.callbacks.slice();
        self.callbacks = [];
        for (var i = 0; i < callbacks.length; i++) callbacks[i]();
      };
      script.onerror = function () {
        self.loading = false;
        self.callbacks = [];
        toast('项目实战案例内容没加载成功 —— 先在项目根目录跑一次 python web/_build.py');
      };
      document.head.appendChild(script);
    }
  };

  function projectLoading() {
    return '<div class="view"><div class="page-body">' +
      '<p class="empty loading">正在加载项目实战案例（11 篇文档 + 76 道练习题，约 800 KB）…</p>' +
      '</div></div>';
  }

  /**
   * 练习记录。**故意和章节 Progress 分开存** —— 这里记的是"这道题我能不能讲出来"，
   * 和"这一章我跑通了没"是两件事，混在一起会让两边的百分比都失真。
   */
  var ProjectProgress = {
    KEY: 'azto.project.review.v1',
    data: { items: {} },

    load: function () {
      try {
        var raw = localStorage.getItem(this.KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          if (parsed && parsed.items) this.data = parsed;
        }
      } catch (err) { /* 隐私模式下降级为内存态 */ }
      if (!this.data.items) this.data.items = {};
    },

    save: function () {
      try { localStorage.setItem(this.KEY, JSON.stringify(this.data)); } catch (err) { /* 忽略 */ }
    },

    scoreOf: function (key) {
      var item = this.data.items[key];
      return item ? item.score : null;
    },

    /** score: 0 不会 / 1 半懂 / 2 掌握 */
    mark: function (key, score) {
      this.data.items[key] = { score: score, at: Date.now() };
      this.save();
    },

    setStats: function (set) {
      var queue = projectQueue(set);
      var done = 0, sum = 0;
      for (var i = 0; i < queue.length; i++) {
        var score = this.scoreOf(queue[i].key);
        if (score === null) continue;
        done += 1;
        sum += score;
      }
      return {
        total: queue.length,
        done: done,
        rate: queue.length ? done / queue.length : 0,
        average: done ? sum / (done * 2) : 0
      };
    },

    percent: function () {
      var project = window.AZTO_PROJECT;
      if (!project) return 0;
      var total = 0, done = 0;
      for (var i = 0; i < project.practice.length; i++) {
        var stats = this.setStats(project.practice[i]);
        total += stats.total;
        done += stats.done;
      }
      return total ? Math.round(done / total * 100) : 0;
    },

    /** 所有标成「不会」的题 —— 面试时最可能被问穿的就是这些 */
    weakList: function () {
      var project = window.AZTO_PROJECT;
      if (!project) return [];
      var out = [];
      for (var i = 0; i < project.practice.length; i++) {
        var set = project.practice[i];
        var queue = projectQueue(set);
        for (var j = 0; j < queue.length; j++) {
          if (this.scoreOf(queue[j].key) === 0) {
            out.push({ set: set.title, group: queue[j].group, prompt: queue[j].prompt });
          }
        }
      }
      return out;
    },

    clearSet: function (setId) {
      for (var key in this.data.items) {
        if (key.indexOf(setId + '/') === 0) delete this.data.items[key];
      }
      this.save();
    },

    exportJson: function () {
      downloadJson('project-practice-' + new Date().toISOString().slice(0, 10) + '.json', {
        project: 'Agent Zero To One · 项目实战案例练习记录',
        exportedAt: new Date().toISOString(),
        percent: this.percent(),
        items: this.data.items
      });
    },

    importJson: function (text) {
      var parsed = JSON.parse(text);
      var incoming = parsed && parsed.items ? parsed.items : parsed;
      if (!incoming || typeof incoming !== 'object') throw new Error('格式不对');
      this.data.items = incoming;
      this.save();
    }
  };

  function findProjectSet(id) {
    var project = window.AZTO_PROJECT;
    if (!project) return null;
    for (var i = 0; i < project.practice.length; i++) {
      if (project.practice[i].id === id) return project.practice[i];
    }
    return null;
  }

  function findProjectDoc(id) {
    var project = window.AZTO_PROJECT;
    if (!project) return null;
    for (var i = 0; i < project.documents.length; i++) {
      if (project.documents[i].id === id) return project.documents[i];
    }
    return null;
  }

  /** 把一套题拍平成一维队列，附带分组名（练习器的游标走的就是这个） */
  function projectQueue(set, groupName) {
    var queue = [];
    for (var i = 0; i < set.rounds.length; i++) {
      var group = set.rounds[i];
      if (groupName && group.name !== groupName) continue;
      for (var j = 0; j < group.questions.length; j++) {
        var question = group.questions[j];
        queue.push({
          key: set.id + '/' + group.name + '/' + question.id,
          id: question.id,
          prompt: question.prompt,
          body: question.body,
          group: group.name
        });
      }
    }
    return queue;
  }

  /** 项目实战案例首页：11 篇文档 + 三套练习入口 + 已练进度 */
  views.projectIndex = function (parts) {
    var index = DATA.project;
    if (!index) return notFound('这份材料没有随项目一起构建');
    if (!window.AZTO_PROJECT) {
      ProjectBundle.ensure(function () { route(); });
      return projectLoading();
    }

    var project = window.AZTO_PROJECT;
    var percent = ProjectProgress.percent();
    var html = [];
    html.push(pageHead(project.meta.title, h(project.meta.tagline),
      '<a class="btn btn-ghost btn-sm" href="#/project/readme">模块说明</a>'));

    html.push('<div class="page-body">');
    html.push('<p class="page-sub">' + h(project.meta.subtitle) + '</p>');
    html.push('<div class="progress-bar" style="margin-top:14px"><div class="progress-fill" style="width:' +
      percent + '%"></div></div>');
    html.push('<p class="page-sub" style="margin-top:8px">练习进度 <b>' + percent + '%</b>' +
      '　·　' + project.stats.exercises + ' 道题　·　' + project.stats.lines.toLocaleString() + ' 行文档' +
      '</p>');
    html.push('</div>');

    // 三套练习
    html.push('<div class="page-body"><h2 class="page-title">三套练习</h2>');
    html.push('<p class="page-sub">先自己讲一遍，再展开参考对照。答不上来的会被记进薄弱清单。</p>');
    var cards = [];
    for (var p = 0; p < project.practice.length; p++) {
      var set = project.practice[p];
      var stats = ProjectProgress.setStats(set);
      cards.push('<article class="card">' +
        '<h3 class="card-title">' + h(set.title) + '</h3>' +
        '<p class="card-summary">' + h(set.desc) + '</p>' +
        '<div class="progress-bar" style="margin:12px 0"><div class="progress-fill" style="width:' +
          Math.round(stats.rate * 100) + '%"></div></div>' +
        '<div class="card-foot"><span class="badge">已练 ' + stats.done + '/' + stats.total + '</span>' +
        '<a class="btn btn-sm btn-primary" href="#/project/practice/' + h(set.id) + '">进入</a></div>' +
        '</article>');
    }
    html.push(cardGrid(cards));
    html.push('</div>');

    // 文档
    html.push('<div class="page-body"><h2 class="page-title">复盘文档</h2>');
    html.push('<p class="page-sub">按顺序读，或者卡在哪块直接跳过去</p>');
    var docCards = project.documents.map(function (doc) {
      return '<a class="card" href="#/project/doc/' + encodeURIComponent(doc.id) + '">' +
        '<h3 class="card-title">' + h(doc.title) + '</h3>' +
        '<p class="card-summary">' + h(doc.summary) + '</p>' +
        '<div class="card-foot"><span class="badge">' + doc.lines + ' 行</span>' +
        '<span class="card-size">' + h(doc.file) + '</span></div></a>';
    });
    html.push(cardGrid(docCards));
    html.push('</div>');

    return html.join('');
  };

  /** 模块说明（07-项目实战案例/README.md）——它讲清了这块内容和模拟面试的分工 */
  views.projectReadme = function () {
    if (!window.AZTO_PROJECT) {
      ProjectBundle.ensure(function () { route(); });
      return projectLoading();
    }
    var project = window.AZTO_PROJECT;
    if (!project.readme) return notFound('模块说明没随内容一起构建');

    var doc = {
      title: '模块说明',
      summary: '这块内容和模拟面试的分工、三套练习怎么用',
      file: '07-项目实战案例/README.md',
      lines: project.readme.split('\n').length,
      markdown: project.readme
    };
    return '<div class="view">' + projectReaderShell(doc, [], 0) + '</div>';
  };

  views.projectDoc = function (parts) {
    if (!window.AZTO_PROJECT) {
      ProjectBundle.ensure(function () { route(); });
      return projectLoading();
    }

    var id = decodeURIComponent(parts.slice(2).join('/'));
    var doc = findProjectDoc(id);
    if (!doc) return notFound('找不到这份文档：' + h(id));

    var index = window.AZTO_PROJECT.documents.indexOf(doc);
    return '<div class="view">' + projectReaderShell(doc, window.AZTO_PROJECT.documents, index) + '</div>';
  };

  /**
   * 把项目实战案例文档里的 [[wikilink]] 变成真链接。
   *
   * 全局的 wikilink 一律渲染成不可点的 span（Obsidian 里的引用，网页上没有对应页面）。
   * 但项目实战案例里的引用基本都在本模块内部，既指得到、也应该点得动。
   * 只处理下面两类：指向本模块目录的，和少数几个已知的模块外目标。
   * 其它文档的 wikilink 行为完全不变。
   */
  function projectLinkify(html) {
    var PREFIX = '07-项目实战案例/';
    // 模块外的目标：这些在网页上有对应页面，值得连通
    var OUTSIDE = {
      '06-模拟面试/README': '#/mock',
      '06-模拟面试': '#/mock',
      '学习中枢': '#/hub'
    };
    var WIKI = /<span class="wikilink" title="([^"]+)">([^<]*)<\/span>/g;

    return html.replace(WIKI, function (whole, target, label) {
      if (Object.prototype.hasOwnProperty.call(OUTSIDE, target)) {
        return '<a class="wikilink" href="' + OUTSIDE[target] + '">' + label + '</a>';
      }
      if (target.indexOf(PREFIX) !== 0) return whole;
      var rest = target.slice(PREFIX.length);
      var href = rest === 'README'
        ? '#/project/readme'
        : '#/project/doc/' + encodeURIComponent(rest);
      return '<a class="wikilink" href="' + href + '">' + label + '</a>';
    });
  }

  /** 阅读器外壳。和章节阅读器同构，但上一篇/下一篇走的是本模块自己的顺序。 */
  function projectReaderShell(doc, siblings, index) {
    var html = [];
    html.push('<div class="page-body">');
    html.push('<article class="reader">');
    html.push('<div class="reader-head">');
    html.push('<div class="reader-crumb"><a href="#/project">项目实战案例</a> / 复盘文档</div>');
    html.push('<h1 class="reader-title">' + h(doc.title) + '</h1>');
    html.push('<p class="page-sub">' + h(doc.summary) + '</p>');
    html.push('<div class="reader-meta"><span class="meta-chip">' + doc.lines + ' 行</span>' +
      '<span class="meta-chip mono">' + h(doc.file) + '</span></div>');
    html.push('</div>');

    var toc = MD.extractToc(doc.markdown);
    html.push('<div class="reader-body">');
    if (toc.length) {
      html.push('<aside class="reader-toc">');
      html.push('<div class="toc-title">本页目录</div>');
      for (var t = 0; t < toc.length; t++) {
        html.push('<a class="toc-item lv' + toc[t].level + '" data-anchor="' + h(toc[t].id) + '" href="#' +
          h(toc[t].id) + '">' + h(toc[t].text) + '</a>');
      }
      html.push('</aside>');
    }
    html.push('<div class="markdown">' + projectLinkify(MD.render(doc.markdown)) + '</div>');
    html.push('</div>');

    if (siblings && siblings.length) {
      var prev = index > 0 ? siblings[index - 1] : null;
      var next = index >= 0 && index < siblings.length - 1 ? siblings[index + 1] : null;
      html.push('<nav class="reader-nav">');
      html.push(prev
        ? '<a class="reader-prev" href="#/project/doc/' + encodeURIComponent(prev.id) + '">' +
          '<span class="rn-dir">← 上一篇</span><span class="rn-title">' + h(prev.title) + '</span></a>'
        : '<span class="reader-prev"></span>');
      html.push(next
        ? '<a class="reader-next" href="#/project/doc/' + encodeURIComponent(next.id) + '">' +
          '<span class="rn-dir">下一篇 →</span><span class="rn-title">' + h(next.title) + '</span></a>'
        : '<span class="reader-next"></span>');
      html.push('</nav>');
    }
    html.push('</article>');
    html.push('</div>');
    return html.join('');
  }

  /** 一套练习的入口页：进度 + 按分组练 + 题目清单（带状态标记） */
  views.projectPractice = function (parts) {
    if (!window.AZTO_PROJECT) {
      ProjectBundle.ensure(function () { route(); });
      return projectLoading();
    }
    var set = findProjectSet(decodeURIComponent(parts[2] || ''));
    if (!set) return notFound('找不到这套练习');

    var stats = ProjectProgress.setStats(set);
    var html = [];
    html.push(pageHead(set.title, h(set.desc),
      '<button class="btn btn-ghost btn-sm" id="projectSetReset" data-set="' + h(set.id) + '">清空这套记录</button>'));

    html.push('<div class="page-body">');
    html.push('<div class="progress-bar"><div class="progress-fill" style="width:' +
      Math.round(stats.rate * 100) + '%"></div></div>');
    html.push('<p class="page-sub" style="margin-top:10px">已练 <b>' + stats.done + '</b> / ' + stats.total +
      ' 题　·　平均掌握度 <b>' + Math.round(stats.average * 100) + '%</b></p>');
    html.push('<div class="page-actions" style="margin-top:16px;margin-bottom:22px">' +
      '<a class="btn btn-primary" href="#/project/run/' + h(set.id) + '/all">' +
      (stats.done ? '继续练习（全部题目）' : '开始练习（全部题目）') + '</a>' +
      (stats.done ? '<a class="btn btn-ghost" href="#/project/run/' + h(set.id) + '/weak">只练没答上的</a>' : '') +
      '</div>');
    html.push('</div>');

    html.push('<div class="page-body"><h2 class="page-title">按分组练</h2>');
    var cards = [];
    for (var i = 0; i < set.rounds.length; i++) {
      var group = set.rounds[i];
      var queue = projectQueue(set, group.name);
      var done = 0;
      for (var j = 0; j < queue.length; j++) {
        if (ProjectProgress.scoreOf(queue[j].key) !== null) done += 1;
      }
      cards.push('<a class="card" href="#/project/run/' + h(set.id) + '/' + encodeURIComponent(group.name) + '">' +
        '<h3 class="card-title">' + h(group.name) + '</h3>' +
        '<p class="card-summary">' + queue.length + ' 题</p>' +
        '<div class="card-foot"><span class="badge">已练 ' + done + '/' + queue.length + '</span></div></a>');
    }
    html.push(cardGrid(cards));
    html.push('</div>');

    // 题目清单：文案给完整原文，交给 .problem-chip 的 CSS 省略号收尾
    // （按字数切会把问句从中间砍断，读起来像坏掉的数据）
    html.push('<div class="page-body"><h2 class="page-title">题目清单</h2>');
    for (var g = 0; g < set.rounds.length; g++) {
      var round = set.rounds[g];
      html.push('<div class="q-section-title" style="margin-top:14px">' + h(round.name) + '</div>');
      html.push('<div class="problem-grid">');
      for (var k = 0; k < round.questions.length; k++) {
        var question = round.questions[k];
        var score = ProjectProgress.scoreOf(set.id + '/' + round.name + '/' + question.id);
        var cls = score === 2 ? 'is-solved' : (score === 1 ? 'is-attempted' : (score === 0 ? 'is-weak' : ''));
        var mark = score === 2 ? ' ✓' : (score === 1 ? ' ·' : (score === 0 ? ' ✗' : ''));
        html.push('<a class="problem-chip ' + cls + '" href="#/project/run/' + h(set.id) + '/' +
          encodeURIComponent(round.name) + '?q=' + encodeURIComponent(question.id) + '" title="' +
          h(question.prompt) + '">' +
          '<span class="problem-no">' + h(question.id) + '</span>' +
          '<span class="problem-name">' + ri(question.prompt) + mark + '</span></a>');
      }
      html.push('</div>');
    }
    html.push('</div>');

    // 上一套 / 下一套
    var all = window.AZTO_PROJECT.practice;
    var here = all.indexOf(set);
    html.push('<div class="page-body"><div class="page-actions">');
    if (here > 0) {
      html.push('<a class="btn btn-ghost btn-sm" href="#/project/practice/' + h(all[here - 1].id) + '">← ' +
        h(all[here - 1].title) + '</a>');
    }
    if (here >= 0 && here < all.length - 1) {
      html.push('<a class="btn btn-ghost btn-sm" href="#/project/practice/' + h(all[here + 1].id) + '">' +
        h(all[here + 1].title) + ' →</a>');
    }
    html.push('</div></div>');

    return html.join('');
  };

  /** 练习运行器。和 MockRunner 同构：先答 → 展开 → 自评 → 报告。 */
  var ProjectRunner = {
    set: null,
    queue: [],
    index: 0,
    filter: 'all',
    revealed: {},

    start: function (setId, filter, jumpTo) {
      var set = findProjectSet(setId);
      if (!set) return false;

      var queue;
      if (filter === 'all') {
        queue = projectQueue(set);
      } else if (filter === 'weak') {
        queue = projectQueue(set).filter(function (item) {
          return ProjectProgress.scoreOf(item.key) === 0;
        });
      } else {
        queue = projectQueue(set, decodeURIComponent(filter));
      }
      if (!queue.length) {
        queue = projectQueue(set);   // 筛选后为空（比如还没标过「不会」）就退回全部
        filter = 'all';
      }

      this.set = set;
      this.queue = queue;
      this.filter = filter;
      this.index = 0;
      this.revealed = {};

      if (jumpTo) {
        for (var i = 0; i < queue.length; i++) {
          if (queue[i].id === jumpTo) { this.index = i; break; }
        }
      }
      return true;
    },

    current: function () { return this.queue[this.index]; },

    render: function () {
      var stage = document.getElementById('projectStage');
      if (!this.set || !stage) return;
      if (this.index >= this.queue.length) return this.renderReport();

      var item = this.current();
      var score = ProjectProgress.scoreOf(item.key);
      var revealed = !!this.revealed[item.key];

      var count = document.getElementById('projectCount');
      if (count) count.textContent = '第 ' + (this.index + 1) + ' / ' + this.queue.length + ' 题';
      var bar = document.getElementById('projectBar');
      if (bar) bar.style.width = Math.round(this.index / this.queue.length * 100) + '%';

      var html = [];
      html.push('<article class="q-card">');
      html.push('<div class="q-meta">' +
        '<span class="meta-chip">' + h(item.id) + '</span>' +
        '<span class="meta-chip">' + h(item.group) + '</span>' +
        (score !== null ? '<span class="meta-chip">上次自评 ' + h(RATE_LABEL[score]) + '</span>' : '') +
        '</div>');
      html.push('<h2 class="q-prompt">' + ri(item.prompt) + '</h2>');
      html.push('<p class="q-hint">' +
        (this.set.mode === 'dialogue'
          ? '先自己开口答一遍（真的说出来，或在纸上写要点），再展开面试者的回答与点评对照。'
          : '先自己答一遍，再展开参考对照。') + '</p>');

      if (!revealed) {
        html.push('<div class="q-actions"><button class="btn btn-primary" id="projectReveal">' +
          '我答完了，看参考</button></div>');
      } else {
        html.push('<div class="q-reveal"><div class="markdown">' + MD.render(item.body) + '</div></div>');
        html.push('<div class="rate-row"><span class="rate-label">自评这一题：</span>');
        for (var i = 0; i < RATE_OPTIONS.length; i++) {
          var rate = RATE_OPTIONS[i];
          html.push('<button class="rate-btn rate-' + rate[0] + (score === rate[0] ? ' active' : '') +
            '" data-score="' + rate[0] + '">' +
            '<strong>' + rate[1] + '</strong><small>' + rate[2] + '</small></button>');
        }
        html.push('</div>');
      }

      html.push('<div class="runner-nav">');
      html.push(this.index > 0
        ? '<button class="btn btn-ghost btn-sm" id="projectPrev">← 上一题</button>'
        : '<span></span>');
      html.push('<div class="runner-nav-right">' +
        '<button class="btn btn-ghost btn-sm" id="projectSkip">跳过</button>' +
        '<button class="btn btn-primary btn-sm" id="projectNext">' +
        (this.index === this.queue.length - 1 ? '提交并看报告' : '下一题 →') + '</button>' +
        '</div>');
      html.push('</div>');
      html.push('</article>');

      stage.innerHTML = html.join('');
      this.wire();
    },

    renderReport: function () {
      var stage = document.getElementById('projectStage');
      if (!stage) return;

      var queue = this.queue;
      var counts = { 0: 0, 1: 0, 2: 0 };
      var weak = [];
      for (var i = 0; i < queue.length; i++) {
        var score = ProjectProgress.scoreOf(queue[i].key);
        if (score === null) score = 0;
        counts[score] += 1;
        if (score !== 2) {
          var entry = { id: queue[i].id, prompt: queue[i].prompt, group: queue[i].group, score: score };
          // 完全不会的排前面：那才是真正卡住的题
          if (score === 0) weak.unshift(entry); else weak.push(entry);
        }
      }
      var total = queue.length;
      var percent = total ? Math.round((counts[2] * 2 + counts[1]) / (total * 2) * 100) : 0;

      var html = [];
      html.push('<article class="report">');
      html.push('<h2 class="report-title">这套练完了</h2>');
      html.push('<div class="report-score">' +
        '<span class="report-num">' + percent + '<small>%</small></span>' +
        '<span class="report-rate">掌握度（掌握=2 分，半懂=1 分）</span></div>');

      html.push('<div class="q-section"><div class="q-section-title">分布</div>');
      var rows = [['掌握', counts[2]], ['半懂', counts[1]], ['不会', counts[0]]];
      for (var r = 0; r < rows.length; r++) {
        var pct = total ? Math.round(rows[r][1] / total * 100) : 0;
        html.push('<div class="dim-row"><span class="dim-name">' + rows[r][0] + '</span>' +
          '<span class="dim-bar"><i style="width:' + pct + '%"></i></span>' +
          '<span class="dim-value">' + rows[r][1] + '</span></div>');
      }
      html.push('</div>');

      if (weak.length) {
        html.push('<div class="q-section minus"><div class="q-section-title">' +
          '要回去补的 ' + weak.length + ' 题（不会排在最前）</div><ul class="q-list">');
        for (var w = 0; w < weak.length && w < 24; w++) {
          html.push('<li><span class="mono">' + h(RATE_LABEL[weak[w].score]) + '</span>　' +
            h(weak[w].id) + '　' + ri(weak[w].prompt) +
            '　<span class="mono">' + h(weak[w].group) + '</span></li>');
        }
        if (weak.length > 24) html.push('<li>… 还有 ' + (weak.length - 24) + ' 题</li>');
        html.push('</ul></div>');
      } else {
        html.push('<div class="q-section ok"><div class="q-section-title">全场都是「掌握」</div>' +
          '<p>可以换一套练，或者去读还没读过的复盘文档。</p></div>');
      }

      html.push('<div class="q-section"><div class="q-section-title">下一步</div><ul class="q-list">' +
        '<li>「不会」的题：回到对应的深挖文档读一遍，然后<b>合上材料自己讲一遍</b></li>' +
        '<li>「半懂」的题：问题不在知识，在表达 —— 练「结论 → 依据（带行号）→ 边界」三句话</li>' +
        '<li>同一套题隔一天再打一次，<b>对比变化比单次分数有意义</b></li>' +
        '<li>答不上来的题会一直待在 <a href="#/project/practice/' + h(this.set.id) + '">这套练习</a> 的薄弱清单里</li>' +
        '</ul></div>');

      html.push('<div class="runner-nav">' +
        '<a class="btn btn-ghost" href="#/project/practice/' + h(this.set.id) + '">回到练习首页</a>' +
        '<div class="runner-nav-right">' +
        '<a class="btn btn-primary" href="#/project/run/' + h(this.set.id) + '/weak">只练没答上的</a>' +
        '</div></div>');
      html.push('</article>');

      stage.innerHTML = html.join('');
      var bar = document.getElementById('projectBar');
      if (bar) bar.style.width = '100%';
      var count = document.getElementById('projectCount');
      if (count) count.textContent = '已完成 ' + total + ' 题';
    },

    wire: function () {
      var self = this;
      var bind = function (id, handler) {
        var element = document.getElementById(id);
        if (element) element.addEventListener('click', handler);
      };

      bind('projectReveal', function () {
        self.revealed[self.current().key] = true;
        self.render();
      });

      var rateButtons = document.querySelectorAll('.rate-btn');
      for (var i = 0; i < rateButtons.length; i++) {
        rateButtons[i].addEventListener('click', function (event) {
          ProjectProgress.mark(self.current().key, parseInt(event.currentTarget.getAttribute('data-score'), 10));
          self.render();
        });
      }

      bind('projectPrev', function () {
        if (self.index > 0) { self.index -= 1; self.render(); }
      });

      bind('projectSkip', function () {
        // 跳过记为「不会」，不是不记录 —— 跳过本身就是信息：一道题你不想展开、
        // 不想自评，说明你没准备好面对它，面试官不会给你跳过的机会。
        // 这也让报告的分布和薄弱清单一致（否则报告把没自评的算成不会，
        // 薄弱清单里却找不到它们）。
        if (self.index < self.queue.length) {
          var item = self.current();
          if (ProjectProgress.scoreOf(item.key) === null) ProjectProgress.mark(item.key, 0);
          self.index += 1;
          self.render();
        }
      });

      bind('projectNext', function () {
        // 没自评就按「不会」计 —— 跳过评分会让报告失真
        var item = self.current();
        if (ProjectProgress.scoreOf(item.key) === null) ProjectProgress.mark(item.key, 0);
        self.index += 1;
        self.render();
      });
    }
  };

  var RATE_OPTIONS = [
    [0, '不会', '答不上来 / 完全没准备'],
    [1, '半懂', '能说一部分，但经不起追问'],
    [2, '掌握', '能说清机制 + 能答追问']
  ];
  var RATE_LABEL = { 0: '不会', 1: '半懂', 2: '掌握' };

  views.projectRun = function (parts) {
    if (!window.AZTO_PROJECT) {
      ProjectBundle.ensure(function () { route(); });
      return projectLoading();
    }

    var setId = decodeURIComponent(parts[2] || '');
    var filter = parts[3] ? decodeURIComponent(parts[3]) : 'all';
    var jump = null;
    if (location.hash.indexOf('?q=') > 0) jump = decodeURIComponent(location.hash.split('?q=')[1]);

    if (!ProjectRunner.start(setId, filter, jump)) return notFound('找不到这套练习');

    var set = ProjectRunner.set;
    var html = [];
    html.push('<div class="view runner">');
    html.push('<div class="runner-head">');
    html.push('<div class="runner-progress">' +
      '<span class="runner-session">' + h(set.title) + '</span>' +
      '<span class="runner-count" id="projectCount"></span>' +
      '<span class="runner-timer">' +
      h(filter === 'all' ? '全部题目' : (filter === 'weak' ? '只练没答上的' : decodeURIComponent(filter))) +
      '</span></div>');
    html.push('<a class="btn btn-sm btn-ghost" href="#/project/practice/' + h(set.id) + '">退出</a>');
    html.push('<div class="progress-bar" style="flex:1 1 100%"><div class="progress-fill" id="projectBar"></div></div>');
    html.push('</div>');
    html.push('<div id="projectStage"></div>');
    html.push('</div>');
    return html.join('');
  };

  var ProjectPracticeView = {
    /** 运行器渲染完之后调用（wireViewEvents 里接） */
    mount: function () {
      if (document.getElementById('projectStage')) ProjectRunner.render();
    }
  };

  /* ------------------------------------------------------------------ 搜索 */

  var Search = {
    index: null,
    indexedProject: false,

    ensure: function () {
      // 项目实战案例在懒加载包里。第一次搜索时它可能还没到，等用户看过那块内容
      // （包已加载）再重建一次索引，否则那 11 篇文档搜不到。
      var wantProject = !!window.AZTO_PROJECT;
      if (this.index && this.indexedProject === wantProject) return this.index;
      this.index = null;
      this.indexedProject = wantProject;

      var items = [];
      var i, j;

      for (i = 0; i < DATA.chapters.length; i++) {
        items.push({
          kind: '第 ' + DATA.chapters[i].no + ' 章',
          title: DATA.chapters[i].title,
          route: '#/chapter/' + DATA.chapters[i].no,
          text: DATA.chapters[i].markdown
        });
      }
      var kinds = [
        ['topics', '专题', DATA.wiki.topics],
        ['cheatsheets', '速查表', DATA.wiki.cheatsheets],
        ['interview', '面试题', DATA.wiki.interview]
      ];
      for (i = 0; i < kinds.length; i++) {
        for (j = 0; j < kinds[i][2].length; j++) {
          items.push({
            kind: kinds[i][1],
            title: kinds[i][2][j].title,
            route: '#/wiki/' + kinds[i][0] + '/' + encodeURIComponent(kinds[i][2][j].id),
            text: kinds[i][2][j].markdown
          });
        }
      }
      for (i = 0; i < DATA.notes.length; i++) {
        items.push({
          kind: 'W' + DATA.notes[i].week,
          title: DATA.notes[i].title,
          route: '#/note/' + DATA.notes[i].week,
          text: DATA.notes[i].markdown
        });
      }
      for (i = 0; i < DATA.raw.length; i++) {
        items.push({ kind: '规划', title: DATA.raw[i].title, route: '#/raw/' + encodeURIComponent(DATA.raw[i].id), text: DATA.raw[i].markdown });
      }
      for (i = 0; i < DATA.config.length; i++) {
        items.push({ kind: '配置', title: DATA.config[i].title, route: '#/config/' + encodeURIComponent(DATA.config[i].id), text: DATA.config[i].markdown });
      }
      for (i = 0; i < DATA.ladder.length; i++) {
        items.push({
          kind: DATA.ladder[i].id,
          title: DATA.ladder[i].name,
          route: '#/ladder',
          text: DATA.ladder[i].one + ' ' + DATA.ladder[i].skills.join(' ') + ' ' + DATA.ladder[i].criteria.join(' ')
        });
      }

      // 模拟面试题库：把题干、参考思路、扣分点都进索引 —— 搜"扣分"能直接搜到题
      for (i = 0; i < DATA.mock.rounds.length; i++) {
        var round = DATA.mock.rounds[i];
        for (j = 0; j < round.questions.length; j++) {
          var question = round.questions[j];
          items.push({
            kind: '面试题 ' + question.id,
            title: question.title,
            route: '#/mock',
            text: [round.name, question.prompt, question.probe,
              question.outline.join(' '), question.plus.join(' '),
              question.minus.join(' '), question.followups.join(' ')].join('\n')
          });
        }
      }

      // 面试配套文档
      for (i = 0; i < DATA.mock.documents.length; i++) {
        items.push({
          kind: '面试文档',
          title: DATA.mock.documents[i].title,
          route: '#/mock/docs/' + encodeURIComponent(DATA.mock.documents[i].id),
          text: DATA.mock.documents[i].markdown
        });
      }

      // 项目实战案例（11 篇复盘文档 + 76 道题）—— 包加载了才进索引
      if (window.AZTO_PROJECT) {
        var project = window.AZTO_PROJECT;
        for (i = 0; i < project.documents.length; i++) {
          items.push({
            kind: '项目复盘',
            title: project.documents[i].title,
            route: '#/project/doc/' + encodeURIComponent(project.documents[i].id),
            text: project.documents[i].markdown
          });
        }
        for (i = 0; i < project.practice.length; i++) {
          var pset = project.practice[i];
          for (j = 0; j < pset.rounds.length; j++) {
            var pgroup = pset.rounds[j];
            for (var k = 0; k < pgroup.questions.length; k++) {
              var pquestion = pgroup.questions[k];
              items.push({
                kind: pset.title + ' · ' + pquestion.id,
                title: pquestion.prompt,
                route: '#/project/run/' + pset.id + '/' + encodeURIComponent(pgroup.name) +
                  '?q=' + encodeURIComponent(pquestion.id),
                text: pquestion.prompt + '\n' + pquestion.body
              });
            }
          }
        }
      }

      // 预先小写，避免每次按键都转换 1.4MB 文本
      for (i = 0; i < items.length; i++) {
        items[i].lower = (items[i].title + '\n' + items[i].text).toLowerCase();
      }
      this.index = items;
      return items;
    },

    query: function (raw) {
      var terms = raw.toLowerCase().split(/\s+/).filter(function (t) { return t.length > 0; });
      if (!terms.length) return [];

      var items = this.ensure();
      var hits = [];
      for (var i = 0; i < items.length && hits.length < 30; i++) {
        var haystack = items[i].lower;
        var ok = true;
        for (var t = 0; t < terms.length; t++) {
          if (haystack.indexOf(terms[t]) === -1) { ok = false; break; }
        }
        if (!ok) continue;

        hits.push({
          kind: items[i].kind,
          title: items[i].title,
          route: items[i].route,
          context: this.snippet(items[i].text, terms[0]),
          score: this.score(items[i], terms)
        });
      }
      hits.sort(function (a, b) { return b.score - a.score; });
      return hits.slice(0, 12);
    },

    score: function (item, terms) {
      var score = 0;
      var lowerTitle = item.title.toLowerCase();
      for (var i = 0; i < terms.length; i++) {
        if (lowerTitle.indexOf(terms[i]) !== -1) score += 10;
        var occurrences = item.lower.split(terms[i]).length - 1;
        score += Math.min(occurrences, 40);
      }
      return score;
    },

    snippet: function (text, term) {
      var position = text.toLowerCase().indexOf(term);
      if (position < 0) {
        return text.replace(/\s+/g, ' ').slice(0, 90);
      }
      var start = Math.max(0, position - 40);
      var end = Math.min(text.length, position + 70);
      var fragment = text.slice(start, end).replace(/\s+/g, ' ');
      // 把命中的词高亮出来（先转义再插入 mark，避免 XSS）
      var escaped = h(fragment);
      try {
        var pattern = new RegExp('(' + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
        escaped = escaped.replace(pattern, '<mark>$1</mark>');
      } catch (err) { /* 正则异常就不高亮 */ }
      return (start > 0 ? '…' : '') + escaped + (end < text.length ? '…' : '');
    }
  };

  function renderSearchResults(hits, raw) {
    var box = $('searchResults');
    if (!hits.length) {
      box.innerHTML = '<div class="search-hit"><span class="hit-title">没有匹配「' + h(raw) + '」的内容</span></div>';
    } else {
      var html = [];
      for (var i = 0; i < hits.length; i++) {
        html.push('<a class="search-hit" href="' + h(hits[i].route) + '">' +
          '<span class="hit-kind">' + h(hits[i].kind) + '</span>' +
          '<span class="hit-title">' + h(hits[i].title) + '</span>' +
          '<span class="hit-context">' + hits[i].context + '</span></a>');
      }
      box.innerHTML = html.join('');
    }
    box.classList.add('open', 'show');
  }

  function hideSearch() {
    var box = $('searchResults');
    if (box) box.classList.remove('open', 'show');
  }

  /* ------------------------------------------------------------------ 路由 */

  function parseHash() {
    var hash = location.hash || '#/';
    return hash.replace(/^#\/?/, '').split('/').filter(function (p) { return p.length > 0; });
  }

  /**
   * 路由分派。route 段 → 视图函数。
   * 有些段是"列表或详情"两种形态（wiki / config / raw），靠后面的段数区分。
   */
  function resolveView(parts) {
    if (!parts.length) return { view: views.home, key: '#/' };
    var head = parts[0];
    var view = null;

    if (head === 'wiki') {
      view = parts.length >= 3 ? views.wikiItem : views.wikiList;
    } else if (head === 'config') {
      view = parts.length >= 2 ? views.configItem : views.config;
    } else if (head === 'raw') {
      view = parts.length >= 2 ? views.rawItem : views.raw;
    } else if (head === 'algo') {
      view = parts.length >= 3 ? views.algoItem : views.algoIndex;
    } else if (head === 'project') {
      if (parts[1] === 'doc') view = views.projectDoc;
      else if (parts[1] === 'readme') view = views.projectReadme;
      else if (parts[1] === 'practice') view = views.projectPractice;
      else if (parts[1] === 'run') view = views.projectRun;
      else view = views.projectIndex;
    } else if (head === 'judge') {
      if (parts[1] === 'records') view = views.judgeRecords;
      else view = parts.length >= 2 ? views.judgeProblem : views.judgeIndex;
    } else if (head === 'mock') {
      if (parts[1] === 'docs') view = parts.length >= 3 ? views.mockDoc : views.mockDocs;
      else if (parts[1] === 'run') view = views.mockRun;
      else view = views.mockHome;
    } else if (typeof views[head] === 'function') {
      view = views[head];
    }

    if (!view) return null;
    return { view: view, key: '#/' + parts.join('/') };
  }

  function route() {
    var parts = parseHash();
    var resolved = resolveView(parts);
    if (!resolved) {
      return render(notFound('没有这个页面：' + h(parts.join('/'))), '#/' + parts.join('/'));
    }
    render(resolved.view(parts), resolved.key);
  }

  function render(html, routeKey) {
    $('main').innerHTML = html;
    window.scrollTo(0, 0);
    markActiveNav(routeKey);
    Progress.refresh();
    wireViewEvents();
    closeSidebar();
  }

  /** 每个视图渲染完之后，绑定它内部的事件 */
  function wireViewEvents() {
    // ---- 模拟面试：首页（场次选择、自由练习筛选、历史记录操作）----
    var startButtons = document.querySelectorAll('[data-start]');
    for (var s = 0; s < startButtons.length; s++) {
      startButtons[s].addEventListener('click', function (event) {
        var sessionId = event.currentTarget.getAttribute('data-start');
        if (sessionId === 'free') {
          // 「自由练习」不是直接开始，而是展开筛选面板
          var panel = $('freePanel');
          if (panel) panel.hidden = false;
          return;
        }
        MockRunner.start(sessionId);
      });
    }

    var freeStart = $('freeStart');
    if (freeStart) {
      freeStart.addEventListener('click', function () {
        MockRunner.start('free', {
          round: selectValue('freeRound'),
          role: selectValue('freeRole'),
          level: selectValue('freeLevel'),
          count: parseInt(selectValue('freeCount'), 10) || 20
        });
      });
    }

    var mockExport = $('mockExport');
    if (mockExport) mockExport.addEventListener('click', function () {
      downloadJson('azto-mock-history-' + new Date().toISOString().slice(0, 10) + '.json',
        { project: 'Agent Zero To One', type: 'mock-interview-history', sessions: MockStore.data.sessions });
      toast('面试记录已导出');
    });

    var mockClear = $('mockClear');
    if (mockClear) mockClear.addEventListener('click', function () {
      if (window.confirm('确定清空全部模拟面试记录吗？')) {
        MockStore.clear();
        route();
        toast('记录已清空');
      }
    });

    // ---- 模拟面试：运行器 ----
    if ($('mockStage') && MockRunner.run) {
      MockRunner.mount();
    }

    // ---- 项目实战案例：练习运行器 + 清空这套记录 ----
    ProjectPracticeView.mount();
    var projectReset = $('projectSetReset');
    if (projectReset) {
      projectReset.addEventListener('click', function (event) {
        var setId = event.currentTarget.getAttribute('data-set');
        if (!window.confirm('清空这套练习的记录？')) return;
        ProjectProgress.clearSet(setId);
        route();
        toast('已清空');
      });
    }

    // ---- 在线判题（探测服务 + 挂编辑器）----
    mountJudge(parseHash());

    // ---- 章节三勾 ----
    var boxes = document.querySelectorAll('.check input[data-no]');
    for (var i = 0; i < boxes.length; i++) {
      boxes[i].addEventListener('change', function (event) {
        Progress.toggleChapter(event.target.getAttribute('data-no'),
          event.target.getAttribute('data-key'), event.target.checked);
      });
    }

    // 周笔记完成
    var noteBoxes = document.querySelectorAll('input[data-note]');
    for (var n = 0; n < noteBoxes.length; n++) {
      noteBoxes[n].addEventListener('change', function (event) {
        Progress.toggleNote(event.target.getAttribute('data-note'));
      });
    }

    // 项目完成
    var projectBoxes = document.querySelectorAll('input[data-project]');
    for (var p = 0; p < projectBoxes.length; p++) {
      projectBoxes[p].addEventListener('change', function (event) {
        Progress.toggleProject(event.target.getAttribute('data-project'));
        toast(event.target.checked ? '项目已标记完成' : '已取消标记');
      });
    }

    // 目录按钮（移动端）
    var tocBtn = $('tocBtn');
    if (tocBtn) {
      tocBtn.addEventListener('click', function () {
        var toc = $('readerToc');
        if (toc) toc.classList.toggle('open');
      });
    }

    // 目录锚点平滑滚动
    var tocLinks = document.querySelectorAll('.toc-item');
    for (var t = 0; t < tocLinks.length; t++) {
      tocLinks[t].addEventListener('click', function (event) {
        var id = event.target.getAttribute('data-anchor');
        var target = document.getElementById(id);
        if (target) {
          event.preventDefault();
          var top = target.getBoundingClientRect().top + window.pageYOffset - 80;
          window.scrollTo({ top: top, behavior: 'smooth' });
          var toc = $('readerToc');
          if (toc) toc.classList.remove('open');
        }
      });
    }

    // 进度页的按钮
    var exportBtn = $('exportBtn');
    if (exportBtn) exportBtn.addEventListener('click', function () {
      Progress.exportJson();
      toast('已导出进度 JSON');
    });

    var importBtn = $('importBtn');
    if (importBtn) importBtn.addEventListener('click', function () {
      var input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,application/json';
      input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () {
          try {
            Progress.importJson(String(reader.result));
            route();
            toast('进度已导入');
          } catch (err) {
            toast('导入失败：文件格式不对');
          }
        };
        reader.readAsText(file, 'utf-8');
      });
      input.click();
    });

    var resetBtn = $('resetBtn');
    if (resetBtn) resetBtn.addEventListener('click', function () {
      if (window.confirm('确定要清空全部学习进度吗？这个操作不可撤销。')) {
        Progress.reset();
        route();
        toast('进度已重置');
      }
    });

    var collapseAll = $('collapseAll');
    if (collapseAll) collapseAll.addEventListener('click', function () {
      location.hash = '#/';
    });

    startScrollSpy();
  }

  /* 目录高亮（滚动时同步） */
  var spyHandler = null;

  function startScrollSpy() {
    if (spyHandler) window.removeEventListener('scroll', spyHandler);
    var links = document.querySelectorAll('.toc-item');
    if (!links.length) { spyHandler = null; return; }

    var targets = [];
    for (var i = 0; i < links.length; i++) {
      var element = document.getElementById(links[i].getAttribute('data-anchor'));
      if (element) targets.push({ el: element, link: links[i] });
    }
    if (!targets.length) { spyHandler = null; return; }

    var ticking = false;
    spyHandler = function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        var current = null;
        for (var j = 0; j < targets.length; j++) {
          if (targets[j].el.getBoundingClientRect().top <= 120) current = targets[j];
        }
        for (var k = 0; k < links.length; k++) links[k].classList.remove('active');
        if (current) current.link.classList.add('active');
        ticking = false;
      });
    };
    window.addEventListener('scroll', spyHandler, { passive: true });
    spyHandler();
  }

  /* -------------------------------------------------------------- 交互杂项 */

  function openSidebar() {
    $('sidebar').classList.add('open');
    $('scrim').classList.add('open', 'show');
    document.body.classList.add('no-scroll');
  }

  function closeSidebar() {
    var sidebar = $('sidebar');
    if (!sidebar) return;
    sidebar.classList.remove('open');
    $('scrim').classList.remove('open', 'show');
    document.body.classList.remove('no-scroll');
  }

  var toastTimer = null;
  function toast(message) {
    var el = $('toast');
    el.textContent = message;
    el.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  /* -------------------------------------------------------------- 启动 */

  function init() {
    Progress.load();
    MockStore.load();
    ProjectProgress.load();
    Theme.init();
    renderNav();
    route();

    // 移除启动画面
    var boot = $('boot');
    if (boot) boot.parentNode.removeChild(boot);

    // 顶栏交互
    $('menuBtn').addEventListener('click', function () {
      if ($('sidebar').classList.contains('open')) closeSidebar();
      else openSidebar();
    });
    $('scrim').addEventListener('click', closeSidebar);
    $('themeBtn').addEventListener('click', function () { Theme.toggle(); });

    // 搜索
    var input = $('searchInput');
    var timer = null;

    input.addEventListener('input', function () {
      var raw = input.value.trim();
      // 清除键的显隐由 CSS 的 .has-value / .show 控制（不是 hidden 属性）
      $('search').classList.toggle('has-value', !!raw);
      $('searchClear').classList.toggle('show', !!raw);
      if (timer) clearTimeout(timer);
      if (!raw) { hideSearch(); return; }
      // 防抖：1.4MB 内容上做全文匹配，等用户停手再算
      timer = setTimeout(function () {
        renderSearchResults(Search.query(raw), raw);
      }, 160);
    });

    input.addEventListener('focus', function () {
      if (input.value.trim()) renderSearchResults(Search.query(input.value.trim()), input.value.trim());
    });

    $('searchClear').addEventListener('click', function () {
      input.value = '';
      $('search').classList.remove('has-value');
      $('searchClear').classList.remove('show');
      hideSearch();
      input.focus();
    });

    // 点搜索结果后收起面板
    $('searchResults').addEventListener('click', function () {
      hideSearch();
      input.blur();
    });

    // 点空白处收起搜索
    document.addEventListener('click', function (event) {
      var search = $('search');
      if (search && !search.contains(event.target)) hideSearch();
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { hideSearch(); closeSidebar(); }
      // "/" 快速聚焦搜索
      if (event.key === '/' && document.activeElement !== input &&
          !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
        event.preventDefault();
        input.focus();
      }
    });

    // 路由变化
    window.addEventListener('hashchange', route);

    // 窗口尺寸变化时收拾一下抽屉
    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (window.innerWidth > 1100) closeSidebar();
      }, 150);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
