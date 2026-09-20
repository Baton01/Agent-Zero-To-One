/* ============================================================================
 * web/assets/markdown.js —— 极简 Markdown 渲染器（纯 JS / 零依赖 / 无 CDN）
 * 接口：render(md)->HTML 全文；renderInline(text)->行内 HTML；
 *       extractToc(md)->[{level:2|3, text, id}]（id 与 render 输出一致：两者共用
 *       parseDocument，一次遍历同时产出正文与目录）
 * 处理阶段（严格按此顺序，每步只干一件事）：
 *   0 预处理  normalize()    换行归一 / 去 BOM / 制表符展开（围栏内保留）/ 清哨兵字符
 *   1 分词    tokenize()     切成 token：代码 span、图片、链接、[[wikilink]]、白名单
 *                            HTML 标签、HTML 实体、纯文本
 *   2 行内    renderTokens() 非文本 token 先渲染并挂哨兵占位；纯文本“先转义再强调”，
 *                            最后回填哨兵 —— 跨 token 的强调（**粗 `码` 体**）也能配对
 *   3 块级    parseBlocks()  逐行识别：围栏代码 / 标题 / 水平线 / 引用 / 表格 / 列表 /
 *                            白名单 HTML 块 / 段落；引用与列表递归调用自身（嵌套由此来）
 *   4 兜底    render()       只放行白名单标签，其余 < > & 一律转义；最外层 try/catch
 *                            保证任何输入都不抛异常
 * 设计倾向：容错优先于严格 —— 宁可少一点排版，也不能丢内容或抛异常。
 * ==========================================================================*/
