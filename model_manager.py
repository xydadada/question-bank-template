from __future__ import annotations

import argparse
import ctypes
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
import yaml


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "models" / "catalog.yaml"
PRESET_DIR = ROOT / "models" / "presets"
LOCAL_SELECTION = ROOT / "models.local.yaml"
LOCAL_CATALOG = ROOT / "models.catalog.local.yaml"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"YAML顶层必须是映射: {path}")
    return value


def catalog() -> dict[str, dict]:
    result = dict(read_yaml(CATALOG_PATH).get("models") or {})
    if LOCAL_CATALOG.is_file():
        local = read_yaml(LOCAL_CATALOG).get("models") or {}
        if not isinstance(local, dict):
            raise RuntimeError("models.catalog.local.yaml中的models必须是映射")
        result.update(local)
    return result


def presets() -> dict[str, dict]:
    return {
        path.stem: read_yaml(path)
        for path in sorted(PRESET_DIR.glob("*.yaml"))
    }


def validate_selection(selection: dict, entries: dict[str, dict]) -> None:
    roles = selection.get("roles") or {}
    if not isinstance(roles, dict):
        raise RuntimeError("roles必须是映射")
    for role, model_id in roles.items():
        if model_id not in entries:
            raise RuntimeError(f"模型目录中不存在: {model_id}")
        allowed = set(entries[model_id].get("roles") or [])
        if role not in allowed:
            raise RuntimeError(f"{model_id}不能用于{role}角色")
        runtime = str(entries[model_id].get("runtime") or "")
        compatible_runtimes = {
            "parser": {"mineru-local", "mineru-cloud"},
            "ocr": {"python-extra", "ollama", "disabled"},
            "vision": {"ollama", "openai-compatible", "disabled"},
            "classification": {"ollama", "openai-compatible", "disabled"},
            "embedding": {"ollama", "openai-compatible"},
            "rerank": {"openai-compatible", "disabled"},
            "chat": {"ollama", "openai-compatible", "disabled"},
        }
        if runtime not in compatible_runtimes.get(role, set()):
            raise RuntimeError(f"运行时{runtime}不能用于{role}角色")
    embedding = entries.get(str(roles.get("embedding")), {})
    dimensions = list(embedding.get("dimensions") or [])
    selected_dimension = selection.get("embedding_dimension")
    if dimensions and selected_dimension not in dimensions:
        raise RuntimeError(
            f"Embedding维度{selected_dimension}不在目录声明{dimensions}中"
        )


def selected_config(name: str | None) -> dict:
    if name:
        available = presets()
        if name not in available:
            raise RuntimeError(
                f"未知预设{name}；可选: {', '.join(sorted(available))}"
            )
        return available[name]
    if not LOCAL_SELECTION.is_file():
        raise RuntimeError("尚未选择模型预设；先运行 model_manager.py select <name>")
    return read_yaml(LOCAL_SELECTION)


def installed_ollama_models(base_url: str) -> set[str]:
    response = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=5)
    response.raise_for_status()
    return {
        str(item.get("name") or "")
        for item in response.json().get("models") or []
    }


def hardware_snapshot() -> dict[str, float | None]:
    memory_gb: float | None = None
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory_gb = status.total_physical / 1024**3
    vram_gb: float | None = None
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        values = [
            float(line.strip()) / 1024
            for line in completed.stdout.splitlines()
            if line.strip().replace(".", "", 1).isdigit()
        ]
        if values:
            vram_gb = max(values)
    return {
        "ram_gb": memory_gb,
        "vram_gb": vram_gb,
        "free_disk_gb": shutil.disk_usage(ROOT).free / 1024**3,
    }


def hardware_findings(selection: dict, entries: dict[str, dict]) -> list[str]:
    snapshot = hardware_snapshot()
    findings: list[str] = []
    for role, model_id in (selection.get("roles") or {}).items():
        entry = entries[str(model_id)]
        for requirement, actual_name, label in (
            ("minimum_ram_gb", "ram_gb", "内存"),
            ("minimum_vram_gb", "vram_gb", "显存"),
            ("minimum_disk_gb", "free_disk_gb", "可用磁盘"),
        ):
            minimum = entry.get(requirement)
            actual = snapshot.get(actual_name)
            # OS reservations make marketed 16 GB / 8 GB devices report a
            # little less. Keep a five-percent tolerance around published
            # hardware tiers while still rejecting a clearly undersized host.
            if (
                minimum
                and actual is not None
                and actual < float(minimum) * 0.95
            ):
                findings.append(
                    f"{role}/{model_id}要求{label}>={minimum}GB，当前约{actual:.1f}GB"
                )
    return findings


def selected_ollama_models(selection: dict) -> list[str]:
    entries = catalog()
    result = []
    for model_id in (selection.get("roles") or {}).values():
        entry = entries.get(str(model_id), {})
        if entry.get("runtime") == "ollama":
            tag = str(entry.get("model") or "").strip()
            if tag and tag not in result:
                result.append(tag)
    return result


