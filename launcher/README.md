# 一键启动器（Windows exe）

这个目录里是给 [GitHub Releases](https://github.com/Baton01/Agent-Zero-To-One/releases) 用的
打包脚本。**学习这个项目不需要看这里** —— 它只跟"把项目变成一个双击就能用的东西"有关。

```
launcher/
├── launcher.py          # 启动器本体（PyInstaller 的入口）
├── build_exe.py         # 把 launcher.py 打成 exe
├── package_release.py   # 把 exe + 项目内容打成发布 zip
└── release/             # 产物目录（不入库）
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
（`languages.py` 的 `run_case`）。如果把判题器也冻进 exe，`sys.executable` 就变成 exe 自己，
判 Python 题会直接坏掉。

而且 `code/` 下 17 章脚本本来就是给读者自己跑、自己改的 —— 这个项目的价值有一半在源码里，
把它藏进 exe 反而是倒退。

所以启动器的定位很明确：**消除"开终端敲命令"，不消除 Python**。机器上没装 Python 时，
它会说清楚怎么装，并且照样把网页版打开（网页版确实不需要 Python）。

C++ 判题还需要 `g++`（MinGW-w64）或 `clang++`，这个是外部的，同样打不进去。

## 怎么构建

```bash
pip install pyinstaller
python launcher/build_exe.py --clean     # 产出 launcher/dist/AgentZeroToOne/
python launcher/package_release.py 1.0.0 # 产出 launcher/release/*.zip
```

## 两个构建上的取舍

**为什么用 `--onedir` 而不是 `--onefile`？**
`onefile` 每次运行都要把整个运行时解压到临时目录，启动慢好几秒 —— 而这个程序的意义就是
"双击后赶紧能用"，慢掉就白做了。另外 `onefile` 的自解压行为是杀软误报的重灾区。
代价是发布包必须带上 `_internal/` 目录，不能只发一个 exe。

**为什么用 `--console` 而不是 `--windowed`？**
判题服务会打印题库数、语言可用性、监听地址这些信息，出问题时用户得看得见。
`windowed` 模式下没有控制台，报错就静默消失了。
