/* ============================================================================
 * web/_test_md.js
 * markdown.js 的自检脚本（Node 直接跑，不需要任何依赖）：
 *
 *     cd D:/AI-learn/Agent-Zero-To-One
 *     node web/_test_md.js
 *
 * 它用项目里的真实 Markdown 文件做验证，逐条覆盖九个自检项：
 *   1  六个文件渲染成功、无异常（也没走 fallback 兜底）
 *   2  内容没丢：表格行 <-> <tr>，围栏数/2 <-> <pre>
 *   3  <details> / <summary> 数量与源文件一致
 *   4  表格渲染出 <table> 且带表头 <th>
 *   5  嵌套列表（引用/折叠块内的列表 + 缩进子列表补充用例）
 *   6  任务列表 class="task" 的 <li>
 *   7  代码块语言类名 class="lang-xxx"
 *   8  extractToc 的每个 id 都能在 render 输出里找到 id="..."
 *   9  没有可疑的裸 <（白名单标签之外全部转义）
 *
 * 最后附一段“补充用例”：语料里没有的边角语法（缩进嵌套列表、反斜杠转义、
 * 未闭合围栏、三种对齐、XSS 载荷）在这里单独验证。
 * ==========================================================================*/
'use strict';

var fs = require('fs');
var path = require('path');

var ROOT = path.dirname(__dirname);
var md = require(path.join(__dirname, 'assets', 'markdown.js'));

/* ---------------------------------------------------------------------------
 * 目标文件：任务书里点名的六个真实文件（相对项目根目录）
 * -------------------------------------------------------------------------*/
var FILES = [
  'docs/第05章 ReAct最小Agent-Loop.md',
  'docs/第08章 RAG全链路.md',
  'docs/第12章 评估可观测性与安全.md',
  '02-Wiki/专题总结/01-Agent是什么.md',
  '02-Wiki/面试题库/01-Agent高频面试题.md',
  '01-Raw/02-12周速通学习计划.md'
];

/* ---------------------------------------------------------------------------
 * 小工具
 * -------------------------------------------------------------------------*/
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

function countMatches(text, re) {
  var m = text.match(re);
  return m ? m.length : 0;
}

function countOf(text, needle) {
  var n = 0;
  var i = 0;
  while (true) {
    i = text.indexOf(needle, i);
    if (i < 0) { return n; }
    n++;
    i += needle.length;
  }
}

