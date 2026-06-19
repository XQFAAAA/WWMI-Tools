// **** RESPONSIVE UI SHADER ****
// Contributors: SinsOfSeven
// Ispired by VV_Mod_Maker

Texture1D<float4> IniParams : register(t120);

#define SIZE IniParams[87].xy
#define OFFSET IniParams[87].zw
#define TINT IniParams[88]
#define CLIP IniParams[89].xy

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

void main(
	vs2ps input,
	out float4 result : SV_Target0)
{
	float2 dims;
	tex.GetDimensions(dims.x, dims.y);
	if (!dims.x || !dims.y) discard;

	float pixel_y = OFFSET.y + input.uv.y * SIZE.y;
	if (pixel_y < CLIP.x || pixel_y > CLIP.y) discard;

	// 手动双线性过滤：使用 4 个 Load 做插值，缓解缩小采样时边框粗细不均
	float2 tex_coord = clamp(input.uv.xy * dims.xy, 0.5, dims.xy - 1.5);
	int2 texel = int2(floor(tex_coord));
	float2 f = tex_coord - floor(tex_coord);

	float4 c00 = tex.Load(int3(texel            , 0));
	float4 c10 = tex.Load(int3(texel + int2(1,0), 0));
	float4 c01 = tex.Load(int3(texel + int2(0,1), 0));
	float4 c11 = tex.Load(int3(texel + int2(1,1), 0));

	result = lerp(lerp(c00, c10, f.x), lerp(c01, c11, f.x), f.y) * TINT;
}
#endif
