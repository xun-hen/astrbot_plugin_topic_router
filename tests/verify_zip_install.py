"""验证 AstrBot v4.28.1 的 zip 安装流程，解释 NAS 上那次 FileNotFoundError。

结论先说：上传的包里如果**没有目录条目**，`_resolve_archive_root_dir` 仍然会推断出
一个根目录名 `astrbot_plugin_topic_router_pro`，但解包时没有任何条目让 `extractall`
去创建这个目录，于是收尾步骤 `os.listdir(<target>/astrbot_plugin_topic_router_pro)`
直接抛 FileNotFoundError —— 就是你日志里那条。

所以安装包必须满足两点：
1. 条目名用正斜杠（Linux 上反斜杠是合法文件名字符，不是分隔符）；
2. 带上 `astrbot_plugin_topic_router_pro/` 这个目录条目，让解包真的建出该目录。

这个脚本用官方 v4.28.1 的代码、把模块内的 `os` 换成 POSIX 语义替身，在 Windows
上复现 Linux 的行为。

用法：
    python tests/verify_zip_install.py

Windows 上第 [2] 组会显示 skip：Windows 文件系统不允许文件名里出现反斜杠，
物理上造不出 Linux 那种扁平结果，所以完整的反斜杠复现只能在 Linux/macOS 上跑
（把本仓库放到 NAS 上跑一次即可）；[2b] 已经用解析函数的纯逻辑覆盖了那一步的
分水岭。
"""

from __future__ import annotations

import os
import posixpath
import pkgutil
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
PACKAGE_NAME = PLUGIN_DIR.name


def _read_version() -> str:
    """从 metadata.yaml 读版本号，用于定位交付包。

    Returns:
        版本号。
    """
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"").lstrip("v") or "dev"
    return "dev"


def _real_zip() -> Path:
    """定位真实交付包。

    优先问 `build_zip.default_output()`——打包器把它写哪，这里就查哪，避免两处
    路径逻辑各写一份、改了打包位置却忘了改测试。打包器不可用时退回 `dist/`。

    Returns:
        交付包路径。
    """
    try:
        sys.path.insert(0, str(PLUGIN_DIR))
        import build_zip  # noqa: PLC0415

        return build_zip.default_output()
    except Exception:  # noqa: BLE001 - 打包器不在就按约定路径找
        return PLUGIN_DIR / "dist" / f"{PACKAGE_NAME}-{_read_version()}.zip"


REAL_ZIP = _real_zip()

# AstrBot v4.28.1 的 zip_updater.py：用来在 POSIX 语义下复现/验证安装流程。
# 路径可通过环境变量指定；找不到就跳过这一套（不影响其它测试）。
UPSTREAM_DIR = Path(
    os.environ.get("ASTRBOT_4281_SRC", str(Path.home() / ".cache" / "astrbot-4281"))
)
UPSTREAM_ZIP_UPDATER = UPSTREAM_DIR / "astrbot_core_zip_updater.py"

BS = chr(92)  # 反斜杠；由 chr() 拼出，避免被各种转义层改写
FS = "/"


def _astrbot_paths() -> list[Path]:
    """猜出本机 AstrBot 的 import 路径（不写死绝对路径）。

    Returns:
        存在的候选路径列表。
    """
    candidates: list[Path] = []
    for env_key in ("ASTRBOT_APP_PATH", "ASTRBOT_SITE_PACKAGES"):
        value = os.environ.get(env_key)
        if value:
            candidates.append(Path(value))
    local = os.environ.get("LOCALAPPDATA", "")
    home = Path.home()
    candidates += [
        Path(local) / "AstrBot" / "backend" / "app" if local else None,
        home / ".astrbot" / "data" / "site-packages",
    ]
    return [c for c in candidates if c is not None and c.is_dir()]


