// Continuous mesh-local tissue: no UV-atlas color, roughness or normal input.
struct TissueTiles {
 float hash(float2 p) {
  float3 h=frac(float3(p.xyx)*.1031);
  h+=dot(h,h.yzx+33.33);
  return frac((h.x+h.y)*h.z);
 }
 float4 samplePatch(Texture2D c, SamplerState csamp, Texture2D n, SamplerState nsamp,
                    float2 p, float seed, out float2 detail) {
  float2 base=floor(p);
  float4 color=0;
  detail=0;
  float total=0;
  float2 dx=ddx(p),dy=ddy(p);
  [unroll] for(int y=0;y<2;y++) {
   [unroll] for(int x=0;x<2;x++) {
    float2 cell=base+float2(x,y),d=p-cell;
    float2 w=saturate(1-abs(d));
    w=w*w*(3-2*w);
    float weight=w.x*w.y;
    float sn,cs;
    sincos(hash(cell+seed)*6.2831853,sn,cs);
    float2x2 rot=float2x2(cs,-sn,sn,cs);
    // d lies in [-1,1]^2. Rotation, scale and offset keep UV in
    // [0.064,0.936], so non-periodic generated image borders never wrap.
    float2 offset=(float2(hash(cell+31+seed),hash(cell-17+seed))-.5)*.08;
    float2 uv=.5+offset+mul(rot,d)*.28;
    color+=Texture2DSampleGrad(c,csamp,uv,mul(rot,dx)*.28,mul(rot,dy)*.28)*weight;
    float2 xy=Texture2DSampleGrad(n,nsamp,uv,mul(rot,dx)*.28,mul(rot,dy)*.28).rg*2-1;
    detail+=mul(xy,rot)*weight;
    total+=weight;
   }
  }
  detail/=max(total,.0001);
  return color/max(total,.0001);
 }
};
TissueTiles tiles;
float3 normal=normalize(N)*Side;
// Compensate for the smaller source-image footprint to retain detail density.
float3 p=P/max(DetailSize*(.28/.55),1);
// Projection weights depend only on the mesh normal, never the seamed atlas.
float3 weights=normal*normal;
weights/=max(weights.x+weights.y+weights.z,.0001);
float2 nxy,nyz,nxz;
float4 cxy=tiles.samplePatch(DetailColor,DetailColorSampler,DetailNormal,DetailNormalSampler,p.xy,3,nxy);
float4 cyz=tiles.samplePatch(DetailColor,DetailColorSampler,DetailNormal,DetailNormalSampler,p.yz,11,nyz);
float4 cxz=tiles.samplePatch(DetailColor,DetailColorSampler,DetailNormal,DetailNormalSampler,p.xz,23,nxz);
float4 detail=cxy*weights.z+cyz*weights.x+cxz*weights.y;
float3 bump=float3(nxy,0)*weights.z+float3(0,nyz)*weights.x+float3(nxz.x,0,nxz.y)*weights.y;
bump-=normal*dot(normal,bump);
LocalNormal=normalize(normal+bump*NormalStrength);
Roughness=clamp(detail.a,.26,.60);
// Same color treatment on either side: winding changes must not create patches.
return saturate(lerp(DetailMean,detail.rgb,saturate(DetailStrength)));
