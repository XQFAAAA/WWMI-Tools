// **** RESPONSIVE UI SHADER ****
// Contributors: SinsOfSeven
// Ispired by VV_Mod_Maker

Texture1D<float4> IniParams : register(t120);

#define SIZE IniParams[87].xy
#define OFFSET IniParams[87].zw
#define TINT IniParams[88]
#define CLIP IniParams[89].xy

// 贴图预览通道模式（由 ListGUI 的通道按钮设置，经 IniParams[90].x 传入）
// 0 = RGB+Alpha（默认，随 alpha 混合显示透明度）
// 1 = RGB（不透明）
// 2 = Alpha（灰度）
// 3 = R（灰度）
// 4 = G（灰度）
// 5 = B（灰度）
#define CHANNEL IniParams[90].x

// Gamma 校正系数：贴图预览比预期暗，将颜色从线性空间编码回 sRGB 空间使显示更接近原图
// 若仍偏亮/偏暗，可调整此值（增大变亮，减小变暗）
#define GAMMA (1.0 / 2.2)

struct vs2ps {
	float4 pos : SV_Position0;
	float2 uv : TEXCOORD1;
};

#ifdef VERTEX_SHADER
void main(
		out vs2ps output,
		uint vertex : SV_VertexID)
{
	float2 BaseCoord,Offset;
	Offset.x = OFFSET.x*2-1;
	Offset.y = (1-OFFSET.y)*2-1;
	BaseCoord.xy = float2((2*SIZE.x),(2*(-SIZE.y)));
	// Not using vertex buffers so manufacture our own coordinates.
	switch(vertex) {
		case 0:
			output.pos.xy = float2(0+Offset.x, 0+Offset.y);
			output.uv = float2(0,0);
			break;
		case 1:
			output.pos.xy = float2(0+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(0,1);
			break;
		case 2:
			output.pos.xy = float2(BaseCoord.x+Offset.x, 0+Offset.y);
			output.uv = float2(1,0);
			break;
		case 3:
			output.pos.xy = float2(BaseCoord.x+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(1,1);
			break;
		default:
			output.pos.xy = 0;
			output.uv = float2(0,0);
			break;
	};
	output.pos.zw = float2(0, 1);
}
#endif

#ifdef PIXEL_SHADER
Texture2D<float4> tex : register(t100);

// 单次双线性采样（4 次 Load + 插值），3DMigoto UI 绘制无线性采样器可用，故手动模拟
float4 SampleBilinear(float2 sc)
{
	int2 texel = int2(floor(sc));
	float2 f = sc - floor(sc);
	float4 c00 = tex.Load(int3(texel            , 0));
	float4 c10 = tex.Load(int3(texel + int2(1,0), 0));
	float4 c01 = tex.Load(int3(texel + int2(0,1), 0));
	float4 c11 = tex.Load(int3(texel + int2(1,1), 0));
	return lerp(lerp(c00, c10, f.x), lerp(c01, c11, f.x), f.y);
}

void main(
	vs2ps input,
	out float4 result : SV_Target0)
{
	float2 dims;
	tex.GetDimensions(dims.x, dims.y);
	if (!dims.x || !dims.y) discard;

	float pixel_y = OFFSET.y + input.uv.y * SIZE.y;
	if (pixel_y < CLIP.x || pixel_y > CLIP.y) discard;

	// 2×2 超采样 (SSAA)：单个输出像素内取 2×2 均匀网格的 4 个采样点，
	// 各做一次双线性后取平均。相比单点双线性，对斜线/圆角/文字边缘抗锯齿更好，
	// 并进一步缓解缩小采样时边框粗细不均。UI 绘制下 16 次 Load 开销可忽略。
	float2 center = input.uv.xy * dims.xy;
	float4 color = 0;
	color += SampleBilinear(clamp(center + float2(-0.25, -0.25), 0.5, dims.xy - 1.5));
	color += SampleBilinear(clamp(center + float2( 0.25, -0.25), 0.5, dims.xy - 1.5));
	color += SampleBilinear(clamp(center + float2(-0.25,  0.25), 0.5, dims.xy - 1.5));
	color += SampleBilinear(clamp(center + float2( 0.25,  0.25), 0.5, dims.xy - 1.5));
	color *= 0.25;

	// 贴图预览通道模式：由 ListGUI 的通道按钮（rgb_alpha/rgb/alpha/r/g/b）切换，互斥高亮
	// 各模式均做 gamma 校正；悬停/选中/未选中状态由贴图框 Border 体现，不影响贴图本身颜色
	float3 display;
	float out_alpha = 1.0;
	if (CHANNEL == 1)          // RGB：不透明
		display = pow(color.rgb, GAMMA);
	else if (CHANNEL == 2)     // Alpha：灰度
		display = pow(color.aaa, GAMMA);
	else if (CHANNEL == 3)     // R：灰度
		display = pow(color.rrr, GAMMA);
	else if (CHANNEL == 4)     // G：灰度
		display = pow(color.ggg, GAMMA);
	else if (CHANNEL == 5)     // B：灰度
		display = pow(color.bbb, GAMMA);
	else {                     // RGB+Alpha（默认）：RGB 随 alpha 混合，显示贴图透明度
		display = pow(color.rgb, GAMMA);
		out_alpha = color.a;
	}
	result = float4(display, out_alpha);
}
#endif
