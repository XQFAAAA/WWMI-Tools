import time
import shutil
import json
import re
import os
import subprocess

from ..libs.unidecode import unidecode

from typing import List, Dict, Union
from dataclasses import dataclass, field

from ..addon.exceptions import ConfigError

from ..migoto_io.blender_interface.utility import *
from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.objects import *
from ..migoto_io.blender_interface.mesh import *
from ..migoto_io.blender_tools.meshes import *
from ..migoto_io.data_model.byte_buffer import NumpyBuffer
from ..migoto_io.data_model.data_model import DataModel
from ..migoto_io.data_model.dxgi_format import DXGIFormatIndex

from ..extract_frame_data.metadata_format import read_metadata, ExtractedObject

from .object_merger import ObjectMerger, SkeletonType, MergedObject, MergedObjectShapeKeysBatch
from .metadata_collector import Version, ModInfo
from .texture_collector import Texture, get_textures
from .ini_maker import IniMaker

from .data_models.data_model_wwmi import DataModelWWMI

class Fatal(Exception): pass


data_models: Dict[str, DataModel] = {
    'WWMI': DataModelWWMI(),
}


class ObjectMergerWWMI(ObjectMerger):
    def __init__(self, **kwargs):
        self._texture_mode = kwargs.pop('texture_mode', 'HASH')
        self._slot_complex = kwargs.pop('slot_complex', False)
        self._match_dds_format = kwargs.pop('match_dds_format', 'LESS')
        self._shader_texture_usage = kwargs.pop('shader_texture_usage', None)
        super().__init__(**kwargs)

    def fill_missing_temp_objects_data(self):
        objects = [temp_object.object for component in self.components for temp_object in component.objects]
        self.fill_missing_data(objects)

    def pre_join_objects(self):
        if self._texture_mode in ('SLOT', 'PATH'):
            self._collect_slot_material_info()

    def _collect_slot_material_info(self):
        """Collect material info from TempObjects for slot mode export."""
        if self._shader_texture_usage is None:
            return

        shader_texture_usage = self._shader_texture_usage
        is_simple = not self._slot_complex
        match_mode = self._match_dds_format  # 'LESS', 'MORE', 'MOST'

        # Regex for object/material name to extract component id
        component_pattern = re.compile(r'.*component[_ -]*(\d+).*', re.IGNORECASE)
        # Regex for node group name matching: Cn-ps=xxx
        node_group_pattern = re.compile(r'C(\d+)-ps=([0-9a-fA-F]+)')

        # Collect all unique images across all components for slot_textures
        all_images = {}  # key: dds_export_name, value: dict with image info

        # Warnings to show after export
        self._slot_warnings = []

        for component in self.components:
            component_match_formats = {}  # key: match_format enum value, value: dict with match_format info
            component_material_collected = False  # For simple mode: only collect first material

            for temp_object in component.objects:
                obj = temp_object.object

                # Get component id from object name
                obj_name = obj.name
                if obj_name.startswith('TEMP_'):
                    obj_name = obj_name[5:]
                obj_match = component_pattern.match(obj_name)
                if not obj_match:
                    continue
                obj_component_id = obj_match.group(1)

                # Use object's component id to look up ShaderTextureUsage.json
                component_key = f"Component {obj_component_id}"
                component_data = shader_texture_usage.get(component_key, {})
                if not component_data:
                    continue

                # Track node groups by ps key for duplicate detection (last overwrites)
                # Same ps under the same Component should only be processed once
                seen_node_group_keys = {}  # key: ps_value, value: index in material_info['node_groups']

                material_info = {
                    'material_name': None,
                    'material_index': obj_component_id,
                    'node_groups': [],
                }

                # Scan all materials on this object for matching node groups
                for mat in obj.data.materials:
                    if mat is None or not mat.use_nodes:
                        continue
                    for node in mat.node_tree.nodes:
                        if node.type != 'GROUP':
                            continue
                        if node.node_tree is None:
                            continue
                        # Check if node group is muted (M key toggles mute)
                        if node.mute:
                            continue
                        ng_match = node_group_pattern.search(node.node_tree.name)
                        if not ng_match:
                            continue

                        ng_component_index = ng_match.group(1)
                        ps_value = ng_match.group(2)

                        # Check if this node group exists in ShaderTextureUsage.json for this component
                        ps_key = f"ps={ps_value}"
                        found_vs_key = None
                        for vs_key_iter, ps_dict in component_data.items():
                            if ps_key in ps_dict:
                                found_vs_key = vs_key_iter
                                break
                        if found_vs_key is None:
                            print(f"Warning: Node group '{node.node_tree.name}' not found in ShaderTextureUsage files "
                                  f"({component_key}, {ps_key}). Skipping.")
                            continue

                        node_group_info = {
                            'name': node.node_tree.name,
                            'component_index': int(ng_component_index),
                            'ps': ps_value,
                            'vs_key': found_vs_key,
                            'inputs': [],
                        }

                        # Iterate over node group inputs
                        for input_socket in node.inputs:
                            input_name = input_socket.name
                            # Only process ps-t inputs, skip ps-t alpha inputs
                            if not input_name.startswith('ps-t') or 'alpha' in input_name.lower():
                                continue

                            # Check if input is linked and the link is active
                            if not input_socket.is_linked:
                                continue

                            link = input_socket.links[0]
                            if link.is_muted:
                                continue
                            from_node = link.from_node

                            # Check if the source is an image texture node
                            if from_node.type != 'TEX_IMAGE':
                                continue

                            # Check if the image node is muted (Ctrl+Alt+RMB toggles mute)
                            if from_node.mute:
                                continue

                            image = from_node.image
                            if image is None:
                                continue

                            # Calculate base_name from image name
                            image_name = image.name
                            dot_index = image_name.find('.')
                            base_name = image_name[:dot_index] if dot_index > 0 else image_name

                            # Determine format from ShaderTextureUsage.json.
                            # Node group inputs may carry a full texture name
                            # (e.g. 'ps-t0: T_R2T1...Cloth_N'), while
                            # ShaderTextureUsage.json is keyed by the base slot name (ps-t0).
                            base_match = re.match(r'ps-t\d+', input_name)
                            base_input_name = base_match.group(0) if base_match else input_name
                            format_enum = None
                            match_format_enum = None
                            slot_data = shader_texture_usage[component_key][found_vs_key][ps_key]
                            if base_input_name in slot_data:
                                format_str = slot_data[base_input_name].get('format', '')
                                if format_str:
                                    try:
                                        format_enum = DXGIFormatIndex[format_str]
                                        match_format_enum = format_enum.to_typeless()
                                    except KeyError:
                                        print(f"Warning: Unknown format '{format_str}' for {input_name}")

                            # Generate resource_name and dds_export_name: use unidecode if non-ASCII
                            sanitized = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_name)
                            has_non_ascii = any(ord(c) > 127 for c in base_name)
                            if has_non_ascii:
                                ascii_name = unidecode(base_name).replace(' ', '')
                                sanitized_ascii = re.sub(r'[^a-zA-Z0-9_\-]', '_', ascii_name)
                                dds_export_name = sanitized_ascii + '.dds'
                                resource_name = sanitized_ascii
                            else:
                                dds_export_name = base_name + '.dds'
                                resource_name = sanitized

                            if match_format_enum is not None:
                                prefix = match_format_enum.name.split('_')[0]
                                ascii_digits = ''.join(str(ord(c)) for c in prefix)
                                filter_index = float(f"83.{ascii_digits}")
                            else:
                                filter_index = 0.0

                            slot_data_entry = slot_data.get(base_input_name, {})
                            asset_path = slot_data_entry.get('asset_path', '')
                            asset_name = slot_data_entry.get('asset_name', '') or (
                                asset_path.rsplit('.', 1)[-1] if asset_path else '')
                            input_info = {
                                'slot': base_input_name,
                                'format': format_enum,
                                'match_format': match_format_enum,
                                'filter_index': filter_index,
                                'image': image,
                                'dds_export_name': dds_export_name,
                                'resource_name': resource_name,
                                'hash': slot_data_entry.get('hash', ''),
                                'asset_path': asset_path,
                                'asset_name': asset_name,
                                'width': slot_data_entry.get('width', 0),
                                'height': slot_data_entry.get('height', 0),
                            }
                            node_group_info['inputs'].append(input_info)

                            # Add to component match_formats
                            if match_format_enum is not None:
                                if match_format_enum.value not in component_match_formats:
                                    if match_mode == 'LESS':
                                        # Less: single match_format (typeless)
                                        component_match_formats[match_format_enum.value] = {
                                            'match_format': match_format_enum,
                                            'filter_index': float(f"83.{''.join(str(ord(c)) for c in match_format_enum.name.split('_')[0])}"),
                                        }
                                    elif match_mode == 'MORE':
                                        # More: typeless + all original formats collected
                                        fmt_list = [match_format_enum]
                                        if format_enum and format_enum != match_format_enum:
                                            fmt_list.append(format_enum)
                                        component_match_formats[match_format_enum.value] = {
                                            'match_format': match_format_enum,
                                            'match_formats': fmt_list,
                                            'filter_index': float(f"83.{''.join(str(ord(c)) for c in match_format_enum.name.split('_')[0])}"),
                                        }
                                    else:
                                        # Most: all same-prefix formats
                                        same_prefix_formats = match_format_enum.get_same_prefix_formats()
                                        component_match_formats[match_format_enum.value] = {
                                            'match_format': match_format_enum,
                                            'match_formats': same_prefix_formats,
                                            'filter_index': float(f"83.{''.join(str(ord(c)) for c in match_format_enum.name.split('_')[0])}"),
                                        }
                                elif match_mode == 'MORE' and format_enum:
                                    # Accumulate additional original formats with same typeless prefix
                                    existing = component_match_formats[match_format_enum.value]
                                    if format_enum not in existing['match_formats']:
                                        existing['match_formats'].append(format_enum)

                            # Add to all_images (deduplicate by dds_export_name)
                            if dds_export_name not in all_images:
                                all_images[dds_export_name] = {
                                    'image': image,
                                    'dds_export_name': dds_export_name,
                                    'resource_name': resource_name,
                                    'hash': slot_data_entry.get('hash', ''),
                                    'asset_path': asset_path,
                                    'asset_name': asset_name,
                                    'width': slot_data_entry.get('width', 0),
                                    'height': slot_data_entry.get('height', 0),
                                }

                        if node_group_info['inputs']:
                            if ps_value in seen_node_group_keys:
                                # Same ps under this Component: overwrite (keep last)
                                old_index = seen_node_group_keys[ps_value]
                                old_ng = material_info['node_groups'][old_index]
                                self._slot_warnings.append(
                                    f"Duplicate ps={ps_value} on object '{obj_name}' "
                                    f"for Component {obj_component_id}: '{node_group_info['name']}' overwrites '{old_ng['name']}'"
                                )
                                material_info['node_groups'][old_index] = node_group_info
                            else:
                                seen_node_group_keys[ps_value] = len(material_info['node_groups'])
                                material_info['node_groups'].append(node_group_info)

                # In simple mode, skip if component already has material collected
                if is_simple and component_material_collected:
                    continue

                if is_simple:
                    # Simple mode: attach material to component (only first)
                    if not component_material_collected and material_info['node_groups']:
                        component.material = material_info
                        component_material_collected = True
                else:
                    # Complex mode: attach material to temp_object
                    temp_object.material = material_info

            # Attach match_formats to component
            component.match_formats = list(component_match_formats.values())

        # Store slot_textures for later use
        self._slot_textures = list(all_images.values())

        print(f"Slot mode ({'simple' if is_simple else 'complex'}, match={match_mode}): collected {len(self._slot_textures)} unique textures across {len(self.components)} components")

    @staticmethod
    def fill_missing_data(objects):
        verts_dict, center, scale = None, None, None
        for object in objects:
            mesh = object.data
            # Fill missing COLOR
            if not mesh.attributes.get('COLOR', None):
                data = numpy.zeros((len(mesh.loops), 4), dtype=numpy.float32)
                data[:, 1] = 0.25
                data[:, 3] = 1.0
                create_color_attribute(mesh, 'COLOR', data)
            # Fill missing COLOR1
            if not mesh.attributes.get('COLOR1', None):
                data = numpy.zeros((len(mesh.loops), 4), dtype=numpy.float32)
                create_color_attribute(mesh, 'COLOR1', data)
            # Fill missing TEXCOORD.xy
            if not mesh.uv_layers.get('TEXCOORD.xy'):
                create_uv_layer(mesh, 'TEXCOORD.xy')
            # Fill missing TEXCOORD1.xy
            if not mesh.uv_layers.get('TEXCOORD1.xy'):
                copy_uv_layer(mesh, 'TEXCOORD.xy', 'TEXCOORD1.xy')
            # Fill missing TEXCOORD2.xy
            if not mesh.uv_layers.get('TEXCOORD2.xy'):
                if verts_dict is None:
                    verts_dict = collect_vertices([object.data for object in objects])
                    # Compute bounding box from all parts
                    center, scale = compute_bounding_box_from_frontal_projection(verts_dict)
                # Generate UV using pre-collected vertices
                create_uv_layer_from_frontal_projection(
                    mesh=mesh,
                    verts=verts_dict[mesh],
                    center=center,
                    scale=scale,
                    uv_layer_name='TEXCOORD2.xy'
                )


