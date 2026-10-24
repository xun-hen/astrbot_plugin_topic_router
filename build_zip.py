"""把插件打包成 AstrBot 能正确安装的 zip。

为什么不用 Windows 的 `Compress-Archive`：它会用反斜杠写条目名
（`astrbot_plugin_topic_router_pro\\detection.py`）。Linux 上 `os.path.normpath`
不把反斜杠当分隔符（它是合法文件名字符），于是解包后得不到预期的目录结构，
AstrBot 的收尾步骤 `_finalize_extracted_archive` 会在 `os.listdir(插件目录)` 上
抛 FileNotFoundError。这里用 zipfile 手工写正斜杠条目，顺便保证：

* 条目名一律用 `/`；
* 目录条目带结尾 `/`，让 `_resolve_archive_root_dir` 能识别出根目录；
* 递归带上 `aggregation/`（内建消息聚合）与 `pages/`（插件详情页）；
* 不包含 `tests/` 子目录 —— 有子目录时 update_dir 会退化成整个插件根目录，
  收尾步骤会把目录往它自己里面搬。

用法：
    python build_zip.py                  # 写到插件工作区的 dist/ 目录
    python build_zip.py <输出路径>        # 写到指定位置
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "astrbot_plugin_topic_router_pro"

# 根目录下必须进包的非 .py 文件；根目录 *.py 由 collect_entries() 自动发现。
# 为什么不再手写 .py 清单：聚合成包后新增模块很容易漏（漏了就是安装后 ImportError）。
REQUIRED_ROOT_FILES = (
    "metadata.yaml",
    "_conf_schema.json",
    "logo.png",
    "README.md",
    "CHANGELOG.md",
    "requirements.txt",
    "LICENSE",
)
EXCLUDE_ROOT_PY = {"build_zip.py"}
# 需要递归打进包里的子目录（页面、内建聚合模块、插件 i18n 都是功能的一部分）
INCLUDE_DIRS = ("aggregation", "pages", ".astrbot-plugin")
EXCLUDE_DIRS = {"__pycache__", "tests", ".git", "dist", "tools"}


def read_version() -> str:
    """从 metadata.yaml 读版本号，用于拼输出文件名。

    Returns:
        版本号，读不到时用 `dev`。
    """
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            return value.lstrip("v") or "dev"
    return "dev"


def default_output() -> Path:
    """默认输出路径：插件工作区内的 `dist/`。

    走过的坑都记在这，别再踩：

    * 放在 `F:\\deep seek` 根下或插件目录根下，zip 会被这个工作区的清理规则扫走
      （文件名以 `astrbot_` 开头、或就在插件目录里，实测都会消失）；
    * 放在插件目录根下还会让"重新打包"把上一个包一起压进去。

    所以固定放 `dist/`：它在 `.gitignore` 里、也被 `tests/run_tests.py` 的
    `ignore_patterns(..., "dist")` 排除，既不会被清理规则扫走，也不会被打进包里。
    需要换位置就直接在命令行传输出路径。

    Returns:
        输出路径。
    """
    return PLUGIN_DIR / "dist" / f"{PACKAGE_NAME}-{read_version()}.zip"


def check_version_consistency() -> list[str]:
    """检查三处版本信息是否一致。

    版本号曾经漂移过一次：CHANGELOG 写了新版、metadata.yaml 却忘了改，结果打出来的包
    文件名是新版、包里元数据还是旧版。打包前先自检，把这个坑堵掉。

    Returns:
        问题描述列表；空列表表示一致。
    """
    problems: list[str] = []
    version = read_version()

    meta_text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    if f"version: v{version}" not in meta_text and f"version: {version}" not in meta_text:
        problems.append(f"metadata.yaml 里的 version 不是 v{version}")

    changelog = PLUGIN_DIR / "CHANGELOG.md"
    if changelog.is_file():
        if f"## v{version}" not in changelog.read_text(encoding="utf-8"):
            problems.append(f"CHANGELOG.md 顶部没有 ## v{version} 小节")

    readme = PLUGIN_DIR / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        # 只挑"本插件的版本号"来比：README 里还会提到 AstrBot 自身的版本（如 v4.28.1），
        # 那些不能算不一致。约定写法是「话题路由器 vX.Y.Z」或「插件 vX.Y.Z」。
        for found in set(re.findall(r"(?:话题路由器|插件)\s*v(\d+\.\d+\.\d+)", text)):
            if found != version:
                problems.append(f"README.md 里出现了本插件的其它版本号 v{found}")
                break
    return problems


def check_logo() -> list[str]:
    """检查 logo.png 是否符合 AstrBot 的推荐规格。

    官方要求：插件目录下的 `logo.png`，长宽比 1:1，推荐 256x256。元数据里**没有**
    logo 字段，靠的就是这个文件名，所以规格错了市场卡片会很难看。

    Returns:
        问题描述列表；空列表表示合规。
    """
    path = PLUGIN_DIR / "logo.png"
    if not path.is_file():
        return ["缺少 logo.png（插件目录下 1:1 图片，推荐 256x256）"]
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        return ["logo.png 不是有效的 PNG 文件"]
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    problems = []
    if width != height:
        problems.append(f"logo.png 不是 1:1（当前 {width}x{height}）")
    if width != 256:
        problems.append(f"logo.png 建议 256x256（当前 {width}x{height}）")
    if path.stat().st_size > 512 * 1024:
        problems.append(f"logo.png 过大（{path.stat().st_size // 1024}KB），建议压到 512KB 内")
    return problems


def check_changelog_format() -> list[str]:
    """检查 CHANGELOG 的 Markdown 结构。

    出过一次错：新版本小节插到顶部时忘了留空行，标题和上一段正文贴在一起，
    Markdown 渲染出来就是一坨。这里把两个容易犯的错钉死。

    Returns:
        问题描述列表；空列表表示合规。
    """
    path = PLUGIN_DIR / "CHANGELOG.md"
    if not path.is_file():
        return ["缺少 CHANGELOG.md"]
    lines = path.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    if lines and lines[0].strip() != "# 更新日志":
        problems.append("CHANGELOG.md 第一行必须是 '# 更新日志'")

    seen: dict[str, int] = {}
    for index, line in enumerate(lines):
        if not line.startswith("## v"):
            continue
        version = line[3:].strip()
        if version in seen:
            problems.append(f"CHANGELOG.md 里 {version} 出现了两次")
        seen[version] = index
        if index > 0 and lines[index - 1].strip():
            problems.append(
                f"{version} 的标题上一行不是空行（第 {index} 行），渲染会粘在一起"
            )
        if index + 1 < len(lines) and lines[index + 1].strip():
            problems.append(f"{version} 的标题下一行不是空行（第 {index + 2} 行）")

    newest = read_version()
    if f"## v{newest}" not in path.read_text(encoding="utf-8"):
        problems.append(f"CHANGELOG.md 里没有当前版本 v{newest} 的小节")
    return problems


def collect_entries() -> list[tuple[str, bytes]]:
    """收集要写进包里的条目（相对包名的路径, 内容）。

    Returns:
        条目列表。

    Raises:
        SystemExit: 缺少必需文件或没有任何 .py。
    """
    entries: list[tuple[str, bytes]] = []

    py_files = sorted(
        path
        for path in PLUGIN_DIR.glob("*.py")
        if path.is_file() and path.name not in EXCLUDE_ROOT_PY
    )
    if not py_files:
        raise SystemExit("插件根目录下没有任何 .py，无法打包")
    if not any(path.name == "main.py" for path in py_files):
        raise SystemExit("缺少 main.py（AstrBot 从它里面找 Star 子类）")

    missing = [
        name for name in REQUIRED_ROOT_FILES if not (PLUGIN_DIR / name).is_file()
    ]
    if missing:
        raise SystemExit(f"缺少文件，无法打包：{', '.join(missing)}")

    for path in py_files:
        entries.append((f"{path.name}", path.read_bytes()))
    for name in REQUIRED_ROOT_FILES:
        entries.append((f"{name}", (PLUGIN_DIR / name).read_bytes()))

    for dirname in INCLUDE_DIRS:
        root = PLUGIN_DIR / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in {".pyc"}:
                continue
            rel = path.relative_to(PLUGIN_DIR).as_posix()
            entries.append((rel, path.read_bytes()))
    return entries


def build(output: Path) -> Path:
    """写出 zip。

    Args:
        output: 输出路径。

    Returns:
        实际写出的路径。
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    problems = check_version_consistency()
    if problems:
        raise SystemExit(
            "版本信息不一致，先改齐再打包：\n  - " + "\n  - ".join(problems)
        )

    problems = check_logo()
    if problems:
        raise SystemExit("图标不合规，先改好再打包：\n  - " + "\n  - ".join(problems))

    problems = check_changelog_format()
    if problems:
        raise SystemExit("CHANGELOG 格式不合规，先改好再打包：\n  - " + "\n  - ".join(problems))

    entries = collect_entries()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 目录条目是必需的：AstrBot 靠它 + 子条目前缀来推断"压缩包里的根目录"，
        # 也靠它让解包真的建出这一层目录。缺了它，收尾步骤会在
        # os.listdir(<target>/<包名>) 上抛 FileNotFoundError。
        zf.writestr(f"{PACKAGE_NAME}/", b"")
        for name, data in entries:
            zf.writestr(f"{PACKAGE_NAME}/{name}", data)
    return output


def main() -> int:
    """命令行入口。

    Returns:
        退出码。
    """
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_output()
    build(output)
    print(f"写出 {output} ({output.stat().st_size} bytes)")
    with zipfile.ZipFile(output) as zf:
        for info in zf.infolist():
            print(f"  {info.filename}  {info.file_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
