"""
Agent Zero To One · 一键启动器

双击 AgentZeroToOne.exe 之后做三件事：

  1. 找到一个可用的 Python 解释器
  2. 用它启动本地判题服务（05-算法面试/06-在线判题/judge_server.py）
  3. 打开浏览器到网页版的「在线判题」页

**为什么这个 exe 不把 Python 打包进去？**
    判题器要执行用户提交的 Python 代码，做法是起一个真实的 python 子进程跑
    driver.py（见 languages.py 的 run_case）。如果把它冻进 exe，sys.executable
    就变成 exe 自己，判 Python 题会直接坏掉。而且 `code/` 下 17 章脚本本来就
    要读者自己跑 —— 这个项目离不开 Python，假装不需要反而更糟。

    所以这个启动器只消除「开终端、cd 目录、敲命令」这一步，不消除 Python 依赖。
    机器上没装 Python 时它会说清楚，并且照样把网页版打开（网页版不需要 Python）。

**判断 Python 可用性**：不能只看有没有 python.exe —— Windows 自带一个微软商店的
    占位程序，执行它会弹应用商店。所以要真的跑一次 --version 看输出。

用法：
    AgentZeroToOne.exe                  # 起服务 + 开浏览器
    AgentZeroToOne.exe --no-browser     # 只起服务（服务器上跑）
    AgentZeroToOne.exe --port 8912      # 换端口
    AgentZeroToOne.exe --web-only       # 只开网页，不起判题服务
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
import webbrowser

#: 判题服务相对项目根的路径
JUDGE_DIR = os.path.join("05-算法面试", "06-在线判题")
SERVER_SCRIPT = "judge_server.py"

DEFAULT_PORT = 8900
MIN_PYTHON = (3, 8)

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------

def _find_root(start: str):
    """
    从 start 开始向上找项目根（以 web/index.html 为标志），找不到返回 None。

    为什么要向上找：开发布局里 exe 在 launcher/dist/AgentZeroToOne/，
    离项目根隔了三层，而用户很自然会去双击那里那个 exe。
    不找的话它只会打印一句"目录不对"然后退出 —— 看起来就是闪退。
    """
    current = os.path.abspath(start)
    for _ in range(6):
        if os.path.isfile(os.path.join(current, "web", "index.html")):
            return current
        parent = os.path.dirname(current)
        if parent == current:          # 到盘符根了
            break
        current = parent
    return None


def app_root() -> str:
    """
    项目根目录。

    两种运行形态：
      - 打包后：exe 就在项目根（发布包里 exe 和文档、code/ 平级）
      - 源码里：本文件在 launcher/ 下，root = 上一层
    再退一步：都不对就从 exe 所在目录向上找 web/index.html。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _find_root(base) or base


