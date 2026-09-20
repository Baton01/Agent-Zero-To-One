"""
C++ 判题运行时（生成物的一部分，用户看不到）。

为什么需要它
------------
Python 判题靠"驱动脚本 import 用户代码"，但 C++ 没有这种动态能力：
    · 把用例值直接烧进源码 → 每个用例都要重新编译，一次提交 6~8 次编译、几十秒，不可接受
    · 让二进制在运行时读用例 → 需要 JSON 解析
所以走第二条路：一份源码编译一次，二进制从 stdin 读用例 JSON、跑完把结果打成 JSON 输出。

这里就是那份"读 JSON + 构造数据结构 + 序列化结果"的运行时。它是**手写的最小实现**，
不引第三方库 —— 整个项目的前提是零依赖。

两个字符串会被拼进生成的 .cpp：
    CPP_PRELUDE   放在用户代码**之前**（类型定义、JSON 解析、序列化、构造器）
    CPP_MAIN_*    放在用户代码**之后**（main 函数）
用户代码插在中间，所以报错行号要减去 CPP_PRELUDE 的行数才能映射回用户的代码。
"""

from __future__ import annotations

import os

#: 放在用户代码之前的运行时。改动它以后，报错行号映射会自动跟着变（见 line_offset）。
CPP_PRELUDE = r'''// ============================================================
//  AZTO C++ 判题运行时（自动生成，请勿手改）
//  你的代码在下面 "YOUR CODE" 标记之后
// ============================================================
#include <algorithm>
#include <array>
#include <bitset>
#include <cctype>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <iostream>
#include <iterator>
#include <list>
#include <map>
#include <numeric>
#include <queue>
#include <set>
#include <sstream>
#include <stack>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

using namespace std;

// ---------- LeetCode 的两个基础数据结构 ----------
struct ListNode {
    int val;
    ListNode *next;
    ListNode() : val(0), next(nullptr) {}
    ListNode(int x) : val(x), next(nullptr) {}
    ListNode(int x, ListNode *n) : val(x), next(n) {}
};

struct TreeNode {
    int val;
    TreeNode *left, *right;
    TreeNode() : val(0), left(nullptr), right(nullptr) {}
    TreeNode(int x) : val(x), left(nullptr), right(nullptr) {}
    TreeNode(int x, TreeNode *l, TreeNode *r) : val(x), left(l), right(r) {}
};

// ---------- 极简 JSON（只覆盖判题用得到的那部分）----------
struct JVal {
    enum Kind { NUL, BOOL, NUM, STR, ARR, OBJ } kind = NUL;
    bool b = false;
    double num = 0;
    string str;
    vector<JVal> arr;
    map<string, JVal> obj;

    bool isNull() const { return kind == NUL; }
    bool isArr() const { return kind == ARR; }
    size_t size() const { return arr.size(); }
    int asInt() const { return (int)llround(num); }
    long long asLong() const { return (long long)llround(num); }
    double asDouble() const { return num; }
    bool asBool() const { return b; }
    const string &asStr() const { return str; }

    const JVal &operator[](const string &key) const {
        static const JVal empty;
        map<string, JVal>::const_iterator it = obj.find(key);
        return it == obj.end() ? empty : it->second;
    }
    const JVal &operator[](size_t index) const {
        static const JVal empty;
        return index < arr.size() ? arr[index] : empty;
    }
};

struct JParser {
    const string &src;
    size_t pos;
    explicit JParser(const string &s) : src(s), pos(0) {}

    void skipWs() {
        while (pos < src.size() &&
               (src[pos] == ' ' || src[pos] == '\n' || src[pos] == '\r' || src[pos] == '\t'))
            pos++;
    }

    JVal parse() {
        skipWs();
        return value();
    }

    JVal value() {
        skipWs();
        if (pos >= src.size()) return JVal();
        char c = src[pos];
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') {
            JVal v;
            v.kind = JVal::STR;
            v.str = parseString();
            return v;
        }
        if (c == 't') { pos += 4; JVal v; v.kind = JVal::BOOL; v.b = true; return v; }
        if (c == 'f') { pos += 5; JVal v; v.kind = JVal::BOOL; v.b = false; return v; }
        if (c == 'n') { pos += 4; return JVal(); }
        return number();
    }

    JVal object() {
        JVal v;
        v.kind = JVal::OBJ;
        pos++;                       // '{'
        skipWs();
        if (pos < src.size() && src[pos] == '}') { pos++; return v; }
        while (pos < src.size()) {
            skipWs();
            string key = parseString();
            skipWs();
            if (pos < src.size() && src[pos] == ':') pos++;
            v.obj[key] = value();
            skipWs();
            if (pos < src.size() && src[pos] == ',') { pos++; continue; }
            if (pos < src.size() && src[pos] == '}') { pos++; break; }
            break;
        }
        return v;
    }

    JVal array() {
        JVal v;
        v.kind = JVal::ARR;
        pos++;                       // '['
        skipWs();
        if (pos < src.size() && src[pos] == ']') { pos++; return v; }
        while (pos < src.size()) {
            v.arr.push_back(value());
            skipWs();
            if (pos < src.size() && src[pos] == ',') { pos++; continue; }
            if (pos < src.size() && src[pos] == ']') { pos++; break; }
            break;
        }
        return v;
    }

    static void appendUtf8(string &out, unsigned cp) {
        if (cp < 0x80) {
            out += (char)cp;
        } else if (cp < 0x800) {
            out += (char)(0xC0 | (cp >> 6));
            out += (char)(0x80 | (cp & 0x3F));
        } else {
            out += (char)(0xE0 | (cp >> 12));
            out += (char)(0x80 | ((cp >> 6) & 0x3F));
            out += (char)(0x80 | (cp & 0x3F));
        }
    }

    string parseString() {
        string out;
        if (pos < src.size() && src[pos] == '"') pos++;
        while (pos < src.size()) {
            char c = src[pos++];
            if (c == '"') break;
            if (c != '\\') { out += c; continue; }
            if (pos >= src.size()) break;
            char e = src[pos++];
            switch (e) {
                case 'n': out += '\n'; break;
                case 't': out += '\t'; break;
                case 'r': out += '\r'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case '/': out += '/'; break;
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case 'u': {
                    unsigned cp = 0;
                    for (int i = 0; i < 4 && pos < src.size(); i++) {
                        char h = src[pos++];
                        cp <<= 4;
                        if (h >= '0' && h <= '9') cp |= (unsigned)(h - '0');
                        else if (h >= 'a' && h <= 'f') cp |= (unsigned)(h - 'a' + 10);
                        else if (h >= 'A' && h <= 'F') cp |= (unsigned)(h - 'A' + 10);
                    }
                    // 代理对：高位后面紧跟一个 \uXXXX 低位
                    if (cp >= 0xD800 && cp <= 0xDBFF && pos + 1 < src.size() &&
                        src[pos] == '\\' && src[pos + 1] == 'u') {
                        pos += 2;
                        unsigned lo = 0;
                        for (int i = 0; i < 4 && pos < src.size(); i++) {
                            char h = src[pos++];
                            lo <<= 4;
                            if (h >= '0' && h <= '9') lo |= (unsigned)(h - '0');
                            else if (h >= 'a' && h <= 'f') lo |= (unsigned)(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') lo |= (unsigned)(h - 'A' + 10);
                        }
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    }
                    appendUtf8(out, cp);
                    break;
                }
                default: out += e; break;
            }
        }
        return out;
    }

    JVal number() {
        size_t start = pos;
        while (pos < src.size() &&
               (isdigit((unsigned char)src[pos]) || src[pos] == '-' || src[pos] == '+' ||
                src[pos] == '.' || src[pos] == 'e' || src[pos] == 'E'))
            pos++;
        JVal v;
        v.kind = JVal::NUM;
        v.num = atof(src.substr(start, pos - start).c_str());
        return v;
    }
};

// ---------- 结果序列化 ----------
string jesc(const string &s) {
    string r = "\"";
    for (size_t i = 0; i < s.size(); i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
            case '"': r += "\\\""; break;
            case '\\': r += "\\\\"; break;
            case '\n': r += "\\n"; break;
            case '\r': r += "\\r"; break;
            case '\t': r += "\\t"; break;
            case '\b': r += "\\b"; break;
            case '\f': r += "\\f"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    r += buf;
                } else {
                    r += (char)c;
                }
        }
    }
    return r + "\"";
}

string jout(int v) { return to_string(v); }
string jout(long long v) { return to_string(v); }
string jout(unsigned int v) { return to_string(v); }
string jout(unsigned long long v) { return to_string(v); }
string jout(short v) { return to_string((int)v); }
string jout(char v) { return jesc(string(1, v)); }
string jout(bool v) { return v ? "true" : "false"; }
string jout(const string &v) { return jesc(v); }
string jout(const char *v) { return jesc(string(v ? v : "")); }
string jout(double v) {
    if (v == (double)(long long)v && fabs(v) < 1e15) return to_string((long long)v);
    char buf[64];
    snprintf(buf, sizeof(buf), "%.10g", v);
    return buf;
}
string jout(float v) { return jout((double)v); }

template <class T>
string jout(const vector<T> &v) {
    string r = "[";
    for (size_t i = 0; i < v.size(); i++) {
        if (i) r += ",";
        r += jout(v[i]);
    }
    return r + "]";
}

// vector<char> 按字符串输出（力扣的约定）
string jout(const vector<char> &v) { return jesc(string(v.begin(), v.end())); }
string jout(const vector<vector<char> > &v) {
    string r = "[";
    for (size_t i = 0; i < v.size(); i++) {
        if (i) r += ",";
        r += jesc(string(v[i].begin(), v[i].end()));
    }
    return r + "]";
}

// 链表：走最多 20000 步，防止带环链表把判题挂死
string jout(ListNode *node) {
    vector<int> out;
    int guard = 0;
    while (node != nullptr && guard++ < 20000) {
        out.push_back(node->val);
        node = node->next;
    }
    return jout(out);
}

// 二叉树：力扣层序格式，尾部多余 null 去掉
string jout(TreeNode *root) {
    if (root == nullptr) return "[]";
    vector<string> out;
    queue<TreeNode *> q;
    q.push(root);
    while (!q.empty()) {
        TreeNode *cur = q.front();
        q.pop();
        if (cur == nullptr) {
            out.push_back("null");
            continue;
        }
        out.push_back(to_string(cur->val));
        q.push(cur->left);
        q.push(cur->right);
    }
    while (!out.empty() && out.back() == "null") out.pop_back();
    string r = "[";
    for (size_t i = 0; i < out.size(); i++) {
        if (i) r += ",";
        r += out[i];
    }
    return r + "]";
}

// ---------- 输入转换 ----------
int toInt(const JVal &v) { return v.asInt(); }
long long toLong(const JVal &v) { return v.asLong(); }
double toDouble(const JVal &v) { return v.asDouble(); }
bool toBool(const JVal &v) { return v.asBool(); }
string toStr(const JVal &v) { return v.str; }

vector<int> toIntVec(const JVal &v) {
    vector<int> r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(v.arr[i].asInt());
    return r;
}
vector<long long> toLongVec(const JVal &v) {
    vector<long long> r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(v.arr[i].asLong());
    return r;
}
vector<double> toDoubleVec(const JVal &v) {
    vector<double> r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(v.arr[i].asDouble());
    return r;
}
vector<string> toStrVec(const JVal &v) {
    vector<string> r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(v.arr[i].str);
    return r;
}
vector<bool> toBoolVec(const JVal &v) {
    vector<bool> r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(v.arr[i].asBool());
    return r;
}
vector<vector<int> > toIntMat(const JVal &v) {
    vector<vector<int> > r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(toIntVec(v.arr[i]));
    return r;
}
vector<vector<string> > toStrMat(const JVal &v) {
    vector<vector<string> > r;
    for (size_t i = 0; i < v.arr.size(); i++) r.push_back(toStrVec(v.arr[i]));
    return r;
}
// 字符网格（岛屿数量这类题）：用例里写的是 ["1","1","0"]，转成 { '1','1','0' }
vector<vector<char> > toCharMat(const JVal &v) {
    vector<vector<char> > r;
    for (size_t i = 0; i < v.arr.size(); i++) {
        vector<char> row;
        for (size_t j = 0; j < v.arr[i].arr.size(); j++) {
            const string &s = v.arr[i].arr[j].str;
            row.push_back(s.empty() ? '\0' : s[0]);
        }
        r.push_back(row);
    }
    return r;
}

ListNode *mklist(const JVal &v) {
    if (!v.isArr() || v.arr.empty()) return nullptr;
    ListNode head(0);
    ListNode *tail = &head;
    for (size_t i = 0; i < v.arr.size(); i++) {
        tail->next = new ListNode(v.arr[i].asInt());
        tail = tail->next;
    }
    return head.next;
}

TreeNode *mktree(const JVal &v) {
    if (!v.isArr() || v.arr.empty() || v.arr[0].isNull()) return nullptr;
    TreeNode *root = new TreeNode(v.arr[0].asInt());
    queue<TreeNode *> q;
    q.push(root);
    size_t i = 1;
    while (!q.empty() && i < v.arr.size()) {
        TreeNode *cur = q.front();
        q.pop();
        if (i < v.arr.size() && !v.arr[i].isNull()) {
            cur->left = new TreeNode(v.arr[i].asInt());
            q.push(cur->left);
        }
        i++;
        if (i < v.arr.size() && !v.arr[i].isNull()) {
            cur->right = new TreeNode(v.arr[i].asInt());
            q.push(cur->right);
        }
        i++;
    }
    return root;
}

static const string AZTO_MARK = "AZTO_RESULT";
static const string AZTO_MEM = "AZTO_MEM";

// ============================================================
//  ===== 你 的 代 码 从 这 里 开 始 =====
// ============================================================
'''

