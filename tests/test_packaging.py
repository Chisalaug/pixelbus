"""打包相关的静态自检：这些错误只会在"发布以后用户装上去"才炸，必须在 CI 里就拦住。"""

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_metadata_is_installable():
    data = _pyproject()
    project = data["project"]
    assert project["name"] == "pixelbus"
    # PEP 639：license 是 SPDX 表达式 + license-files，不再是 table / classifier
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]
    # 新写法要 setuptools>=77，否则构建端不认
    requires = " ".join(data["build-system"]["requires"])
    assert "setuptools>=77" in requires


def test_scripts_and_extras_are_declared():
    project = _pyproject()["project"]
    assert project["scripts"]["pixelbus"] == "pixelbus.cli:main"
    extras = project["optional-dependencies"]
    for name in ("keys", "ocr", "mqtt", "mcp", "dev"):
        assert name in extras, f"缺 extra: {name}"
    # 可选依赖不能混进核心依赖
    core = " ".join(project["dependencies"]).lower()
    for optional in ("pynput", "rapidocr", "paho-mqtt", "mcp"):
        assert optional not in core


def test_manifest_keeps_business_screenshots_out():
    text = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    # 用户自己界面上的截图、本地复现页，必须被显式排除
    assert "prune demos/_mock" in text
    assert "recursive-exclude demos/*/shots *" in text
    # conftest.py 必须能被收到（setuptools 默认模板只收 tests/test*.py）
    assert "recursive-include tests *.py" in text
    # 排除规则要写在 include 之后，MANIFEST 是后写覆盖先写
    assert text.index("recursive-include demos") < text.index("prune demos/_mock")


def test_package_data_has_py_typed():
    data = _pyproject()
    assert data["tool"]["setuptools"]["package-data"]["pixelbus"] == ["py.typed"]
    assert (ROOT / "pixelbus" / "py.typed").is_file()
