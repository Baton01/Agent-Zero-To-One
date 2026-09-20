"""
多语言判题回归测试。

为什么需要它
------------
`harness.py --selfcheck` 只能验证 Python —— 因为每道题存的参考解法都是 Python。
但 C++ 走的是完全不同的一条路（生成源码 → 编译 → 二进制读 JSON → 输出 JSON），
那条路上的任何一个环节坏了，Python 自检是发现不了的。

所以这个文件用**真实的 C++ 解法**去跑，覆盖每一种数据类型和每一种题型：

    基本类型 / 字符串 / 链表 / 二叉树 / 字符网格 / 整数矩阵
    / 原地修改 / 解集顺序不固定 / 类设计题 / 编译错误 / 运行时错误 / 超时

跑法：
    cd 05-算法面试/06-在线判题
    python test_multilang.py              # 全部
    python test_multilang.py -v           # 打印每题的逐用例结果

编译器没装时，C++ 部分会自动跳过并明确说明 —— 不算失败。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from harness import VERDICT_LABEL, load_testcases, run_all  # noqa: E402
from languages import language_status  # noqa: E402

# ---------------------------------------------------------------------------
# 各题型的 C++ 解法。每一份都应该是正确解 —— 用它来证明"这个题型判得通"。
# ---------------------------------------------------------------------------

CPP_SOLUTIONS = {
    # 整数数组 + 整数
    "1-两数之和": '''class Solution {
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
''',
    # 字符串
    "20-有效的括号": '''class Solution {
public:
    bool isValid(string s) {
        stack<char> st;
        unordered_map<char, char> pairs = {{')', '('}, {']', '['}, {'}', '{'}};
        for (char c : s) {
            if (pairs.count(c)) {
                if (st.empty() || st.top() != pairs[c]) return false;
                st.pop();
            } else {
                st.push(c);
            }
        }
        return st.empty();
    }
};
''',
    # 链表
    "206-反转链表": '''class Solution {
public:
    ListNode* reverseList(ListNode* head) {
        ListNode* pre = nullptr;
        while (head) { ListNode* nxt = head->next; head->next = pre; pre = head; head = nxt; }
        return pre;
    }
};
''',
    # 二叉树
    "104-二叉树的最大深度": '''class Solution {
public:
    int maxDepth(TreeNode* root) {
        if (!root) return 0;
        return 1 + max(maxDepth(root->left), maxDepth(root->right));
    }
};
''',
    # 字符网格
    "200-岛屿数量": '''class Solution {
public:
    int numIslands(vector<vector<char>>& grid) {
        if (grid.empty()) return 0;
        int n = grid.size(), m = grid[0].size(), count = 0;
        function<void(int, int)> dfs = [&](int i, int j) {
            if (i < 0 || j < 0 || i >= n || j >= m || grid[i][j] != '1') return;
            grid[i][j] = '0';
            dfs(i + 1, j); dfs(i - 1, j); dfs(i, j + 1); dfs(i, j - 1);
        };
        for (int i = 0; i < n; i++)
            for (int j = 0; j < m; j++)
                if (grid[i][j] == '1') { count++; dfs(i, j); }
        return count;
    }
};
''',
    # 整数矩阵 + 原地修改（void 返回，判的是被改动的参数）
    "73-矩阵置零": '''class Solution {
public:
    void setZeroes(vector<vector<int>>& g) {
        int n = g.size(), m = g[0].size();
        bool fr = false, fc = false;
        for (int j = 0; j < m; j++) if (g[0][j] == 0) fr = true;
        for (int i = 0; i < n; i++) if (g[i][0] == 0) fc = true;
        for (int i = 1; i < n; i++)
            for (int j = 1; j < m; j++)
                if (g[i][j] == 0) { g[i][0] = 0; g[0][j] = 0; }
        for (int i = 1; i < n; i++)
            for (int j = 1; j < m; j++)
                if (g[i][0] == 0 || g[0][j] == 0) g[i][j] = 0;
        if (fr) for (int j = 0; j < m; j++) g[0][j] = 0;
        if (fc) for (int i = 0; i < n; i++) g[i][0] = 0;
    }
};
''',
    # 原地修改 + 数组
    "75-颜色分类": '''class Solution {
public:
    void sortColors(vector<int>& nums) {
        int l = 0, r = nums.size() - 1, i = 0;
        while (i <= r) {
            if (nums[i] == 0) swap(nums[i++], nums[l++]);
            else if (nums[i] == 2) swap(nums[i], nums[r--]);
            else i++;
        }
    }
};
''',
    # 解集顺序不固定（compare: unordered）
    "15-三数之和": '''class Solution {
public:
    vector<vector<int>> threeSum(vector<int>& nums) {
        sort(nums.begin(), nums.end());
        vector<vector<int>> res;
        int n = nums.size();
        for (int i = 0; i + 2 < n; i++) {
            if (i && nums[i] == nums[i - 1]) continue;
            int l = i + 1, r = n - 1;
            while (l < r) {
                int s = nums[i] + nums[l] + nums[r];
                if (s == 0) {
                    res.push_back({nums[i], nums[l], nums[r]});
                    while (l < r && nums[l] == nums[l + 1]) l++;
                    while (l < r && nums[r] == nums[r - 1]) r--;
                    l++; r--;
                } else if (s < 0) l++; else r--;
            }
        }
        return res;
    }
};
''',
    # 类设计题：带参数的构造函数
    "146-LRU缓存": '''class LRUCache {
    int cap;
    list<pair<int, int>> items;
    unordered_map<int, list<pair<int, int>>::iterator> pos;
public:
    LRUCache(int capacity) : cap(capacity) {}
    int get(int key) {
        auto it = pos.find(key);
        if (it == pos.end()) return -1;
        items.splice(items.begin(), items, it->second);
        return it->second->second;
    }
    void put(int key, int value) {
        auto it = pos.find(key);
        if (it != pos.end()) {
            it->second->second = value;
            items.splice(items.begin(), items, it->second);
            return;
        }
        items.push_front({key, value});
        pos[key] = items.begin();
        if ((int)items.size() > cap) { pos.erase(items.back().first); items.pop_back(); }
    }
};
''',
    # 类设计题：无参构造（曾经因为 most vexing parse 编译不过）
    "208-实现Trie": '''class Trie {
    struct Node { int ch[26]; bool end; Node() { memset(ch, -1, sizeof ch); end = false; } };
    vector<Node> t;
public:
    Trie() { t.push_back(Node()); }
    void insert(string w) {
        int c = 0;
        for (char x : w) {
            int k = x - 'a';
            if (t[c].ch[k] == -1) { t[c].ch[k] = t.size(); t.push_back(Node()); }
            c = t[c].ch[k];
        }
        t[c].end = true;
    }
    bool search(string w) {
        int c = 0;
        for (char x : w) { int k = x - 'a'; if (t[c].ch[k] == -1) return false; c = t[c].ch[k]; }
        return t[c].end;
    }
    bool startsWith(string p) {
        int c = 0;
        for (char x : p) { int k = x - 'a'; if (t[c].ch[k] == -1) return false; c = t[c].ch[k]; }
        return true;
    }
};
''',
    # 类设计题：返回类型要按每个方法分别推（getMin/top 返回 int，push/pop 返回 void）
    "155-最小栈": '''class MinStack {
    stack<int> data, mins;
public:
    MinStack() {}
    void push(int val) { data.push(val); mins.push(mins.empty() ? val : min(val, mins.top())); }
    void pop() { data.pop(); mins.pop(); }
    int top() { return data.top(); }
    int getMin() { return mins.top(); }
};
''',
}

# ---------------------------------------------------------------------------
# 反向用例：这些**必须**被判为失败（绝不是"通过"），否则说明判题器在放水。
#
# 期望值写成"可接受的判决集合"而不是单个值，因为有两种情况没有唯一正确答案：
#   · 无限递归在 -O2 下可能被 GCC 优化成循环，于是表现为超时而不是爆栈
#   · "没原地改"的解可能在部分用例上恰好碰对（输入本来就有序），于是是部分通过
# 这两种都属于"判题器正确识别了失败"，不该因为判决名不同就算测试失败。
# ---------------------------------------------------------------------------

CPP_SHOULD_FAIL = {
    # 编译错误 —— 顺便验证行号映射到了用户代码
    "1-两数之和#语法错": ('''class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        return [0, 1];
    }
};
''', {"compile_error"}),

    # 答案错误
    "1-两数之和#答案错": ('''class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        return {0, 0};
    }
};
''', {"wrong_answer"}),

    # 段错误（空指针解引用）
    "1-两数之和#段错误": ('''class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        int* p = nullptr;
        *p = 1;
        return {0, 1};
    }
};
''', {"runtime_error"}),

    # 死循环
    "1-两数之和#死循环": ('''class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        volatile long long x = 0;
        while (true) { x++; }
        return {};
    }
};
''', {"timeout"}),

    # 无限递归：爆栈或超时都算判对
    "1-两数之和#爆栈": ('''class Solution {
public:
    int boom(int n) { return boom(n + 1) + 1; }
    vector<int> twoSum(vector<int>& nums, int target) {
        return {0, boom(0)};
    }
};
''', {"runtime_error", "timeout"}),

    # 原地修改题：只排序副本、不碰参数 —— 应该判错（部分用例碰对就是"部分通过"）
    "75-颜色分类#没原地改": ('''class Solution {
public:
    void sortColors(vector<int>& nums) {
        vector<int> copy = nums;
        sort(copy.begin(), copy.end());
        // 故意不写回 nums
    }
};
''', {"wrong_answer", "partial"}),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="多语言判题回归测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印失败的用例详情")
    args = parser.parse_args()

    problems = load_testcases()
    if not problems:
        print("读不到题库：testcases.json")
        return 2

    status = language_status()
    languages = [name for name, info in status.items() if info["available"]]
    print("=" * 74)
    print("  多语言判题回归测试")
    print("=" * 74)
    for name, info in status.items():
        print("  %-10s %-5s %s" % (info["label"], "可用" if info["available"] else "跳过",
                                   info["version"] or info["reason"]))
    print("-" * 74)

    passed = failed = 0

    for language in languages:
        label = status[language]["label"]

        if language != "python":
            print("\n[%s] 正确解法应通过：" % label)
            for pid, code in CPP_SOLUTIONS.items():
                problem = problems.get(pid)
                if not problem:
                    print("  [跳过] %-22s 题库里没有这道题" % pid)
                    continue
                report = run_all(code, problem, language=language)
                ok = report["verdict"] == "accepted"
                passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
                print("  [%s] %-22s %d/%d  %s  %.1f MB" % (
                    "通过" if ok else "失败", pid, report["passed"], report["total"],
                    ("%d ms" % report["ms"]).ljust(8), (report.get("memoryKB") or 0) / 1024))
                if not ok and args.verbose:
                    for item in report["results"]:
                        if item["status"] != "pass":
                            print("        用例%d %s 得到 %s 期望 %s %s" % (
                                item["index"], item["status"], item.get("actual"),
                                item.get("expected"), (item.get("error") or "")[:120]))

            print("\n[%s] 错误解法应被判失败：" % label)
            for name, (code, expected) in CPP_SHOULD_FAIL.items():
                pid = name.split("#")[0]
                problem = problems.get(pid)
                if not problem:
                    continue
                acceptable = {expected} if isinstance(expected, str) else set(expected)
                report = run_all(code, problem, language=language, timeout=5.0)
                ok = report["verdict"] in acceptable
                passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
                print("  [%s] %-24s 判为 %-12s（可接受：%s）" % (
                    "通过" if ok else "失败", name,
                    VERDICT_LABEL.get(report["verdict"], report["verdict"]),
                    "、".join(VERDICT_LABEL.get(v, v) for v in sorted(acceptable))))
                if "compile_error" in acceptable and args.verbose:
                    print("        编译错误映射：%s" % (report.get("compileError") or "")[:200])

    print()
    print("=" * 74)
    print("  结果：%d 项通过，%d 项失败" % (passed, failed))
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
