import json
import bpy

from pathlib import Path


def setup_materials(object_source_folder, imported_objects):
    """Setup materials for imported objects based on ShaderTextureUsage.json."""
    shader_usage_path = Path(object_source_folder) / 'ShaderTextureUsage.json'
    if not shader_usage_path.is_file():
        print(f"Warning: No ShaderTextureUsage.json found in '{object_source_folder}', skipping material setup")
        return

    with open(shader_usage_path, 'r') as f:
        shader_usage = json.load(f)

    for component_i in range(len(imported_objects)):
        component_key = f'Component {component_i}'
        if component_key not in shader_usage:
            print(f"Warning: '{component_key}' not found in ShaderTextureUsage.json, skipping")
            continue

        obj = imported_objects[component_i]
        if obj is None:
            continue

        vs_ps_data = shader_usage[component_key]
        _create_component_material(obj, component_key, object_source_folder, vs_ps_data, component_i)

    print(f"Material setup completed for {len(imported_objects)} components")


def _create_component_material(obj, component_key, object_source_folder, vs_ps_data, component_i):
    """Create material and setup nodes for a single component."""
    mat = bpy.data.materials.new(component_key)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # Clear default nodes, keep Material Output
    nodes.clear()

    output_node = nodes.new('ShaderNodeOutputMaterial')
    output_node.location = (2000, 0)

    # Layout parameters
    ng_start_x = 500
    ng_dx = 500
    ng_dy = 500
    texture_start_x = 0
    texture_spacing_x = 300
    texture_spacing_y = 300
    texture_col_size = 8

    ng_index = 0
    tex_index = 0
    tex_nodes_by_file = {}
    for vs_key, ps_dict in vs_ps_data.items():
        for ps_key, texture_slots in ps_dict.items():
            ng_name = f'vb={object_source_folder.name}-C{component_i}-{ps_key}'
            ng = _get_or_create_node_group(ng_name, texture_slots)

            # Node group: diagonal placement (each lower-right of previous)
            ng_node = nodes.new('ShaderNodeGroup')
            ng_node.node_tree = ng
            ng_node.name = ng_name
            ng_node.mute = True  # Disabled by default, user enables needed ones
            ng_node.location = (ng_start_x + ng_index * ng_dx, -(ng_index * ng_dy))
            if hasattr(ng_node, 'width'):
                ng_node.width = 400

            # Sort slots by index (ps-t0, ps-t1, ...)
            sorted_slots = sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1]))

            for slot_idx, slot_key in enumerate(sorted_slots):
                texture_info = texture_slots[slot_key]
                filename = texture_info.get('filename', '')
                file_key = filename if filename else f'{ng_name}_{slot_key}'

                # Reuse existing texture node if same image already created
                if file_key in tex_nodes_by_file:
                    tex_node = tex_nodes_by_file[file_key]
                else:
                    # Texture nodes: grid layout, 8 per column, overflow to left
                    col = tex_index // texture_col_size
                    row = tex_index % texture_col_size
                    tex_node = nodes.new('ShaderNodeTexImage')
                    tex_node.name = filename if filename else f'{ng_name}_{slot_key}'
                    tex_node.label = tex_node.name
                    tex_node.location = (texture_start_x - col * texture_spacing_x, -(row * texture_spacing_y))

                    # Load image
                    if filename:
                        img_path = Path(object_source_folder) / filename
                        if img_path.is_file():
                            img = bpy.data.images.load(str(img_path))
                            tex_node.image = img
                            fmt = texture_info.get('format', '')
                            img.colorspace_settings.name = 'sRGB' if 'SRGB' in fmt.upper() else 'Non-Color'
                            img.alpha_mode = 'CHANNEL_PACKED'

                    tex_nodes_by_file[file_key] = tex_node
                    tex_index += 1

                # Connect image texture to node group inputs
                links.new(tex_node.outputs['Color'], ng_node.inputs[slot_key])
                links.new(tex_node.outputs['Alpha'], ng_node.inputs[f'{slot_key} alpha'])

            # Mute all links to node group inputs (disabled by default)
            for inp in ng_node.inputs:
                for link in inp.links:
                    link.is_muted = True

            ng_index += 1

    # Assign material to object
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _get_or_create_node_group(ng_name, texture_slots):
    """Get existing node group or create a new pass-through node group."""
    if ng_name in bpy.data.node_groups:
        return bpy.data.node_groups[ng_name]

    ng = bpy.data.node_groups.new(ng_name, 'ShaderNodeTree')

    # Sort slots by index
    sorted_slots = sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1]))

    # Create inputs and outputs: ps-tN (Color) and ps-tN alpha (Float)
    for slot_key in sorted_slots:
        ng.interface.new_socket(slot_key, socket_type='NodeSocketColor', in_out='INPUT')
        ng.interface.new_socket(f'{slot_key} alpha', socket_type='NodeSocketFloat', in_out='INPUT')
        ng.interface.new_socket(slot_key, socket_type='NodeSocketColor', in_out='OUTPUT')
        ng.interface.new_socket(f'{slot_key} alpha', socket_type='NodeSocketFloat', in_out='OUTPUT')

    # Create internal nodes: Group Input → Group Output (pass-through)
    input_node = ng.nodes.new('NodeGroupInput')
    input_node.location = (-400, 0)

    output_node = ng.nodes.new('NodeGroupOutput')
    output_node.location = (400, 0)

    # Connect all inputs directly to outputs
    for slot_key in sorted_slots:
        ng.links.new(input_node.outputs[slot_key], output_node.inputs[slot_key])
        ng.links.new(input_node.outputs[f'{slot_key} alpha'], output_node.inputs[f'{slot_key} alpha'])

    return ng