def hold_console():
    """
    失败时停住等回车，否则双击运行的用户什么都看不见。

    双击启动的 exe 拥有一个新建的控制台，进程一退出窗口立刻消失 ——
    用户只看到"闪退"，而真正的原因（目录不对 / 没装 Python / 端口占用）
    一闪而过。所以非正常退出时要把消息留在屏幕上。

    只在打包运行 + 真有控制台（stdin 是终端）时才停：
    从命令行带重定向跑、或被别的程序调用时，停住会把人卡死。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except (ValueError, OSError):
        return
    try:
        input("\n按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


def web_entry(root: str) -> str:
    return os.path.join(root, "web", "index.html")


# ---------------------------------------------------------------------------
# 找 Python
# ---------------------------------------------------------------------------

def _candidates():
    """按优先级列出可能的 Python 可执行文件（只列，不验证）"""
    import shutil

    found = []
    seen = set()

    def add(path):
        if not path or not os.path.isfile(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    # 1) PATH 上的 python / python3
    for name in ("python", "python3"):
        add(shutil.which(name))

    # 2) py launcher（Windows 官方安装器会装，能选到最新的 3.x）
    add(shutil.which("py"))

    # 3) 常见安装位置（PATH 没配的时候还能找到）
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        roots = [
            os.path.join(local, "Programs", "Python"),
            os.environ.get("ProgramFiles", "C:\\Program Files"),
            os.environ.get("ProgramFiles(x86)", ""),
            "C:\\",
        ]
        for base in roots:
            if not base or not os.path.isdir(base):
                continue
            try:
                # 目录名倒序：Python312 排在 Python39 前面，优先选新版本
                names = sorted(os.listdir(base), reverse=True)
            except OSError:
                continue
            for name in names:
                if name.lower().startswith("python"):
                    add(os.path.join(base, name, "python.exe"))
        add(os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "py.exe"))
    else:
        for path in ("/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
            add(path)

    return found


def _probe(exe: str):
    """
    真的执行一次 --version。返回 (major, minor) 或 None。

    为什么不能只看文件存在：Windows 的 %LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe
    是个占位程序，执行它会弹微软应用商店。只有能正常打印版本号的才算数。
    """
    try:
        completed = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15,
            # 别继承 stdin：某些情况下 python 会尝试读输入
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None

    out = (completed.stdout or "") + (completed.stderr or "")
    out = out.strip()
    if not out.lower().startswith("python"):
        return None
    digits = out.split()[1].split(".") if len(out.split()) > 1 else []
    if len(digits) < 2:
        return None
    try:
        return int(digits[0]), int(digits[1])
    except ValueError:
        return None


def find_python():
    """
    返回 (可执行文件, 版本说明, 用于启动服务的参数前缀)。

    `py.exe` 是多版本启动器，要多带一个 `-3` 明确要 Python 3；
    别的解释器直接跟脚本路径。注意别用 startswith("py") 来判断 ——
    `python.exe` 也以 "py" 开头，会被误当成启动器。
    """
    best = None
    for exe in _candidates():
        version = _probe(exe)
        if version is None or version < MIN_PYTHON:
            continue
        # 优先选真正的 python.exe；py.exe 作为兜底（它启动时多一层间接）
        is_launcher = os.path.basename(exe).lower() in ("py", "py.exe")
        score = 1 if is_launcher else 0
        if best is None or score < best[0]:
            prefix = [exe, "-3"] if is_launcher else [exe]
            label = "Python %d.%d（%s）" % (version[0], version[1], exe)
            best = (score, prefix, label)
    if best is None:
        return None, None
    return best[1], best[2]


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

def server_alive(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """判题服务在不在（直接请求 /status，比看端口更可靠）"""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
                "http://%s:%d/status" % (host, port), timeout=timeout) as response:
            json.loads(response.read().decode("utf-8"))
            return True
    except (urllib.error.URLError, ValueError, OSError):
        return False


def start_server(python_prefix, root, port):
    """起判题服务，返回 Popen（stdout/stderr 直接接到当前控制台，出错看得见）"""
    judge_dir = os.path.join(root, JUDGE_DIR)
    script = os.path.join(judge_dir, SERVER_SCRIPT)
    if not os.path.isfile(script):
        print("[!] 找不到判题服务：%s" % script)
        return None

    env = dict(os.environ)
    # 子进程的中文输出按 UTF-8 编码；控制台编码由 setup_console() 统一成 65001
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    command = list(python_prefix) + [SERVER_SCRIPT, "--port", str(port)]
    print("[*] 启动判题服务：%s" % " ".join(command))
    print("    工作目录：%s" % judge_dir)
    print()
    try:
        return subprocess.Popen(command, cwd=judge_dir, env=env)
    except OSError as exc:
        print("[!] 启动失败：%s" % exc)
        return None


def wait_for_server(port, process, seconds=25.0):
    """等服务起来。中途进程死了就直接返回 False，别干等。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if server_alive(port):
            return True
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# 控制台
# ---------------------------------------------------------------------------

def setup_console():
    """
    统一成 UTF-8。

    Windows 控制台默认是 cp936（GBK），而项目里到处是中文和 `─` 这类字符。
    编码不统一会打出乱码，甚至直接 UnicodeEncodeError 崩掉。
    """
    if IS_WINDOWS:
        try:
            os.system("chcp 65001 >nul 2>&1")
        except OSError:
            pass
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                # line_buffering：输出被重定向到文件时也要能立刻看到，
                # 否则起服务那几行会卡在缓冲区里，直到进程退出才出现
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (ValueError, OSError):
                pass


def banner():
    print("=" * 68)
    print("  Agent Zero To One · 一键启动")
    print("=" * 68)