var AZTO_MD = (function () {
  'use strict';

  // 允许原样透传的 HTML 标签（大小写不敏感）；其余一律转义，防 XSS
  var TAG_WHITELIST = {
    details: 1, summary: 1, br: 1, sub: 1, sup: 1, kbd: 1, mark: 1, span: 1,
    div: 1, a: 1, img: 1, p: 1, ul: 1, ol: 1, li: 1, code: 1, pre: 1
  };
  var VOID_TAGS = { br: 1, img: 1, hr: 1 };              // 空元素：不用找闭合标签
  // 白名单标签上允许保留的属性；on* 事件处理器与未知属性一律丢弃
  var ALLOWED_ATTRS = {
    'class': 1, id: 1, title: 1, alt: 1, src: 1, href: 1, target: 1, rel: 1, open: 1,
    type: 1, checked: 1, disabled: 1, colspan: 1, rowspan: 1, align: 1, lang: 1, dir: 1,
    width: 1, height: 1, datetime: 1, cite: 1, start: 1, value: 1, loading: 1
  };
  var URL_ATTRS = { href: 1, src: 1 };
  var BAD_URL = /^[\s\u0000-\u001f]*(javascript|vbscript|data)\s*:/i;
  var ESCAPABLE = /[\\`*_{}\[\]()#+\-.!~|<>$%&:;'"@^=?,/]/;   // 可被 \ 转义的标点
  var MARK_OPEN = '\u0002', LIT_OPEN = '\u0003';              // 哨兵字符（正文里不存在）

  // ==== 阶段 0：预处理 + 基础转义。换行归一 / 去 BOM / 清哨兵字符；
  // 制表符只在围栏之外展开 —— 围栏里的代码要逐字节保留。
  function normalize(text) {
    var s = String(text == null ? '' : text);
    s = s.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').replace(/[\u0002\u0003]/g, '').replace(/\u00a0/g, ' ');
    s = stripFrontmatter(s);
    var lines = s.split('\n'), inFence = false;
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*(`{3,}|~{3,})/.test(lines[i])) { inFence = !inFence; continue; }
      if (!inFence && lines[i].indexOf('\t') >= 0) lines[i] = lines[i].replace(/\t/g, '    ');
    }
    return lines.join('\n');
  }

  // 剥掉开头的 YAML frontmatter。项目里的章节和专题都带 frontmatter
  // （title / description / keywords / tags），那是给构建脚本和搜索用的元数据，
  // 不该出现在阅读器正文里 —— 不剥的话会被渲染成一条 <hr> 加一段可见的 "title: …"。
  // 构建脚本已经剥过一层，这里是兜底：直接渲染原始文件时也正确。
  function stripFrontmatter(s) {
    if (s.indexOf('---') !== 0) return s;
    var match = /^---[ \t]*\n[\s\S]*?\n---[ \t]*\n?/.exec(s);
    return match ? s.slice(match[0].length) : s;
  }
  function trimHs(s) { return String(s == null ? '' : s).replace(/^[ \t]+|[ \t]+$/g, ''); }
  function trimRight(s) { return String(s == null ? '' : s).replace(/\s+$/, ''); }
  function escapeHtml(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

  // ==== 阶段 1：行内分词。一次字符扫描，按优先级尝试：反斜杠转义 -> 代码 span ->
  // 图片 -> 链接 -> wikilink -> 白名单标签 -> HTML 实体 -> 普通文本。
  // 匹配失败就退化成普通字符，绝不因为一个坏结构丢掉后面的内容。
  function tickRun(text, pos) { var n = 0; while (text.charAt(pos + n) === '`') n++; return n; }

  // pos 处是否被奇数个反斜杠转义
  function isBackslashEscaped(text, pos) {
    var n = 0, i = pos - 1;
    while (i >= 0 && text.charAt(i) === '\\') { n++; i--; }
    return n % 2 === 1;
  }
  // 找与 pos 处等长（run 个）的反引号串，返回起点；找不到返回 -1。
  // 宽容一条：紧跟反斜杠的反引号不当作收尾 —— 作者常把 `\`` 当成“代码里的反引号”，
  // 按 CommonMark 严格解释会把这一格拆坏（本项目语料里有实例）。
  function findTickClose(text, pos, run) {
    var i = pos;
    while (i < text.length) {
      if (text.charAt(i) !== '`') { i++; continue; }
      var r = tickRun(text, i);
      if (r === run && !isBackslashEscaped(text, i)) return i;
      i += r;
    }
    return -1;
  }
  // [[target]] 或 [[target|label]]
  function matchWiki(text, i) {
    var end = text.indexOf(']]', i + 2);
    if (end < 0) return null;
    var inner = text.substring(i + 2, end);
    if (inner.length > 300 || inner.indexOf('\n') >= 0) return null;
    var bar = inner.indexOf('|');
    var target = bar >= 0 ? inner.substring(0, bar) : inner;
    var label = bar >= 0 ? inner.substring(bar + 1) : inner;
    if (!target.replace(/\s/g, '')) return null;
    return { token: { type: 'wiki', target: target.trim(), label: label.trim() || target.trim() }, next: end + 2 };
  }
  // [label](href "title") / ![alt](src "title")；label 支持嵌套方括号与代码 span
  function matchLinkLike(text, i, isImage, depth) {
    var j = i + (isImage ? 2 : 1), opened = isImage ? i + 1 : i, level = 0, labelEnd = -1, c, r, close;
    while (j < text.length) {
      c = text.charAt(j);
      if (c === '\\') { j += 2; continue; }
      if (c === '`') {                                      // 代码 span 里的 ] 不算括号
        r = tickRun(text, j);
        close = findTickClose(text, j + r, r);
        if (close > -1) { j = close + r; continue; }
      }
      if (c === '[') level++;
      else if (c === ']') { level--; if (level <= 0) { labelEnd = j; break; } }
      j++;
    }
    if (labelEnd < 0 || text.charAt(labelEnd + 1) !== '(') return null;
    var k = labelEnd + 2, paren = 0, dest = '', d;
    while (k < text.length) {                               // 目标：允许括号成对出现
      d = text.charAt(k);
      if (d === '\n') return null;
      if (d === '\\' && k + 1 < text.length) { dest += text.charAt(k + 1); k += 2; continue; }
      if (d === '(') paren++;
      else if (d === ')') { if (paren === 0) break; paren--; }
      dest += d;
      k++;
    }
    if (k >= text.length || text.charAt(k) !== ')') return null;   // 没闭合
    var href = dest.trim(), title = '';
    var tm = /^(\S+)\s+["']([\s\S]*)["']$/.exec(href);
    if (tm) { href = tm[1]; title = tm[2]; }
    var label = text.substring(opened + 1, labelEnd);
    if (isImage) return { token: { type: 'image', src: href, alt: label, title: title }, next: k + 1 };
    return { token: { type: 'link', href: href, title: title, children: tokenize(label, depth + 1) }, next: k + 1 };
  }
  // 解析一个 HTML 标签 <name attr="v"> / </name> / <br/>；解析不了返回 null。
  // 手写扫描而不是正则，是为了正确处理引号里带 > 的属性值。
  function parseTag(text, pos) {
    if (text.charAt(pos) !== '<') return null;
    var i = pos + 1, closing = false, attrs = [], nameM, ws, ch, m, attrName, value, eq, q, close;
    if (text.charAt(i) === '/') { closing = true; i++; }
    nameM = /^[A-Za-z][A-Za-z0-9-]*/.exec(text.substring(i));
    if (!nameM) return null;
    var name = nameM[0];
    i += name.length;
    while (i < text.length) {
      ws = /^\s*/.exec(text.substring(i))[0].length;
      i += ws;
      ch = text.charAt(i);
      if (ch === '>') return { closing: closing, name: name, attrs: attrs, selfClosing: false, end: i + 1 };
      if (ch === '/' && text.charAt(i + 1) === '>') return { closing: closing, name: name, attrs: attrs, selfClosing: true, end: i + 2 };
      if (!ws) return null;                                 // 属性之间必须有空白
      m = /^[^\s=\/>]+/.exec(text.substring(i));
      if (!m) return null;
      attrName = m[0];
      i += attrName.length;
      value = null;
      eq = i;                                               // 允许 name = "value" 这种写法
      i += /^\s*/.exec(text.substring(i))[0].length;
      if (text.charAt(i) === '=') {
        i += 1 + /^\s*/.exec(text.substring(i + 1))[0].length;
        q = text.charAt(i);
        if (q === '"' || q === "'") {
          close = text.indexOf(q, i + 1);
          if (close < 0) return null;
          value = text.substring(i + 1, close);
          i = close + 1;
        } else {
          m = /^[^\s>]*/.exec(text.substring(i));
          value = m ? m[0] : '';
          i += value.length;
        }
      } else i = eq;
      attrs.push({ name: attrName, value: value });
    }
    return null;                                            // 没有闭合的 >
  }
  // 标签白名单 + 属性白名单：不通过返回 null（调用方按文本转义处理）
  function sanitizeTag(parsed) {
    var name = parsed.name.toLowerCase();
    if (!TAG_WHITELIST[name]) return null;
    if (parsed.closing) return '</' + name + '>';
    var out = '<' + name;
    for (var i = 0; i < parsed.attrs.length; i++) {
      var attr = parsed.attrs[i], an = attr.name.toLowerCase();
      if (an.length > 2 && an.indexOf('on') === 0) continue;      // 事件处理器
      if (!ALLOWED_ATTRS[an]) continue;                          // 未知属性
      if (attr.value === null) { out += ' ' + an; continue; }    // 布尔属性
      if (URL_ATTRS[an] && BAD_URL.test(attr.value)) continue;   // 危险协议
      out += ' ' + an + '="' + escapeAttr(attr.value) + '"';
    }
    return out + '>';
  }
  function matchTag(text, i) {
    var parsed = parseTag(text, i), html;
    if (!parsed) return null;
    html = sanitizeTag(parsed);
    return html === null ? null : { value: html, next: parsed.end };
  }
  var ENTITY_RE = /^&(#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});/;   // 实体原样保留
  function matchEntity(text, i) {
    var m = ENTITY_RE.exec(text.substring(i));
    return m ? { value: m[0], next: i + m[0].length } : null;
  }
  function tokenize(text, depth) {
    depth = depth || 0;
    var tokens = [], buf = '', i = 0, n = text.length, ch, sub, code, run, close;
    function flush() { if (buf) { tokens.push({ type: 'text', value: buf }); buf = ''; } }
    while (i < n) {
      ch = text.charAt(i);
      if (ch === '\\' && i + 1 < n && ESCAPABLE.test(text.charAt(i + 1))) {   // \* \_ \` ...
        flush(); tokens.push({ type: 'literal', value: text.charAt(i + 1) }); i += 2; continue;
      }
      if (ch === '`') {                                     // 代码 span（未闭合则退化）
        run = tickRun(text, i);
        close = findTickClose(text, i + run, run);
        if (close > -1) {
          code = text.substring(i + run, close).replace(/\n/g, ' ');
          // CommonMark 规则：内容首尾各去掉一个空格
          if (code.length > 1 && code.charAt(0) === ' ' && code.charAt(code.length - 1) === ' ' && /\S/.test(code)) code = code.substring(1, code.length - 1);
          flush(); tokens.push({ type: 'code', value: code }); i = close + run; continue;
        }
        buf += text.substr(i, run); i += run; continue;
      }
      sub = (ch === '!' && text.charAt(i + 1) === '[' && depth < 8) ? matchLinkLike(text, i, true, depth) : null;
      if (!sub && ch === '[') {                             // wikilink / 链接
        sub = text.charAt(i + 1) === '[' ? matchWiki(text, i)
            : (depth < 8 ? matchLinkLike(text, i, false, depth) : null);
      }
      if (!sub && ch === '<') sub = matchTag(text, i);                       // 白名单标签
      if (!sub && ch === '&') sub = matchEntity(text, i);                    // HTML 实体
      if (sub) { flush(); tokens.push(sub.token || { type: 'raw', value: sub.value }); i = sub.next; continue; }
      buf += ch; i++;
    }
    flush(); return tokens;
  }

  // ==== 阶段 2：行内渲染（非文本 token 先渲染并挂哨兵 -> 纯文本先转义再强调 -> 回填哨兵）
  function emphasize(text) {
    text = text.replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
    text = text.replace(/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, '<strong><em>$1</em></strong>');
    text = text.replace(/(^|[^A-Za-z0-9_])___(?=\S)([\s\S]*?\S)___(?![A-Za-z0-9_])/g, '$1<strong><em>$2</em></strong>');
    text = text.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|[^A-Za-z0-9_])__(?=\S)([\s\S]*?\S)__(?![A-Za-z0-9_])/g, '$1<strong>$2</strong>');
    text = text.replace(/\*(?=\S)([^*]*?\S)\*/g, '<em>$1</em>');
    return text.replace(/(^|[^A-Za-z0-9_])_(?=\S)([^_]*?\S)_(?![A-Za-z0-9_])/g, '$1<em>$2</em>');
  }
  function renderTokens(tokens, depth) {
    if (!tokens || !tokens.length) return '';
    depth = depth || 0;
    // 递归保护：太深就退化成纯文本
    if (depth > 10) { var raw = ''; for (var x = 0; x < tokens.length; x++) raw += tokens[x].value || ''; return escapeHtml(raw); }
    var registry = [], flat = '', t, html;
    for (var i = 0; i < tokens.length; i++) {
      t = tokens[i];
      if (t.type === 'text') { flat += t.value; continue; }
      if (t.type === 'literal') {                           // 转义字面量：单独一类哨兵
        registry.push(escapeHtml(t.value)); flat += LIT_OPEN + (registry.length - 1) + LIT_OPEN; continue;
      }
      if (t.type === 'code') html = '<code>' + escapeHtml(t.value) + '</code>';
      else if (t.type === 'raw') html = t.value;
      else if (t.type === 'wiki') html = '<span class="wikilink" title="' + escapeAttr(t.target) + '">' + escapeHtml(t.label) + '</span>';
      else if (t.type === 'image') {
        html = '<img src="' + escapeAttr(t.src) + '" alt="' + escapeAttr(t.alt) + '"' +
               (t.title ? ' title="' + escapeAttr(t.title) + '"' : '') + ' loading="lazy">';
      } else if (t.type === 'link') {
        html = '<a href="' + escapeAttr(t.href) + '"' + (t.title ? ' title="' + escapeAttr(t.title) + '"' : '') +
               ' target="_blank" rel="noopener noreferrer">' + renderTokens(t.children, depth + 1) + '</a>';
      } else html = escapeHtml(t.value || '');
      registry.push(html); flat += MARK_OPEN + (registry.length - 1) + MARK_OPEN;
    }
    var out = emphasize(escapeHtml(flat));                  // 纯文本：先转义再上强调
    return out.replace(/\u0002(\d+)\u0002|\u0003(\d+)\u0003/g, function (m, a, b) {
      var v = registry[a === undefined ? +b : +a];
      return v === undefined ? '' : v;
    });
  }
  function renderInline(text) {                             // 行内渲染入口
    try { return renderTokens(tokenize(normalize(text), 0), 0); }
    catch (err) { return escapeHtml(text); }                // 兜底：只退化，不抛异常
  }
  // 标题/目录用的纯文本：抹掉行内标记，只留可读文字
  function plainText(text) {
    return String(text == null ? '' : text)
      .replace(/`+([^`]*)`+/g, '$1')
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[\[([^\]|]*)(?:\|([^\]]*))?\]\]/g, function (m, p, l) { return l || p; })
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/<\/?[A-Za-z][^<>]*>/g, '')
      .replace(/(\*\*\*|\*\*|\*|~~|___|__|_)/g, '')
      .replace(/\\([\\`*_{}\[\]()#+\-.!~|<>])/g, '$1')
      .replace(/\s+/g, ' ').trim();
  }

  // ==== 阶段 3：块级识别工具
  function isBlank(line) { return !line || !line.replace(/\s/g, ''); }
  function indentOf(line) { var m = /^ */.exec(line); return m ? m[0].length : 0; }
  function isQuoteLine(line) { return /^ {0,3}>/.test(line); }
  function stripQuotePrefix(line) { return line.replace(/^ {0,3}>[ ]?/, ''); }
  var FENCE_RE = /^(\s*)(`{3,}|~{3,})[ \t]*([^\s`]*)?.*$/;  // 围栏起始（允许信息串）
  function matchFence(line) {
    var m = FENCE_RE.exec(line);
    return m ? { indent: m[1].length, marker: m[2], info: m[3] || '' } : null;
  }
  function isFenceEnd(line, marker) {                       // 结束：同种字符、不短于起始
    var m = /^(\s*)(`{3,}|~{3,})\s*$/.exec(line);
    return !!m && m[2].charAt(0) === marker.charAt(0) && m[2].length >= marker.length;
  }
  var HEADING_RE = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/;  // ATX 标题
  function matchHeading(line) {
    var m = HEADING_RE.exec(line);
    if (!m) return null;
    return { level: m[1].length, text: trimHs(m[2] === undefined ? '' : m[2]).replace(/[ \t]+#+$/, '') };
  }
  function isHr(line) {                                     // --- / *** / ___
    var t = line.trim();
    return t.length >= 3 && /^([-*_])[ \t]*(\1[ \t]*){2,}$/.test(t);
  }
  function isDelimiterRow(line) {                            // |---|:---:|---:|
    if (!line) return false;
    var t = line.trim();
    return t.indexOf('|') >= 0 && t.indexOf('-') >= 0 && /^[|:\-\s]+$/.test(t);
  }
  function isTableStart(lines, i) {                          // 表头 + 下一行分隔行
    var head = lines[i], delim = lines[i + 1], t;
    if (!head || !delim || head.indexOf('|') < 0) return false;
    t = head.trim();
    if (t.charAt(0) !== '|' && (t.match(/\|/g) || []).length < 2) return false;
    if (matchFence(head) || matchHeading(head) || isHr(head) || isQuoteLine(head)) return false;
    return isDelimiterRow(delim);
  }
  // 按未转义的 | 切分表格行（跳过代码 span 内的 |）
  function splitRow(line) {
    var s = line.trim(), cells = [], buf = '', i = 0, inCode = false, tick = 0, c, r;
    if (s.charAt(0) === '|') s = s.substring(1);
    if (s.charAt(s.length - 1) === '|') s = s.substring(0, s.length - 1);
    while (i < s.length) {
      c = s.charAt(i);
      if (c === '\\' && s.charAt(i + 1) === '|') { buf += '|'; i += 2; continue; }
      if (c === '`') {
        r = tickRun(s, i);
        if (!inCode) { inCode = true; tick = r; } else if (r === tick) inCode = false;
        buf += s.substr(i, r);
        i += r;
        continue;
      }
      if (c === '|' && !inCode) { cells.push(trimHs(buf)); buf = ''; i++; continue; }
      buf += c;
      i++;
    }
    cells.push(trimHs(buf));
    return cells;
  }
  function cellAlign(spec) {                                 // :--- / :---: / ---:
    var t = (spec || '').trim();
    var left = t.charAt(0) === ':', right = t.charAt(t.length - 1) === ':';
    return (left && right) ? 'center' : right ? 'right' : left ? 'left' : '';
  }
  var LIST_RE = /^( *)([-*+]|\d{1,9}[.)])([ \t]+)(.*)$/;     // - * + / 1. 1)
  function matchListMarker(line) {
    var m = LIST_RE.exec(line), marker, task;
    if (!m) return null;
    marker = m[2];
    task = /^\[([ xX])\](?:[ \t]+(.*))?$/.exec(m[4]);
    return {
      indent: m[1].length, ordered: /\d/.test(marker), start: /\d/.test(marker) ? parseInt(marker, 10) : 1,
      contentIndent: m[1].length + marker.length + m[3].length, text: m[4],
      task: task ? { checked: task[1].toLowerCase() === 'x', text: task[2] || '' } : null
    };
  }
  // 行首的白名单 HTML 标签串（<details><summary>x</summary> 会被整体吃掉）
  function splitLeadingHtml(line) {
    var pos = 0, head = '', n = line.length, guard = 0, ws, parsed, name, tagHtml, tagEnd, gap, closer, ci;
    while (pos < n && guard++ < 40) {
      ws = 0;
      while (pos + ws < n && (line.charAt(pos + ws) === ' ' || line.charAt(pos + ws) === '\t')) ws++;
      if (line.charAt(pos + ws) !== '<') break;
      parsed = parseTag(line, pos + ws);
      if (!parsed) break;
      name = parsed.name.toLowerCase();
      if (!TAG_WHITELIST[name]) break;
      tagHtml = sanitizeTag(parsed);
      if (!tagHtml) break;
      tagEnd = parsed.end;
      gap = line.substring(pos, pos + ws);
      // 闭合标签与空元素直接透传（</details> 不能变成段落）
      if (parsed.closing || VOID_TAGS[name] || parsed.selfClosing) { head += gap + tagHtml; pos = tagEnd; continue; }
      closer = '</' + name + '>';                           // 同行闭合的容器标签
      ci = line.toLowerCase().indexOf(closer, tagEnd);
      if (ci >= 0) {
        head += gap + tagHtml + renderInline(line.substring(tagEnd, ci).trim()) + closer;
        pos = ci + closer.length;
        continue;
      }
      head += gap + tagHtml; pos = tagEnd;
    }
    return head ? { head: head, rest: line.substring(pos) } : null;
  }
  // 这一行是不是“新块”的开头（段落收集器靠它决定何时收手）
  function startsNewBlock(line, lines, i) {
    if (isBlank(line)) return true;
    if (matchFence(line) || matchHeading(line) || isHr(line) || isQuoteLine(line)) return true;
    if (matchListMarker(line) || isTableStart(lines, i)) return true;
    return /^ {0,3}</.test(line) && !!splitLeadingHtml(line);
  }

  // ==== 阶段 3：块级渲染（ctx = { inQuote, tight, state: { toc: [], counter } }）
  function emit(out, res) { out.push(res.html); return res.next; }   // 收下一个块的结果

  // 围栏代码块：内容原样保留（只做 HTML 转义），未闭合也照常输出到文末
  function renderFence(lines, start, fence) {
    var body = [], i = start + 1, lang, cls;
    while (i < lines.length) {
      if (isFenceEnd(lines[i], fence.marker)) { i++; break; }
      body.push(lines[i++]);
    }
    lang = String(fence.info || '').replace(/[^A-Za-z0-9_+#.-]/g, '');
    cls = lang ? ' class="lang-' + escapeAttr(lang) + '"' : '';
    return { html: '<pre><code' + cls + '>' + escapeHtml(body.join('\n')) + '</code></pre>', next: i };
  }
  // 引用块：连续 > 行合并成一个 blockquote，内部递归渲染（嵌套 >> 自然生效）
  function renderQuote(lines, start, ctx) {
    var inner = [], i = start, j;
    while (i < lines.length) {
      if (isBlank(lines[i])) {
        j = i;                                              // 空行后面还有 > 才算同一块
        while (j < lines.length && isBlank(lines[j])) j++;
        if (j < lines.length && isQuoteLine(lines[j])) { inner.push(''); i++; continue; }
        break;
      }
      if (!isQuoteLine(lines[i])) break;                    // 语料里引用行都带 >，不做懒惰续行
      inner.push(stripQuotePrefix(lines[i]));
      i++;
    }
    return {
      html: '<blockquote>\n' + parseBlocks(inner, { inQuote: true, tight: ctx.tight, state: ctx.state }) + '\n</blockquote>',
      next: i
    };
  }
  // 表格：表头 + 分隔行 + 若干数据行
  function renderTable(lines, start) {
    var head = splitRow(lines[start]), aligns = splitRow(lines[start + 1]).map(cellAlign);
    var out = ['<div class="table-wrap"><table>', '<thead><tr>'], body = [], i = start + 2;
    var headerPipes = (lines[start].match(/\|/g) || []).length, c, line, t, cells;
    for (c = 0; c < head.length; c++) {
      out.push('<th' + (aligns[c] ? ' style="text-align:' + aligns[c] + '"' : '') + '>' + renderInline(head[c]) + '</th>');
    }
    out.push('</tr></thead>');
    while (i < lines.length) {
      line = lines[i];
      if (isBlank(line)) break;
      if (matchFence(line) || matchHeading(line) || isHr(line) || isQuoteLine(line)) break;
      t = line.trim();
      if (t.charAt(0) !== '|' && !(line.indexOf('|') >= 0 && (line.match(/\|/g) || []).length === headerPipes)) break;
      cells = splitRow(line);
      body.push('<tr>');
      for (c = 0; c < cells.length; c++) {
        body.push('<td' + (aligns[c] ? ' style="text-align:' + aligns[c] + '"' : '') + '>' + renderInline(cells[c]) + '</td>');
      }
      body.push('</tr>'); i++;
    }
    if (body.length) out.push('<tbody>' + body.join('') + '</tbody>');
    out.push('</table></div>');
    return { html: out.join('\n'), next: i };
  }
  // 列表：按缩进判断层级。做法是“收集整块 -> 按同级标记拆项 -> 每项内容递归 parseBlocks”，
  // 嵌套列表就是上一层 <li> 内部递归出来的子块。
  function renderList(lines, start, ctx) {
    var first = matchListMarker(lines[start]), baseIndent = first.indent, ordered = first.ordered;
    var block = [], i = start, sawMarker = false, line, m, nm, j;

    // (1) 收集属于这个列表的所有行（含空行、续行、更深的嵌套行）
    while (i < lines.length) {
      line = lines[i];
      if (isBlank(line)) {                                  // 空行：后面还有列表内容才吃进来
        j = i;
        while (j < lines.length && isBlank(lines[j])) j++;
        nm = j < lines.length ? matchListMarker(lines[j]) : null;
        if (!(j < lines.length && ((nm && nm.indent >= baseIndent) || indentOf(lines[j]) > baseIndent))) break;
        block.push('');
        i++;
        continue;
      }
      m = matchListMarker(line);
      if (m && m.indent >= baseIndent) {                    // 同级或更深的列表项
        if (m.indent === baseIndent && m.ordered !== ordered && sawMarker) break;   // 换了列表类型
        sawMarker = true;
      } else if (indentOf(line) <= baseIndent) {            // 同级非标记行：续行或结束
        if (!sawMarker || startsNewBlock(line, lines, i)) break;
      }
      block.push(line);
      i++;
    }
    // (2) 按同级标记拆项；更深或缩进的行属于当前项的内容
    var items = [], cur = null, b, bl, mm;
    for (b = 0; b < block.length; b++) {
      bl = block[b];
      mm = matchListMarker(bl);
      if (mm && mm.indent === baseIndent) {
        cur = { marker: mm, lines: [mm.task ? mm.task.text : mm.text] };   // 任务项正文从 [ ] 后开始
        items.push(cur);
        continue;
      }
      if (!cur) { cur = { marker: first, lines: [] }; items.push(cur); }   // 极端容错
      if (isBlank(bl)) { cur.lines.push(''); continue; }
      cur.lines.push(bl.substring(Math.min(indentOf(bl), Math.max(cur.marker.contentIndent, 2))));
    }
    if (!items.length) items.push({ marker: first, lines: [first.text] });

    // (3) 逐项渲染：内容递归走一遍块级解析（tight 模式不包 <p>，列表更紧凑）
    var out = [], item, content;
    for (b = 0; b < items.length; b++) {
      item = items[b];
      content = trimRight(parseBlocks(item.lines, { inQuote: ctx.inQuote, tight: true, state: ctx.state }));
      if (item.marker.task) {
        out.push('<li class="task"><input type="checkbox" disabled' +
                 (item.marker.task.checked ? ' checked' : '') + '>' + (content ? ' ' + content : '') + '</li>');
        continue;
      }
      out.push('<li>' + content + '</li>');
    }
    return {
      html: '<' + (ordered ? 'ol' : 'ul') + (ordered && first.start !== 1 ? ' start="' + first.start + '"' : '') +
            '>\n' + out.join('\n') + '\n</' + (ordered ? 'ol' : 'ul') + '>',
      next: i
    };
  }
  // 段落：连续非块级行合并成一个 <p>（tight 模式下不包 <p>）
  function renderParagraph(lines, start, ctx) {
    var buf = [], i = start, line;
    while (i < lines.length) {
      line = lines[i];
      if (isBlank(line) || (buf.length && startsNewBlock(line, lines, i))) break;
      buf.push(trimHs(line)); i++;
    }
    var text = renderInline(buf.join('\n'));
    return { html: ctx.tight ? text : '<p>' + text + '</p>', next: i };
  }
  // 块级主循环：按优先级逐行分派
  function parseBlocks(lines, ctx) {
    ctx = ctx || {};
    var state = ctx.state || { toc: [], counter: 0 };
    var out = [], i = 0, line, res, heading, level, hid, idAttr, htmlHead;
    while (i < lines.length) {
      line = lines[i];
      if (isBlank(line)) { i++; continue; }
      res = matchFence(line);                                // (1) 围栏代码
      if (res) { i = emit(out, renderFence(lines, i, res)); continue; }
      heading = matchHeading(line);                          // (2) ATX 标题
      if (heading) {
        if (ctx.inQuote) {                                   // 引用块内：降级为粗体段落
          out.push('<p><strong>' + renderInline(heading.text) + '</strong></p>');
        } else {
          level = heading.level;
          idAttr = '';
          if (level <= 4) {
            hid = 'sec-' + (++state.counter);
            idAttr = ' id="' + hid + '"';
            if (level === 2 || level === 3) state.toc.push({ level: level, text: plainText(heading.text), id: hid });
          }
          out.push('<h' + level + idAttr + '>' + renderInline(heading.text) + '</h' + level + '>');
        }
        i++;
        continue;
      }
      if (isHr(line)) { out.push('<hr>'); i++; continue; }                    // (3) 水平线
      if (isQuoteLine(line)) { i = emit(out, renderQuote(lines, i, ctx)); continue; }        // (4) 引用块
      if (isTableStart(lines, i)) { i = emit(out, renderTable(lines, i)); continue; }         // (5) 表格
      if (matchListMarker(line)) { i = emit(out, renderList(lines, i, ctx)); continue; }      // (6) 列表
      // (7) 原始 HTML 透传：<details><summary>…</summary> 原样输出，
      //     标签之间的 Markdown 继续按块级规则渲染
      htmlHead = /^ {0,3}</.test(line) ? splitLeadingHtml(line) : null;
      if (htmlHead) {
        out.push(htmlHead.head);
        if (htmlHead.rest && htmlHead.rest.trim()) { lines[i] = htmlHead.rest; continue; }
        i++;
        continue;
      }
      res = renderParagraph(lines, i, ctx);                  // (8) 段落
      out.push(res.html); i = res.next > i ? res.next : i + 1;
    }
    return out.join('\n');
  }

  // ------------------------------------------------- 文档级：一次遍历出 HTML + 目录
  function parseDocument(markdown) {
    var state = { toc: [], counter: 0 };
    var html = parseBlocks(normalize(markdown).split('\n'), { inQuote: false, tight: false, state: state });
    return { html: html, toc: state.toc };
  }
  function render(markdown) {                                // 出错退化为 <pre> 原文，绝不抛
    try { return parseDocument(markdown).html; }
    catch (err) { return '<pre class="md-fallback">' + escapeHtml(normalize(markdown)) + '</pre>'; }
  }
  function extractToc(markdown) {                            // level 只取 2/3，id 与 render 一致
    try { return parseDocument(markdown).toc; }
    catch (err) { return []; }
  }
  return { render: render, renderInline: renderInline, extractToc: extractToc };
})();

/* 挂到全局：浏览器挂 window，Node 用 globalThis 兜底，
   同时让文件末尾的 module.exports 片段在两种环境都能直接跑。 */
var AZTO_GLOBAL = (typeof globalThis !== 'undefined') ? globalThis
  : (typeof self !== 'undefined') ? self : (typeof window !== 'undefined') ? window : this;
AZTO_GLOBAL.AZTO_MD = AZTO_MD;
if (typeof AZTO_GLOBAL.window === 'undefined') {
  try { AZTO_GLOBAL.window = AZTO_GLOBAL; } catch (e) { /* 只读环境忽略 */ }
}
if (typeof module !== 'undefined' && module.exports) { module.exports = window.AZTO_MD || AZTO_MD; }