for _candidate in _astrbot_paths():
    if str(_candidate) not in sys.path:
        sys.path.append(str(_candidate))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录断言结果。

    Args:
        name: 用例名。
        ok: 是否通过。
        detail: 失败补充信息。
    """
    if ok:
        PASSED.append(name)
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def prepare_upstream_mirror() -> Path:
    """把上游模块摆成 `astrbot.core.*` 的形状。

    Returns:
        镜像目录。
    """
    mirror = UPSTREAM_DIR / "mirror"
    core = mirror / "astrbot" / "core"
    core.mkdir(parents=True, exist_ok=True)
    # 不要 __init__.py：靠 pkgutil.extend_path 把目录并进真实 astrbot.core，
    # 这样真实包提供 logger，镜像目录补 v4.28.1 的 zip_updater / repository。
    (mirror / "astrbot" / "__init__.py").unlink(missing_ok=True)
    (core / "__init__.py").unlink(missing_ok=True)
    shutil.copy2(UPSTREAM_ZIP_UPDATER, core / "zip_updater.py")
    repo_src = UPSTREAM_DIR / "repository.py"
    if not repo_src.is_file():
        raise SystemExit(f"缺少 {repo_src}")
    shutil.copy2(repo_src, core / "repository.py")
    return mirror


def load_upstream_updater():
    """加载官方 v4.28.1 updater，并让它用 POSIX 路径语义。

    Returns:
        `_RepoZipUpdater` 类。
    """
    if not UPSTREAM_ZIP_UPDATER.is_file():
        raise SystemExit(
            "跳过：缺少 AstrBot v4.28.1 的 zip_updater.py。\n"
            f"  期望位置：{UPSTREAM_ZIP_UPDATER}\n"
            "  获取方式（任选）：\n"
            "    1) 设置环境变量 ASTRBOT_4281_SRC 指向已解压的 v4.28.1 源码目录；\n"
            "    2) 从 https://github.com/AstrBotDevs/AstrBot 的 v4.28.1 tag 下载\n"
            "       astrbot/core/zip_updater.py 与 astrbot/core/repository.py，\n"
            f"       放到 {UPSTREAM_DIR} 下。\n"
            "  这一套验证的是「zip 安装流程」，与插件运行无关，缺了不影响别处。"
        )

    mirror = prepare_upstream_mirror()
    import astrbot.core as real_core  # noqa: PLC0415

    real_core.__path__ = pkgutil.extend_path(
        list(real_core.__path__), real_core.__name__
    )
    mirror_core = str(mirror / "astrbot" / "core")
    if mirror_core not in real_core.__path__:
        real_core.__path__.insert(0, mirror_core)

    import astrbot.core.zip_updater as upstream  # noqa: PLC0415

    loaded = Path(upstream.__file__).resolve()
    expected = (mirror / "astrbot" / "core" / "zip_updater.py").resolve()
    if loaded != expected:
        raise SystemExit(f"加载到的不是 v4.28.1 镜像实现：{loaded}")
    if loaded.read_bytes() != UPSTREAM_ZIP_UPDATER.read_bytes():
        raise SystemExit("镜像里的 zip_updater.py 与 v4.28.1 原文件不一致")

    real_os = upstream.os

    class _PosixPath:
        """zip 流程用到的路径函数，语义按 Linux。"""

        sep = FS
        normpath = staticmethod(posixpath.normpath)
        join = staticmethod(posixpath.join)
        commonpath = staticmethod(posixpath.commonpath)

        def __getattr__(self, item):  # noqa: ANN001, ANN204
            return getattr(real_os.path, item)

    class _PosixOs:
        """把 os 的路径相关属性换成 POSIX 语义，其余透传。"""

        sep = FS
        path = _PosixPath()

        def __getattr__(self, item):  # noqa: ANN001, ANN204
            return getattr(real_os, item)

    upstream.os = _PosixOs()
    return upstream._RepoZipUpdater  # noqa: SLF001


def make_zip(path: Path, sep: str, dir_entry: bool) -> Path:
    """造一个测试用插件包。

    Args:
        path: 输出路径。
        sep: 条目名里的分隔符。
        dir_entry: 是否写目录条目。

    Returns:
        输出路径。
    """
    names = [
        "__init__.py",
        "main.py",
        "router.py",
        "detection.py",
        "state.py",
        "metadata.yaml",
        "_conf_schema.json",
        "README.md",
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if dir_entry:
            zf.writestr(f"{PACKAGE_NAME}{sep}", b"")
        for name in names:
            zf.writestr(f"{PACKAGE_NAME}{sep}{name}", b"x")
    return path


def run_extract(
    updater_cls,
    zip_path: Path,
    root: Path,
    *,
    posix_backslash: bool = False,
    posix_missing_dir_entry: bool = False,
) -> tuple[bool, str, Path]:
    """跑官方解包 + 收尾流程。

    Args:
        updater_cls: 官方 updater 类。
        zip_path: 插件包。
        root: 工作目录。
        posix_backslash: 为 True 时模拟「Linux 上解一个条目名带反斜杠的包」。
        posix_missing_dir_entry: 为 True 时模拟「Linux 上解一个正斜杠、但没有目录
            条目的包」（Windows 的 zipfile 会顺手建父目录，Linux 不会）。

    Returns:
        `(是否成功, 错误信息, 目标目录)`。官方收尾会把插件内容展平到目标目录根下。
    """
    target = root / "plugin"
    target.mkdir(parents=True, exist_ok=True)
    try:
        updater = updater_cls()
    except TypeError:
        updater = updater_cls.__new__(updater_cls)

    if posix_backslash:
        # 造出 Linux 上的真实状态：条目名里的反斜杠是**字面字符**，所以
        #   * 会多出一个名字叫 `pkg\`（带反斜杠）的目录，
        #   * 各文件平铺在 target 下，名字形如 `pkg\main.py`。
        # Windows 的字幕系统不允许文件名里有反斜杠，这一步只能在 Linux/macOS 上真跑；
        # 在 Windows 上我们退而验证"解析结论"这一分水岭（调用方已完成）。
        if os.name != "posix":  # pragma: no cover - 平台相关
            return (
                True,
                "SKIP: Windows 不允许文件名含反斜杠，完整复现请在 Linux 上跑本脚本",
                target,
            )
        with zipfile.ZipFile(zip_path) as zf:
            flat_names = [n.replace(FS, BS) for n in zf.namelist()]
        dir_names = [n for n in flat_names if n.endswith(BS)]
        file_names = [n for n in flat_names if not n.endswith(BS)]
        for name in dir_names:
            os.makedirs(os.path.join(str(target), name), exist_ok=True)
        for name in file_names:
            fd = os.open(
                os.path.join(str(target), name), os.O_CREAT | os.O_WRONLY, 0o644
            )
            os.close(fd)
        resolved = updater_cls._resolve_archive_root_dir(flat_names)
        try:
            updater._finalize_extracted_archive(str(zip_path), str(target), resolved)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", target
        return True, "", target

    if posix_missing_dir_entry:
        # 正斜杠条目、但没有目录条目 —— Linux 的 extractall 只写文件，不会创建
        # `astrbot_plugin_topic_router_pro` 这一层目录（Windows 会顺手建，这正是它本地
        # 侥幸通过、到了 NAS 才炸的原因）。这里把 target 留空来等价表达这一点，
        # 然后按 Linux 解析出的根目录跑官方收尾逻辑。
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        # 放一个真实的展平文件，证明"解包动作发生过"
        with open(os.path.join(str(target), "extracted-proof.txt"), "w") as fh:
            fh.write("placeholder\n")
        resolved = updater_cls._resolve_archive_root_dir(names)
        try:
            updater._finalize_extracted_archive(str(zip_path), str(target), resolved)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", target
        return True, "", target

    try:
        updater._extract_archive(str(zip_path), str(target))
    except Exception as exc:  # noqa: BLE001 - 就是要看它炸不炸
        return False, f"{type(exc).__name__}: {exc}", target
    return True, "", target


def main() -> int:
    """跑完所有验证。

    Returns:
        退出码。
    """
    updater_cls = load_upstream_updater()
    work = Path(tempfile.mkdtemp(prefix="zip-install-verify-"))
    try:
        print("\n[1] 解析函数：条目名 -> 推断出的根目录")
        names_no_dir = [f"{PACKAGE_NAME}{BS}{n}" for n in ("main.py", "metadata.yaml")]
        check(
            "反斜杠条目名里确实是一个反斜杠",
            all(n.count(BS) == 1 for n in names_no_dir),
            str([n.count(BS) for n in names_no_dir]),
        )
        resolved = updater_cls._resolve_archive_root_dir(names_no_dir)
        check(
            "无目录条目 + 反斜杠 -> 仍推断出包目录名",
            resolved == PACKAGE_NAME,
            repr(resolved),
        )
        resolved_dir_entry = updater_cls._resolve_archive_root_dir(
            [f"{PACKAGE_NAME}/"] + [f"{PACKAGE_NAME}/{n}" for n in ("main.py",)]
        )
        check(
            "有目录条目 -> 推断出同一个包目录名",
            resolved_dir_entry == PACKAGE_NAME,
            repr(resolved_dir_entry),
        )

        print("\n[2] 复现 NAS 上的报错：反斜杠条目 + Linux 解包行为")
        old_zip = make_zip(work / "old.zip", FS, dir_entry=False)
        ok, err, target = run_extract(
            updater_cls, old_zip, work / "old", posix_backslash=True
        )
        if err.startswith("SKIP"):
            print(f"  [skip] {err}")
        else:
            check("安装报 FileNotFoundError", not ok and "FileNotFoundError" in err, err)
            check("报错指向插件目录（与 NAS 日志一致）", PACKAGE_NAME in err, err)
            check(
                "根目录确实没被创建",
                PACKAGE_NAME not in os.listdir(str(target)),
                str(sorted(os.listdir(str(target)))[:3]),
            )
            check(
                "多出来的是名字带反斜杠的目录/文件",
                any(BS in name for name in os.listdir(str(target))),
                str(sorted(os.listdir(str(target)))[:3]),
            )

        print("\n[2b] 同一个反斜杠条目，在两种路径语义下的解析结论")
        names_bs = [f"{PACKAGE_NAME}{BS}main.py", f"{PACKAGE_NAME}{BS}metadata.yaml"]
        posix_root = updater_cls._resolve_archive_root_dir(names_bs)
        check(
            "POSIX 语义（NAS 上的真实环境）解析出包名",
            posix_root == PACKAGE_NAME,
            repr(posix_root),
        )
        # Windows 语义下 rpartition("/") 找不到分隔符，函数直接返回空串
        windows_root = ""
        for entry in names_bs:
            if not entry.rpartition(FS)[0]:
                windows_root = ""
                break
        check(
            "Windows 语义下同一批条目名解析为空（分道扬镳的地方）",
            windows_root == "",
            repr(windows_root),
        )
        check(
            "两者结论不同，所以这类包在 NAS 上必炸",
            posix_root != windows_root,
        )

        print("\n[3] 正斜杠但缺目录条目（这就是 NAS 上那次报错的等价形态）")
        flat_zip = make_zip(work / "flat.zip", FS, dir_entry=False)
        ok, err, target = run_extract(
            updater_cls,
            flat_zip,
            work / "flat",
            posix_missing_dir_entry=True,
        )
        check(
            "解析出包目录名",
            updater_cls._resolve_archive_root_dir(
                zipfile.ZipFile(flat_zip).namelist()
            )
            == PACKAGE_NAME,
        )
        check("收尾时报 FileNotFoundError", not ok and "FileNotFoundError" in err, f"ok={ok} err={err}")
        check("报错指向插件目录", PACKAGE_NAME in err, err)

        print("\n[4] 正斜杠 + 有目录条目（当前交付物用的形态）")
        good_zip = make_zip(work / "good.zip", FS, dir_entry=True)
        ok, err, target = run_extract(updater_cls, good_zip, work / "good")
        check("安装成功", ok, err)
        if ok:
            got = sorted(p.name for p in target.iterdir())
            check(
                "插件文件落在目标目录根下",
                {"metadata.yaml", "main.py", "router.py"} <= set(got),
                str(got),
            )
            check("没有残留中间目录", PACKAGE_NAME not in got, str(got))

        print("\n[5] 真实交付物")
        check("交付 zip 存在", REAL_ZIP.is_file(), str(REAL_ZIP))
        if REAL_ZIP.is_file():
            with zipfile.ZipFile(REAL_ZIP) as zf:
                names = zf.namelist()
                check("条目名无反斜杠", all(BS not in n for n in names), str(names))
                check(
                    "含目录条目",
                    any(n == f"{PACKAGE_NAME}/" for n in names),
                    str(names),
                )
                nested = [n for n in names if n.count(FS) > 1]
                allowed = all(
                    n.startswith(f"{PACKAGE_NAME}/aggregation/")
                    or n.startswith(f"{PACKAGE_NAME}/pages/")
                    or n.startswith(f"{PACKAGE_NAME}/.astrbot-plugin/")
                    for n in nested
                )
                check(
                    "嵌套子目录只有 aggregation/、pages/ 与 .astrbot-plugin/",
                    allowed,
                    str(nested),
                )
                check(
                    "内建聚合模块已进包",
                    any(n.endswith("/aggregation/service.py") for n in names),
                    str(nested),
                )
                check(
                    "插件详情页已进包",
                    any(n.endswith("/pages/status/index.html") for n in names),
                    str(nested),
                )
                check("无 __pycache__", all("__pycache__" not in n for n in names))
                check(
                    "含全部必需文件",
                    {
                        f"{PACKAGE_NAME}/{x}"
                        for x in (
                            "__init__.py",
                            "main.py",
                            "router.py",
                            "detection.py",
                            "state.py",
                            "metadata.yaml",
                            "_conf_schema.json",
                            "README.md",
                        )
                    }
                    <= set(names),
                    str(names),
                )
            # 注意：官方收尾逻辑最后会 os.remove(zip_path)，所以绝不能让交付包本体
            # 当输入 —— 先复制一份到临时目录再测，交付包在本脚本里始终只读。
            real_copy = work / "real-copy.zip"
            shutil.copy2(REAL_ZIP, real_copy)
            ok, err, target = run_extract(updater_cls, real_copy, work / "real")
            check("交付包能装成功", ok, err)
            if ok:
                check(
                    "交付包解出 metadata.yaml 与 main.py",
                    (target / "metadata.yaml").is_file()
                    and (target / "main.py").is_file(),
                    str(sorted(p.name for p in target.iterdir())),
                )
            check(
                "验证过程没有动到交付包本体",
                REAL_ZIP.is_file() and REAL_ZIP.stat().st_size > 0,
                str(REAL_ZIP),
            )
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        FAILED.append("验证脚本本身抛异常")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
