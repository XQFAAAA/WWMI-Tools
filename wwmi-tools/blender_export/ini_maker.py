import hashlib
import os
import time
import uuid
import bpy

from typing import List, Dict, Union, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from datetime import datetime

from ..addon.settings import WWMI_Settings
from ..migoto_io.blender_interface.utility import *
from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.objects import *
from ..migoto_io.blender_interface.mesh import *

from ..migoto_io.data_model.byte_buffer import NumpyBuffer

from ..extract_frame_data.metadata_format import ExtractedObject

from .object_merger import MergedObject, SkeletonType
from .metadata_collector import Version, ModInfo
from .texture_collector import Texture
from .text_formatter import TextFormatter

from ..libs.jinja2 import Template, TemplateSyntaxError, UndefinedError
from ..libs.unidecode import unidecode


chached_template: Optional[str] = None
chached_template_string: Optional[Template] = None


@dataclass
class IniMaker:
    # Input
    cfg: WWMI_Settings
    mod_info: ModInfo
    extracted_object: ExtractedObject
    merged_object: MergedObject
    buffers: Dict[str, NumpyBuffer]
    textures: List[Texture]
    comment_code: bool
    unrestricted_custom_shape_keys: bool
    skeleton_scale: float
    slot_textures: list = None
    path_textures: list = None
    path_hash_textures: list = None
    path_complex: dict = None
    slot_groups: dict = None
    menu_switches: list = field(default_factory=list)
    formatter: TextFormatter = TextFormatter()
    # Generated
    namespace: str = field(init=False)
    # Output
    ini_string: str = field(init=False)
    
    def __post_init__(self):
        mod_name = self.mod_info.mod_name
        if mod_name.strip() == 'Unnamed Mod':
            # Default mod name: use a random hash for the namespace
            self.namespace = 'Mods\\' + uuid.uuid4().hex
        else:
            self.namespace = 'Mods\\' + unidecode(mod_name).replace(' ', '')

    def start_live_write(self, context, cfg):
        thread = Thread(target=self.live_write_thread, args=(context, cfg))
        thread.start()

    def live_write_thread(self, context, cfg):
        print('Started live ini updates.')
        
        if cfg.custom_template_source == 'INTERNAL':
            text = bpy.data.texts["CustomIniTemplate"]
            template_string = None
            custom_template_path = None
            mod_time = None
        else:
            custom_template_path = resolve_path(cfg.custom_template_path)
            mod_time = custom_template_path.stat().st_mtime
        
        while True:

            if not cfg.custom_template_live_update:
                print('Stopped live ini updates.')
                return

            if mod_time is None:
                new_template_string = text.as_string()
                template_updated = template_string != new_template_string
                if template_updated:
                    template_string = new_template_string
            else:
                new_mod_time = custom_template_path.stat().st_mtime
                template_updated = mod_time != new_mod_time
                if template_updated:
                    new_template_string = self.get_custom_template(context, cfg)
                    mod_time = new_mod_time

            if template_updated:
                try:
                    result = self.build_from_template(context, cfg, template_string=new_template_string, with_checksum=True)
                except ValueError as e:
                    result = str(e)
                except Exception as e:
                    import traceback
                    result = f'Ini Template error:\n\n{str(e)}\n\n\n{traceback.format_exc()}'

                self.write(ini_string=result)

            time.sleep(0.05)      

    @staticmethod
    def get_default_template(context, cfg, remove_code_comments = False):

        default_templates_path = Path(os.path.realpath(__file__)).parent.parent / 'templates'

        if cfg.mod_skeleton_type == 'MERGED':
            default_template_path = default_templates_path / 'merged.ini.j2'
        elif cfg.mod_skeleton_type == 'MERGED_INSTANCE':
            default_template_path = default_templates_path / 'merged_instance.ini.j2'
        elif cfg.mod_skeleton_type == 'COMPONENT':
            default_template_path = default_templates_path / 'per_component.ini.j2'
        else:
            raise ValueError(f'Unknown skeleton type {cfg.mod_skeleton_type}!')

        result = ''

        with open(default_template_path, 'r', encoding='utf-8') as f:
            raw_data = f.read()

            if not remove_code_comments:
                return raw_data

            for line in raw_data.split('\n'):
                if not line.strip().startswith('{{note'):
                    result += line + '\n'

        return result

    @staticmethod
    def get_custom_template(context, cfg):
        if cfg.custom_template_source == 'INTERNAL':
            template_text = bpy.data.texts["CustomIniTemplate"]
            if template_text is not None:
                template = template_text.as_string()
        else:
            template_path = resolve_path(cfg.custom_template_path)
            if not template_path.is_file():
                raise ValueError(f'Custom ini template file not found: `{template_path}`!')
            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()
        return template

    def build_from_template(self, context, cfg, template_string = None, with_checksum = False):
        # Try to load custom template
        if template_string is None and cfg.use_custom_template:
            template_string = self.get_custom_template(context, cfg)
        # Use default template if custom one is not configured or empty
        if template_string is None or not(template_string.strip()):
            template_string = self.get_default_template(context, cfg, remove_code_comments=not cfg.comment_ini)

        global chached_template, chached_template_string
        if chached_template_string is not None and template_string == chached_template_string:
            template = chached_template
        else:
            start_time = time.time()
            try:
                template = Template(template_string)
                chached_template = template
                chached_template_string = template_string
            except TemplateSyntaxError as e:
                template_lines = template_string.split('\n')
                template_fragment = ''
                for i in range(e.lineno-4, e.lineno+2):
                    template_fragment += f'{i}: {template_lines[i]}\n'
                raise ValueError(f'Ini Template syntax error:\n\n'
                                 f'{e.message}\n\n'
                                 f'Line Number: {e.lineno} (actual cause may be located above this line)\n\n'
                                 f'Template Fragment:\n'
                                 f'{template_fragment}')
            print(f'Ini template caching time: {time.time() - start_time :.3f}s')

        try:
            rendered_string = template.render(vars(self))
        except UndefinedError as e:
                raise ValueError(f'Ini Template filling error:\n'
                                 f'{e}')

        result = ''.join([line + '\n' for line in rendered_string.split('\n') if not line.strip().startswith(';DEL')])

        if with_checksum:
            result = self.with_checksum(result)
        
        self.ini_string = result

        return result

    def write(self, ini_string: str = None, ini_path = None):
        if ini_path is None:
            ini_path = resolve_path(self.cfg.mod_output_folder) / 'mod.ini'
        if ini_string is None:
            ini_string = self.ini_string
        if ini_path.is_file() and self.is_ini_edited(ini_path):
            timestamp = datetime.now().strftime('%Y-%m-%d %H-%M-%S')
            backup_path = ini_path.with_name(f'{ini_path.name} {timestamp}.BAK')
            print(f'Writing backup {backup_path.name}...')
            ini_path.rename(backup_path)
        with open(ini_path, 'w', encoding='utf-8') as f:
            print(f'Writing {ini_path.name}...')
            f.write(ini_string)  
    
    @staticmethod
    def with_checksum(lines):
        '''
        Calculates sha256 hash of provided lines and adds following looking entry to the end:
        '; SHA256 CHECKSUM: 401cafcfdb224c5013802b3dd5a5442df5f082404a9a1fed91b0f8650d604370' + '\n'
        Allows to detect if mod.ini was manually edited to prevent accidental overwrite
        '''
        lines = lines.strip() + '\n'
        sha256 = hashlib.sha256(lines.encode('utf-8')).hexdigest()
        lines += f'; SHA256 CHECKSUM: {sha256}' + '\n'
        return lines

    @staticmethod
    def is_ini_edited(ini_path):
        '''
        Extracts defined SHA256 CHECKSUM from provided file and calculates sha256 of remaining lines
        If hashes match, it means that file doesn't contain any manual edits
        Allows to detect if mod.ini was manually edited to prevent accidental overwrite
        '''
        with open(ini_path, 'r') as f:
            data = list(f)

            # Extract data from expected location of checksum stamp
            checksum = data[-1].strip()

            # Ensure that checksum stamp has expected prefix 
            checksum_prefix = '; SHA256 CHECKSUM: '
            if not checksum.startswith(checksum_prefix):
                return False
            
            # Extract sha256 hash value from checksum stamp
            sha256 = checksum.replace(checksum_prefix, '')
            
            # Calculate sha256 hash of all lines above checksum stamp
            ini_data = data[:-1]
            ini_sha256 = hashlib.sha256(''.join(ini_data).encode('utf-8')).hexdigest()

            # Check if checksums are matching, different sha256 means data was edited
            if ini_sha256 != sha256:
                return True

            return False

    def build_list_gui_ini(self, header_height=102, footer_height=60, footer_link_height=0, button_height=75):
        list_gui_template_path = Path(os.path.realpath(__file__)).parent.parent / 'templates' / 'list_gui.ini.j2'
        with open(list_gui_template_path, 'r', encoding='utf-8') as f:
            template_string = f.read()

        # Map of mutual-exclusion groups (object name -> other object names in the same group)
        exclusion_groups = self._build_mutual_exclusion_groups()

        # Collect all objects for ListGUI buttons (skip empty meshes with <= 4 vertices
        # and objects hidden in viewport)
        list_gui_objects = []
        for component in self.merged_object.components:
            for obj in component.objects:
                if obj.vertex_count <= 4 or obj.hidden:
                    continue
                list_gui_objects.append({
                    'name': obj.name,
                    'draw_var': self.formatter.format_ini_drawvar(obj.name),
                    # Draw vars of the other members of the same mutual-exclusion group
                    # (toggling one sets all the others to 0)
                    'exclusive_others': [
                        self.formatter.format_ini_drawvar(other)
                        for other in exclusion_groups.get(obj.name, [])
                    ],
                })

        template = Template(template_string)
        rendered_string = template.render(
            list_gui_objects=list_gui_objects,
            header_height=header_height,
            footer_height=footer_height,
            footer_link_height=footer_link_height,
            button_height=button_height,
            **vars(self)
        )
        result = ''.join([line + '\n' for line in rendered_string.split('\n') if not line.strip().startswith(';DEL')])
        return result

    def _build_mutual_exclusion_groups(self):
        """Build mutual-exclusion groups from parent-child relationships inside the
        exported detection collection.

        Every exported object whose parent is also exported belongs to the same
        mutual-exclusion group as its parent and all of the parent's descendants
        (recursively). Returns {object_name: [other object names in the group]}.
        """
        # object name -> parent object name (or None)
        parent_map = {}
        for component in self.merged_object.components:
            for obj in component.objects:
                parent_map[obj.name] = obj.parent

        # Group all objects sharing the same root ancestor
        root_groups = {}
        for name in parent_map:
            root = name
            seen = set()
            while parent_map.get(root) is not None:
                if root in seen:  # safety against parent cycles
                    break
                seen.add(root)
                root = parent_map[root]
            root_groups.setdefault(root, []).append(name)

        result = {}
        for members in root_groups.values():
            if len(members) <= 1:
                continue
            for name in members:
                result[name] = [m for m in members if m != name]
        return result

    def write_list_gui(self, mod_output_folder: Path):
        try:
            from PIL import Image, ImageDraw, ImageFont
            from .text_to_image import Text2Image, generate_solid_background, generate_button_border, generate_button_background
        except ImportError as e:
            raise ImportError(
                'Pillow (PIL) is required for List GUI image generation but auto-install failed. '
                'Try manually: open Blender\'s Python console and run: '
                'import subprocess; subprocess.check_call([__import__(\"sys\").executable, \"-m\", \"pip\", \"install\", \"pillow\"])'
            ) from e
        import shutil as shutil_mod

        gui_folder = mod_output_folder / 'GUI'
        res_folder = gui_folder / 'res'
        gui_folder.mkdir(parents=True, exist_ok=True)
        res_folder.mkdir(parents=True, exist_ok=True)

        # Copy hlsl from templates
        hlsl_src = Path(os.path.realpath(__file__)).parent.parent / 'templates' / 'draw_2d.hlsl'
        hlsl_dst = res_folder / 'draw_2d.hlsl'
        shutil_mod.copy(hlsl_src, hlsl_dst)
        # Copy texture preview shader (draw_2d_textures.hlsl) from templates
        tex_hlsl_src = Path(os.path.realpath(__file__)).parent.parent / 'templates' / 'draw_2d_textures.hlsl'
        tex_hlsl_dst = res_folder / 'draw_2d_textures.hlsl'
        if tex_hlsl_src.is_file():
            shutil_mod.copy(tex_hlsl_src, tex_hlsl_dst)

        # Copy left-side icon bar placeholder resources (Reload / Save)
        # from the templates folder (State1-3 are generated below; user may replace later)
        templates_dir = Path(os.path.realpath(__file__)).parent.parent / 'templates'
        for icon_name in ('Reload', 'Save'):
            icon_src = templates_dir / f'{icon_name}.png'
            if icon_src.is_file():
                shutil_mod.copy(icon_src, res_folder / f'{icon_name}.png')

        # Copy texture-preview channel mode icons (rgb_alpha / rgb / alpha)
        # from the templates folder (R/G/B channel icons are generated below)
        for channel_name in ('ChannelRGBAlpha', 'ChannelRGB', 'ChannelAlpha'):
            channel_src = templates_dir / f'{channel_name}.png'
            if channel_src.is_file():
                shutil_mod.copy(channel_src, res_folder / f'{channel_name}.png')

        # Generate background image (fully transparent)
        generate_solid_background(str(res_folder / 'Background.png'))

        button_w = 720
        button_h = 108

        # Generate shared button border and background (reused across all buttons)
        generate_button_border(str(res_folder / 'ButtonBorder.png'), button_w, button_h, border_thickness=4)
        generate_button_background(str(res_folder / 'ButtonBg.png'), button_w, button_h, border_thickness=4)

        # Generate a dedicated texture-frame border (square, NOT the wide button strip,
        # to avoid distortion). Frame width 0.125 -> 480px at 720px UI width.
        # Reused for tinting; user may replace.
        tex_frame_w = int(button_w * 0.125 / 0.1875)      # 480
        tex_frame_h = tex_frame_w                         # square
        generate_button_border(str(res_folder / 'TexFrameBorder.png'), tex_frame_w, tex_frame_h, border_thickness=8, border_radius=0)

        # Generate side-button icons (R/G/B channel + 1/2/3 preset):
        # Segoe UI Black (seguibl.ttf) bundled locally in templates so users without
        # the font installed still get correct output. Square border (no rounded
        # corners), white content so the UI hover/selected/normal tint colors them at
        # runtime. User may replace later.
        try:
            side_size = 200
            side_thick = 10
            side_margin = 12  # 边框到边界的距离（对齐 ChannelRGBAlpha.png 的内容边距）
            side_fill = (255, 255, 255, 255)
            side_font = ImageFont.truetype(str(templates_dir / 'seguibl.ttf'), 180)
            for ch, fn in [("R", "ChannelR.png"), ("G", "ChannelG.png"), ("B", "ChannelB.png"),
                           ("1", "State1.png"), ("2", "State2.png"), ("3", "State3.png")]:
                img = Image.new("RGBA", (side_size, side_size), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                d.rectangle(
                    [side_margin, side_margin,
                     side_size - side_margin, side_size - side_margin],
                    outline=side_fill, width=side_thick,
                )
                bbox = d.textbbox((0, 0), ch, font=side_font)
                w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                d.text(((side_size - w) / 2 - bbox[0], (side_size - h) / 2 - bbox[1]),
                       ch, font=side_font, fill=side_fill)
                img.save(res_folder / fn)
        except Exception as e:
            print(f"Warning: failed to generate side-button icons (will be missing in res/): {e}")

        # Text2Image: header/footer (transparent bg, no border, fixed width)
        t2i_header = Text2Image(
            font_path="H7GBK-Heavy.ttf",
            text_color=(249, 255, 255, 255),
            border_thickness=0,
            bg_color=(0, 0, 0, 0),
            font_size=48,
            padding=(16, 16, 24, 16),
        )
        # Text2Image: buttons (transparent background, no border, just text)
        t2i_button_text = Text2Image(
            font_path="H7GBK-Heavy.ttf",
            bg_color=(0, 0, 0, 0),
            border_thickness=0,
            font_size=34,
            padding=(10, 10, 24, 12),
        )

        # Helper to strip "Component " prefix from object names for images
        import re
        def strip_component_prefix(name):
            name = re.sub(r'^component[_ ]?', '', name, flags=re.IGNORECASE)
            return name

        # Header image (Mod Name), split by "-", left-aligned in fixed width
        header_name = self.mod_info.mod_name.replace('-', '\n')
        header_path = str(res_folder / 'Header.png')
        header_w, header_h = t2i_header.generate_fixed(header_name, header_path, button_w, text_align='left', line_spacing=0.5)
        # Draw bottom border line on header
        header_im = Image.open(header_path)
        _draw = ImageDraw.Draw(header_im)
        _draw.line([(2, header_h - 3), (button_w - 2, header_h - 3)], fill=(61, 78, 90, 255), width=5)
        header_im.save(header_path)

        # The top border line delimits the whole info block (Mod Link + Author Name),
        # so it is drawn on whichever of the two rows is on top
        has_mod_link = self.mod_info.mod_link.strip() != ''

        # Footer image (Author Name), right-aligned in fixed width
        footer_path = str(res_folder / 'Footer.png')
        footer_w, footer_h = t2i_header.generate_fixed(self.mod_info.mod_author, footer_path, button_w, text_align='right', line_spacing=0.5)
        if not has_mod_link:
            # Draw top border line on footer (only when there is no Mod Link row above it)
            footer_im = Image.open(footer_path)
            _draw = ImageDraw.Draw(footer_im)
            _draw.line([(2, 2), (button_w - 2, 2)], fill=(61, 78, 90, 255), width=5)
            footer_im.save(footer_path)

        # Mod Link image, left-aligned above the author line (smaller font)
        footer_link_h = 0
        if has_mod_link:
            t2i_footer_link = Text2Image(
                font_path="H7GBK-Heavy.ttf",
                text_color=(249, 255, 255, 255),
                border_thickness=0,
                bg_color=(0, 0, 0, 0),
                font_size=30,
                # Extra top padding leaves the same gap under the border line as the footer
                padding=(16, 8, 24, 16),
            )
            footer_link_path = str(res_folder / 'FooterLink.png')
            # Break long links right after a '/' so they wrap at path boundaries
            _, footer_link_h = t2i_footer_link.generate_fixed(
                self.mod_info.mod_link, footer_link_path, button_w,
                text_align='left', line_spacing=0.5, break_chars='/')
            # Draw top border line on the Mod Link row, so it sits above the link text
            footer_link_im = Image.open(footer_link_path)
            _draw = ImageDraw.Draw(footer_link_im)
            _draw.line([(2, 2), (button_w - 2, 2)], fill=(61, 78, 90, 255), width=5)
            footer_link_im.save(footer_link_path)

        # Button text images (one per object) - fixed size, transparent bg, no border
        for component in self.merged_object.components:
            for obj in component.objects:
                if obj.vertex_count <= 4 or obj.hidden:
                    continue
                icon_name = self.formatter.format_ini_drawvar(obj.name).replace('$', '')
                display_name = strip_component_prefix(obj.name)
                t2i_button_text.generate_fixed(display_name, str(res_folder / f'{icon_name}.png'), button_w, button_h)

        # Menu Switch button text images (one per switch list item)
        for switch in (self.menu_switches or []):
            t2i_button_text.generate_fixed(
                switch['display_name'],
                str(res_folder / f'{switch["identifier"]}.png'),
                button_w, button_h,
            )

        # Write ListGUI.ini
        list_gui_ini = self.build_list_gui_ini(
            header_height=header_h,
            footer_height=footer_h,
            footer_link_height=footer_link_h,
            button_height=button_h,
        )
        list_gui_path = gui_folder / 'ListGUI.ini'
        with open(list_gui_path, 'w', encoding='utf-8') as f:
            print(f'Writing {list_gui_path.name}...')
            f.write(list_gui_ini)
