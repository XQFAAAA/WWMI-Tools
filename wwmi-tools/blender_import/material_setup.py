import json
import re
import bpy

from pathlib import Path

# Texture roles by filename suffix
_TEXTURE_ROLES = ('N', 'FTM', 'RGID', 'D', 'HN')

# Role -> texture format, used to identify a role when the filename carries no suffix
_ROLE_FORMATS = {
    'D': 'BC7_UNORM_SRGB',
    'N': 'BC7_UNORM',
    'FTM': 'BC3_UNORM',
    'RGID': 'R8_UNORM',
    'HN': 'B8G8R8A8_UNORM',
}

# Role textures must be square and at least this many pixels per side
_ROLE_MIN_SIZE = 256

# Non-role textures that are not square, or have a side <= this limit, get their
# links disabled
_SQUARE_SIZE_LIMIT = 512

# Non-role square textures of these formats stay connected regardless of size
_SQUARE_FORMATS = ('BC3_UNORM', 'BC3_UNORM_SRGB')


def _get_texture_role(filename):
    """Determine texture role from filename suffix: N/FTM/RGID/D/HN, else None.

    e.g. T_R2T1XuanlingMd10011Bangs_FTM.dds -> FTM
         T_R2T1XuanlingMd10011Bangs_D.dds    -> D
    """
    stem = Path(filename).stem
    match = re.search(r'_([A-Za-z0-9]+)$', stem)
    if not match:
        return None
    suffix = match.group(1).upper()
    if suffix in _TEXTURE_ROLES:
        return suffix
    return None


def _get_slot_interface_name(slot_key, texture_format):
    """Return the node group input socket name for a slot.

    Full form: 'ps-t0: BC7_UNORM_SRGB'; the matching alpha socket is
    '————————> ps-t0 alpha' (see _get_slot_alpha_name), which carries no
    format suffix.
    Falls back to the plain slot name when no format is available.
    """
    if texture_format:
        return f'{slot_key}: {texture_format}'
    return slot_key


def _get_slot_alpha_name(slot_key):
    """Alpha input socket name: '————————> ps-t0 alpha'.

    Carries no format suffix; the leading separator keeps it visually apart
    from the colour input while the slot key preserves uniqueness.
    """
    return f'————————> {slot_key} alpha'


def _get_slot_output_name(slot_key, alpha=False):
    """Output-side interface name: 'ps-t0' (alpha -> 'ps-t0 alpha')."""
    return f'{slot_key} alpha' if alpha else slot_key


def _is_role_size(info):
    """True when the texture is square and at least _ROLE_MIN_SIZE pixels per side."""
    width = info.get('width') or 0
    return width >= _ROLE_MIN_SIZE and width == (info.get('height') or 0)


def _resolve_role_slots(texture_slots):
    """Resolve the D/N/FTM/RGID/HN role slots for a texture-slot dict.

    Returns {role: slot_key}, with None for roles that are not present. Only
    square textures of at least _ROLE_MIN_SIZE px per side can be role textures.
    Prefers the filename suffix (first slot per role wins); when no suffix is
    found, falls back to matching by texture format, preferring the largest
    width, then the earliest slot.
    HN is only searched while one of the other four roles is still missing.
    """
    sorted_slots = sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1]))

    eligible = [s for s in sorted_slots if _is_role_size(texture_slots[s])]

    # Assign texture roles from the filename suffix: first slot per role wins
    slot_roles = {}
    for slot_key in eligible:
        role = _get_texture_role(texture_slots[slot_key].get('filename', ''))
        if role and role not in slot_roles.values():
            slot_roles[slot_key] = role

    def _by_suffix(role):
        return next((s for s, r in slot_roles.items() if r == role), None)

    def _by_format(role):
        candidates = [s for s in eligible
                      if texture_slots[s].get('format', '') == _ROLE_FORMATS[role]]
        if not candidates:
            return None
        return max(candidates,
                   key=lambda s: (texture_slots[s].get('width', 0), -sorted_slots.index(s)))

    if slot_roles:
        picker = _by_suffix
        roles = {role: _by_suffix(role) for role in ('D', 'N', 'FTM', 'RGID')}
    else:
        picker = _by_format
        normal_slot = _by_format('N')
        rgid_slot = _by_format('RGID')
        # N and ID must both be matched, otherwise connect neither
        if normal_slot is None or rgid_slot is None:
            normal_slot = None
            rgid_slot = None
        roles = {
            'D': _by_format('D'),
            'N': normal_slot,
            'FTM': _by_format('FTM'),
            'RGID': rgid_slot,
        }

    # Skip HN once all of the other four roles have been found
    roles['HN'] = None if all(roles.values()) else picker('HN')
    return roles