#: 峰值内存测量。
#: 放在用户代码**之后**是刻意的 —— windows.h 会定义 min/max 宏，那是经典的
#: "为什么我的 std::min 编译不过"来源。放在后面，用户代码就完全不受影响。
CPP_MEM_FN = r'''
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>
static long long azto_peak_kb() {
    PROCESS_MEMORY_COUNTERS pmc;
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc))) {
        return (long long)(pmc.PeakWorkingSetSize / 1024);
    }
    return 0;
}
#else
#include <sys/resource.h>
static long long azto_peak_kb() {
    struct rusage ru;
    if (getrusage(RUSAGE_SELF, &ru) == 0) {
#ifdef __APPLE__
        return (long long)(ru.ru_maxrss / 1024);
#else
        return (long long)ru.ru_maxrss;
#endif
    }
    return 0;
}
#endif
'''

#: 放在用户代码之后的 main（普通函数题）
#: {call_args} 由生成器填：按参数名依次展开的实参
CPP_MAIN_FUNCTION = r'''
// ============================================================
//  ===== 你 的 代 码 到 此 结 束 =====
// ============================================================
__MEM_FN__

int main() {
    ios::sync_with_stdio(false);
    string _input((istreambuf_iterator<char>(cin)), istreambuf_iterator<char>());
    JVal _root = JParser(_input).parse();

__DECLS__
__INVOKE__
    // 约定：上面注入的那段代码负责产出一个**已经序列化好的** _OUT 字符串。
    // 不要在 main 里再 jout 一次 —— 那样字符串会被再套一层引号。
    cout << AZTO_MARK << _OUT << endl;
    cout << AZTO_MEM << azto_peak_kb() << endl;
    return 0;
}
'''

