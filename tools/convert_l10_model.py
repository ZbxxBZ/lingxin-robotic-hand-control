"""把官方 L10（SDK 型号名 L20Lite）的 URDF + OBJ 模型转换成网页控制台用的紧凑格式。

来源：linker-bot/linker-sim-assets（Apache-2.0）里的 hands/l20lite/：
  l20lite_left.urdf / l20lite_right.urdf   关节树、限位、联动（mimic）
  meshes/visual/{lh,rh}_*_vis.obj           各连杆的可视网格（单位：米）
  sdk_channels.yaml                         SDK 10 个通道 → URDF 关节的官方对应关系
官方换算约定（linear-fit-v0）：SDK 百分比 100 = 关节下限，0 = 上限，中间线性。
换成原始值 0~255 就是：角度 = 上限 + (下限 - 上限) × raw / 255。

输出到 web/model/：
  l20lite_{side}.json  关节树、通道对应、各网格在 .bin 里的位置和包围盒
  l20lite_{side}.bin   各网格的顶点（uint16 量化坐标）和三角形索引（uint16）

    python tools/convert_l10_model.py
    python tools/convert_l10_model.py --assets D:/project/linkerbot_sdk/linker-sim-assets/hands/l20lite

只做离线数据转换，不连接真机。需要 numpy（l10_hand 环境里已有）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

import numpy as np

DEFAULT_ASSETS = pathlib.Path("D:/project/linkerbot_sdk/linker-sim-assets/hands/l20lite")
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "model"


def floats(text: str | None, count: int, default: float = 0.0) -> list[float]:
    if not text:
        return [default] * count
    values = [float(v) for v in text.split()]
    if len(values) != count:
        raise ValueError(f"需要 {count} 个数，实际是 {text!r}")
    return values


def read_channels(assets: pathlib.Path, prefix: str) -> list[str]:
    """从 sdk_channels.yaml 读 10 个通道对应的关节名（{S} 换成 l / r）。"""
    text = (assets / "sdk_channels.yaml").read_text(encoding="utf-8")
    block = text.split("channels:", 1)[1].split("\n\n", 1)[0]
    names = re.findall(r'-\s*"([^"]+)"', block)
    if len(names) != 10:
        raise ValueError(f"sdk_channels.yaml 里应有 10 个通道，读到 {len(names)} 个")
    return [name.replace("{S}", prefix) for name in names]


def parse_obj(path: pathlib.Path) -> tuple[np.ndarray, np.ndarray]:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.startswith("v "):
                x, y, z = line.split()[1:4]
                vertices.append((float(x), float(y), float(z)))
            elif line.startswith("f "):
                idx = []
                for token in line.split()[1:]:
                    i = int(token.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(vertices) + i)
                for k in range(1, len(idx) - 1):  # 多边形按扇形拆成三角形
                    faces.append((idx[0], idx[k], idx[k + 1]))
    return np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def convert(assets: pathlib.Path, side: str) -> dict:
    prefix = "l" if side == "left" else "r"
    urdf_path = assets / f"l20lite_{side}.urdf"
    root = ET.parse(urdf_path).getroot()

    joints = []
    for j in root.findall("joint"):
        origin = j.find("origin")
        axis = j.find("axis")
        limit = j.find("limit")
        mimic = j.find("mimic")
        axis_v = np.asarray(floats(axis.get("xyz") if axis is not None else "1 0 0", 3))
        axis_v = axis_v / (np.linalg.norm(axis_v) or 1.0)
        joints.append(
            {
                "name": j.get("name"),
                "type": j.get("type"),
                "parent": j.find("parent").get("link"),
                "child": j.find("child").get("link"),
                "xyz": floats(origin.get("xyz") if origin is not None else None, 3),
                "rpy": floats(origin.get("rpy") if origin is not None else None, 3),
                "axis": [round(float(v), 7) for v in axis_v],
                "lower": float(limit.get("lower", 0)) if limit is not None else 0.0,
                "upper": float(limit.get("upper", 0)) if limit is not None else 0.0,
                "mimic": (
                    {
                        "joint": mimic.get("joint"),
                        "multiplier": float(mimic.get("multiplier", 1)),
                        "offset": float(mimic.get("offset", 0)),
                    }
                    if mimic is not None
                    else None
                ),
            }
        )
    children = {j["child"] for j in joints}
    roots = [l.get("name") for l in root.findall("link") if l.get("name") not in children]
    if len(roots) != 1:
        raise ValueError(f"URDF 应只有一个根连杆，实际 {roots}")

    blob = bytearray()
    meshes = []
    all_min = np.full(3, np.inf)
    all_max = np.full(3, -np.inf)
    for link in root.findall("link"):
        mesh_el = link.find("visual/geometry/mesh")
        if mesh_el is None:
            continue
        origin = link.find("visual/origin")
        if origin is not None and (any(floats(origin.get("xyz"), 3)) or any(floats(origin.get("rpy"), 3))):
            raise ValueError(f"{link.get('name')} 的 visual 有非零 origin，转换脚本暂不支持")
        vertices, faces = parse_obj(assets / mesh_el.get("filename"))
        if len(vertices) >= 65536:
            raise ValueError(f"{link.get('name')} 顶点数 {len(vertices)} 超出 uint16 索引范围")
        vmin, vmax = vertices.min(axis=0), vertices.max(axis=0)
        extent = np.where(vmax - vmin > 0, vmax - vmin, 1.0)
        quantized = np.round((vertices - vmin) / extent * 65535).astype("<u2")
        while len(blob) % 4:
            blob.append(0)
        pos_offset = len(blob)
        blob += quantized.tobytes()
        while len(blob) % 4:
            blob.append(0)
        idx_offset = len(blob)
        blob += faces.astype("<u2").tobytes()
        meshes.append(
            {
                "link": link.get("name"),
                "vertexCount": int(len(vertices)),
                "indexCount": int(faces.size),
                "posOffset": pos_offset,
                "idxOffset": idx_offset,
                "min": [round(float(v), 7) for v in vmin],
                "extent": [round(float(v), 7) for v in extent],
            }
        )
        all_min = np.minimum(all_min, vmin)
        all_max = np.maximum(all_max, vmax)

    try:
        commit = subprocess.run(
            ["git", "-C", str(assets), "log", "-1", "--format=%h %cd", "--date=short"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"

    manifest = {
        "model": "L20Lite (L10)",
        "side": side,
        "source": f"linker-bot/linker-sim-assets hands/l20lite ({commit})",
        "license": "Apache-2.0",
        "convention": "linear-fit-v0: angle = upper + (lower - upper) * raw / 255",
        "root": roots[0],
        "channels": read_channels(assets, prefix),
        "joints": joints,
        "meshes": meshes,
        "bin": f"l20lite_{side}.bin",
        "byteLength": len(blob),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"l20lite_{side}.bin").write_bytes(bytes(blob))
    (OUT_DIR / f"l20lite_{side}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    triangles = sum(m["indexCount"] for m in meshes) // 3
    print(f"{side}: {len(meshes)} 个网格，{triangles} 个三角形，{len(blob) / 1e6:.2f} MB → {OUT_DIR}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="转换 L10（L20Lite）官方模型给网页控制台用")
    parser.add_argument("--assets", type=pathlib.Path, default=DEFAULT_ASSETS)
    args = parser.parse_args()
    for side in ("left", "right"):
        convert(args.assets, side)


if __name__ == "__main__":
    main()
