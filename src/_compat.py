"""Windows / 新版依赖的兼容层。**必须在任何 `import viseval` 之前导入。**

viseval 0.0.3 是按 2024 年的依赖写的，直接在新环境里跑会挂两处：

1) cairosvg
   viseval/evaluate.py 第 10 行在**模块顶层** `import cairosvg`，
   cairosvg → cairocffi → 需要系统 libcairo。Windows 没有系统级 cairo，
   conda-forge 装的 cairo 又常因 freetype / fontconfig 版本冲突加载失败
   （OSError: [WinError 127] 找不到指定的程序）。
   cairosvg 在 VisEval 里**只有一个用途**：convert_svg_to_base64() 把 SVG 转 PNG
   给视觉模型打分（readability 维度）。不做 readability 时根本不会被调用，
   所以只要让 `import cairosvg` 成功即可 —— 真 cairo 挂了就放占位模块。

2) langchain.schema
   viseval/check/readability_check.py 里 `from langchain.schema import ...`，
   这是 langchain 0.1 的路径；langchain 1.x 搬到了 langchain_core.messages。
   这里做一个别名模块顶上去。

长期修复见 PLAN.md「Windows 已知坑」。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import types
import pathlib


# ---------------------------------------------------------------- cairosvg
def _prepare_dll_path() -> None:
    if sys.platform != "win32":
        return
    lib_bin = pathlib.Path(sys.prefix) / "Library" / "bin"
    if not lib_bin.exists():
        return
    os.environ["PATH"] = str(lib_bin) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        with contextlib.suppress(OSError, FileNotFoundError):
            os.add_dll_directory(str(lib_bin))


def _install_cairosvg_stub(reason: str) -> None:
    def svg2png(*_args, **_kwargs):
        raise RuntimeError(
            "cairosvg 在本机不可用，无法把 SVG 转成 PNG。\n"
            "readability 维度需要它。两个办法：\n"
            "  1) P0 先跳过 readability：运行 run_viseval.py 时不要传 --judge\n"
            "  2) 修好 cairo：见 PLAN.md「Windows 已知坑」\n"
            f"原始原因: {reason}"
        )

    mod = types.ModuleType("cairosvg")
    mod.svg2png = svg2png
    mod.__version__ = "0.0.0-stub"
    mod.__stub_reason__ = reason
    sys.modules["cairosvg"] = mod


def ensure_cairosvg() -> bool:
    """True = 真 cairosvg 可用；False = 已换成占位模块（readability 会不可用）。"""
    if "cairosvg" in sys.modules:
        return not hasattr(sys.modules["cairosvg"], "__stub_reason__")
    _prepare_dll_path()
    try:
        import cairosvg  # noqa: F401
        return True
    except Exception as exc:  # OSError（DLL 加载失败）/ ImportError 都算
        _install_cairosvg_stub(f"{type(exc).__name__}: {exc}")
        return False


# ------------------------------------------------------------ langchain
def ensure_langchain_schema() -> bool:
    """把 langchain 0.1 的 langchain.schema 别名到 langchain_core.messages。"""
    with contextlib.suppress(Exception):
        import langchain.schema  # noqa: F401
        return True
    try:
        from langchain_core.messages import (  # noqa: F401
            AIMessage,
            HumanMessage,
            SystemMessage,
        )
    except Exception:
        return False

    mod = types.ModuleType("langchain.schema")
    mod.HumanMessage = HumanMessage
    mod.SystemMessage = SystemMessage
    mod.AIMessage = AIMessage
    sys.modules["langchain.schema"] = mod
    with contextlib.suppress(Exception):
        import langchain

        langchain.schema = mod
    return True


# ------------------------------------------------------------ viseval 0.0.3 自身 bug
def patch_viseval_missing_imports() -> bool:
    """vis-evaluator 0.0.3 的 evaluate.py 调用了 surface_form_check()，
    但顶部 from .check import (...) 里漏了它（check/__init__.py 里有）。
    NameError: name 'surface_form_check' is not defined。
    这里不动 site-packages，从外部把函数补进去。"""
    try:
        from viseval import evaluate as _ev
        if not hasattr(_ev, "surface_form_check"):
            from viseval.check import surface_form_check as _sfc
            _ev.surface_form_check = _sfc
        return True
    except Exception:
        return False


# ------------------------------------------- Chrome 截图替代 cairosvg
def find_chrome() -> str | None:
    """在本机常见位置找 chrome.exe。"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        pathlib.Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for p in candidates:
        if p and pathlib.Path(p).exists():
            return str(p)
    return None