/** 去掉围栏代码块里的内容（统计表格行时不能把代码块里的 | 算进来） */
function stripFences(src) {
  var out = [];
  var lines = src.replace(/\r\n?/g, '\n').split('\n');
  var inFence = false;
  for (var i = 0; i < lines.length; i++) {
    if (/^\s*```/.test(lines[i])) { inFence = !inFence; continue; }
    out.push(inFence ? '' : lines[i]);
  }
  return out.join('\n');
}

function isTableRow(line) { return /^\s*\|/.test(line); }
function isDelimiterRow(line) {
  var t = line.trim();
  return t.indexOf('|') >= 0 && /^[|:\-\s]+$/.test(t) && t.indexOf('-') >= 0;
}

/* ---------------------------------------------------------------------------
 * 断言收集
 * -------------------------------------------------------------------------*/
var checks = [];
function check(name, ok, evidence) {
  checks.push({ name: name, ok: !!ok, evidence: evidence });
  return !!ok;
}

/* ---------------------------------------------------------------------------
 * 0. 对外接口形状
 * -------------------------------------------------------------------------*/
(function apiShape() {
  var ok = typeof md.render === 'function' &&
           typeof md.renderInline === 'function' &&
           typeof md.extractToc === 'function';
  check('接口形状 render / renderInline / extractToc 都是函数', ok,
    'render=' + typeof md.render + ' renderInline=' + typeof md.renderInline +
    ' extractToc=' + typeof md.extractToc);
})();

/* ---------------------------------------------------------------------------
 * 逐文件渲染
 * -------------------------------------------------------------------------*/
var rendered = {};   // rel -> { src, html, toc, error }

FILES.forEach(function (rel) {
  var src = read(rel);
  var entry = { src: src, html: '', toc: [], error: null, ms: 0 };
  var t0 = Date.now();
  try {
    entry.html = md.render(src);
    entry.toc = md.extractToc(src);
  } catch (err) {
    entry.error = err && err.message ? err.message : String(err);
  }
  entry.ms = Date.now() - t0;
  rendered[rel] = entry;
  console.log(rel + '  in=' + src.length + ' out=' + entry.html.length +
              ' toc=' + entry.toc.length + '  ' + entry.ms + 'ms');
});

/* ---------------------------------------------------------------------------
 * 自检 1：六个文件都渲染成功、无异常、没走 fallback
 * -------------------------------------------------------------------------*/
(function selfCheck1() {
  var failed = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    if (e.error) { failed.push(rel + '(' + e.error + ')'); return; }
    if (!e.html || e.html.length < 200) { failed.push(rel + '(输出过短)'); return; }
    if (e.html.indexOf('md-fallback') >= 0) { failed.push(rel + '(走了容错兜底)'); }
  });
  check('1. 六个文件渲染成功、无异常、无兜底', failed.length === 0,
    failed.length ? '失败：' + failed.join(', ') :
      '全部成功：' + FILES.map(function (f) { return rendered[f].html.length; }).join(' / ') + ' 字符');
})();

/* ---------------------------------------------------------------------------
 * 自检 2：内容没丢（表格行 vs <tr>，围栏数/2 vs <pre>）
 * -------------------------------------------------------------------------*/
(function selfCheck2() {
  var rows = [];
  var okAll = true;
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var plain = stripFences(e.src);

    // 表格行：以 | 开头的行，去掉分隔行
    var srcTableRows = 0;
    plain.split('\n').forEach(function (l) {
      if (isTableRow(l) && !isDelimiterRow(l)) { srcTableRows++; }
    });
    var srcPipes = countOf(plain, '|');
    var tr = countOf(e.html, '<tr>');
    var cells = countOf(e.html, '<td>') + countOf(e.html, '<td ') +
                countOf(e.html, '<th>') + countOf(e.html, '<th ');

    // 围栏：源文件里以 ``` 开头的行数 / 2 == <pre> 数
    var srcFences = countMatches(e.src, /^\s*`{3,}/gm);
    var pre = countOf(e.html, '<pre>') + countOf(e.html, '<pre ');

    var rowOk = (srcTableRows === tr);
    var fenceOk = (Math.floor(srcFences / 2) === pre);
    var cellRatio = srcPipes ? (cells / srcPipes) : 0;
    var cellOk = srcPipes === 0 || (cellRatio > 0.6 && cellRatio < 2.5);
    if (!rowOk || !fenceOk || !cellOk) { okAll = false; }

    rows.push({
      file: rel,
      tableRows: srcTableRows, tr: tr, pipes: srcPipes, cells: cells,
      ratio: cellRatio.toFixed(2),
      fences: srcFences, pre: pre,
      ok: rowOk && fenceOk && cellOk
    });
  });

  console.log('\n[自检2] 内容没丢');
  rows.forEach(function (r) {
    console.log('  ' + (r.ok ? 'OK  ' : 'FAIL') + ' ' + r.file +
      '  表格行=' + r.tableRows + ' <tr>=' + r.tr +
      '  |数=' + r.pipes + ' 单元格=' + r.cells + '(比 ' + r.ratio + ')' +
      '  围栏=' + r.fences + ' 围栏/2=' + Math.floor(r.fences / 2) + ' <pre>=' + r.pre);
  });
  check('2. 表格行/围栏数与输出量级一致（表格行==<tr>，围栏/2==<pre>）', okAll,
    rows.map(function (r) { return path.basename(r.file) + ':' + (r.ok ? 'OK' : 'FAIL'); }).join(', '));
})();

/* ---------------------------------------------------------------------------
 * 自检 3：<details> / <summary> 与源文件数量一致
 * -------------------------------------------------------------------------*/
(function selfCheck3() {
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var srcDetails = countMatches(e.src, /<details>/g);
    var srcSummary = countMatches(e.src, /<summary>/g);
    var outDetails = countOf(e.html, '<details>');
    var outSummary = countOf(e.html, '<summary>') + countOf(e.html, '<summary ');
    var outClose = countOf(e.html, '</details>');
  var ok = (srcDetails === outDetails && srcSummary === outSummary && srcDetails === outClose);
  if (!ok) { okAll = false; }
  // 折叠块结构本身不能坏：</details> 不能被包进 <p>
  var wrapped = countMatches(e.html, /<p>\s*<\/(details|summary|ul|ol|li|pre|table)>/g);
  if (wrapped) { okAll = false; }
  if (srcDetails) {
    detail.push(path.basename(rel) + ' 源=' + srcDetails + ' 输出=' + outDetails +
                ' summary=' + outSummary + ' 闭合=' + outClose +
                ' 被包进p=' + wrapped + (ok && !wrapped ? ' OK' : ' FAIL'));
  }
  });
  console.log('\n[自检3] details 折叠块');
  detail.forEach(function (d) { console.log('  ' + d); });
  var ch5 = countOf(rendered[FILES[0]].html, '<details>');
  var ch12 = countOf(rendered[FILES[2]].html, '<details>');
  check('3. <details>/<summary> 数量与源一致（第05章、第12章各 5 个）', okAll && ch5 === 5 && ch12 === 5,
    '第05章=' + ch5 + ' 第12章=' + ch12 + ' | ' + detail.join(' ; '));
})();

/* ---------------------------------------------------------------------------
 * 自检 4：表格渲染出 <table> 且带表头 <th>
 * -------------------------------------------------------------------------*/
(function selfCheck4() {
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var fakeWrap = countOf(e.html, '<div class="table-wrap"><table>');
    var tables = countOf(e.html, '<table>');
    var th = countOf(e.html, '<th>') + countOf(e.html, '<th ');
    var thead = countOf(e.html, '<thead>');
    var tbody = countOf(e.html, '<tbody>');
    var ok = tables > 0 && th > 0 && fakeWrap === tables && thead === tables && tbody === tables;
    if (!ok) { okAll = false; }
    detail.push(path.basename(rel) + ' table=' + tables + ' wrap=' + fakeWrap +
                ' thead=' + thead + ' tbody=' + tbody + ' th=' + th + (ok ? ' OK' : ' FAIL'));
  });
  console.log('\n[自检4] 表格');
  detail.forEach(function (d) { console.log('  ' + d); });
  check('4. 有 <div class="table-wrap"><table>，且表头 <th> 齐全', okAll, detail.join(' ; '));
})();

/* ---------------------------------------------------------------------------
 * 自检 5：嵌套列表
 * 语料实况：67 个 md 文件里没有任何“缩进子列表”（列表标记全部在缩进 0），
 * 所以真实文件里的嵌套体现为“引用块 / 折叠块内部的列表”。
 * 缩进层级本身用下面的补充用例验证。
 * -------------------------------------------------------------------------*/
(function selfCheck5() {
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    // 引用块里嵌套的列表
    var quoteList = countMatches(e.html, /<blockquote>[\s\S]*?<(ul|ol)>[\s\S]*?<\/blockquote>/g);
    // 折叠块里嵌套的列表
    var detailList = countMatches(e.html, /<details>[\s\S]*?<(ul|ol)>[\s\S]*?<\/details>/g);
    // 列表项里嵌套块级内容（第04章的 <li> 里嵌了 <pre>）
    var liBlock = countMatches(e.html, /<li>[\s\S]*?<(pre|ul|ol|div|blockquote)>/g);
    if (quoteList + detailList + liBlock === 0) { okAll = false; }
    detail.push(path.basename(rel) + ' 引用内列表=' + quoteList + ' 折叠内列表=' + detailList +
                ' 列表项内块=' + liBlock);
  });
  console.log('\n[自检5] 嵌套列表（真实文件）');
  detail.forEach(function (d) { console.log('  ' + d); });
  var totalNested = detail.reduce(function (a, d) {
    var m = d.match(/=(\d+)/g) || [];
    return a + m.reduce(function (x, y) { return x + parseInt(y.substring(1), 10); }, 0);
  }, 0);
  check('5. 输出里存在嵌套列表结构（引用/折叠块内 + 列表项内），共 ' + totalNested + ' 处',
    okAll && totalNested > 0, detail.join(' ; '));
})();

/* ---------------------------------------------------------------------------
 * 自检 6：任务列表
 * -------------------------------------------------------------------------*/
(function selfCheck6() {
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var plain = stripFences(e.src);
    var srcTasks = countMatches(plain, /^\s*[-*+] \[[ xX]\]/gm);
    var srcChecked = countMatches(plain, /^\s*[-*+] \[[xX]\]/gm);
    var outTasks = countOf(e.html, '<li class="task">');
    var outChecked = countMatches(e.html, /<li class="task"><input type="checkbox" disabled checked>/g);
    var ok = (srcTasks === outTasks) && (srcChecked === outChecked);
    if (srcTasks || outTasks) {
      if (!ok) { okAll = false; }
      detail.push(path.basename(rel) + ' 源=' + srcTasks + '(勾选' + srcChecked + ') 输出=' +
                  outTasks + '(勾选' + outChecked + ')' + (ok ? ' OK' : ' FAIL'));
    }
  });
  console.log('\n[自检6] 任务列表');
  detail.forEach(function (d) { console.log('  ' + d); });
  var ch3 = countOf(read('docs/第03章 Prompt工程与结构化输出.md').length ? md.render(read('docs/第03章 Prompt工程与结构化输出.md')) : '', '<li class="task">');
  check('6. class="task" 的 <li> 数量与源文件 - [ ] 一致', okAll && ch3 > 0,
    detail.join(' ; ') + ' | 第03章(额外验证)=' + ch3);
})();

/* ---------------------------------------------------------------------------
 * 自检 7：代码块语言类名
 * -------------------------------------------------------------------------*/
(function selfCheck7() {
  var okAll = true;
  var detail = [];
  var langsSeen = {};
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var srcLangs = {};
    (e.src.match(/^\s*`{3,}([A-Za-z][\w+#.-]*)/gm) || []).forEach(function (m) {
      var lang = m.replace(/^\s*`{3,}/, '');
      srcLangs[lang] = (srcLangs[lang] || 0) + 1;
    });
    var outLangs = {};
    (e.html.match(/class="lang-([^"]+)"/g) || []).forEach(function (m) {
      var lang = m.replace('class="lang-', '').replace('"', '');
      outLangs[lang] = (outLangs[lang] || 0) + 1;
      langsSeen[lang] = (langsSeen[lang] || 0) + 1;
    });
    var ok = true;
    Object.keys(srcLangs).forEach(function (l) {
      if ((outLangs[l] || 0) !== srcLangs[l]) { ok = false; }
    });
    if (!ok) { okAll = false; }
    detail.push(path.basename(rel) + ' 源=' + JSON.stringify(srcLangs) + ' 输出=' + JSON.stringify(outLangs) + (ok ? ' OK' : ' FAIL'));
  });
  console.log('\n[自检7] 代码块语言类名');
  detail.forEach(function (d) { console.log('  ' + d); });
  check('7. class="lang-xxx" 与源文件围栏语言一一对应', okAll && Object.keys(langsSeen).length >= 3,
    '出现的语言：' + Object.keys(langsSeen).sort().join(', ') + ' | ' + detail.join(' ; '));
})();

/* ---------------------------------------------------------------------------
 * 自检 8：标题 id 与 TOC 一致
 * -------------------------------------------------------------------------*/
(function selfCheck8() {
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var missing = [];
    e.toc.forEach(function (item) {
      if (item.level !== 2 && item.level !== 3) { missing.push('level异常:' + item.id); }
      if (e.html.indexOf('id="' + item.id + '"') < 0) { missing.push(item.id); }
      if (!item.text) { missing.push('空标题:' + item.id); }
    });
    var outIds = countMatches(e.html, /id="sec-\d+"/g);
    var ok = missing.length === 0 && e.toc.length > 0 && outIds >= e.toc.length;
    if (!ok) { okAll = false; }
    detail.push(path.basename(rel) + ' toc=' + e.toc.length + ' 输出id=' + outIds +
                (missing.length ? ' 缺失=' + missing.slice(0, 5).join(',') : '') + (ok ? ' OK' : ' FAIL'));
  });
  console.log('\n[自检8] 标题 id 与 TOC 一致');
  detail.forEach(function (d) { console.log('  ' + d); });
  var sample = rendered[FILES[0]].toc.slice(0, 3).map(function (t) {
    return t.level + ':' + t.id + ':' + t.text;
  });
  check('8. extractToc 的每个 id 都能在 render 输出里找到 id="..."', okAll,
    detail.join(' ; ') + ' | 样例 ' + JSON.stringify(sample));
})();

/* ---------------------------------------------------------------------------
 * 自检 9：没有可疑的裸 <
 * 做法：把渲染器自己会产生的标签 + 白名单标签全部剔除，剩下的 < 就是漏转义。
 * -------------------------------------------------------------------------*/
(function selfCheck9() {
  var GENERATED = 'h1|h2|h3|h4|h5|h6|p|ul|ol|li|pre|code|table|thead|tbody|tr|th|td|' +
                  'div|span|a|img|input|blockquote|hr|del|strong|em|details|summary|br|sub|sup|kbd|mark';
  var okAll = true;
  var detail = [];
  FILES.forEach(function (rel) {
    var e = rendered[rel];
    var probe = e.html
      .replace(new RegExp('</?(?:' + GENERATED + ')(?:\\s[^<>]*)?/?>', 'g'), '')
      .replace(/&lt;/g, '')
      .replace(/&gt;/g, '');
    var bad = probe.match(/<[^>]{0,60}/g) || [];
    if (bad.length) { okAll = false; }
    detail.push(path.basename(rel) + ' 可疑裸<=' + bad.length +
                (bad.length ? ' 例:' + bad.slice(0, 3).join(' ') : '') + (bad.length ? ' FAIL' : ' OK'));
  });
  console.log('\n[自检9] 裸尖括号');
  detail.forEach(function (d) { console.log('  ' + d); });

  // 语料里真实存在的“危险文本”必须被转义
  var ch3 = rendered[FILES[0]] ? null : null;
  var ch3html = md.render(read('docs/第03章 Prompt工程与结构化输出.md'));
  var escapedEmail = ch3html.indexOf('&lt;email&gt;') >= 0 && ch3html.indexOf('<email>') < 0;
  var ch12html = rendered[FILES[2]].html;
  var escapedLt = ch12html.indexOf('&lt; 75% / &gt;') >= 0;
  var logHtml = md.render(read('00-配置/学习日志.md'));
  var escapedComment = logHtml.indexOf('&lt;!--') >= 0 && logHtml.indexOf('<!--') < 0;
  var noScript = countMatches(md.render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>'),
    /<script|<img\b(?![^>]*src="x")|onerror=/g) === 0;

  check('9. 白名单之外没有未转义的裸 <（含 <email>/< 75% / HTML 注释/XSS 载荷）',
    okAll && escapedEmail && escapedLt && escapedComment && noScript,
    detail.join(' ; ') + ' | &lt;email&gt;=' + escapedEmail + ' &lt; 75% / &gt;=' + escapedLt +
    ' 注释转义=' + escapedComment + ' XSS 载荷被转义=' + noScript);
})();

/* ===========================================================================
 * 补充用例：语料里没有的边角语法，单独钉死
 * ========================================================================*/
(function fixtures() {
  console.log('\n[补充用例] 语料未覆盖的语法');

  // (1) 缩进嵌套列表
  var nest = md.render('- 一级 A\n  - 二级 A1\n    - 三级 A11\n  - 二级 A2\n- 一级 B\n');
  var nestOk = /<ul>[\s\S]*<ul>[\s\S]*<ul>[\s\S]*<\/ul>[\s\S]*<\/ul>[\s\S]*<\/ul>/.test(nest);
  console.log('  嵌套列表深度3\n' + nest.split('\n').map(function (l) { return '    ' + l; }).join('\n'));
  check('补充: 缩进嵌套列表（3 层）', nestOk, nestOk ? '三层 <ul> 正确嵌套' : '嵌套结构不对：' + nest);

  // (2) 有序 + 无序混合嵌套
  var mix = md.render('1. 步骤一\n   - 子步骤\n2. 步骤二\n');
  var mixOk = /<ol>[\s\S]*<li>步骤一[\s\S]*<ul>[\s\S]*<li>子步骤<\/li>/.test(mix) &&
              countOf(mix, '<ol>') === 1 && countOf(mix, '<ul>') === 1;
  check('补充: 有序/无序混合嵌套', mixOk, mixOk ? 'ol 内嵌 ul，共 1 ol + 1 ul' : mix);

  // (3) 反斜杠转义
  var esc = md.renderInline('\\*不是斜体\\* \\_不是下划线\\_ \\`不是代码\\` \\\\ 反斜杠');
  var escOk = esc.indexOf('<em>') < 0 && esc.indexOf('<code>') < 0 &&
              esc.indexOf('*不是斜体*') >= 0 && esc.indexOf('_不是下划线_') >= 0 &&
              esc.indexOf('`不是代码`') >= 0;
  check('补充: \\* \\_ \\` \\\\ 输出字面量', escOk, esc);

  // (4) 未闭合围栏不崩、内容仍在
  var unclosed = md.render('段落\n\n```python\nprint(1)\nprint(2)\n');
  var unclosedOk = unclosed.indexOf('<pre><code class="lang-python">') >= 0 &&
                   unclosed.indexOf('print(2)') >= 0;
  check('补充: 未闭合围栏容错', unclosedOk, unclosedOk ? '未闭合围栏照常输出代码块' : unclosed);

  // (5) 三种对齐
  var align = md.render('| 左 | 中 | 右 |\n|:---|:---:|---:|\n| a | b | c |\n');
  var alignOk = align.indexOf('style="text-align:left"') >= 0 &&
                align.indexOf('style="text-align:center"') >= 0 &&
                align.indexOf('style="text-align:right"') >= 0;
  check('补充: :--- / :---: / ---: 对齐', alignOk, alignOk ? '左中右三种对齐都输出' : align);

  // (6) 单元格内的行内标记
  var cell = md.render('| 列 | 说明 |\n|---|---|\n| **粗体** | `代码` |\n');
  var cellOk = cell.indexOf('<td><strong>粗体</strong></td>') >= 0 &&
               cell.indexOf('<td><code>代码</code></td>') >= 0;
  check('补充: 表格单元格里的 **粗体** / `代码`', cellOk, cellOk ? '单元格行内标记已渲染' : cell);

  // (7) 表格前后缺空行
  var tight = md.render('**标题**\n| a | b |\n|---|---|\n| 1 | 2 |\n正文接着写\n');
  var tightOk = countOf(tight, '<table>') === 1 && countOf(tight, '<tr>') === 2 &&
                tight.indexOf('<strong>标题</strong>') >= 0;
  check('补充: 表格前后缺空行也能识别', tightOk, tightOk ? '无空行表格正常渲染' : tight);

  // (8) 嵌套引用 >> 与引用内列表
  var quote = md.render('> 外层\n>> 内层\n\n> 1. 一\n> 2. 二\n');
  var quoteOk = countOf(quote, '<blockquote>') === 2 && countOf(quote, '<ol>') === 1;
  check('补充: 嵌套引用 >> 与引用内有序列表', quoteOk, quoteOk ? '两层 blockquote + 内部 ol' : quote);

  // (9) 引用内标题降级为粗体
  var qh = md.render('> ## 引用里的标题\n');
  var qhOk = qh.indexOf('<h2') < 0 && qh.indexOf('<strong>引用里的标题</strong>') >= 0;
  check('补充: 引用块内标题降级为粗体段落', qhOk, qhOk ? '没有生成 h2，改为粗体段落' : qh);

  // (10) 5/6 级标题能渲染且不分配 id
  var h56 = md.render('##### 五级\n\n###### 六级\n');
  var h56Ok = h56.indexOf('<h5>五级</h5>') >= 0 && h56.indexOf('<h6>六级</h6>') >= 0 &&
              h56.indexOf('id="sec-') < 0;
  check('补充: 5/6 级标题渲染但不加 id', h56Ok, h56Ok ? 'h5/h6 正常且无 id' : h56);

  // (11) 围栏内容原样保留（内部标记不渲染）
  var fenced = md.render('```text\n**不该加粗** <b>不该当标签</b>\n```\n');
  var fencedOk = fenced.indexOf('&lt;b&gt;') >= 0 && fenced.indexOf('<strong>') < 0 &&
                 fenced.indexOf('**不该加粗**') >= 0;
  check('补充: 围栏代码内容原样保留', fencedOk, fencedOk ? '内部标记未被渲染、尖括号已转义' : fenced);

  // (12) 行内元素全家桶
  var inline = md.renderInline('**粗** *斜* _斜2_ ~~删~~ `码` [文](u) ![图](i.jpg) [[路径|标签]]');
  var inlineOk = inline.indexOf('<strong>粗</strong>') >= 0 &&
                 inline.indexOf('<em>斜</em>') >= 0 &&
                 inline.indexOf('<em>斜2</em>') >= 0 &&
                 inline.indexOf('<del>删</del>') >= 0 &&
                 inline.indexOf('<code>码</code>') >= 0 &&
                 inline.indexOf('>文</a>') >= 0 &&
                 inline.indexOf('<img src="i.jpg" alt="图"') >= 0 &&
                 inline.indexOf('<span class="wikilink" title="路径">标签</span>') >= 0;
  check('补充: 行内元素全家桶', inlineOk, inlineOk ? '粗/斜/删/码/链接/图片/wikilink 全部正确' : inline);

  // (13) 跨行内代码的强调
  var cross = md.renderInline('**加粗里有 `代码` 还有字**');
  var crossOk = cross.indexOf('<strong>加粗里有 <code>代码</code> 还有字</strong>') >= 0;
  check('补充: 强调跨行内代码仍能配对', crossOk, crossOk ? 'strong 正确包住 code' : cross);

  // (14) 任务列表渲染
  var task = md.render('- [ ] 未完成\n- [x] 已完成\n');
  var taskOk = task.indexOf('<li class="task"><input type="checkbox" disabled> 未完成</li>') >= 0 &&
               task.indexOf('<li class="task"><input type="checkbox" disabled checked> 已完成</li>') >= 0;
  check('补充: 任务列表勾选框', taskOk, taskOk ? '未完成/已完成两种输出都正确' : task);

  // (15) XSS 载荷
  var xss = md.render('普通 <img src=x onerror=alert(1)> 文本\n\n<iframe src="evil"></iframe>\n\n<a href="javascript:alert(1)">点</a>\n');
  var xssOk = xss.indexOf('onerror=alert(1)>') < 0 || xss.indexOf('&lt;img src=x') >= 0;
  xssOk = xssOk && xss.indexOf('<iframe') < 0 && countOf(xss, '&lt;iframe') === 1;
  check('补充: XSS 载荷（img/iframe）被转义', xssOk, xssOk ? '非白名单标签全部转义' : xss);

  // (16) 一行内多个 wikilink + 中文链接地址
  var links = md.renderInline('[[A/B]]、[[C/D|显示文字]]、[中文](第01章 认识AI-Agent.md)');
  var linksOk = links.indexOf('title="A/B">A/B</span>') >= 0 &&
                links.indexOf('title="C/D">显示文字</span>') >= 0 &&
                links.indexOf('href="第01章 认识AI-Agent.md"') >= 0;
  check('补充: wikilink（含 | 标签）与中文链接', linksOk, linksOk ? '三种链接都正确' : links);
})();

/* ===========================================================================
 * 全站回归：把项目里所有 md 都渲染一遍，确认没有异常 / 兜底 / 丢内容
 * ========================================================================*/
(function fullRegression() {
  function walk(dir, acc) {
    fs.readdirSync(dir, { withFileTypes: true }).forEach(function (entry) {
      if (entry.name === '.git' || entry.name === '__pycache__') { return; }
      // launcher/ 下的打包产物会把整个项目复制一份，排掉它，
      // 否则这里的"全站 md 回归"会把每个文档测两遍。
      if (entry.isDirectory() && /^(build|dist|release)$/.test(entry.name)) { return; }
      var p = path.join(dir, entry.name);
      if (entry.isDirectory()) { walk(p, acc); }
      else if (/\.md$/.test(entry.name)) { acc.push(p); }
    });
    return acc;
  }
  var files = walk(ROOT, []);

  var CJK = /[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]/g;
  // 与渲染器保持一致：剥掉开头的 YAML frontmatter，作为内容守恒比对的基准。
  // 不用正则，避免在生成/转义环节出岔子。
  function stripFrontmatter(text) {
    var s = String(text == null ? '' : text);
    if (s.indexOf('---') !== 0) return s;
    var end = s.indexOf('\n---', 3);
    if (end < 0) return s;
    var after = s.indexOf('\n', end + 1);
    return after < 0 ? '' : s.slice(after + 1);
  }

  function hist(s) {
    var h = {}, m = s.match(CJK) || [];
    m.forEach(function (c) { h[c] = (h[c] || 0) + 1; });
    return h;
  }
  // 把 HTML 还原成“所有可读文字”：标签只丢语法本身，属性值保留（正文可能只出现在
  // href / title / alt 里，例如 wikilink 的 title 就是它的路径）
  function readable(html) {
    return html
      .replace(/<[^>]*>/g, function (tag) {
        var vals = tag.match(/"[^"]*"/g) || [];
        return ' ' + vals.map(function (v) { return v.slice(1, -1); }).join(' ') + ' ';
      })
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  }

  var flagged = [];
  var totalMs = 0;
  var gainTotal = 0;
  files.forEach(function (file) {
    var rel = path.relative(ROOT, file).replace(/\\/g, '/');
    var src = fs.readFileSync(file, 'utf8');
    var html, toc;
    var t0 = Date.now();
    try {
      html = md.render(src);
      toc = md.extractToc(src);
    } catch (err) {
      flagged.push(rel + ' :: 抛异常 ' + err.message);
      return;
    }
    totalMs += Date.now() - t0;

    var flags = [];
    if (html.indexOf('md-fallback') >= 0) { flags.push('走了 fallback'); }
    if (/[\u0001\u0002\u0003]/.test(html)) { flags.push('哨兵字符泄漏'); }
    if (/<p>\s*<\/(details|summary|ul|ol|li|pre|table)>/.test(html)) { flags.push('块级闭标签被包进 <p>'); }
    if (/<p>\s*<p>/.test(html)) { flags.push('<p> 嵌套'); }

    // 内容守恒：源里每个字符都要在输出里（文字或属性值）。只查“丢字”：
    // 属性里出现“多字”是接口要求的副产物（wikilink 的 title 就是它的路径），不算错。
    //
    // 注意：比对基准要用「剥掉 frontmatter 之后的源」。渲染器会主动丢弃开头的 YAML
    // frontmatter（title/description/keywords 是构建脚本和搜索引擎用的元数据，不该
    // 出现在阅读器正文里）—— 那是预期行为，不是丢字。不剥的话每个带 frontmatter 的
    // 章节都会误报一百多个"丢字"。
    var a = hist(stripFrontmatter(src)), b = hist(readable(html));
    var lost = 0, gain = 0;
    Object.keys(a).forEach(function (k) {
      var d = (b[k] || 0) - (a[k] || 0);
      if (d < 0) { lost += -d; }
    });
    Object.keys(b).forEach(function (k) {
      var d = (b[k] || 0) - (a[k] || 0);
      if (d > 0) { gain += d; }
    });
    gainTotal += gain;
    if (lost) { flags.push('丢字 ' + lost + '（源 ' + (a[Object.keys(a)[0]] || 0) + '）'); }

    // 目录 id 全部能对上
    var missing = toc.filter(function (t) { return html.indexOf('id="' + t.id + '"') < 0; });
    if (missing.length) { flags.push('目录 id 缺失 ' + missing.length); }

    // 成对标签数量一致（<li>/<code> 有带属性的变体，要一起算）
    [['<li>', '</li>', '<li '], ['<code>', '</code>', '<code '],
     ['<p>', '</p>', null], ['<ul>', '</ul>', '<ul '], ['<ol>', '</ol>', '<ol '],
     ['<blockquote>', '</blockquote>', null], ['<details>', '</details>', null],
     ['<strong>', '</strong>', null], ['<em>', '</em>', null], ['<del>', '</del>', null],
     ['<table>', '</table>', null], ['<tr>', '</tr>', null]
    ].forEach(function (pair) {
      var open = countOf(html, pair[0]) + (pair[2] ? countOf(html, pair[2]) : 0);
      var close = countOf(html, pair[1]);
      if (open !== close) { flags.push(pair[0] + ' 开=' + open + ' 闭=' + close); }
    });

    if (flags.length) { flagged.push(rel + ' :: ' + flags.join(' | ')); }
  });

  console.log('\n[全站回归] 共 ' + files.length + ' 个 md 文件，渲染总耗时 ' + totalMs + 'ms');
  if (flagged.length) { flagged.forEach(function (f) { console.log('  FAIL ' + f); }); }
  else {
    console.log('  全部通过：无异常 / 无兜底 / 无丢字 / 目录 id 一致 / 成对标签平衡');
    console.log('  （属性里另有 ' + gainTotal +
      ' 个重复字符，来自 wikilink 的 title 与图片 alt —— 接口要求，不是内容重复）');
  }
  check('10. 全站 ' + files.length + ' 个 md 回归（无异常、无兜底、无丢字、标签平衡）',
    flagged.length === 0,
    flagged.length ? flagged.slice(0, 8).join(' ; ')
      : '全部 ' + files.length + ' 个文件通过；零丢字；属性内额外 ' + gainTotal + ' 字符（wikilink title / img alt）');
})();

/* ===========================================================================
 * 汇总
 * ========================================================================*/
console.log('\n==================== 自检汇总 ====================');
var failed = 0;
checks.forEach(function (c) {
  console.log((c.ok ? '[PASS] ' : '[FAIL] ') + c.name);
  if (!c.ok) { console.log('        证据: ' + c.evidence); failed++; }
});
console.log('=================================================');
console.log('共 ' + checks.length + ' 项，通过 ' + (checks.length - failed) + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);