def print_no_python(root):
    print()
    print("-" * 68)
    print("  没找到可用的 Python")
    print("-" * 68)
    print()
    print("  网页版（17 章教程 + 算法题解 + 模拟面试 + 项目实战案例）不需要 Python，")
    print("  已经给你打开了。但这两件事需要它：")
    print()
    print("    · 跑每章的配套代码（code/ 下 17 个脚本）")
    print("    · 在线判题（要起子进程跑你提交的代码）")
    print()
    print("  装一个 Python 3.9 或更新版本，再双击本程序即可：")
    print("    https://www.python.org/downloads/")
    print()
    print("  安装时记得勾上「Add Python to PATH」。")
    print("  装完不用重启，直接再双击一次就行。")
    print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    setup_console()

    parser = argparse.ArgumentParser(
        description="Agent Zero To One 一键启动器（起判题服务 + 打开网页版）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="判题服务端口，默认 %d" % DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    parser.add_argument("--web-only", action="store_true",
                        help="只打开网页版，不启动判题服务")
    args = parser.parse_args()

    root = app_root()
    banner()

    if not os.path.isfile(web_entry(root)):
        search_from = (os.path.dirname(os.path.abspath(sys.executable))
                       if getattr(sys, "frozen", False) else root)
        print()
        print("[!] 没找到项目文件（web/index.html）。")
        print()
        print("    从这里开始向上找了 6 层：%s" % search_from)
        print()
        print("    这个 exe 要和 web/、docs/、code/ 放在一起（也就是项目根目录）。")
        print("    用 GitHub Releases 下载的压缩包的话，解压后直接双击里面的 exe 就行；")
        print("    自己构建的话，跑 launcher/package_release.py 打成发布包再运行。")
        return 2

    # 网页版：本地文件直接打开，不需要任何服务
    page = web_entry(root)
    index_url = "file:///" + page.replace("\\", "/")

    if args.web_only:
        print()
        print("[*] 只打开网页版：%s" % index_url)
        if not args.no_browser:
            webbrowser.open(index_url)
        return 0

    # 判题服务
    python_prefix, python_label = find_python()
    port = args.port

    if server_alive(port):
        print()
        print("[=] 端口 %d 上已经有一个判题服务在跑，直接用。" % port)
        server = None
        ready = True
    elif python_prefix is None:
        print_no_python(root)
        print("[*] 只打开网页版：")
        print("    %s" % index_url)
        if not args.no_browser:
            webbrowser.open(index_url)
        print()
        print("  想退出，关掉这个窗口就行。")
        return 1
    else:
        print("[*] %s" % python_label)
        print()
        server = start_server(python_prefix, root, port)
        if server is None:
            return 2
        ready = wait_for_server(port, server)

    # 结果
    judge_url = index_url + "#/judge"
    if ready:
        print()
        print("=" * 68)
        print("  就绪")
        print("=" * 68)
        print()
        print("  判题服务    http://127.0.0.1:%d" % port)
        print("  网页版      %s" % index_url)
        print()
        print("  在网页里点左侧「面试准备 → 在线判题」开始做题。")
        print("  关闭这个窗口就会停掉判题服务。")
        print()
        if not args.no_browser:
            webbrowser.open(judge_url)
    else:
        print()
        print("[!] 判题服务没起来。可能的原因：")
        print("    · 端口 %d 被别的程序占了 —— 换个端口：--port 8912" % port)
        print("    · Python 环境有问题 —— 手动跑一次看报错：")
        print("      cd \"%s\"" % os.path.join(root, JUDGE_DIR))
        print("      python %s" % SERVER_SCRIPT)
        print()
        print("  网页版仍然可以打开（不需要判题服务）：")
        print("    %s" % index_url)
        if not args.no_browser:
            webbrowser.open(index_url)

    return keep_running(server)


def keep_running(server):
    """
    守在这里，让 Ctrl+C 和关窗口都能干净地停掉子进程。

    服务是别人起的（server is None）就直接退出：没必要再占一个窗口，
    用户想停那个服务得去它自己的窗口按 Ctrl+C。
    """
    if server is None:
        print("  这个窗口可以关掉了。")
        return 0

    try:
        print("  按 Ctrl+C 退出。")
        while True:
            if server.poll() is not None:
                print()
                print("[!] 判题服务自己退出了（退出码 %s）。" % server.returncode)
                return server.returncode or 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("[*] 正在停止判题服务…")
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
        print("[*] 已停止。")


if __name__ == "__main__":
    # 退出码非 0 时要停一下再关窗口，否则双击运行的用户只看到"闪退"，
    # 真正的原因（目录不对 / 没装 Python / 端口占用）根本来不及看。
    exit_code = 1
    try:
        exit_code = main()
    except KeyboardInterrupt:
        exit_code = 130
    except Exception:                                  # noqa: BLE001 - 兜底，别静默退出
        traceback.print_exc()
        print()
        print("[!] 启动器自己出错了。把上面的信息发到项目的 Issues 里，谢谢。")
        exit_code = 1

    if exit_code != 0:
        hold_console()
    sys.exit(exit_code)
