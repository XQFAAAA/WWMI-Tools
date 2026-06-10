import time
import shutil
import json
import re
import hashlib
import os
import subprocess

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
        self._shader_texture_usage = kwargs.pop('shader_texture_usage', None)
        super().__init__(**kwargs)

    def fill_missing_temp_objects_data(self):
        objects = [temp_object.object for component in self.components for temp_object in component.objects]
        self.fill_missing_data(objects)

    def pre_join_objects(self):
        if self._texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            self._collect_slot_material_info()

    def _collect_slot_material_info(self):
        """Collect material info from TempObjects for slot mode export."""
        if self._shader_texture_usage is None:
            return

        shader_texture_usage = self._shader_texture_usage
        is_simple = self._texture_mode == 'SLOT_SIMPLE'

        # Regex for material name matching
        material_pattern = re.compile(r'.*component[_ -]*(\d+).*', re.IGNORECASE)
        # Regex for node group name matching: vs=xxx-ps=xxx
        node_group_pattern = re.compile(r'vs=([0-9a-f]+)-ps=([0-9a-f]+)')

        # Collect all unique images across all components for slot_textures
        all_images = {}  # key: dds_export_name, value: dict with image info

        for component in self.components:
            component_match_formats = {}  # key: match_format enum value, value: dict with match_format info
            component_material_collected = False  # For simple mode: only collect first material

            for temp_object in component.objects:
                obj = temp_object.object
                material_info = {
                    'material_name': None,
                    'material_index': None,
                    'node_groups': [],
                }

                # Find material matching component pattern
                matched_material = None
                if obj.data.materials:
                    for mat in obj.data.materials:
                        if mat is None:
                            continue
                        match = material_pattern.match(mat.name)
                        if match:
                            if matched_material is not None:
                                print(f"Warning: Multiple materials matching component pattern found on '{obj.name}', using first match '{matched_material.name}'")
                                break
                            matched_material = mat
                            material_info['material_name'] = mat.name
                            material_info['material_index'] = match.group(1)

                            # Check material Component index matches object Component index
                            obj_name = obj.name
                            if obj_name.startswith('TEMP_'):
                                obj_name = obj_name[5:]
                            obj_match = material_pattern.match(obj_name)
                            if obj_match:
                                obj_component_id = obj_match.group(1)
                                mat_component_id = match.group(1)
                                if obj_component_id != mat_component_id:
                                    raise ConfigError('object_source_folder',
                                        f"Material Component index ({mat_component_id}) doesn't match object Component index ({obj_component_id})!\n"
                                        f"Object: '{obj_name}', Material: '{mat.name}'")

                # In simple mode, skip if component already has material collected
                if is_simple and component_material_collected:
                    continue

                # Find node groups in the matched material (or all materials if no match)
                if matched_material is not None and matched_material.use_nodes:
                    node_group_materials = [matched_material]
                elif obj.data.materials:
                    node_group_materials = [
                        mat for mat in obj.data.materials
                        if mat is not None and mat.use_nodes
                    ]
                else:
                    node_group_materials = []

                for mat in node_group_materials:
                    for node in mat.node_tree.nodes:
                        if node.type != 'GROUP':
                            continue
                        if node.node_tree is None:
                            continue
                        # Check if node group is muted (M key toggles mute)
                        if node.mute:
                            continue
                        ng_match = node_group_pattern.match(node.node_tree.name)
                        if not ng_match:
                            continue

                        vs_value = ng_match.group(1)
                        ps_value = ng_match.group(2)
                        node_group_info = {
                            'name': node.node_tree.name,
                            'vs': vs_value,
                            'ps': ps_value,
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

                            # Determine format from ShaderTextureUsage.json
                            format_enum = None
                            match_format_enum = None
                            if material_info['material_index'] is not None:
                                component_key = f"Component {material_info['material_index']}"
                                if component_key in shader_texture_usage:
                                    vs_key = f"vs={vs_value}"
                                    ps_key = f"ps={ps_value}"
                                    if vs_key in shader_texture_usage[component_key]:
                                        if ps_key in shader_texture_usage[component_key][vs_key]:
                                            slot_data = shader_texture_usage[component_key][vs_key][ps_key]
                                            if input_name in slot_data:
                                                format_str = slot_data[input_name].get('format', '')
                                                if format_str:
                                                    try:
                                                        format_enum = DXGIFormatIndex[format_str]
                                                        match_format_enum = format_enum.to_typeless()
                                                    except KeyError:
                                                        print(f"Warning: Unknown format '{format_str}' for {input_name}")

                            # Generate resource_name and dds_export_name: use hash if non-ASCII
                            sanitized = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_name)
                            has_non_ascii = any(ord(c) > 127 for c in base_name)
                            if has_non_ascii:
                                name_hash = hashlib.sha256(base_name.encode('utf-8')).hexdigest()[:16]
                                dds_export_name = f'{name_hash}.dds'
                                resource_name = name_hash
                            else:
                                dds_export_name = base_name + '.dds'
                                resource_name = sanitized

                            if match_format_enum is not None:
                                prefix = match_format_enum.name.split('_')[0]
                                ascii_digits = ''.join(str(ord(c)) for c in prefix)
                                filter_index = float(f"83.{ascii_digits}")
                            else:
                                filter_index = 0.0

                            input_info = {
                                'slot': input_name,
                                'format': format_enum,
                                'match_format': match_format_enum,
                                'filter_index': filter_index,
                                'image': image,
                                'dds_export_name': dds_export_name,
                                'resource_name': resource_name,
                            }
                            node_group_info['inputs'].append(input_info)

                            # Add to component match_formats
                            if match_format_enum is not None and match_format_enum.value not in component_match_formats:
                                if is_simple:
                                    # Simple mode: single match_format per entry
                                    component_match_formats[match_format_enum.value] = {
                                        'match_format': match_format_enum,
                                        'filter_index': float(f"83.{''.join(str(ord(c)) for c in match_format_enum.name.split('_')[0])}"),
                                    }
                                else:
                                    # Complex mode: include all same-prefix formats
                                    same_prefix_formats = match_format_enum.get_same_prefix_formats()
                                    component_match_formats[match_format_enum.value] = {
                                        'match_format': match_format_enum,
                                        'match_formats': same_prefix_formats,
                                        'filter_index': float(f"83.{''.join(str(ord(c)) for c in match_format_enum.name.split('_')[0])}"),
                                    }

                            # Add to all_images (deduplicate by dds_export_name)
                            if dds_export_name not in all_images:
                                all_images[dds_export_name] = {
                                    'image': image,
                                    'dds_export_name': dds_export_name,
                                    'resource_name': resource_name,
                                }

                        if node_group_info['inputs']:
                            material_info['node_groups'].append(node_group_info)

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

        print(f"Slot mode ({'simple' if is_simple else 'complex'}): collected {len(self._slot_textures)} unique textures across {len(self.components)} components")

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

    def __init__(self, context, cfg, excluded_buffers: List[str]):
        self.context = context
        self.cfg = cfg
        self.excluded_buffers = excluded_buffers

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

    def build_merged_object(self):
        start_time = time.time()
        
        # Read ShaderTextureUsage.json for slot mode
        shader_texture_usage = None
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            shader_usage_path = self.object_source_folder / 'ShaderTextureUsage.json'
            if not shader_usage_path.is_file():
                raise ConfigError('object_source_folder', 'ShaderTextureUsage.json not found in object source folder. This file is required for slot mode export.')
            with open(shader_usage_path, 'r', encoding='utf-8') as f:
                shader_texture_usage = json.load(f)

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
            shader_texture_usage=shader_texture_usage,
        )
        self.merged_object = object_merger.merged_object

        # Collect slot_textures from object_merger
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            self.slot_textures = getattr(object_merger, '_slot_textures', [])
            
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
            slot_textures=self.slot_textures if self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX') else None,
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
            if self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX') and self.slot_textures:
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
                
        print(f'Disk write time: {time.time() - start_time :.3f}s')

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

            # Save image as TGA (restore original settings to avoid modifying blend file)
            _orig_filepath = image.filepath_raw
            _orig_file_format = image.file_format
            try:
                image.filepath_raw = str(tga_path)
                image.file_format = 'TARGA'
                image.save()
            except Exception as e:
                print(f"Warning: Failed to save image '{image.name}' as TGA: {e}")
                continue
            finally:
                image.filepath_raw = _orig_filepath
                image.file_format = _orig_file_format

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
