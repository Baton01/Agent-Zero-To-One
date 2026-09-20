# 230. 二叉搜索树中第 K 小的元素 (Medium)

> **专题归类：** [[05-算法面试/02-专题总结/05-二叉树]]
> **LeetCode 链接：** https://leetcode.cn/problems/kth-smallest-element-in-a-bst/

---

## 题目描述

给定一个二叉搜索树的根节点 `root`，和一个整数 `k`，请你设计一个算法查找其中第 `k` 小的元素（从 1 开始计数）。

**示例 1：**
```
输入：root = [3,1,4,null,2], k = 1
输出：1
```

**示例 2：**
```
输入：root = [5,3,6,2,4,null,null,1], k = 3
输出：3
```

**提示：**
- 树中的节点数为 `n`
- `1 <= k <= n <= 10^4`
- `0 <= Node.val <= 10^4`

**进阶：** 如果二叉搜索树经常被修改（插入/删除操作）并且你需要频繁地查找第 k 小的值，你将如何优化算法？

---

## 🧩 题目详细分析

- **数据范围含义：** 最多 10000 节点，k 保证有效（1 ≤ k ≤ n），所以不需要处理 k 越界的情况。值范围 [0, 10^4]。
- **输入输出特征：** 输入根节点和整数 k，输出第 k 小的节点值（不是节点本身）。
- **边界条件：** k = 1 时返回最小值（最左叶子）；k = n 时返回最大值（最右叶子）。
- **核心约束：** 充分利用 BST 性质——中序遍历有序。不能简单用暴力全部遍历再排序（虽然可行但低效）。
- **隐藏条件：** 进阶问题考察的是能否想到用「左子树大小」来优化，支持频繁增删查第 k 小（类似树状数组/线段树思路）。

---

## 👶 小白版直白理解

就像在一个已经按身高排好队的人群中找第 k 矮的人。由于队伍已经排好了（BST 的中序就是从小到大），你只需要从左到右数到第 k 个就行。不需要重新排序，也不需要把所有人的身高都看一遍——数到第 k 个就可以停了。

---

## 💡 解题思路

### 思路一：中序遍历 + 计数器（推荐）

**核心思想：** BST 的中序遍历是递增序列，对 BST 做中序遍历，访问的第 k 个节点就是第 k 小的元素。

**为什么用中序？** 因为 BST 最核心的性质就是中序遍历结果递增。这个性质直接决定了「中序遍历的第 k 个节点 = 第 k 小元素」。不需要比较大小，不需要额外排序。

```python
def kthSmallest(root, k):
    count = 0
    result = 0

    def inorder(node):
        nonlocal count, result
        if not node:
            return

        inorder(node.left)          # 左：先遍历左子树（更小的元素）

        count += 1                   # 根：访问当前节点，计数器 +1
        if count == k:
            result = node.val
            return                   # 找到后提前返回

        inorder(node.right)         # 右：遍历右子树

    inorder(root)
    return result
```

### 思路二：中序遍历迭代法（提前终止）

用栈模拟中序遍历，可以随时暂停和继续。找到第 k 个元素后立即停止，不需要继续遍历剩余节点。

**优势：** 相比递归，迭代可以精确控制「什么时候停」，避免不必要的递归调用。

```python
def kthSmallest(root, k):
    stack = []
    cur = root

    while cur or stack:
        # 一路向左
        while cur:
            stack.append(cur)
            cur = cur.left

        cur = stack.pop()
        k -= 1
        if k == 0:
            return cur.val  # 找到第 k 小的元素

        cur = cur.right
```

### 思路三：记录左子树大小（进阶解法）

**核心思想：** 每个节点记录其左子树的节点数 `leftSize`。查找第 k 小元素时：
- 如果 `k <= leftSize`：第 k 小在左子树中
- 如果 `k == leftSize + 1`：当前节点就是第 k 小
- 如果 `k > leftSize + 1`：第 k 小在右子树中，且是右子树的第 `k - leftSize - 1` 小

**适用场景：** 当 BST 频繁被修改且需要多次查询第 k 小时，可以在 O(log n) 时间内完成查找。

```python
class TreeNodeWithSize:
    def __init__(self, val):
        self.val = val
        self.left = None
        self.right = None
        self.left_size = 0  # 左子树节点数

def kthSmallest(root, k):
    """假设树节点已经维护了 left_size 属性"""
    cur = root
    while cur:
        if k <= cur.left_size:
            cur = cur.left
        elif k == cur.left_size + 1:
            return cur.val
        else:
            k -= cur.left_size + 1
            cur = cur.right
    return -1
```

---

## ⚠️ 易错点

- **计数器 +1 的位置：** 计数器必须在左子树遍历完之后、右子树遍历之前加 1。因为中序的顺序是左→根→右，当前节点是第几个访问的由左子树的节点数决定。
- **找到后不停止：** 递归法在找到 result 后，递归调用仍在进行。最好在赋值后设置一个标志位（或像上面的代码在 count==k 时 return），避免不必要的继续遍历。
- **k 是从 1 开始计数的：** k=1 表示第一个最小的元素，即 BST 的最左节点值。
- **迭代法 `k -= 1` 的位置：** 必须在弹出栈顶（访问节点）时减 1，而不是入栈时减 1。

---

## 🔧 框架提炼

**中序遍历 + 计数器模板：** 利用有序性查找第 k 个元素的标准模板。

```python
def kthElement(root, k):
    count = 0
    result = None

    def inorder(node):
        nonlocal count, result
        if not node or result is not None:
            return
        inorder(node.left)
        count += 1
        if count == k:
            result = node.val
            return
        inorder(node.right)

    inorder(root)
    return result
```

**BST 中提前终止的迭代模板：**

```python
def inorder_iterative(root, k):
    stack, cur = [], root
    while cur or stack:
        while cur:
            stack.append(cur)
            cur = cur.left
        cur = stack.pop()
        # 处理当前节点
        if some_condition(cur):
            return cur.val
        cur = cur.right
```

---

## 🔗 关联题目

- [[94-二叉树的中序遍历]] — 中序遍历是本题的基础，必须熟练掌握中序的递归和迭代写法。
- [[98-验证二叉搜索树]] — 同样利用 BST 中序递增的性质，但 98 题是要验证整个序列，本题是取第 k 个值。
- [[215-数组中的第K个最大元素]] — 在无序数组中找第 k 大，思路不同（快速选择/堆），可以做对比学习。