#: 放在用户代码之后的 main（类设计题）。
#: C++ 没有反射，没法在运行时按名字调方法，所以把每个用例的操作序列直接展开成代码，
#: 用 argv[1] 选跑哪一段 —— 这样仍然只编译一次。
CPP_MAIN_OPERATIONS = r'''
// ============================================================
//  ===== 你 的 代 码 到 此 结 束 =====
// ============================================================
__MEM_FN__

static string run_case(int _idx) {
    string _res = "[";
__BODY__
    _res += "]";
    return _res;
}

int main(int argc, char **argv) {
    ios::sync_with_stdio(false);
    int _idx = (argc > 1) ? atoi(argv[1]) : 0;
    cout << AZTO_MARK << run_case(_idx) << endl;
    cout << AZTO_MEM << azto_peak_kb() << endl;
    return 0;
}
'''


def line_offset() -> int:
    """
    用户代码在生成文件里的起始行号（1-based）。

    编译报错给的是"生成文件"的行号，减掉这个偏移才是用户代码的行号 ——
    不减的话学员会看到 "solution.cpp:240: 错误"，而他的代码只有 12 行，
    完全不知道去改哪里。
    """
    return CPP_PRELUDE.count("\n") + 1


# ---------------------------------------------------------------------------
# 自检：真的编译一次、跑一次，确认运行时是可用的
# ---------------------------------------------------------------------------