def command_list() -> None:
    entries = catalog()
    print("预设：")
    for name, value in presets().items():
        print(f"  {name:16} {value.get('description', '')}")
    print("\n模型目录：")
    for model_id, value in entries.items():
        roles = ",".join(value.get("roles") or [])
        location = "local" if value.get("local") else "cloud"
        print(f"  {model_id:26} {location:5} {roles}")


def resolved_selection(selection: dict) -> dict:
    entries = catalog()
    validate_selection(selection, entries)
    roles = {
        role: {"id": model_id, **entries[str(model_id)]}
        for role, model_id in (selection.get("roles") or {}).items()
    }
    return {
        "name": selection.get("name"),
        "embedding_dimension": selection.get("embedding_dimension"),
        "roles": roles,
    }


def command_select(name: str) -> None:
    selection = selected_config(name)
    validate_selection(selection, catalog())
    LOCAL_SELECTION.write_text(
        yaml.safe_dump(selection, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"已选择预设{name}: {LOCAL_SELECTION.name}")
    print("尚未下载任何模型；运行install后只下载选中的本地组件。")


def command_set(role: str, model_id: str, dimension: int | None) -> None:
    selection = selected_config(None)
    selection.setdefault("roles", {})[role] = model_id
    if role == "embedding" and dimension is not None:
        selection["embedding_dimension"] = dimension
    validate_selection(selection, catalog())
    LOCAL_SELECTION.write_text(
        yaml.safe_dump(selection, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"已设置{role}: {model_id}")


def command_add(
    model_id: str,
    roles: list[str],
    runtime: str,
    model: str,
    dimensions: list[int],
) -> None:
    allowed_roles = {
        "parser", "ocr", "vision", "classification", "embedding",
        "rerank", "chat",
    }
    if not roles or not set(roles) <= allowed_roles:
        raise RuntimeError("roles包含未知角色")
    if runtime != "ollama":
        raise RuntimeError("add当前用于自定义Ollama模型；云端提供商在WeKnora中配置")
    if "embedding" in roles and not dimensions:
        raise RuntimeError("自定义Embedding必须声明至少一个输出维度")
    built_in = read_yaml(CATALOG_PATH).get("models") or {}
    if model_id in built_in:
        raise RuntimeError("不能用本地目录覆盖内置模型ID")
    local_value = (
        read_yaml(LOCAL_CATALOG)
        if LOCAL_CATALOG.is_file()
        else {"version": 1, "models": {}}
    )
    local_value.setdefault("models", {})[model_id] = {
        "roles": roles,
        "runtime": runtime,
        "model": model,
        "local": runtime in {"ollama", "mineru-local", "python-extra"},
        **({"dimensions": dimensions} if dimensions else {}),
        "description": "User-defined local catalog entry",
    }
    validate_selection(
        {
            "roles": {role: model_id for role in roles},
            **(
                {"embedding_dimension": dimensions[0]}
                if "embedding" in roles and dimensions
                else {}
            ),
        },
        {**built_in, **local_value["models"]},
    )
    LOCAL_CATALOG.write_text(
        yaml.safe_dump(local_value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"已加入本地模型目录: {model_id}")


def command_use_ollama(
    role: str,
    model: str,
    model_id: str | None,
    dimension: int | None,
) -> None:
    """Select a built-in or arbitrary Ollama tag in one operation."""
    if role not in {"ocr", "vision", "classification", "embedding", "chat"}:
        raise RuntimeError(
            "use-ollama支持ocr、vision、classification、embedding和chat角色"
        )
    # Fail before writing a custom catalog when the user has not selected a
    # base preset yet.
    selected_config(None)
    entries = catalog()
    same_tag = [
        (entry_id, entry)
        for entry_id, entry in entries.items()
        if entry.get("runtime") == "ollama"
        and str(entry.get("model") or "") == model
    ]
    matching_ids = [
        entry_id
        for entry_id, entry in same_tag
        if role in set(entry.get("roles") or [])
    ]
    if matching_ids:
        selected_id = matching_ids[0]
        if role == "embedding" and dimension is None:
            declared = list(entries[selected_id].get("dimensions") or [])
            if len(declared) == 1:
                dimension = int(declared[0])
    elif same_tag:
        declared_roles = sorted(
            {
                declared_role
                for _, entry in same_tag
                for declared_role in entry.get("roles") or []
            }
        )
        raise RuntimeError(
            f"{model}已收录为{','.join(declared_roles)}用途，不能用于{role}"
        )
    else:
        if role == "embedding" and dimension is None:
            raise RuntimeError("未收录的Embedding模型必须用--dimension声明输出维度")
        selected_id = model_id or (
            "custom-" + re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")
        )
        dimensions = [dimension] if dimension is not None else []
        command_add(selected_id, [role], "ollama", model, dimensions)
    command_set(role, selected_id, dimension)


def command_status(base_url: str) -> int:
    selection = selected_config(None)
    validate_selection(selection, catalog())
    print(yaml.safe_dump(selection, allow_unicode=True, sort_keys=False).strip())
    snapshot = hardware_snapshot()
    print(
        "硬件："
        + "｜".join(
            f"{name}={value:.1f}GB" if value is not None else f"{name}=unknown"
            for name, value in snapshot.items()
        )
    )
    for finding in hardware_findings(selection, catalog()):
        print("资源提示: " + finding)
    required = selected_ollama_models(selection)
    if not required:
        return 0
    try:
        installed = installed_ollama_models(base_url)
    except requests.RequestException as exc:
        print(f"Ollama当前不可达: {type(exc).__name__}", file=sys.stderr)
        return 1
    missing = [model for model in required if model not in installed]
    print("Ollama已安装: " + ", ".join(sorted(installed)))
    if missing:
        print("选中但尚未安装: " + ", ".join(missing))
        return 1
    print("选中的Ollama模型均已安装。")
    return 0


def pull_ollama(model: str, base_url: str) -> None:
    with requests.post(
        f"{base_url.rstrip('/')}/api/pull",
        json={"model": model, "stream": True},
        stream=True,
        timeout=3600,
    ) as response:
        response.raise_for_status()
        last_status = ""
        for line in response.iter_lines():
            if not line:
                continue
            item = json.loads(line)
            if item.get("error"):
                raise RuntimeError(f"Ollama下载失败: {item['error']}")
            status = str(item.get("status") or "")
            if status and status != last_status:
                print(f"  {model}: {status}")
                last_status = status


def command_install(base_url: str) -> None:
    selection = selected_config(None)
    entries = catalog()
    validate_selection(selection, entries)
    findings = hardware_findings(selection, entries)
    if findings:
        raise RuntimeError("所选预设不满足本机最低资源要求: " + "; ".join(findings))
    runtimes = {
        entries[str(model_id)].get("runtime")
        for model_id in (selection.get("roles") or {}).values()
    }
    ollama_models = selected_ollama_models(selection)
    if ollama_models:
        try:
            installed = installed_ollama_models(base_url)
        except requests.RequestException as exc:
            raise RuntimeError(
                "Ollama尚未运行；启动Ollama后重新执行install"
            ) from exc
        for model in ollama_models:
            if model in installed:
                print(f"已存在，跳过: {model}")
            else:
                print(f"按需下载: {model}")
                pull_ollama(model, base_url)
    if "python-extra" in runtimes:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("缺少uv，无法安装本地OCR可选依赖")
        subprocess.run([uv, "sync", "--extra", "ocr"], cwd=ROOT, check=True)
    if "mineru-local" in runtimes:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("缺少uv，无法创建隔离的MinerU本地环境")
        runtime = ROOT / ".runtime" / "mineru"
        python = runtime / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if shutil.disk_usage(ROOT).free < 20 * 1024**3:
            raise RuntimeError("本地MinerU要求至少20GB可用磁盘空间")
        parser_id = str((selection.get("roles") or {}).get("parser") or "")
        parser_entry = entries.get(parser_id, {})
        python_version = str(parser_entry.get("python") or "3.12")
        if not python.is_file():
            subprocess.run(
                [uv, "venv", "--python", python_version, str(runtime)],
                cwd=ROOT,
                check=True,
            )
        mineru_package = str(
            parser_entry.get("package") or "mineru[all]"
        )
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), mineru_package],
            cwd=ROOT,
            check=True,
        )
        print(
            "MinerU已安装到隔离目录.runtime/mineru；首次本地解析会由"
            "官方运行时下载所选后端需要的模型。"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="题库模板模型目录与按需安装")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    select = subparsers.add_parser("select")
    select.add_argument("preset")
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("role")
    set_parser.add_argument("model_id")
    set_parser.add_argument("--dimension", type=int)
    add = subparsers.add_parser("add")
    add.add_argument("model_id")
    add.add_argument("--role", action="append", required=True, dest="roles")
    add.add_argument("--runtime", default="ollama")
    add.add_argument("--model", required=True)
    add.add_argument("--dimension", action="append", type=int, default=[])
    use_ollama = subparsers.add_parser("use-ollama")
    use_ollama.add_argument("role")
    use_ollama.add_argument("model")
    use_ollama.add_argument("--id", dest="model_id")
    use_ollama.add_argument("--dimension", type=int)
    subparsers.add_parser("status")
    subparsers.add_parser("install")
    subparsers.add_parser("resolve")
    subparsers.add_parser("hardware")
    args = parser.parse_args()
    if args.command == "list":
        command_list()
    elif args.command == "select":
        command_select(args.preset)
    elif args.command == "set":
        command_set(args.role, args.model_id, args.dimension)
    elif args.command == "add":
        command_add(
            args.model_id, args.roles, args.runtime, args.model, args.dimension
        )
    elif args.command == "use-ollama":
        command_use_ollama(
            args.role, args.model, args.model_id, args.dimension
        )
    elif args.command == "status":
        raise SystemExit(command_status(args.ollama_url))
    elif args.command == "install":
        command_install(args.ollama_url)
    elif args.command == "resolve":
        print(json.dumps(resolved_selection(selected_config(None))))
    elif args.command == "hardware":
        print(json.dumps(hardware_snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
