# WWMI Tools · xqfa 分支

基于 [SpectrumQT/WWMI-Tools](https://github.com/SpectrumQT/WWMI-Tools) 的增强分支。
上游的 **Extract Objects From Dump → Import Object → Export Mod** 工作流保持不变，本分支主要围绕**纹理解析与绑定、多实例骨架、导入材质生成、导出组织方式、游戏内 GUI** 做扩展。

- 当前版本：`1.8.0.21`
- 依赖：Blender **4.5+**（4.2 不支持）、WWMI **1.1.0+**
- 本分支持续合入上游更新，新增功能集中在导出面板的 **Branch Options / 分支选项** 抽屉
- 上游功能与用法仍可参考 [Modder Guide](https://github.com/SpectrumQT/WWMI-TOOLS/blob/main/guides/modder_guide.md)

---

## 与 main 分支的差异总览

| 环节 | xqfa 分支新增内容 |
| --- | --- |
| 帧提取 | 导出 `ShaderTextureUsage.json`；`Skip Dirty Slot` 过滤；`TextureAssetManifest` 资源路径命名；texdiag 读取贴图格式与尺寸 |
| 对象导入 | 依据 `ShaderTextureUsage.json` 自动创建材质与节点组，并按 `D / N / FTM / RGID / HN` 角色自动连线 |
| 导出·纹理 | 新增 `Slot` 与 `Path(Hash)` 两种纹理模式；`Slot Complex`、`Hash Complex`、`Max PS-T`；`Export Textures` 自动转 DDS |
| 导出·骨架 | 新增 `Merged Instance` 骨架与 `Instance Count`；16-bit Blend Buffer；最多 1024 根骨骼，无需额外 shader |
| 导出·切线 | 新增 `Tangent` 来源选择：`Default` / `Active UV` / `Object Sources` 自动反推 |
| 导出·组织 | 新增 `Component By Collection` 与 `Add Collections` 一键建子集合 |
| 导出·界面 | 新增 `List GUI`：生成游戏内可开关模型的列表界面；支持 `Menu Switch` 贴图池 |
| 其他 | 形态键链路过滤、导出贴图命名与格式处理、若干模板与稳定性修复 |

### 默认值差异

- 导出纹理模式默认 `Slot`，骨架默认 `Merged Instance`
- `Skip Dirty Slot`、`TextureAssetManifest`、`Mirror Mesh`、`Export Textures` 默认开启
- `Skip Small`、`Skip .jpg`、`Skip Known Cubemaps`、`Skip Empty Vertex Groups`、`Add Missing Vertex Groups`、`Fill Missing Mesh Data` 默认关闭

---

## 1. 帧提取：Extract Objects From Dump

### ShaderTextureUsage.json

每次提取都会在对象目录额外写出 `ShaderTextureUsage.json`，按以下层级记录“某个 Component 的某个 VS/PS 组合在各 `ps-t` 槽位用了哪张贴图”：

```
Component 0 → vs_hash → ps_hash → ps-tN → {filename, hash, format, width, height, asset_path?}
```

导入时用它生成材质节点组，导出时用它判定槽位格式、贴图角色与 DDS 目标格式。

### Textures Filtering: Skip Dirty Slot

默认开启。只保留日志中由 `PSSetShaderResources` **显式绑定**的贴图，过滤掉因资源懒更新残留在槽位上的无效贴图。若你确实需要这些残留贴图，可以关闭，但通常不必要。

### TextureAssetManifest

开启后会读取 Frame Dump 目录下的 `TextureAssetManifest.jsonl`：

- 贴图文件名改为资源路径中的 UE 对象名（如 `T_xxx_D.dds`）
- `ShaderTextureUsage.json` 中额外写入 `asset_path` / `asset_name`
- 找不到清单条目时，回退为 `Components-<组件号> t=<hash>.<ext>`

该文件由配合 [WWMI-AssetPath-DLL-Core](https://github.com/visaokc/WWMI-AssetPath-DLL-Core) 的 dump 产生，使用前请确认已安装对应 DLL 版本。

### 贴图元数据

提取阶段通过内置 `texdiag.exe` 读取每张贴图的 **DXGI 格式、宽、高**，写入 `ShaderTextureUsage.json`。这些信息是后续槽位格式判断、贴图角色识别和 DDS 转换格式的依据。

---

## 2. 对象导入：Import Object 自动生成材质

导入时若存在 `ShaderTextureUsage.json`，会为每个 Component 自动建立材质：

- 每个 `VS/PS` 组合对应一个节点组，命名形如 `vb=<Object Sources 名>-C<组件号>-ps=<ps hash>`
- 节点组输入按槽位区分，并带格式后缀（如 `ps-t0: BC7_UNORM_SRGB`），另有独立的 alpha 输入 `————————> ps-t0 alpha`
- 贴图节点按 `ShaderTextureUsage.json` 中的文件名自动加载，并按格式设置 sRGB / Non-Color 与 Alpha 模式
- 默认只激活**输入接口最多**的那一组节点组并接到材质输出，其余节点组静音、连线静音，便于手动切换
- 仅方形且边长 ≥256 的贴图可参与 `D / N / FTM / RGID / HN` 角色识别；非角色贴图中非方形或边长 ≤512 的连线会被静音（方形 BC3 除外）

节点组名称参与导出查找，**不要重命名**。

---

## 3. 导出：Export Mod

除纹理模式与骨架类型外，本节新增选项都在 **Branch Options / 分支选项** 抽屉中展开。

### 3.1 纹理模式

`Texture` 下拉框决定 `mod.ini` 如何引用贴图：

| 模式 | 说明 |
| --- | --- |
| `Hash` | 上游原有方式，按贴图 hash 覆盖 |
| `Slot` | 读取每个物体的材质节点组，按 `ps-tX->format` 条件在绘制前替换槽位贴图（默认模式） |
| `Path(Hash)` | 仍按 hash 触发覆盖，但 `ResourceTexture` 使用导出的文件名，并写入 `TextureOverrideTexture` 段 |

#### Slot 模式

- 每个物体单独绘制，绘制前按节点组的 `ps-tX->format == <DXGI 枚举>` 组合判断并替换对应槽位
- 贴图按格式精确匹配，不依赖 `filter_index` 浮点值
- `ps-t` 的 alpha 通道不参与导出
- 依赖 `ShaderTextureUsage.json`；请勿修改节点组名称

**Slot Complex**：导出前先按材质把物体拆成一个材质一个物体再合并，使同一 Component 内不同材质能各自使用不同贴图。

**槽位稳定性建议**：

1. 所选槽位组合应在目标着色器上具有唯一性（格式组合能与其他着色器区分）
2. 注意同角色可能存在多个近似着色器（如出场瞬间与常态），个别槽位绑定的贴图不同
3. 不同角色形态的槽位往往不同，不能跨形态复用同一套槽位判断

#### Path(Hash) 模式

- 为每个贴图 hash 生成 `[TextureOverrideTexture]`，并生成对应的 `[ResourceTexture]` 指向实际导出文件
- **Hash Complex**：同一 hash 被多个物体复用时，3DMigoto 只会触发一次覆盖；该选项会按材质分组，在每组绘制前用 `$texture_componentN_count` 选择正确贴图，绘制后恢复原贴图
- **Max PS-T**：控制模板中 `ps-t` 的最大编号（默认 `11`，即最多到 `ps-t11`），影响触发、备份与恢复贴图的相关 CommandList

#### Export Textures 与 Copy Textures

- **Copy Textures**：把原始贴图文件复制到导出目录
- **Export Textures**：按 `ShaderTextureUsage.json` 中对应槽位的格式导出 DDS。原图已在 Object Sources 中则直接复制；否则先保存为 TGA，再用内置 `texconv.exe` 转换。针对 BC7 全透明贴图会修正 Alpha 丢失问题
- 两者互斥，勾选一个会自动取消另一个。启用 Export Textures 时建议关闭 Copy Textures
- Export Textures 不改变 `mod.ini` 的内容

#### Menu Switch

材质节点组中的 Blender `Menu Switch` 节点会被识别为“贴图池”：其各分支输入的贴图会一并导出，生成运行时 `ResourceTexture` 别名，并在 `List GUI` 中成为可切换列表项。

### 3.2 骨架类型

| 类型 | 说明 |
| --- | --- |
| `Merged` | 上游原有合并骨架，所有 Component 共享统一 VG 列表 |
| `Merged Instance` | 合并骨架的多实例版，支持同屏多个相同模型（默认） |
| `Per-Component` | 按 Component 拆分 VG 列表，无 1 帧延迟但权重受限 |

**Merged Instance** 要点：

- 通过带标记的 Mesh Data CB 获取当前绘制的 `$instance_id`
- 每实例拥有独立的骨架池与合并状态（`PoolInstanceID`、`PoolMergeStatus_*`）
- 使用 16-bit Blend Buffer（`Blend_R16.buf`），支持 VG id 超过 255，骨骼上限 1024
- 不再附带额外的 1024 骨骼 shader，统一使用 WWMI 内置 `SkeletonMerger`
- **Instance Count**：同屏实例数量，`1–16`。数量越多，生成的池与状态变量越多
- 已修复单实例因闪避/受击导致模型矩阵指针变化而出现的 1 帧收集重置；多实例下若某实例的 `$instance_id` 发生变化，仍可能有 1 帧瑕疵

### 3.3 切线来源 Tangent

| 选项 | 说明 |
| --- | --- |
| `Default` | 从 `TEXCOORD.xy` 生成切线（上游行为） |
| `Active UV` | 合并前逐物体把各自的活动 UV 复制到临时层，再按临时层生成切线 |
| `Object Sources` | 逐个 Component 读取原始 VB 的切线，反推其使用的 UV 层，再按该层计算 |

`Object Sources` 的结果按 Object Sources 目录缓存；切换来源后如果模型切线异常，优先检查该选项。

### 3.4 组件组织：Component By Collection

- 开启后，`Components` 集合下的子集合会被视为 Component：**子集合名中的第一个数字**即编号（`C0_帽子` → Component 0、`Component 1` → Component 1），该子集合（含更深层嵌套）下所有物体都归入该 Component
- 根集合及非 Component 子集合下的物体仍按物体名匹配
- 与 `Ignore Nested Collections` 互斥
- **Add Collections** 按钮会读取 Object Sources 中的 `Metadata.json`，自动创建缺失的 `C<序号>` 子集合（已存在的序号跳过），并把根集合下名称以 `C<序号>` 开头或符合 `component N` 规则的对象移入对应集合

### 3.5 List GUI

开启 `List GUI`（位于 **Branch Options / 分支选项** 抽屉）后，导出会额外生成 `GUI/` 目录：

```
GUI/
├── ListGUI.ini
└── res/
    ├── draw_2d.hlsl
    ├── draw_2d_textures.hlsl
    ├── Header.png / Footer.png / FooterLink.png
    ├── ButtonBg.png / ButtonBorder.png / TexFrameBorder.png
    ├── Reload.png / Save.png / State1-3.png
    ├── ChannelRGBAlpha.png / ChannelRGB.png / ChannelAlpha.png / ChannelR/G/B.png
    └── <每个物体一张按钮文字图>
```

功能包括：

- 游戏内列表界面：顶部标题（Mod 名）、底部作者与 Mod 链接、每个物体一个开关按钮
- 父子物体自动互斥：同一父子链下的物体同属一组，开启一个会关闭组内其他项
- 左侧图标栏：Reload、Save、State1/2/3 状态槽
- 贴图预览：侧边通道按钮切换 RGB / Alpha / R / G / B 显示
- 列表滚动、按钮隐藏补位；空网格（顶点数 ≤ 4）不生成按钮
- `Menu Switch` 节点的每个贴图池也会生成独立列表项与切换逻辑
- `ListGUI.ini` 通过 `use_ini_toggles` 与主 `mod.ini` 协作；开启 List GUI 会自动开启 `Use Ini Toggles`

界面图片由 Pillow 生成；若缺失会尝试自动安装。生成后可以自行替换 `res/` 中的图片。

---

## 4. 其他改进与修复

- 提取时按“形态键链路起点 hash 与模型 VB 匹配”归属链路，避免同帧多条形态键链路互相干扰
- 日志解析改为先按 `call_id` 分组再批量处理，减少逐行状态跟踪的开销
- 导出贴图名称做非 ASCII 转换与去重，支持中文/特殊字符贴图名
- 修正多处分组与节点组条件判断、路径模式重复覆盖与命名冲突问题
- 修复应用修改器导出含形态键物体时，因 `show_only_shape_key` 导致形态键偏移全部归零的问题
- 统一模板中的槽位覆盖命名与备份/恢复逻辑

---

## 5. 使用注意

- **不要重命名材质节点组**：导出要靠名称去 `ShaderTextureUsage.json` 查找
- **Slot / Path 模式需要 `ShaderTextureUsage.json`**：请使用本分支的提取流程生成，或确保旧数据补齐该文件
- 槽位判断依赖 DXGI 格式。多数 DDS 使用对应 TYPELESS 格式，少数（如 `R8`）例外；若遇到特殊格式导致无法匹配，可提 issue 补充
- `Merged Instance` 的 Instance Count 应与实际同屏数量匹配：过小可能超出池容量，过大只会增加状态变量
- Blender 4.2 因自定义拆分法线回归问题不受支持

---

## 6. 安装

1. 安装 Blender 4.5 或更高版本
2. 下载本分支的发布包 `WWMI-Tools-X.X.X.zip`
3. 打开 Blender → `Edit` → `Preferences` → `Add-ons`
4. 右上角菜单选择 `Install from Disk...`，选中压缩包并安装
5. 搜索 `WWMI`，勾选启用 **Object: WWMI Tools**

---

## 7. 相关链接

- 本分支仓库：[XQFAAAA/WWMI-Tools](https://github.com/XQFAAAA/WWMI-Tools)
- 上游仓库：[SpectrumQT/WWMI-Tools](https://github.com/SpectrumQT/WWMI-Tools)
- WWMI：[SpectrumQT/WWMI](https://github.com/SpectrumQT/WWMI)
- XXMI Launcher：[SpectrumQT/XXMI-Launcher](https://github.com/SpectrumQT/XXMI-Launcher)
- 贴图资源路径 DLL：[visaokc/WWMI-AssetPath-DLL-Core](https://github.com/visaokc/WWMI-AssetPath-DLL-Core)
- 现成模型资源：[WWMI Assets](https://github.com/SpectrumQT/WWMI-Assets)

## License

GPLv3，与上游保持一致。