SELFTEST_SOURCE = r'''
class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        unordered_map<int, int> seen;
        for (int i = 0; i < (int)nums.size(); i++) {
            auto it = seen.find(target - nums[i]);
            if (it != seen.end()) return {it->second, i};
            seen[nums[i]] = i;
        }
        return {};
    }
};
'''


def selftest(workdir: str) -> int:
    """编译并运行一次内置样例，验证 CPP_PRELUDE 是可用的。"""
    import json
    import os
    import subprocess

    # 用 __占位符__ 替换而不是 str.format —— 模板里全是 C++ 的花括号，
    # format 会把它们当占位符，直接 KeyError。
    tail = CPP_MAIN_FUNCTION.replace("__MEM_FN__", CPP_MEM_FN).replace(
        "__DECLS__",
        '    vector<int> _a0 = toIntVec(_root["nums"]);\n'
        '    int _a1 = toInt(_root["target"]);').replace(
        "__INVOKE__",
        "    Solution _sol;\n"
        "    vector<int> _result = _sol.twoSum(_a0, _a1);\n"
        "    string _OUT = jout(_result);")
    source = CPP_PRELUDE + SELFTEST_SOURCE + tail

    src_path = os.path.join(workdir, "selftest.cpp")
    exe_path = os.path.join(workdir, "selftest.exe" if os.name == "nt" else "selftest")
    with open(src_path, "w", encoding="utf-8") as handle:
        handle.write(source)

    flags = CPP_COMPILE_FLAGS + ["-o", exe_path, src_path] + CPP_LINK_FLAGS
    compile_result = subprocess.run(["g++"] + flags, capture_output=True, text=True)
    if compile_result.returncode != 0:
        print("编译失败：")
        print(compile_result.stderr[:3000])
        return 1
    print("编译通过（用户代码起始行 %d）" % line_offset())

    cases = [
        ({"nums": [2, 7, 11, 15], "target": 9}, [0, 1]),
        ({"nums": [3, 3], "target": 6}, [0, 1]),
        ({"nums": [-3, 4, 3, 90], "target": 0}, [0, 2]),
    ]
    failures = 0
    for payload, expected in cases:
        run = subprocess.run([exe_path], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        stdout = run.stdout
        marker = stdout.rfind(AZTO_MARK_PY)
        actual = None
        if marker >= 0:
            rest = stdout[marker + len(AZTO_MARK_PY):]
            actual = json.loads(rest.split("\n")[0])
        mem_marker = stdout.rfind("AZTO_MEM")
        peak = stdout[mem_marker + len("AZTO_MEM"):].strip() if mem_marker >= 0 else "?"
        ok = actual == expected
        print("  %-46s -> %-14s %-8s 峰值 %s KB" % (
            json.dumps(payload, ensure_ascii=False), json.dumps(actual),
            "OK" if ok else "FAIL 期望 " + json.dumps(expected), peak))
        if not ok:
            failures += 1
    return 1 if failures else 0


AZTO_MARK_PY = "AZTO_RESULT"

#: 编译标志。
#: -O2 是必要的：用 -O0 编译时，复杂度达标的解法可能因为常数因子被判超时，
#: 那对学员不公平（力扣自己也开优化）。
CPP_COMPILE_FLAGS = ["-std=c++17", "-O2", "-pipe"]

#: 链接库，必须放在**源文件之后** —— GCC 的链接顺序敏感，
#: 放前面就是 undefined reference to `GetProcessMemoryInfo'。
#: 只在 Windows 需要（psapi 不在默认链接的库里）。
CPP_LINK_FLAGS = ["-lpsapi"] if os.name == "nt" else []


if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="azto_cpp_selftest_") as tmp:
        sys.exit(selftest(tmp))