def find_chromedriver() -> str | None:
    """找 chromedriver：先 PATH，再 Selenium Manager 的缓存目录。

    viseval 0.0.3 的 layout_check 写死了 Service(webdriver_path)，
    所以必须给一个真实路径，不能靠 selenium 自己解析。
    """
    p = shutil.which("chromedriver")
    if p:
        return p
    cache = pathlib.Path.home() / ".cache" / "selenium" / "chromedriver" / "win64"
    if cache.exists():
        found = sorted(cache.rglob("chromedriver.exe"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if found:
            return str(found[0])
    return None


def install_chrome_svg_converter(chrome_path: str | None = None,
                                 window_size: tuple[int, int] = (900, 450)) -> bool:
    """把 viseval 的 SVG→PNG 换成 Chrome headless 截图。

    为什么：cairosvg 依赖系统级 libcairo，Windows 上没有，conda-forge 装的也常因
    freetype / fontconfig 版本冲突加载失败（WinError 127）。而 readability 的
    layout check 本来就要用 Chrome，顺手让它截图，整个 cairo 依赖就绕开了。

    返回 True 表示替换成功（之后 convert_svg_to_base64 走 Chrome）。
    """
    chrome = chrome_path or find_chrome()
    if not chrome:
        return False

    import base64
    import subprocess
    import tempfile

    def _convert(svg_string: str) -> str:
        tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="viseval_svg_"))
        svg_path = tmp_dir / "chart.svg"
        png_path = tmp_dir / "chart.png"
        try:
            svg_path.write_text(svg_string, encoding="utf-8")
            subprocess.run(
                [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                 f"--screenshot={png_path}",
                 f"--window-size={window_size[0]},{window_size[1]}",
                 f"file:///{svg_path.as_posix()}"],
                timeout=120, capture_output=True, check=False,
            )
            png = png_path.read_bytes() if png_path.exists() else b""
        finally:
            for f in (svg_path, png_path):
                with contextlib.suppress(Exception):
                    f.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                tmp_dir.rmdir()
        if not png:
            raise RuntimeError("Chrome 截图失败：没生成 PNG（SVG 为空或 Chrome 被占用）")
        return f"data:image/png;base64,{base64.b64encode(png).decode('utf-8')}"

    try:
        from viseval import evaluate as _ev
        _ev.convert_svg_to_base64 = _convert
        return True
    except Exception:
        return False


def install_chrome_layout_check(chrome_path: str | None = None) -> bool:
    """用 Chrome 命令行（--dump-dom）替代需要 chromedriver 的 layout_check。

    为什么：viseval 0.0.3 的 layout_check 走 selenium + chromedriver。本机
    Chrome 153 与 chromedriver 的会话不通——连 about:blank 都 get 不动，
    一律报 "unhandled request"，换启动参数（--no-sandbox /
    --remote-allow-origins=* / --headless=new）都无效。而 Chrome 命令行
    headless 是好的（截图就靠它），所以改成：
        SVG 内联进 HTML → 页面 JS 跑检测 → 结果写进 DOM → --dump-dom 读回来
    完全不需要 chromedriver。

    检测用的 JS 直接从 viseval.check.layout_check 里取（overflowScript /
    overlapScript），不自己重写，保证判定逻辑与官方逐字一致。
    """
    chrome = chrome_path or find_chrome()
    if not chrome:
        return False
    try:
        from viseval.check.layout_check import overlapScript, overflowScript
    except Exception:
        return False

    import html as html_lib
    import json as json_lib
    import re as re_lib
    import subprocess as subprocess_lib
    import tempfile as tempfile_lib
    import urllib.parse

    script = (
        "try{"
        "var __ov=(function(){%s})();"
        "var __ol=(function(){%s})();"
        "var p=document.createElement('pre');p.id='viseval_layout_result';"
        "p.textContent=encodeURIComponent(JSON.stringify({overflow:__ov,overlap:__ol,error:''}));"
        "document.body.appendChild(p);"
        "}catch(e){"
        "var p=document.createElement('pre');p.id='viseval_layout_result';"
        "p.textContent=encodeURIComponent(JSON.stringify({overflow:false,overlap:false,error:String(e)}));"
        "document.body.appendChild(p);}"
    ) % (overflowScript, overlapScript)

    def _layout_check(context: dict, webdriver_path=None):
        svg = (context or {}).get("svg_string") or ""
        m = re_lib.search(r"<svg[\s\S]*</svg>", svg)
        if not m:
            return None, "SVG 里找不到 <svg> 元素。"
        page = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
                + m.group(0) + "<script>" + script + "</script></body></html>")

        tmp_dir = pathlib.Path(tempfile_lib.mkdtemp(prefix="viseval_layout_"))
        html_path = tmp_dir / "chart.html"
        try:
            html_path.write_text(page, encoding="utf-8")
            proc = subprocess_lib.run(
                [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                 "--virtual-time-budget=3000", "--dump-dom",
                 f"file:///{html_path.as_posix()}"],
                timeout=120, capture_output=True,
            )
            dom = proc.stdout.decode("utf-8", errors="ignore")
        finally:
            with contextlib.suppress(Exception):
                html_path.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                tmp_dir.rmdir()

        m2 = re_lib.search(r'<pre id="viseval_layout_result">(.*?)</pre>', dom, re_lib.S)
        if not m2:
            return None, "Chrome 没返回布局检测结果。"
        try:
            data = json_lib.loads(urllib.parse.unquote(html_lib.unescape(m2.group(1))))
        except Exception:
            return None, "布局检测结果无法解析。"
        if data.get("error"):
            return None, f"布局检测脚本报错: {data['error']}"

        # 与官方一致：没溢出 / 没重叠才算通过
        overflow_ok = not bool(data.get("overflow"))
        overlap_ok = not bool(data.get("overlap"))
        if overflow_ok and overlap_ok:
            msg = "No overflow or overlap detected."
        elif not overflow_ok and not overlap_ok:
            msg = "Overflow and overlap detected."
        elif not overflow_ok:
            msg = "Overflow detected."
        else:
            msg = "Overlap detected."
        return overflow_ok and overlap_ok, msg

    try:
        from viseval import evaluate as _ev
        _ev.layout_check = _layout_check
        return True
    except Exception:
        return False


CAIROSVG_REAL = ensure_cairosvg()
LANGCHAIN_SHIMMED = ensure_langchain_schema()
VISEVAL_PATCHED = patch_viseval_missing_imports()


if __name__ == "__main__":
    print("cairosvg real:", CAIROSVG_REAL)
    print("langchain.schema shimmed:", LANGCHAIN_SHIMMED)
    print("chrome:", find_chrome())
    print("chromedriver:", find_chromedriver())
