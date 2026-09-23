# 一键启动器（Windows exe）

这个目录里是给 [GitHub Releases](https://github.com/Baton01/Agent-Zero-To-One/releases) 用的
启动器与打包脚本。**学习这个项目不需要看这里** —— 它只跟"把项目变成一个双击就能用的东西"有关。

```
launcher/
├── launcher_win.c       # Windows 启动器（C，编成 exe 的就是它）
├── launcher.py          # 跨平台启动器（macOS / Linux 用这个，功能一致）
├── build_exe.py         # 编译 Windows exe（gcc）
├── package_release.py   # 把 exe + 项目内容打成发布 zip
├── dist/                # 产物：AgentZeroToOne.exe（不入库）
└── release/             # 产物：发布 zip（不入库）
```

## 它解决什么问题

不用启动器的话，想用「在线判题」得这么干：

```bash
cd 05-算法面试/06-在线判题
python judge_server.py
# 然后自己回到浏览器打开网页 → 点「在线判题」
```

启动器把这三步合成一次双击：找 Python → 起服务 → 打开浏览器。

## 关于 Python 依赖：这个 exe 不能省掉它

一个自然的想法是"把 Python 打包进 exe，用户就不用装了"。**这里做不到，也不该这么做**：

判题器要执行用户提交的代码，做法是起一个真实的 python 子进程去跑 `driver.py`
（`languages.py` 的 `run_case`）。如果把它冻进 exe，`sys.executable` 就变成 exe 自己，
判 Python 题会直接坏掉。

而且 `code/` 下 17 章脚本本来就是给读者自己跑、自己改的 —— 这个项目的价值有一半在源码里，
把它藏进 exe 反而是倒退。

所以启动器的定位很明确：**消除"开终端敲命令"，不消除 Python**。机器上没装 Python 时，
它会说清楚怎么装，并且照样把网页版打开（网页版确实不需要 Python）。

C++ 判题还需要 `g++`（MinGW-w64）或 `clang++`，这个是外部的，同样打不进去。

## 为什么是 C，不是 PyInstaller

**最早这版是用 PyInstaller 把 `launcher.py` 冻成 exe 的，结果在装了 360 安全卫士的机器上
直接被隔离**：双击后先提示"拒绝访问"，随后 exe 从磁盘消失。

这不是个例。PyInstaller 的引导器（bootloader）是杀软启发式规则的常客，而未签名的 exe
对国产杀软尤其敏感 —— 而这个项目的读者大多在国内，等于一半人拿到手就用不了。

对照实验：

| 方式 | 体积 | 360 的行为 |
|---|---|---|
| PyInstaller 冻结 Python | 13.9 MB | 双击即隔离，文件被删 |
| gcc 编译的原生 C | 73 KB | 正常运行，不被碰 |

于是换成原生实现。顺带三个好处：体积降到 1/190、启动更快（不用解压运行时）、
也不需要构建机装 PyInstaller。

## 怎么构建

```bash
python launcher/build_exe.py --clean      # 产出 launcher/dist/AgentZeroToOne.exe
python launcher/package_release.py 1.0.0  # 产出 launcher/release/*.zip
```

编译只需要 gcc；这个项目判 C++ 题本来就需要 g++，所以依赖通常已经在了。

## 行为细节

- **找不到项目根就向上找**：exe 在 `launcher/dist/` 下时离项目根隔了两层，
  直接双击那里那个 exe 也能工作（按 `web/index.html` 是否存在来判断）。
- **失败时会停住等回车**：双击启动的 exe 独占一个控制台，进程一退出窗口就没了，
  用户只看到"闪退"。所以非正常退出时会暂停，把原因留在屏幕上
  （从已有终端里运行时不暂停，否则会把人卡住）。
- **端口被占**：如果那个端口上已经有判题服务在跑，就直接复用它，不再起一个。
- **中文输出**：控制台切到 UTF-8（`SetConsoleOutputCP`），子进程也带
  `PYTHONIOENCODING=utf-8`，否则中文和框线字符在 cp936 下会乱码。

跨平台用 `python launcher/launcher.py`，参数和行为与 exe 一致
（`--port` / `--no-browser` / `--web-only`）。
