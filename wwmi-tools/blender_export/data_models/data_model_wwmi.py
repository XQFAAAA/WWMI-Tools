import time
import re
import numpy
import bpy
import json
import math

from typing import Tuple, List, Dict, Optional


from ...migoto_io.data_model.dxgi_format import DXGIFormat, DXGIType
from ...migoto_io.data_model.byte_buffer import Semantic, AbstractSemantic, BufferSemantic, BufferLayout, NumpyBuffer
from ...migoto_io.data_model.data_model import DataModel
# from .color1_encoder import *


class DataModelWWMI(DataModel):
    buffers_format: Dict[str, BufferLayout] = {
        'Index': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Index), DXGIFormat.R32_UINT, stride=12)
        ]),
        'Position': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Position, 0), DXGIFormat.R32G32B32_FLOAT)
        ]),
        'Blend': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Blendindices, 0), DXGIFormat.R8_UINT, stride=4),
            BufferSemantic(AbstractSemantic(Semantic.Blendweight, 0), DXGIFormat.R8_UINT, stride=4),
        ]),
        'Vector': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Tangent, 0), DXGIFormat.R8G8B8A8_SNORM),
            BufferSemantic(AbstractSemantic(Semantic.Normal, 0), DXGIFormat.R8G8B8_SNORM),
            BufferSemantic(AbstractSemantic(Semantic.BitangentSign, 0), DXGIFormat.R8_SNORM),
        ]),
        'Color': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Color, 0), DXGIFormat.R8G8B8A8_UNORM),
        ]),
        'TexCoord': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
            BufferSemantic(AbstractSemantic(Semantic.Color, 1), DXGIFormat.R16G16_UNORM),
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 1), DXGIFormat.R16G16_FLOAT),
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 2), DXGIFormat.R16G16_FLOAT),
        ]),
        'ShapeKeyOffset': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 0), DXGIFormat.R32G32B32A32_UINT),
        ]),
        'ShapeKeyVertexId': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 1), DXGIFormat.R32_UINT),
        ]),
        'ShapeKeyVertexOffset': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 2), DXGIFormat.R16_FLOAT),
        ]),
    }

    def __init__(self):
        self.flip_winding = True
        self.flip_bitangent_sign = True
        self.flip_texcoord_v = True
        self.semantic_converters = {}
        self.format_converters = {}
        self.semantic_encoders = {
            # Reshape flat array [[0,0,0],[0,0,0]] to [[0,0,0,1],[0,0,0,1]]
            AbstractSemantic(Semantic.Tangent, 0): [lambda data: self.converter_resize_second_dim(data, 4, fill=1)],
            # Normalize weights to 8-bit values, skip sanitizing since it's already done by DataExtractor
            AbstractSemantic(Semantic.Blendweight, 0): [lambda data: self.converter_normalize_weights(data, sanitize=False, dtype=numpy.uint8)],
        }
        self.format_encoders = {
            # Reshape flat array [0,1,2,3,4,5] to [[0,1,2],[3,4,5]]
            AbstractSemantic(Semantic.Index): [lambda data: self.converter_reshape_second_dim(data, 3)],
            # Trim color array [[1,1,0,0],[1,1,0,0]] to [[1,1],[1,1]]
            AbstractSemantic(Semantic.Color, 1): [lambda data: self.converter_resize_second_dim(data, 2)],
        }

    def get_data(
            self,
            context: bpy.types.Context,
            collection: bpy.types.Collection,
            obj: bpy.types.Object,
            excluded_buffers: List[str],
            buffers_format: Optional[Dict[str, BufferLayout]] = None,
            mirror_mesh: bool = False,
            mesh_scale: float = 1.0,
            mesh_rotation: Tuple[float] = (0.0, 0.0, 0.0),
            object_index_layout: Optional[List[int]] = None,
            build_blend_r16: bool = False,
        ) -> Tuple[Dict[str, NumpyBuffer], int, Optional[List[int]]]:

        if buffers_format is None:
            buffers_format = self.buffers_format

        # Merged instance mods request 16-bit VG ids to build their 16-bit blend indices
        # buffer (Blend_R16.buf) directly from the exported vertex data. They don't use
        # the blend remap system: it works with 8-bit blend indices only and its lookup
        # tables are only 512 VG ids long, so it fails for mods using more VGs.
        build_blend_r16 = build_blend_r16 and 'Blend' not in excluded_buffers
        build_blend_remaps = (
            object_index_layout is not None and 'Blend' not in excluded_buffers and not build_blend_r16)

        # Request 16-bit VG ids for Blend Remap system / Blend_R16 buffer
        if build_blend_remaps or build_blend_r16:
            # Copy the layouts to not leak the request into other exports
            # (`self.buffers_format` is a shared class attribute)
            buffers_format = dict(buffers_format)
            # Number of VGs per vertex may vary based on buffers_format, we should respect it
            num_vgs = buffers_format['Blend'].get_element(AbstractSemantic(Semantic.Blendindices, 0)).get_num_values()
            buffers_format['BlendRemapVertexVG' if build_blend_remaps else 'BlendVertexId16'] = BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Blendindices, 1), DXGIFormat.R16_UINT, stride=num_vgs*2),
            ])

        # # RESEARCH: COLOR1 TBN data request (tangents, bitangent signs and normals) signs for encoding
        # buffers_format['TBN'] = BufferLayout([
        #     BufferSemantic(AbstractSemantic(Semantic.Tangent, 1), DXGIFormat.R32G32B32_FLOAT),
        #     BufferSemantic(AbstractSemantic(Semantic.BitangentSign, 1), DXGIFormat.R16_FLOAT),
        #     BufferSemantic(AbstractSemantic(Semantic.Normal, 1), DXGIFormat.R32G32B32_FLOAT),
        # ])

        index_data, vertex_buffer = self.export_data(
            context=context,
            collection=collection,
            mesh=obj.evaluated_get(context.evaluated_depsgraph_get()).to_mesh(),
            excluded_buffers=excluded_buffers,
            buffers_format=buffers_format,
            mirror_mesh=mirror_mesh,
            mesh_scale=mesh_scale,
            mesh_rotation=mesh_rotation,
            cache_index_data=build_blend_remaps,
        )

        buffers = self.build_buffers(index_data, vertex_buffer, excluded_buffers, buffers_format)

        if build_blend_r16:
            # `Blend` is only used as a source of the blend weights here, the mod uses
            # `Blend_R16` as its 16-bit blend buffer, so the 8-bit one is not exported
            vg_ids_buffer = buffers.pop('BlendVertexId16', None)
            blend_buffer = buffers.pop('Blend', None)
            if vg_ids_buffer is None or blend_buffer is None:
                raise ValueError('Blend_R16 buffer requires `Blend` and 16-bit VG ids buffers to be generated!')
            buffers['Blend_R16'] = self.build_blend_r16_buffer(
                vg_ids_buffer.get_field(vg_ids_buffer.layout.semantics[0].get_name()),
                blend_buffer,
            )

        vertex_ids = vertex_buffer.get_field(AbstractSemantic(Semantic.VertexId).get_name())


        # # RESEARCH: COLOR1 TBN data encoder test (write result to new vertex attribute)
        # tangents = vertex_buffer.get_field(AbstractSemantic(Semantic.Tangent, 1))
        # normals = vertex_buffer.get_field(AbstractSemantic(Semantic.Normal, 1))
        # bitangent_signs = vertex_buffer.get_field(AbstractSemantic(Semantic.BitangentSign, 1))
        # positions = vertex_buffer.get_field(AbstractSemantic(Semantic.Position, 0))
        
        # def test_tangents_encoder(tangents, bitangent_signs, normals):
        #     # smooth_normals = get_smooth_loop_normals(mesh)
        #     # smooth_normals = smooth_normals[vertex_ids]
            
        #     # smooth_normals = calc_smooth_normals(mesh)
        #     # smooth_normals = numpy.array([list(x) for x in smooth_normals.values()])
        #     # smooth_normals = smooth_normals[vertex_ids]
            
        #     smooth_normals = smooth_normals_angle_weighted(positions, index_data)
        #     # smooth_normals = smooth_normals_angle_weighted_vectorized(positions, index_data)
        #     tangent_normal = compute_tangent_normals(smooth_normals, tangents, bitangent_signs, normals)
        #     data = self.converter_oct_encode_vector(tangent_normal)

        #     # data = numpy.array([list(unit_vector_to_octahedron(Vector(x))) for x in tangent_normal.tolist()])

        #     data = (data + 1.0) * 0.5

        #     data = self.converter_resize_second_dim(data, 4)
        #     self._create_verterx_attribute('TANGENT_NEW_TEST', 'Component 0.001', data, vertex_ids)

        # test_tangents_encoder(tangents, bitangent_signs, normals)


        if build_blend_remaps:
            blend_buffer = buffers.get('Blend', None)
            if blend_buffer is not None:
                index_buffer = buffers.get('Index', None)
                vg_buffer = buffers.get('BlendRemapVertexVG', None)
                blend_remaps = self.build_blend_remap(context, object_index_layout, index_buffer, blend_buffer, vg_buffer)
                buffers.update(blend_remaps)

        shapekeys = self.export_shapekeys(obj, vertex_ids, excluded_buffers, mirror_mesh, mesh_scale, mesh_rotation)
        buffers.update(shapekeys)

        return buffers, len(vertex_ids)

    def export_shapekeys(
            self, 
            obj: bpy.types.Object,  
            vertex_ids: numpy.ndarray, 
            excluded_buffers: List[str],
            mirror_mesh: bool = False,
            mesh_scale: float = 1.0,
            mesh_rotation: Tuple[float] = (0.0, 0.0, 0.0),
        ) -> Dict[str, NumpyBuffer]:
        
        start_time = time.time()

        if obj.data.shape_keys is None or len(getattr(obj.data.shape_keys, 'key_blocks', [])) == 0:
            print(f'No shapekeys found to process!')
            return {}

        buffers = {}
        for buffer_name, buffer_layout in self.buffers_format.items():
            if buffer_name in excluded_buffers:
                continue
            for semantic in buffer_layout.semantics:
                if semantic.abstract.enum == Semantic.ShapeKey:
                    buffers[buffer_name] = NumpyBuffer(buffer_layout)
                    break

        if len(buffers) == 0:
            print(f'Skipped shapekeys fetching!')
            return {}

        shapekey_pattern = re.compile(r'.*(?:deform|custom)[_ -]*(\d+).*')
        shapekey_ids = {}
        
        for shapekey in obj.data.shape_keys.key_blocks:
            match = shapekey_pattern.findall(shapekey.name.lower())
            if len(match) == 0:
                continue
            shapekey_id = int(match[0])
            shapekey_ids[shapekey_id] = shapekey.name

        shapekeys = self.data_extractor.get_shapekey_data(obj, names_filter=list(shapekey_ids.values()), deduct_basis=True)

        max_key_id = max(shapekey_ids.keys())
        batch_count = math.ceil(max_key_id / 127)

        shapekey_offsets, shapekey_vertex_ids, shapekey_vertex_offsets = [], [], []
        
        for batch_id in range(batch_count):
            # Offsets sequence always starts with 0 for any batch
            shapekey_offsets.append(0)
            shapekey_verts_count = 0
            
            # Single batch contains up to 127 shapekeys (since first value is always 0)
            # So 254 shapekeys should be divided to 2 batches:
            # Batch 0: from 0   to 126 (aka range(0,   127))
            # Batch 1: from 127 to 253 (aka range(127, 254))
            shapekey_id_offset = batch_id * 127

            for shapekey_id in range(shapekey_id_offset, shapekey_id_offset + 127):

                shapekey = shapekeys.get(shapekey_ids.get(shapekey_id, -1), None)
                if shapekey is None or not (-0.00000001 > numpy.min(shapekey) or numpy.max(shapekey) > 0.00000001):
                    shapekey_offsets.append(shapekey_verts_count)
                    continue

                shapekey = shapekey[vertex_ids]

                shapekey_vert_ids = numpy.where(numpy.any(shapekey != 0, axis=1))[0]

                shapekey_verts_count += len(shapekey_vert_ids)
                shapekey_offsets.append(shapekey_verts_count)

                shapekey_vertex_ids.extend(shapekey_vert_ids)
                shapekey_vertex_offsets.extend(shapekey[shapekey_vert_ids])
            
        if len(shapekey_vertex_ids) == 0:
            return {}

        shapekey_offsets = numpy.array(shapekey_offsets, dtype=numpy.uint32)
        
        shapekey_vertex_offsets_np = numpy.zeros(len(shapekey_vertex_offsets), dtype=(numpy.float16, 6))
        # shapekey_vertex_offsets = numpy.zeros(len(shapekey_vertex_offsets), dtype=numpy.float16)
        shapekey_vertex_offsets_np[:, 0:3] = shapekey_vertex_offsets

        if mirror_mesh:
            shapekey_vertex_offsets_np[:, 0] *= -1

        if mesh_rotation != (0.0, 0.0, 0.0):
            shapekey_vertex_offsets_np[:, 0:3] = self.converter_rotate_vector(shapekey_vertex_offsets_np[:, 0:3], mesh_rotation)

        if mesh_scale != 1.0:
            shapekey_vertex_offsets_np[:, 0:3] = self.converter_scale_vector(shapekey_vertex_offsets_np[:, 0:3], mesh_scale)

        shapekey_vertex_ids = numpy.array(shapekey_vertex_ids, dtype=numpy.uint32)

        buffers['ShapeKeyOffset'].set_data(shapekey_offsets)
        buffers['ShapeKeyVertexId'].set_data(shapekey_vertex_ids)
        buffers['ShapeKeyVertexOffset'].set_data(shapekey_vertex_offsets_np)

        print(f'Shape Keys formatting time: {time.time() - start_time :.3f}s ({len(shapekey_vertex_ids)} shapekeyed vertices)')

        return buffers

    def build_blend_remap(
            self, 
            context: bpy.types.Context, 
            index_layout: List[int], 
            index_buffer: NumpyBuffer,
            blend_buffer: NumpyBuffer,
            vg_buffer: NumpyBuffer,
        ) -> Dict[str, NumpyBuffer]:
        
        start_time = time.time()

        remapped_vgs_counts = []

        if context.scene.wwmi_tools_settings.index_data_cache:
            # Partial export is enabled and index buffer cache exists, lets load it
            index_data = numpy.array(json.loads(context.scene.wwmi_tools_settings.index_data_cache)).ravel()
        else:
            if index_buffer is None:
                raise ValueError(f'Failed to build blend remap: `Index` buffer does not exist!')
            index_data = index_buffer.get_field(0).ravel()

        vg_ids = vg_buffer.get_field(vg_buffer.layout.get_element(AbstractSemantic(Semantic.Blendindices, 1)).get_name())
        vg_weights = blend_buffer.get_field(blend_buffer.layout.get_element(AbstractSemantic(Semantic.Blendweight, 0)).get_name())
        
        blend_remap_forward = numpy.empty(0, dtype=numpy.uint16)
        blend_remap_reverse = numpy.empty(0, dtype=numpy.uint16)

        index_offset = 0
        for index_count in index_layout:
            # Skip remapping the component if its custom mesh is empty
            if index_count == 0:
                remapped_vgs_counts.append(0)
                continue
    
            # Extract a segment of Index Buffer for the component (index_count number of indices starting from index_offset)
            vertex_ids = index_data[index_offset:index_offset+index_count]
            # Remove duplicate vertex ids (since multiple indices may reference the same vertex)
            vertex_ids = numpy.unique(vertex_ids)

            # Get VG ids used to weight vertices used in the component
            obj_vg_ids = vg_ids[vertex_ids].flatten()
            
            # Skip remapping the component if it references VG ids below 256 only
            if numpy.max(obj_vg_ids) < 256:
                index_offset += index_count
                remapped_vgs_counts.append(0)
                continue

            # Get weights for vertices referenced by the component
            obj_vg_weights = vg_weights[vertex_ids].flatten()
            # Get indices of non-zero weights (to skip remapping VG ids that are listed but not actually used)
            non_zero_idx = numpy.nonzero(obj_vg_weights > 0)[0]

            obj_vg_ids = obj_vg_ids[non_zero_idx]
            obj_vg_ids = numpy.unique(obj_vg_ids)

            if numpy.max(obj_vg_ids) < 256:
                index_offset += index_count
                remapped_vgs_counts.append(0)
                continue
            
            remapped_vgs_counts.append(len(obj_vg_ids))

            forward = numpy.zeros(512, dtype=numpy.uint16)
            forward[numpy.arange(len(obj_vg_ids))] = obj_vg_ids

            reverse = numpy.zeros(512, dtype=numpy.uint16)
            reverse[obj_vg_ids] = numpy.arange(len(obj_vg_ids))

            blend_remap_forward = numpy.concatenate((blend_remap_forward, forward), axis=0)
            blend_remap_reverse = numpy.concatenate((blend_remap_reverse, reverse), axis=0)

            index_offset += index_count

        buffers = {}

        buffers['BlendRemapForward'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 0), DXGIFormat.R16_UINT),
        ]))
        buffers['BlendRemapReverse'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 1), DXGIFormat.R16_UINT),
        ]))
        buffers['BlendRemapLayout'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 2), DXGIFormat.R32_UINT),
        ]))

        buffers['BlendRemapForward'].set_data(blend_remap_forward)
        buffers['BlendRemapReverse'].set_data(blend_remap_reverse)
        buffers['BlendRemapLayout'].set_data(numpy.array(remapped_vgs_counts))

        print(f'Blend remap time: {time.time() - start_time :.3f}s ({int(len(blend_remap_forward) / 512)} remaps)')

        return buffers

    def build_blend_r16_buffer(
            self,
            vg_ids_16: numpy.ndarray,
            blend_buffer: NumpyBuffer,
    ) -> NumpyBuffer:
        """Builds the 16-bit blend indices/weights buffer used by merged instance mods.

        Follows the conversion algorithm of BlendBufferrR8-R16.py:
        1. Convert the 8-bit UNORM weights to 16-bit UNORM via `x * 257`
           (keeps the floating point value identical: x/255 == x*257/65535).
        2. Replace the bone indices with the 16-bit VG ids of each vertex taken
           from the exported vertex data, so VG ids above 255 (8-bit blend indices
           of `Blend` buffer cannot address them) are supported.

        Resulting per-vertex layout (8 values per vertex):
          [0:8]   R16G16B16A16_UINT   bone indices 0-3   (16-bit VG ids)
          [8:16]  R16G16B16A16_UINT   bone indices 4-7   (16-bit VG ids)
          [16:24] R16G16B16A16_UNORM  bone weights 0-3   (from Blend * 257)
          [24:32] R16G16B16A16_UNORM  bone weights 4-7   (from Blend * 257)
        """
        start_time = time.time()

        # Collect blend weight fields in layout order
        weight_fields = [s for s in blend_buffer.layout.semantics if s.abstract.enum == Semantic.Blendweight]
        if not weight_fields:
            raise ValueError('Blend buffer layout is missing BLENDWEIGHT semantics!')

        weights_8 = numpy.concatenate([blend_buffer.get_field(s.get_name()) for s in weight_fields], axis=1)
        # 16-bit UNORM weights: bit-replicate 8-bit values (x * 257)
        weights_16 = (weights_8.astype(numpy.uint16) * 257)

        vg_ids_16 = vg_ids_16.astype(numpy.uint16)
        if vg_ids_16.shape[1] != weights_16.shape[1]:
            raise ValueError(
                f'16-bit VG id count per vertex ({vg_ids_16.shape[1]}) does not match '
                f'Blend weight count per vertex ({weights_16.shape[1]})!')

        # Build the R16 layout: all index chunks first, then all weight chunks
        r16_semantics = []
        for chunk in range(vg_ids_16.shape[1] // 4):
            r16_semantics.append(BufferSemantic(
                AbstractSemantic(Semantic.Blendindices, chunk),
                DXGIFormat.R16_UINT, stride=8))
        for chunk in range(weights_16.shape[1] // 4):
            r16_semantics.append(BufferSemantic(
                AbstractSemantic(Semantic.Blendweight, chunk),
                DXGIFormat.R16_UNORM, stride=8))

        r16 = NumpyBuffer(BufferLayout(r16_semantics), size=len(blend_buffer))

        index_chunk = 0
        weight_chunk = 0
        for semantic in r16_semantics:
            if semantic.abstract.enum == Semantic.Blendindices:
                r16.set_field(AbstractSemantic(Semantic.Blendindices, index_chunk),
                              vg_ids_16[:, index_chunk*4:(index_chunk+1)*4])
                index_chunk += 1
            else:
                r16.set_field(AbstractSemantic(Semantic.Blendweight, weight_chunk),
                              weights_16[:, weight_chunk*4:(weight_chunk+1)*4])
                weight_chunk += 1

        print(f'Blend_R16 buffer build time: {time.time() - start_time :.3f}s '
              f'({len(blend_buffer)} vertices, {r16.layout.stride * len(blend_buffer)} bytes)')

        return r16
