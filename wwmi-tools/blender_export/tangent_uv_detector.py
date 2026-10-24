import os
import re
import numpy

from pathlib import Path
from typing import Dict, Optional

from ..migoto_io.data_model.byte_buffer import MigotoFmt, NumpyBuffer, AbstractSemantic, BufferSemantic, Semantic
from ..migoto_io.data_model.dxgi_format import DXGIType


# 判定某个 UV 层与 VB 切线数据匹配所需的最低平均点积
MIN_MATCH_SCORE = 0.5
# 参与判定所需的最少有效面角数量，避免退化 UV 导致的误判
MIN_VALID_CORNERS = 64


def detect_tangent_uv_layers(object_source_folder: Path) -> Dict[int, str]:
    """逐个 Component 校验原始 VB 中的切线数据，反推生成该切线所用的 UV 层。

    Returns:
        Dict[int, str]: {component_id: uv_layer_name}，仅包含匹配成功的 Component
    """
    result = {}
    component_pattern = re.compile(r'.*component[ _-]*(\d+).*')

    for filename in sorted(os.listdir(object_source_folder)):
        if not filename.lower().endswith('.fmt'):
            continue
        match = component_pattern.findall(filename.lower())
        if len(match) != 1:
            continue

        component_id = int(match[0])
        fmt_path = Path(object_source_folder) / filename
        ib_path = fmt_path.with_suffix('.ib')
        vb_path = fmt_path.with_suffix('.vb')
        if not ib_path.is_file() or not vb_path.is_file():
            continue

        uv_name = _detect_component_uv(fmt_path, ib_path, vb_path)
        if uv_name is not None:
            result[component_id] = uv_name

    return result


def _detect_component_uv(fmt_path: Path, ib_path: Path, vb_path: Path) -> Optional[str]:
    with open(fmt_path, 'r') as fmt_file:
        fmt = MigotoFmt(fmt_file)

        index_buffer = NumpyBuffer(fmt.ib_layout)
        index_buffer.import_raw_data(ib_path.read_bytes())
        vertex_buffer = NumpyBuffer(fmt.vb_layout)
        vertex_buffer.import_raw_data(vb_path.read_bytes())

    index_semantic = fmt.ib_layout.get_element(AbstractSemantic(Semantic.Index))
    position_semantic = fmt.vb_layout.get_element(AbstractSemantic(Semantic.Position))
    normal_semantic = fmt.vb_layout.get_element(AbstractSemantic(Semantic.Normal))
    tangent_semantic = fmt.vb_layout.get_element(AbstractSemantic(Semantic.Tangent))

    if None in (index_semantic, position_semantic, normal_semantic, tangent_semantic):
        return None

    indices = _decode_semantic(index_buffer, index_semantic).astype(numpy.int64)
    if len(indices) == 0:
        return None

    positions = _decode_semantic(vertex_buffer, position_semantic).astype(numpy.float32)
    normals = _decode_semantic(vertex_buffer, normal_semantic).astype(numpy.float32)[:, :3]
    tangents = _decode_semantic(vertex_buffer, tangent_semantic).astype(numpy.float32)[:, :3]

    face_positions = positions[indices]
    face_normals = normals[indices]
    face_tangents = tangents[indices]

    edge1 = face_positions[:, 1] - face_positions[:, 0]
    edge2 = face_positions[:, 2] - face_positions[:, 0]

    normal_lengths = numpy.linalg.norm(face_normals, axis=2)
    face_normals = face_normals / numpy.maximum(normal_lengths[:, :, None], 1e-9)
    tangent_lengths = numpy.linalg.norm(face_tangents, axis=2)

    scores = {}
    for semantic in fmt.vb_layout.semantics:
        if semantic.abstract.enum != Semantic.TexCoord:
            continue

        uvs = _decode_semantic(vertex_buffer, semantic).astype(numpy.float32)
        if uvs.shape[1] < 2:
            continue
        face_uvs = uvs[indices, :2]

        duv1 = face_uvs[:, 1] - face_uvs[:, 0]
        duv2 = face_uvs[:, 2] - face_uvs[:, 0]
        # UV 面积不为 0 的面才可用于切线计算
        determinant = duv1[:, 0] * duv2[:, 1] - duv2[:, 0] * duv1[:, 1]
        valid = numpy.abs(determinant) > 1e-9
        if not valid.any():
            continue

        reciprocal = numpy.zeros_like(determinant)
        reciprocal[valid] = 1.0 / determinant[valid]

        # 标准切线公式 T = dp/du（方向），再对每个面角的顶点法线做正交化
        tangent = (edge1 * duv2[:, 1:2] - edge2 * duv1[:, 1:2]) * reciprocal[:, None]
        tangent_lengths_computed = numpy.linalg.norm(tangent, axis=1)
        valid = valid & (tangent_lengths_computed > 1e-9)
        tangent = tangent / numpy.maximum(tangent_lengths_computed, 1e-9)[:, None]

        # 每个面角按各自的顶点法线正交化，结果的形状为 (面数, 3 角, 3 分量)
        tangent = tangent[:, None, :] - face_normals * numpy.sum(tangent[:, None, :] * face_normals, axis=2)[:, :, None]
        tangent = tangent / numpy.maximum(numpy.linalg.norm(tangent, axis=2), 1e-9)[:, :, None]

        # 与 VB 中存储的切线逐面角比较方向
        valid = valid & (tangent_lengths > 1e-6).all(axis=1)
        if int(valid.sum()) < MIN_VALID_CORNERS:
            continue

        dots = numpy.sum(tangent[valid] * face_tangents[valid], axis=2) / numpy.maximum(tangent_lengths[valid], 1e-9)
        score = float(numpy.clip(dots, -1.0, 1.0).mean())
        scores[semantic.get_name()] = score

    if len(scores) == 0:
        return None

    best_uv = max(scores, key=scores.get)
    print(f'Tangent UV check {fmt_path.stem}: ' +
          ', '.join(f'{name}={score:+.4f}' for name, score in scores.items()) +
          f' -> {best_uv}')

    if scores[best_uv] < MIN_MATCH_SCORE:
        print(f'Tangent UV check {fmt_path.stem}: no confident match, falling back to TEXCOORD.xy')
        return None

    return best_uv


def _decode_semantic(buffer: NumpyBuffer, semantic: BufferSemantic) -> numpy.ndarray:
    data = buffer.get_field(semantic.get_name())
    # 归一化整数需要先转为浮点，才能参与切线计算
    if semantic.format.dxgi_type in [DXGIType.UNORM16, DXGIType.UNORM8, DXGIType.SNORM16, DXGIType.SNORM8]:
        if not numpy.issubdtype(data.dtype, numpy.floating):
            data = semantic.format.type_decoder(data)
    return data