# TODO: Add support of export of unhandled semantics from vertex attributes
class ModExporter:
    extracted_object: ExtractedObject
    merged_object: MergedObject
    buffers: Dict[str, NumpyBuffer]
    textures: List[Texture] = {}
    ini: IniMaker
    slot_textures: List[Dict] = None
    _slot_warnings: List[str] = None

    def __init__(self, context, cfg, excluded_buffers: List[str]):
        self.context = context
        self.cfg = cfg
        self.excluded_buffers = excluded_buffers
        self._slot_warnings = []
        self._shader_texture_usage = None

        self.object_source_folder = resolve_path(cfg.object_source_folder)
        self.mod_output_folder = resolve_path(cfg.mod_output_folder)
        self.meshes_path = self.mod_output_folder / 'Meshes'
        self.meshes_path.mkdir(parents=True, exist_ok=True)
        self.textures_path = self.mod_output_folder / 'Textures'
        self.textures_path.mkdir(parents=True, exist_ok=True)
        self.local_mod_logo_path = self.textures_path / 'Logo.dds'

    def export_mod(self):
    
        self.verify_config()

        start_time = time.time()
        print(f"Mod export started for '{self.cfg.component_collection.name}' object")

        if self.cfg.custom_template_live_update:
            self.cfg.partial_export = False
            self.cfg.write_ini = True

        try:
            self.extracted_object = read_metadata(self.object_source_folder / 'Metadata.json')
        except FileNotFoundError:
            raise ConfigError('object_source_folder', 'Specified folder is missing Metadata.json!')
        except Exception as e:
            raise ConfigError('object_source_folder', f'Failed to load Metadata.json:\n{e}')

        user_context = get_user_context(self.context)

        try:
            self.build_merged_object()
        except ConfigError as e:
            raise e
        except Exception as e:
            raise ConfigError('component_collection', f'Failed to create merged object from collection:\n{e}')

        try:
            self.build_data_buffers()
        except Exception as e:
            raise e
        finally:
            if self.cfg.remove_temp_object:
                remove_mesh(self.merged_object.object.data)
            set_user_context(self.context, user_context)

        if not self.cfg.partial_export:
            self.textures = get_textures(self.object_source_folder, ['af26db30', '1320a071', '10d7937d', '87505b2b', 'e5df00a8', 'ec2fecec', 'd313d349'] if self.cfg.skip_known_cubemap_textures else [])

            if self.cfg.write_ini:
                try:
                    self.build_mod_ini()
                except FileNotFoundError:
                    raise ConfigError('custom_template_source', f'Specified custom template file not found!')
                except Exception as e:
                    raise ConfigError('use_custom_template', f'Failed to build mod.ini from ini template:\n{e}')

        if self.cfg.custom_template_live_update:
            print(f'Total live ini template initialization time: {time.time() - start_time :.3f}s')
            return

        try:
            self.write_files()
        except Exception as e:
            raise ConfigError('mod_output_folder', f'Failed to write files to mod folder:\n{e}')

        print(f'Total mod export time: {time.time() - start_time :.3f}s')

    def verify_config(self):
        if self.cfg.component_collection is None:
            raise ConfigError('component_collection', f'Components collection is not specified!')
        if self.cfg.component_collection not in list(get_scene_collections()):
            raise ConfigError('component_collection', f'Collection "{self.cfg.component_collection.name}" is not a member of "Scene Collection"!')

    @staticmethod
    def _merge_shader_texture_usage(dicts):
        """Merge multiple ShaderTextureUsage dicts. First dict has highest priority.
        For each Component, ps keys from the primary dict take precedence;
        ps keys only present in fallback dicts are added.
        """
        if not dicts:
            return {}
        result = json.loads(json.dumps(dicts[0]))  # deep copy primary
        for fallback in dicts[1:]:
            for comp_key, comp_data in fallback.items():
                if comp_key not in result:
                    result[comp_key] = json.loads(json.dumps(comp_data))
                    continue
                # Collect all ps keys already in result for this component
                existing_ps = set()
                for vs_data in result[comp_key].values():
                    existing_ps.update(vs_data.keys())
                # Add fallback ps entries not already present
                for vs_key, ps_dict in comp_data.items():
                    for ps_key, slot_data in ps_dict.items():
                        if ps_key not in existing_ps:
                            result[comp_key].setdefault(vs_key, {})[ps_key] = slot_data
                            existing_ps.add(ps_key)
        return result

    def _load_and_merge_shader_texture_usage(self):
        """Find all ShaderTextureUsage*.json files, load and merge them.
        ShaderTextureUsage.json has highest priority, others are fallbacks.
        """
        source_folder = self.object_source_folder
        files = sorted(source_folder.glob('ShaderTextureUsage*.json'))
        if not files:
            raise ConfigError('object_source_folder',
                'No ShaderTextureUsage*.json found in object source folder. '
                'ShaderTextureUsage.json is required for slot mode export.')
        # Ensure ShaderTextureUsage.json is first
        primary_path = source_folder / 'ShaderTextureUsage.json'
        primary_files = [f for f in files if f == primary_path]
        other_files = [f for f in files if f != primary_path]
        ordered_files = primary_files + other_files
        if not primary_files:
            raise ConfigError('object_source_folder',
                'ShaderTextureUsage.json not found in object source folder. '
                'This file is required for slot mode export.')
        dicts = []
        for fpath in ordered_files:
            with open(fpath, 'r', encoding='utf-8') as f:
                dicts.append(json.load(f))
        return self._merge_shader_texture_usage(dicts)

    def build_merged_object(self):
        start_time = time.time()
        
        # Read ShaderTextureUsage*.json files for slot/path modes
        shader_texture_usage = None
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT', 'PATH'):
            shader_texture_usage = self._load_and_merge_shader_texture_usage()
        self._shader_texture_usage = shader_texture_usage

        object_merger = ObjectMergerWWMI(
            extracted_object=self.extracted_object,
            ignore_nested_collections=self.cfg.ignore_nested_collections,
            ignore_hidden_collections=self.cfg.ignore_hidden_collections,
            ignore_hidden_objects=self.cfg.ignore_hidden_objects,
            ignore_muted_shape_keys=self.cfg.ignore_muted_shape_keys,
            apply_modifiers=self.cfg.apply_all_modifiers,
            context=self.context,
            collection=self.cfg.component_collection,
            skeleton_type=SkeletonType.Merged if self.cfg.mod_skeleton_type == 'MERGED' else SkeletonType.PerComponent,
            fill_missing_mesh_data=self.cfg.fill_missing_mesh_data,
            add_missing_vertex_groups=self.cfg.add_missing_vertex_groups,
            texture_mode=self.cfg.texture_mode,
            slot_complex=self.cfg.slot_complex,
            match_dds_format=self.cfg.match_dds_format,
            shader_texture_usage=shader_texture_usage,
        )
        self.merged_object = object_merger.merged_object

        # Collect slot_textures and slot_warnings from object_merger
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT', 'PATH'):
            self.slot_textures = getattr(object_merger, '_slot_textures', [])
            self._slot_warnings = getattr(object_merger, '_slot_warnings', [])
            
        print(f'Merged object build time: {time.time() - start_time :.3f}s ({self.merged_object.vertex_count} vertices, {self.merged_object.index_count} indices)')

    def build_data_buffers(self):
        start_time = time.time()

        global data_models
        data_model = data_models['WWMI']

        buffers_format = None
        if self.extracted_object.export_format is not None and len(self.extracted_object.export_format) > 0:
            buffers_format = {}
            for buffer_name, buffer_layout in self.extracted_object.export_format.items():
                buffers_format[buffer_name] = buffer_layout.get_layout()

        index_layout = None
        if len(self.merged_object.object.vertex_groups) > 256:
            index_layout = []
            for component in self.merged_object.components:
                index_layout.append(component.index_count)
                
        self.buffers, vertex_count = data_model.get_data(
            context=self.context, 
            collection=self.cfg.component_collection, 
            obj=self.merged_object.object, 
            excluded_buffers=self.excluded_buffers,
            buffers_format=buffers_format,
            mirror_mesh=self.cfg.mirror_mesh,
            mesh_scale=100,
            mesh_rotation=(0, 0, 180),
            object_index_layout=index_layout,
        )

        self.merged_object.vertex_count = vertex_count

        # Build shapekeys batches metadata
        shapekey_offsets = self.buffers.get('ShapeKeyOffset', None)
        if shapekey_offsets:
            batches_count = int(len(shapekey_offsets.data)/128)

            for batch_id in range(batches_count):
                batch_vertex_count = shapekey_offsets.data[((batch_id+1)*128)-1]
                self.merged_object.shapekeys.batches.append(MergedObjectShapeKeysBatch(
                    vertex_count=batch_vertex_count,
                    vertex_offset=self.merged_object.shapekeys.vertex_count,
                ))
                self.merged_object.shapekeys.vertex_count += batch_vertex_count

            # Ensure offsets sanity
            shapekey_vertex_ids = self.buffers.get('ShapeKeyVertexId', None)
            if self.merged_object.shapekeys.vertex_count != len(shapekey_vertex_ids):
                raise ValueError(f'Total vertex count in ShapeKeyOffset across {batches_count} bathces does not match ShapeKeyVertexId size of {len(shapekey_vertex_ids)}!')

        # Build blend remap system metadata
        remapped_vgs_counts = self.buffers.pop('BlendRemapLayout', None)
        if remapped_vgs_counts is not None:
            remap_id = 0
            for component_id, vg_count in enumerate(remapped_vgs_counts.data.tolist()):
                if vg_count == 0:
                    continue
                component = self.merged_object.components[component_id]
                if vg_count > 256:            
                    raise ConfigError('component_collection', f'Component{component_id} 256 VG limit exceeded!\n'
                                      f'Currently it consists of {len(component.objects)} object(s) using total of {vg_count} VGs with non-zero weights.\n'
                                      f'Please reduce the number of non-empty VGs or split objects between different components.')
                component.blend_remap_id = remap_id
                component.blend_remap_vg_count = vg_count
                remap_id += 1
            self.merged_object.blend_remap_count = remap_id

        print(f'Total mesh data collection time: {time.time() - start_time :.3f}s')
    
    def build_mod_ini(self):
        start_time = time.time()

        ini_maker = IniMaker(
            cfg=self.cfg,
            mod_info=ModInfo(
                wwmi_tools_version=Version(self.cfg.wwmi_tools_version),
                required_wwmi_version=Version(self.cfg.required_wwmi_version),
                mod_name=self.cfg.mod_name,
                mod_author=self.cfg.mod_author,
                mod_desc=self.cfg.mod_desc,
                mod_link=self.cfg.mod_link,
                mod_logo=self.local_mod_logo_path,
            ),
            extracted_object=self.extracted_object,
            merged_object=self.merged_object,
            buffers=self.buffers,
            textures=self.textures,
            comment_code=self.cfg.comment_ini,
            skeleton_scale=self.cfg.skeleton_scale,
            unrestricted_custom_shape_keys=self.cfg.unrestricted_custom_shape_keys,
            slot_textures=self.slot_textures if self.cfg.texture_mode == 'SLOT' else None,
            path_textures=self.build_path_textures() if self.cfg.texture_mode == 'PATH' else None,
        )

        self.ini = ini_maker

        if self.cfg.custom_template_live_update:
            self.ini.start_live_write(self.context, self.cfg)
        else:
            self.ini.build_from_template(self.context, self.cfg, with_checksum=True)

        print(f'Total mod ini build time: {time.time() - start_time :.3f}s')

    def write_files(self):
        start_time = time.time()

        for buffer_name, buffer in self.buffers.items():
            print(f'Writing {buffer_name}.buf...')
            with open(self.meshes_path / f'{buffer_name}.buf', 'wb') as f:
                f.write(buffer.get_bytes())

        if not self.cfg.partial_export:
            # Write textures
            if self.cfg.copy_textures:
                for texture in self.textures:
                    texture_path = self.textures_path / texture.filename
                    if texture_path.is_file():
                        continue
                    print(f'Copying {texture_path.name}...')
                    shutil.copy(texture.path, texture_path)
            if self.cfg.texture_mode == 'SLOT' and self.slot_textures:
                self.write_slot_textures()
            elif self.cfg.texture_mode == 'PATH' and self.cfg.textures_ini == 'FROM_SLOT_HASH' and self.slot_textures:
                self.write_slot_textures()
            # Write mod logo
            mod_logo_path = resolve_path(self.cfg.mod_logo)
            if mod_logo_path.is_file():
                print(f'Copying {self.local_mod_logo_path.name}...')
                shutil.copy(mod_logo_path, self.local_mod_logo_path)
            # Write mod.ini
            if self.cfg.write_ini:
                self.ini.write(ini_path=self.mod_output_folder / 'mod.ini')
                # self.ini.write(ini_path=self.mod_output_folder / 'mod_old.ini', ini_string=self.ini.build_old())

            # Write ListGUI
            if self.cfg.use_list_gui:
                self.ini.write_list_gui(self.mod_output_folder)
                
        print(f'Disk write time: {time.time() - start_time :.3f}s')

    def build_path_textures(self):
        """Build the texture list for the PATH mode ini section.

        COPY_ALL_HASH: use all hash-collected textures, asset info looked up by
                       hash in ShaderTextureUsage.json.
        FROM_SLOT_HASH: use textures collected from node groups, hash/asset info
                       read from ShaderTextureUsage.json per slot.
        """
        usage = self._shader_texture_usage or {}
        if self.cfg.textures_ini == 'FROM_SLOT_HASH':
            result = []
            for st in self.slot_textures or []:
                asset_path = st.get('asset_path', '')
                resource_name = st.get('resource_name', '')
                asset_name = st.get('asset_name', '') or (
                    asset_path.rsplit('.', 1)[-1] if asset_path else resource_name)
                result.append({
                    'filename': st['dds_export_name'],
                    'hash': st.get('hash', ''),
                    'asset_path': asset_path,
                    'asset_name': asset_name,
                    'resource_name': resource_name,
                    'width': st.get('width', 0),
                    'height': st.get('height', 0),
                })
            return result

        # COPY_ALL_HASH: map hash -> metadata (asset_path/width/height) from ShaderTextureUsage.json
        hash_to_meta = {}
        for comp_data in usage.values():
            for vs_dict in comp_data.values():
                for slot_dict in vs_dict.values():
                    for entry in slot_dict.values():
                        h = entry.get('hash')
                        ap = entry.get('asset_path')
                        if h and ap and h not in hash_to_meta:
                            hash_to_meta[h] = entry

        result = []
        for texture in self.textures:
            meta = hash_to_meta.get(texture.hash, {})
            asset_path = meta.get('asset_path', '')
            resource_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', texture.filename.rsplit('.', 1)[0])
            asset_name = meta.get('asset_name', '') or (
                asset_path.rsplit('.', 1)[-1] if asset_path else resource_name)
            result.append({
                'filename': texture.filename,
                'hash': texture.hash,
                'asset_path': asset_path,
                'asset_name': asset_name,
                'resource_name': resource_name,
                'width': meta.get('width', 0),
                'height': meta.get('height', 0),
            })
        return result

    def _find_texture_format(self, dds_export_name):
        """Find the target format for a texture from material info. Returns format name string or 'BC7_UNORM'."""
        for component in self.merged_object.components:
            # Simple mode: check component.material
            if component.material:
                for ng in component.material.get('node_groups', []):
                    for inp in ng.get('inputs', []):
                        if inp.get('dds_export_name') == dds_export_name and inp.get('format') is not None:
                            return inp['format'].name
            # Complex mode: check temp_object.material
            for temp_object in component.objects:
                if temp_object.material is None:
                    continue
                for ng in temp_object.material.get('node_groups', []):
                    for inp in ng.get('inputs', []):
                        if inp.get('dds_export_name') == dds_export_name and inp.get('format') is not None:
                            return inp['format'].name
        return 'BC7_UNORM'

    def write_slot_textures(self):
        """Write textures for slot mode export."""
        import bpy

        if not self.cfg.export_textures:
            return

        texconv_path = Path(os.path.realpath(__file__)).parent.parent / 'DirectXTex' / 'texconv.exe'
        if not texconv_path.is_file():
            raise ConfigError('mod_output_folder', f'texconv.exe not found at {texconv_path}!')

        for slot_texture in self.slot_textures:
            image = slot_texture['image']
            dds_export_name = slot_texture['dds_export_name']
            dest_path = self.textures_path / dds_export_name

            # Check if image file is under Object Sources folder
            image_filepath = bpy.path.abspath(image.filepath)
            src_path = Path(image_filepath)

            if image.packed_file is None and src_path.is_file():
                try:
                    src_path.relative_to(self.object_source_folder)
                    # File is inside Object Sources folder
                    if src_path.suffix.lower() == '.dds':
                        print(f'Copying {dds_export_name}...')
                        shutil.copy(src_path, dest_path)
                        continue
                except ValueError:
                    pass  # Not under Object Sources folder, will convert

            # Image not in Object Sources or not a DDS - save as TGA then convert with texconv
            # Find the format to use for conversion
            target_format = self._find_texture_format(dds_export_name)

            # Save image as TGA RGBA 8-bit
            tga_name = dds_export_name.replace('.dds', '.tga')
            tga_path = self.textures_path / tga_name

            print(f'Converting {dds_export_name} via TGA...')

            # Tweak first pixel's alpha to force DDS encoder to preserve alpha channel.
            # When alpha is purely 0 or 255, some DDS compressors (BC3/BC7) can
            # optimize it away or collapse precision. Nudging the first pixel by
            # 1/255 keeps visual change invisible while forcing real alpha data.
            old_pixels = None
            try:
                pixels_tuple = image.pixels[:]
                if len(pixels_tuple) >= 4:
                    alpha_idx = 3
                    a_float = pixels_tuple[alpha_idx]
                    if a_float < 0.5:
                        new_float = a_float + 0.05
                    else:
                        new_float = a_float - 0.05
                    new_float = max(0.0, min(1.0, new_float))
                    if abs(new_float - a_float) > 1e-6:
                        old_pixels = pixels_tuple
                        pixels_list = list(pixels_tuple)
                        pixels_list[alpha_idx] = new_float
                        image.pixels = pixels_list
            except Exception as e:
                print(f"Warning: Failed to nudge alpha for '{image.name}': {e}")
                old_pixels = None

            # Save as TGA using save_render to force RGBA output
            try:
                scene = bpy.context.scene
                image_settings = scene.render.image_settings
                old_format = image_settings.file_format
                old_color_mode = image_settings.color_mode
                try:
                    image_settings.file_format = 'TARGA'
                    image_settings.color_mode = 'RGBA'
                    image.save_render(filepath=str(tga_path), scene=scene)
                finally:
                    image_settings.file_format = old_format
                    image_settings.color_mode = old_color_mode
            except Exception as e:
                print(f"Warning: Failed to save image '{image.name}' as TGA: {e}")
                if old_pixels is not None:
                    try:
                        image.pixels = old_pixels
                    except Exception:
                        pass
                continue

            # Restore original image pixels so the blend file is not modified
            if old_pixels is not None:
                try:
                    image.pixels = old_pixels
                except Exception as e:
                    print(f"Warning: Failed to restore pixels for '{image.name}': {e}")

            # Convert TGA to DDS using texconv
            cmd = [
                str(texconv_path),
                '-f', target_format,
                '-srgb',
                '-m', '1',
                '-y',
                '-o', str(self.textures_path),
                str(tga_path),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=60)
                if result.returncode != 0:
                    print(f"Warning: texconv failed for {tga_name}: {result.stderr.decode('utf-8', errors='replace')}")
            except subprocess.TimeoutExpired:
                print(f"Warning: texconv timed out for {tga_name}")
            except Exception as e:
                print(f"Warning: texconv error for {tga_name}: {e}")
            finally:
                # Clean up TGA file
                if tga_path.is_file():
                    tga_path.unlink()

    def compare_outputs(self, old_path: Path, new_path: Path):

        global data_models
        data_model = data_models['WWMI']

        for buffer_name, layout in data_model.buffers_format.items():

            print(f'Comparing {buffer_name}.buf buffers...')

            with open(old_path / (buffer_name + '.buf'), 'rb') as f1, open(new_path / (buffer_name + '.buf'), 'rb') as f2:
                
                old_buffer = NumpyBuffer(layout)
                old_buffer.import_raw_data(f1.read())

                new_buffer = NumpyBuffer(layout)
                new_buffer.import_raw_data(f2.read())

                for semantic in layout.semantics:

                    old_semantic_data = old_buffer.get_field(semantic.get_name()).tolist()
                    new_semantic_data = new_buffer.get_field(semantic.get_name()).tolist()

                    if old_semantic_data == new_semantic_data:
                        print(f'{buffer_name} {semantic.abstract} matches!')
                    else:
                        # print(f'{buffer_name} {semantic.abstract} differs:')

                        verbose = True
                        if buffer_name == 'Vector':
                            print(f'Comparing {semantic.abstract} in silent mode...')
                            verbose = False
                        else:
                            print(f'Comparing {semantic.abstract} in verbose mode...')

                        num_diffs = 0

                        for i in range(len(old_semantic_data)):
                            old_data = old_semantic_data[i]
                            new_data = new_semantic_data[i]

                            if old_data != new_data:
                                num_diffs += 1
                                if verbose:
                                    print(f'Element {i} diff: {old_data} != {new_data}')

                        print(f'Found {num_diffs} diffs (out of {len(old_semantic_data)} entries)')

def blender_export(operator, context, cfg, excluded_buffers):
    mod_exporter = ModExporter(context, cfg, excluded_buffers)
    mod_exporter.export_mod()

    # Report slot mode warnings
    slot_warnings = getattr(mod_exporter, '_slot_warnings', [])
    if slot_warnings:
        operator.report({'WARNING'}, "Slot mode warnings:\n" + "\n".join(slot_warnings))