def _group_matches_slots(ng, texture_slots):
    """True if the node group already exposes input sockets for all given slots.

    Decides whether an existing group with the same base name can be reused,
    or whether it was built for a different texture configuration and a
    suffixed group should be created instead.
    """
    existing = {item.name for item in ng.interface.items_tree}
    for slot_key in sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1])):
        iface_key = _get_slot_interface_name(slot_key, texture_slots[slot_key].get('format', ''))
        if iface_key not in existing or _get_slot_alpha_name(slot_key) not in existing:
            return False
    return True


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
    group_entries = []
    for vs_key, ps_dict in vs_ps_data.items():
        for ps_key, texture_slots in ps_dict.items():
            ng_name = f'vb={object_source_folder.name}-C{component_i}-{ps_key}'
            ng, ng_name = _get_or_create_node_group(ng_name, texture_slots)

            # Node group: diagonal placement (each lower-right of previous)
            ng_node = nodes.new('ShaderNodeGroup')
            ng_node.node_tree = ng
            ng_node.name = ng_name
            ng_node.location = (ng_start_x + ng_index * ng_dx, -(ng_index * ng_dy))
            if hasattr(ng_node, 'width'):
                ng_node.width = 400

            # Mute state is decided after all groups are built (see below)
            link_states = []

            # Sort slots by index (ps-t0, ps-t1, ...)
            sorted_slots = sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1]))
            # D/N/FTM/RGID/HN role slots keep their connections; every other slot
            # that is not a square texture with both sides > _SQUARE_SIZE_LIMIT
            # gets its links disabled.
            role_slot_set = {s for s in _resolve_role_slots(texture_slots).values() if s}

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

                # Disable the link for non-role textures that are not square or
                # have a side <= _SQUARE_SIZE_LIMIT; square BC3 textures are kept
                # connected regardless of size
                disable_link = False
                if slot_key not in role_slot_set:
                    width = texture_info.get('width') or 0
                    height = texture_info.get('height') or 0
                    if width > 0 and height > 0:
                        is_square_format = (width == height
                                            and texture_info.get('format', '') in _SQUARE_FORMATS)
                        disable_link = not is_square_format and (
                            width <= _SQUARE_SIZE_LIMIT
                            or height <= _SQUARE_SIZE_LIMIT
                            or width != height)

                # Connect image texture to node group inputs
                iface_name = _get_slot_interface_name(slot_key, texture_info.get('format', ''))
                link_color = links.new(tex_node.outputs['Color'], ng_node.inputs[iface_name])
                link_alpha = links.new(tex_node.outputs['Alpha'], ng_node.inputs[_get_slot_alpha_name(slot_key)])
                link_states.append((link_color, link_alpha, disable_link))

            group_entries.append({
                'node': ng_node,
                'index': ng_index,
                'input_count': len(ng_node.inputs),
                'link_states': link_states,
            })
            ng_index += 1

    # Activate the node group with the most input sockets, ties going to the one
    # that comes first. Only that group is unmuted and wired to the material
    # output; the others are muted and left unconnected so the user can wire them
    # manually.
    active_entry = max(group_entries, key=lambda e: (e['input_count'], -e['index'])) \
        if group_entries else None
    for entry in group_entries:
        is_active = entry is active_entry
        entry['node'].mute = not is_active
        for link_color, link_alpha, disable_link in entry['link_states']:
            if not is_active or disable_link:
                link_color.is_muted = True
                link_alpha.is_muted = True
        if is_active:
            diffuse_out = entry['node'].outputs.get('Diffuse')
            links.new(diffuse_out if diffuse_out is not None else entry['node'].outputs['BSDF'],
                      output_node.inputs['Surface'])

    # Assign material to object
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _get_or_create_node_group(ng_name, texture_slots):
    """Get existing node group or create a new one with the full WWMI shader structure.

    Returns (node_group, final_name). If the base name is already taken by a group
    built for a different texture configuration, a '.001' style suffix is appended
    to the new group; an existing group whose interface matches the requested slots
    is reused as-is.
    """
    final_name = ng_name
    index = 1
    while True:
        existing = bpy.data.node_groups.get(final_name)
        if existing is None or _group_matches_slots(existing, texture_slots):
            break
        final_name = f'{ng_name}.{index:03d}'
        index += 1

    if final_name in bpy.data.node_groups:
        return bpy.data.node_groups[final_name], final_name

    ng = bpy.data.node_groups.new(final_name, 'ShaderNodeTree')
    nodes = ng.nodes
    links = ng.links

    # Sort slots by index
    sorted_slots = sorted(texture_slots.keys(), key=lambda s: int(s.split('-t')[-1]))

    role_slots = _resolve_role_slots(texture_slots)
    d_slot = role_slots['D']
    normal_slot = role_slots['N']
    ftm_slot = role_slots['FTM']
    rgid_slot = role_slots['RGID']

    # Interface name per slot (full form, e.g. ps-t0 -> 'ps-t0: BC7_UNORM_SRGB')
    slot_iface = {k: _get_slot_interface_name(k, texture_slots[k].get('format', '')) for k in sorted_slots}

    # Create interface outputs (only for roles present in this node group,
    # so every output socket has an internal connection).
    # Diffuse sits above BSDF so it is the first (auto-connected) output.
    output_defs = []
    if d_slot:
        ng.interface.new_socket('Diffuse', socket_type='NodeSocketColor', in_out='OUTPUT')
    ng.interface.new_socket('BSDF', socket_type='NodeSocketShader', in_out='OUTPUT')
    if d_slot:
        output_defs.append(('阴影', 'NodeSocketFloat'))
    if normal_slot:
        output_defs.append(('Normal', 'NodeSocketColor'))
        output_defs.append(('Roughness', 'NodeSocketFloat'))
    if ftm_slot:
        output_defs.append(('Metallic', 'NodeSocketFloat'))
        output_defs.append(('战损', 'NodeSocketFloat'))
        output_defs.append(('Alpha', 'NodeSocketFloat'))
    if rgid_slot:
        output_defs.append(('ID', 'NodeSocketColor'))
    for name, socket_type in output_defs:
        ng.interface.new_socket(name, socket_type=socket_type, in_out='OUTPUT')

    # ps-t output panel (ps-tN / ps-tN alpha pass-through sockets), collapsed by default
    ps_output_panel = ng.interface.new_panel('ps-t', default_closed=True)
    for slot_key in sorted_slots:
        out_key = _get_slot_output_name(slot_key)
        alpha_out_key = _get_slot_output_name(slot_key, alpha=True)
        ng.interface.new_socket(out_key, socket_type='NodeSocketColor', in_out='OUTPUT', parent=ps_output_panel)
        ng.interface.new_socket(alpha_out_key, socket_type='NodeSocketFloat', in_out='OUTPUT', parent=ps_output_panel)

    # ps-t input panel (ps-tN / ps-tN alpha image inputs)
    ps_input_panel = ng.interface.new_panel('ps-t input', default_closed=False)
    for slot_key in sorted_slots:
        iface_key = slot_iface[slot_key]
        socket = ng.interface.new_socket(iface_key, socket_type='NodeSocketColor', in_out='INPUT', parent=ps_input_panel)
        socket.default_value = (0.0, 0.0, 0.0, 1.0)
        ng.interface.new_socket(_get_slot_alpha_name(slot_key), socket_type='NodeSocketFloat', in_out='INPUT', parent=ps_input_panel)

    # Group Input / Group Output
    input_node = nodes.new('NodeGroupInput')
    input_node.location = (-1080, 0)
    output_node = nodes.new('NodeGroupOutput')
    output_node.location = (612, -71)

    # Principled BSDF
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.name = 'Principled BSDF'
    bsdf.location = (231, 613)
    bsdf.inputs['Roughness'].default_value = 1.0

    # N role: 法线补B通道 (normal B-channel reconstruction) + Normal Map
    normal_fix_node = None
    if normal_slot:
        normal_fix = _get_or_create_normal_fix_group()
        normal_fix_node = nodes.new('ShaderNodeGroup')
        normal_fix_node.node_tree = normal_fix
        normal_fix_node.name = '法线补B通道'
        normal_fix_node.width = 245
        normal_fix_node.location = (-412, 285)
        links.new(input_node.outputs[slot_iface[normal_slot]], normal_fix_node.inputs['是'])
        normal_fix_node.inputs['反转G'].default_value = True

        normal_map = nodes.new('ShaderNodeNormalMap')
        normal_map.name = 'Normal Map'
        normal_map.width = 180
        normal_map.location = (-39, 444)
        links.new(normal_fix_node.outputs['计算后法线'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    # FTM role: Separate Color (R->Alpha, G->Metallic, B->战损)
    separate_node = None
    if ftm_slot:
        separate_node = nodes.new('ShaderNodeSeparateColor')
        separate_node.name = 'Separate Color'
        separate_node.location = (-380, 47)
        links.new(input_node.outputs[slot_iface[ftm_slot]], separate_node.inputs['Color'])

    # D role: connect D texture directly to BSDF Base Color
    if d_slot:
        links.new(input_node.outputs[slot_iface[d_slot]], bsdf.inputs['Base Color'])

    # Principled BSDF inputs
    if separate_node:
        links.new(separate_node.outputs['Green'], bsdf.inputs['Metallic'])
        links.new(separate_node.outputs['Red'], bsdf.inputs['Alpha'])
    if normal_slot:
        links.new(input_node.outputs[_get_slot_alpha_name(normal_slot)], bsdf.inputs['Roughness'])

    # Group output connections
    links.new(bsdf.outputs['BSDF'], output_node.inputs['BSDF'])
    if d_slot:
        links.new(input_node.outputs[slot_iface[d_slot]], output_node.inputs['Diffuse'])
        links.new(input_node.outputs[_get_slot_alpha_name(d_slot)], output_node.inputs['阴影'])
    if normal_slot and normal_fix_node:
        links.new(normal_fix_node.outputs['计算后法线'], output_node.inputs['Normal'])
        links.new(input_node.outputs[_get_slot_alpha_name(normal_slot)], output_node.inputs['Roughness'])
    if separate_node:
        links.new(separate_node.outputs['Green'], output_node.inputs['Metallic'])
        links.new(separate_node.outputs['Blue'], output_node.inputs['战损'])
        links.new(separate_node.outputs['Red'], output_node.inputs['Alpha'])
    if rgid_slot:
        links.new(input_node.outputs[slot_iface[rgid_slot]], output_node.inputs['ID'])

    # Pass-through: ps-tN and ps-tN alpha
    for slot_key in sorted_slots:
        iface_key = slot_iface[slot_key]
        out_key = _get_slot_output_name(slot_key)
        alpha_out_key = _get_slot_output_name(slot_key, alpha=True)
        links.new(input_node.outputs[iface_key], output_node.inputs[out_key])
        links.new(input_node.outputs[_get_slot_alpha_name(slot_key)], output_node.inputs[alpha_out_key])

    # Safety net: remove any output socket that ended up without an internal connection
    for item in list(ng.interface.items_tree):
        if item.item_type == 'SOCKET' and getattr(item, 'in_out', None) == 'OUTPUT':
            output_socket = output_node.inputs.get(item.name)
            if output_socket is None or not output_socket.is_linked:
                ng.interface.remove(item)

    return ng, final_name


def _get_or_create_normal_fix_group():
    """Create the reusable '法线补B通道' node group (normal map B-channel reconstruction).

    Reconstructs the B channel of a normal map from R and G channels:
      B = (sqrt(max(0, 1 - (R*2-1)^2 - (G*2-1)^2)) + 1) / 2
    Also supports inverting G channel.
    """
    ng_name = '法线补B通道'
    if ng_name in bpy.data.node_groups:
        return bpy.data.node_groups[ng_name]

    ng = bpy.data.node_groups.new(ng_name, 'ShaderNodeTree')
    nodes = ng.nodes
    links = ng.links

    # Interface
    input_is = ng.interface.new_socket('是', socket_type='NodeSocketColor', in_out='INPUT')
    input_is.default_value = (0.8, 0.8, 0.8, 1.0)
    ng.interface.new_socket('反转G', socket_type='NodeSocketBool', in_out='INPUT')
    ng.interface.new_socket('计算后法线', socket_type='NodeSocketColor', in_out='OUTPUT')
    ng.interface.new_socket('原B通道', socket_type='NodeSocketFloat', in_out='OUTPUT')

    input_node = nodes.new('NodeGroupInput')
    input_node.location = (-515, 20)
    output_node = nodes.new('NodeGroupOutput')
    output_node.location = (1216, -103)

    # Separate color channels
    sep = nodes.new('ShaderNodeSeparateColor')
    sep.name = 'Separate Color'
    sep.location = (-210, 94)
    links.new(input_node.outputs['是'], sep.inputs['Color'])

    comb = nodes.new('ShaderNodeCombineColor')
    comb.name = 'Combine Color'
    comb.location = (751, 134)

    # R * 2 - 1
    math_r = nodes.new('ShaderNodeMath')
    math_r.name = 'Math'
    math_r.operation = 'MULTIPLY_ADD'
    math_r.inputs[1].default_value = 2.0
    math_r.inputs[2].default_value = -1.0
    math_r.location = (16, 939)
    links.new(sep.outputs['Red'], math_r.inputs[0])

    # G * 2 - 1
    math_g = nodes.new('ShaderNodeMath')
    math_g.name = 'Math.001'
    math_g.operation = 'MULTIPLY_ADD'
    math_g.inputs[1].default_value = 2.0
    math_g.inputs[2].default_value = -1.0
    math_g.location = (12, 724)
    links.new(sep.outputs['Green'], math_g.inputs[0])

    # (R*2-1)^2
    math_r2 = nodes.new('ShaderNodeMath')
    math_r2.name = 'Math.002'
    math_r2.operation = 'POWER'
    math_r2.inputs[1].default_value = 2.0
    math_r2.location = (223, 953)
    links.new(math_r.outputs[0], math_r2.inputs[0])

    # (G*2-1)^2
    math_g2 = nodes.new('ShaderNodeMath')
    math_g2.name = 'Math.003'
    math_g2.operation = 'POWER'
    math_g2.inputs[1].default_value = 2.0
    math_g2.location = (228, 730)
    links.new(math_g.outputs[0], math_g2.inputs[0])

    # (R*2-1)^2 + (G*2-1)^2
    math_sum = nodes.new('ShaderNodeMath')
    math_sum.name = 'Math.004'
    math_sum.location = (442, 882)
    links.new(math_r2.outputs[0], math_sum.inputs[0])
    links.new(math_g2.outputs[0], math_sum.inputs[1])

    # 1 - sum
    value_one = nodes.new('ShaderNodeValue')
    value_one.name = 'Value'
    value_one.outputs[0].default_value = 1.0
    value_one.location = (458, 987)
    math_one_minus = nodes.new('ShaderNodeMath')
    math_one_minus.name = 'Math.005'
    math_one_minus.operation = 'SUBTRACT'
    math_one_minus.location = (647, 984)
    links.new(value_one.outputs[0], math_one_minus.inputs[0])
    links.new(math_sum.outputs[0], math_one_minus.inputs[1])

    # sqrt(1 - sum)
    math_sqrt = nodes.new('ShaderNodeMath')
    math_sqrt.name = 'Math.006'
    math_sqrt.operation = 'SQRT'
    math_sqrt.location = (832, 939)
    links.new(math_one_minus.outputs[0], math_sqrt.inputs[0])

    # sqrt(1-sum) + 1
    value_one_b = nodes.new('ShaderNodeValue')
    value_one_b.name = 'Value.001'
    value_one_b.outputs[0].default_value = 1.0
    value_one_b.location = (62, 407)
    math_b_add = nodes.new('ShaderNodeMath')
    math_b_add.name = 'Math.007'
    math_b_add.location = (239, 504)
    links.new(math_sqrt.outputs[0], math_b_add.inputs[0])
    links.new(value_one_b.outputs[0], math_b_add.inputs[1])

    # (sqrt(1-sum)+1) / 2
    math_b_div = nodes.new('ShaderNodeMath')
    math_b_div.name = 'Math.008'
    math_b_div.operation = 'DIVIDE'
    math_b_div.inputs[1].default_value = 2.0
    math_b_div.location = (423, 502)
    links.new(math_b_add.outputs[0], math_b_div.inputs[0])

    # 1 - G (for 反转G option)
    math_g_inv = nodes.new('ShaderNodeMath')
    math_g_inv.name = 'Math.009'
    math_g_inv.operation = 'SUBTRACT'
    math_g_inv.inputs[0].default_value = 1.0
    math_g_inv.location = (130, 52)
    links.new(sep.outputs['Green'], math_g_inv.inputs[1])

    # Mix: Factor = 反转G, A = G, B = 1-G
    mix = nodes.new('ShaderNodeMix')
    mix.name = 'Mix'
    mix.location = (378, 169)
    links.new(input_node.outputs['反转G'], mix.inputs['Factor'])
    links.new(sep.outputs['Green'], mix.inputs['A'])
    links.new(math_g_inv.outputs[0], mix.inputs['B'])

    # Combine: R = R, G = Mix, B = Math.008
    links.new(sep.outputs['Red'], comb.inputs['Red'])
    links.new(mix.outputs[0], comb.inputs['Green'])
    links.new(math_b_div.outputs[0], comb.inputs['Blue'])

    # Outputs
    links.new(comb.outputs[0], output_node.inputs['计算后法线'])
    links.new(sep.outputs['Blue'], output_node.inputs['原B通道'])

    return ng
