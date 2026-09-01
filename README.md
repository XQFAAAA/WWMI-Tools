
这个分支版本增加从材质节点组导出的Texture的slot模式和path hash模式
使用以下dll，能获得更好的体验，它包含贴图的资源路径的额外功能
https://github.com/visaokc/WWMI-AssetPath-DLL-Core

合并骨骼多实例先遣版现已推出
但是由于我对pool的语法仍不是很熟悉
整体代码结构还有待优化（性能没有多大影响，正是为了避免重复计算，才导致代码结构比较复杂）
SQT更新的话，我也会及时更进

# "Extract Objects From Dump" 修改项

### "ShaderTextureUsage.json" 文件

额外导出文件，用于存储模型着色器的各槽位贴图信息

### "Textures Filtering: Skip Dirty Slot" 选项

读取日志中显示声明的贴图，避免资源懒更新导致的无效信息

### "TextureAssetManifest" 选项

配套visaokc的dll，获取贴图的资源路径

# Import Object

导入时，会根据ShaderTextureUsage.json作为材质节点组

如果之前使用了visaokc的dll，获取贴图的资源路径，会根据贴图的N D FTM ID后缀自动连接一个简单的材质

材质中的第一个节点组是激活的，其他默认关闭

# Export Mod

导出时将只对已激活的vs=ps=的节点组和已激活的输入接口进行导出
使用M键激活节点组，使用ctrl alt 右键激活连线

将图片连接到需要的ps-t slot上。ps-t alpha不会被插件处理

## Slot 模式

### "Export Textures" 选项

如果图片在ObjectSources中，会复制
如果不是，会保存为副本为tga，并使用texconv.exe转化为dds格式，dds格式由ShaderTextureUsage.json中对应slot位置控制

是否开启不影响ini文件的内容

Export Textures并不影响Copy Textures功能，但我还是建议把Copy Textures关闭

### "Match DDS Format" 选项

选项分为Less、More、Most
用来控制模糊匹配的格式多少
如果每个component只使用一个节点组，建议选择Less
如果每个component有多个节点组，建议选择More

### "Slot Complex" 选项

按材质分离每个物体
每个物体都读取一遍材质中的节点组
使得同个component能够分开使用不同的贴图

### "RabbitFX" 选项

开启后，插件会加入RabbitFX中的正则表达式 filter_index 1718.1作为条件判断


### 注意

不要修改节点组的名称，因为要去ShaderTextureUsage.json中查找

大部分dds格式都是其对应的TYPELESS，除非它被其他mod截取
但是少部分dds格式不是这样，比如R8，如果你发现有其他奇怪的格式需要特殊处理，请告诉我

### slot稳定性

应该使用适当的slot来保证稳定性，这个插件功能不可能适配所有的问题，但初衷是尽可能不使用贴图的hash值

1. 确保你选择的几个slot所在的着色器能够和其他着色器区分开，即这个dds format组合是唯一的，
2. 一些同样作用的着色器可能存在略微的差别，比如在角色出现的瞬间的着色器和正常情况的着色器，可能在某一个slot位置插入的其他贴图
3. 角色不同形态，大概率槽位也会不同

## Path Hash 模式

索引节点组位置对应的hash值

### "Export Textures" 选项

同Slot 模式

### "Hash Complex" 选项

同Slot 模式

### "Max Ps-T" 选项

checktextureoverride = ps-t的上限

### visaokc的dll兼容性

导出后以注释的形式，提前写上部分修复格式

