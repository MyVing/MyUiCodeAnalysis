#!/usr/bin/env python3
"""统一 IR 类契约的双语言生成与漂移检查。

`shared/schemas/` 是唯一真源，Python 与 TypeScript 两侧类型均由本脚本生成，
禁止手写同名定义。

用法::

    python shared/scripts/generate_contracts.py            # 生成并写盘
    python shared/scripts/generate_contracts.py --check    # 只校验，不写盘

退出码：0 一致或生成成功；1 存在漂移；2 环境缺失（生成器未安装）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
REPO_ROOT = Path(__file__).resolve().parents[2]

# 每个契约的生成目标：schema 名 -> 两侧输出路径
TARGETS: dict[str, tuple[Path, Path]] = {
    "ir": (
        REPO_ROOT / "backend" / "app" / "contracts" / "ir.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "ir.ts",
    ),
    "plugin": (
        REPO_ROOT / "backend" / "app" / "contracts" / "plugin.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "plugin.ts",
    ),
    "runtime": (
        REPO_ROOT / "backend" / "app" / "contracts" / "runtime.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "runtime.ts",
    ),
    "mapping": (
        REPO_ROOT / "backend" / "app" / "contracts" / "mapping.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "mapping.ts",
    ),
    "requirement": (
        REPO_ROOT / "backend" / "app" / "contracts" / "requirement.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "requirement.ts",
    ),
    # requirement.schema.json 通过 $ref 引用 chain.schema.json 的 ActionChain，
    # 两个生成器都会跨文件解析并把被引用的定义内联进产物——因此顺序无关，
    # 但两边必须来自同一份 chain.schema.json，否则会出现两套互不兼容的链类型。
    "chain": (
        REPO_ROOT / "backend" / "app" / "contracts" / "chain.py",
        REPO_ROOT / "shared" / "contracts" / "src" / "chain.ts",
    ),
}

# datamodel-code-generator 0.96.0 的参数，已用现有 contracts.py 逐字节验证还原。
# 修改这些参数会产生大面积无关 diff，必须同步更新所有已提交的生成文件。
PYTHON_CODEGEN_ARGS = [
    "--input-file-type",
    "jsonschema",
    "--output-model-type",
    "pydantic_v2.BaseModel",
    "--target-python-version",
    "3.12",
    "--use-standard-collections",
    "--use-union-operator",
    "--use-annotated",
    "--disable-timestamp",
    "--extra-fields",
    "forbid",
    "--use-title-as-name",
    "--allow-population-by-field-name",
]


def _find_python_codegen() -> Path | None:
    """优先用 backend 虚拟环境里的生成器，避免依赖全局安装。"""
    candidates = [
        REPO_ROOT / "backend" / ".venv" / "Scripts" / "datamodel-codegen.exe",
        REPO_ROOT / "backend" / ".venv" / "bin" / "datamodel-codegen",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("datamodel-codegen")
    return Path(found) if found else None


def _find_json2ts_cmd() -> list[str] | None:
    """优先使用 frontend 已安装的 json-schema-to-typescript。

    注意入口是 dist/src/cli.js（bin 声明的 json2ts 指向它）；
    dist/src/index.js 是库入口，被调用时不会产出文件。
    """
    local_cli = REPO_ROOT / "frontend" / "node_modules" / "json-schema-to-typescript" / "dist" / "src" / "cli.js"
    if local_cli.exists():
        node = shutil.which("node")
        if node:
            return [node, str(local_cli)]
    npx = shutil.which("npx")
    return [npx, "json2ts"] if npx else None


def _normalize(path: Path) -> str:
    """统一换行与行尾空白，使跨平台生成结果可比较。"""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{result.stderr}{result.stdout}")


def _generate(name: str, out_dir: Path, codegen: Path, json2ts: list[str]) -> tuple[Path, Path]:
    schema = SCHEMA_DIR / f"{name}.schema.json"
    py_out = out_dir / f"{name}.py"
    ts_out = out_dir / f"{name}.ts"

    # 两个生成器都按 **cwd** 解析 schema 里的相对 $ref（requirement 引用
    # chain.schema.json）。在仓库根目录跑会把 ref 解析成仓库根下的同名文件，
    # 因此必须把 cwd 设成 schema 目录，并用相对文件名传参。
    _run([str(codegen), "--input", schema.name, "--output", str(py_out), *PYTHON_CODEGEN_ARGS], SCHEMA_DIR)
    _run([*json2ts, schema.name, "-o", str(ts_out)], SCHEMA_DIR)
    return py_out, ts_out


def main() -> int:
    # Windows 控制台默认 GBK，中文提示会被截断；统一走 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验生成产物是否与 schema 一致")
    args = parser.parse_args()

    codegen = _find_python_codegen()
    json2ts = _find_json2ts_cmd()
    if codegen is None or json2ts is None:
        missing = []
        if codegen is None:
            missing.append("datamodel-codegen（backend/.venv 内）")
        if json2ts is None:
            missing.append("json-schema-to-typescript（frontend/node_modules 内）")
        print(f"缺少生成器：{'、'.join(missing)}", file=sys.stderr)
        return 2

    drifted: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for name, (py_target, ts_target) in TARGETS.items():
            py_gen, ts_gen = _generate(name, tmp_dir, codegen, json2ts)

            if args.check:
                for generated, committed in ((py_gen, py_target), (ts_gen, ts_target)):
                    if not committed.exists():
                        drifted.append(f"{committed.relative_to(REPO_ROOT)}（缺失）")
                        continue
                    if _normalize(generated) != _normalize(committed):
                        drifted.append(str(committed.relative_to(REPO_ROOT)))
            else:
                py_target.parent.mkdir(parents=True, exist_ok=True)
                ts_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(py_gen, py_target)
                shutil.copyfile(ts_gen, ts_target)
                print(f"生成 {py_target.relative_to(REPO_ROOT)}")
                print(f"生成 {ts_target.relative_to(REPO_ROOT)}")

    if args.check:
        if drifted:
            print("契约漂移，以下文件与 shared/schemas 不一致：", file=sys.stderr)
            for item in drifted:
                print(f"  - {item}", file=sys.stderr)
            print("\n运行 python shared/scripts/generate_contracts.py 重新生成。", file=sys.stderr)
            return 1
        print("契约一致：所有生成产物与 shared/schemas 匹配。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
