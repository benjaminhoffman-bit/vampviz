"""VAMPVIZ shaders: one fullscreen-triangle vertex shader, the scene shader (all scenes, feedback), bloom and the
composite pass. Plain GLSL 330 strings; vampviz.pyw compiles them with moderngl.

Uniforms the app sets each frame on the scene program (any a scene does not read is dropped by the driver and skipped):
  res, time, bass, mid, high, kick, rms, hue, beats, variant, scene; samplers spec, wave, prev.
Composite: tex, bloom, time, kick, hue.
"""
N_BANDS = 96  # must match vampviz.N_BANDS

# ─── shaders ────────────────────────────────────────────────────────────────────────────────────────────────────────
VERT = """#version 330
out vec2 st;
void main(){ vec2 v = vec2((gl_VertexID<<1)&2, gl_VertexID&2); st = v; gl_Position = vec4(v*2.0-1.0, 0.0, 1.0); }"""

COMMON = """#version 330
uniform vec2 res; uniform float time, bass, mid, high, kick, rms, hue, beats, variant; uniform int scene;
uniform sampler2D spec, wave, prev;
in vec2 st; out vec4 frag;
#define PI 3.14159265
const float N = __N__;
float spectrum(float x){ return texture(spec, vec2(clamp(x,0.0,1.0), 0.5)).r; }
float waveform(float x){ return texture(wave, vec2(fract(x), 0.5)).r; }
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
float noise(vec2 p){ vec2 i=floor(p), f=fract(p); f=f*f*(3.0-2.0*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),f.x), mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x), f.y); }
float fbm(vec2 p){ float v=0.0, a=0.5; mat2 m=mat2(1.6,1.2,-1.2,1.6); for(int i=0;i<5;i++){ v+=a*noise(p); p=m*p; a*=0.5; } return v; }
// vamp palette: void → dried blood → neon crimson → violet
vec3 pal(float t){ t=fract(t);
  vec3 a=vec3(0.03,0.0,0.05), b=vec3(0.55,0.0,0.10), c=vec3(1.0,0.10,0.28), d=vec3(0.45,0.0,1.0);
  return t<0.33 ? mix(a,b,t/0.33) : t<0.66 ? mix(b,c,(t-0.33)/0.33) : mix(c,d,(t-0.66)/0.34); }
vec3 hueShift(vec3 c, float h){ const vec3 k=vec3(0.57735); float cs=cos(h), sn=sin(h);
  return c*cs + cross(k,c)*sn + k*dot(k,c)*(1.0-cs); }
mat2 rot(float a){ float c=cos(a), s=sin(a); return mat2(c,-s,s,c); }
vec3 feedback(float zoom, float spin, float decay, vec2 warp){
  vec2 c = (st-0.5)*zoom; c = rot(spin)*c + warp;
  return max(texture(prev, c+0.5).rgb*decay - 0.012, 0.0); }  // floor subtract: trails die, black stays black
"""

SCENE = COMMON.replace("__N__", f"{N_BANDS}.0") + """
vec3 vamp(vec2 uv){ // kaleidoscope spectrum ring
  float r=length(uv), a=atan(uv.y,uv.x);
  float seg=floor(3.0+variant*6.0); float spin=(variant-0.5)*0.5;                   // 3..8 mirrors, either way
  float sa=mod(a+time*spin, 2.0*PI/seg); sa=abs(sa-PI/seg); float fx=sa/(PI/seg);
  float s=spectrum(fx*0.85+0.02);
  float ring=0.14+variant*0.12+bass*0.07+kick*0.02, d=r-ring, bar=s*(0.18+variant*0.16);
  float inside=smoothstep(bar, bar-0.012, d)*step(0.0,d);
  float line=0.0035/(abs(d)+0.0035);
  vec3 col=pal(fx*0.45+s*0.3+hue)*(inside*(0.35+0.6*s)+line*0.4*(0.4+bass));
  col+=vec3(1.0,0.08,0.22)*pow(max(0.0,1.0-r/(ring*0.92)),3.0)*(0.25+kick*1.6);  // beating core
  float wr=0.52+waveform(a/(2.0*PI)+0.5)*0.07*(0.4+rms*4.0);                       // waveform halo
  col+=pal(0.85+hue)*0.0025/(abs(r-wr)+0.0025)*0.9;
  float smoke=fbm(uv*2.5+vec2(time*0.05,-time*0.03))*0.5;                           // void smoke
  col+=vec3(0.10,0.0,0.03)*smoke*(0.2+bass)*smoothstep(ring,ring+0.5,r);
  return col*0.8+feedback(0.986, 0.004*(1.0+bass*2.0), 0.80, vec2(0.0));
}
vec3 slowed(vec2 uv){ // domain-warped fluid, heavy smear
  vec2 p=uv*(1.2+variant*1.4); float t=time*(0.06+variant*0.14);
  float n1=fbm(p+vec2(t,-t*0.7)), n2=fbm(p*1.7+vec2(n1*2.0,t*0.5));
  float n=fbm(p+3.0*vec2(n1,n2)*(0.5+bass*0.8));
  float s=spectrum(fract(n*1.4+bass*0.2));
  vec3 col=pal(n*0.9+hue+0.45)*(0.12+1.1*s);
  col+=vec3(0.55,0.08,1.0)*mid*pow(n2,3.0)*1.2;
  float wy=waveform(st.x*0.5+time*0.02)*0.18*(0.5+rms*3.0)+sin(time*0.3)*0.1;    // drifting waveform
  col+=pal(0.9+hue)*0.004/(abs(uv.y-wy)+0.004)*0.6;
  vec2 warp=vec2(fbm(uv*3.0+time*0.2)-0.5, fbm(uv*3.0-time*0.17)-0.5)*0.004;
  return col*0.22+feedback(0.993, 0.0015, 0.90, warp);
}
vec3 rage(vec2 uv){ // tunnel, strobing on kicks
  uv+=vec2(hash(vec2(time,1.0)),hash(vec2(2.0,time)))*kick*0.01;                    // shake
  float r=length(uv), a=atan(uv.y,uv.x);
  float z=0.25/(r+0.02)+time*(0.6+variant*1.5+rms*8.0)+beats*0.5;
  float ang=a/(2.0*PI)+0.5+time*0.03;
  float s=spectrum(abs(fract(ang*2.0)*2.0-1.0)*0.9);
  float rings=smoothstep(0.08,0.0,abs(fract(z)-0.5)-0.42);
  float walls=floor(12.0+variant*40.0);
  float wall=smoothstep(0.2,0.0,abs(fract(ang*walls)-0.5)-0.45*(0.1+s*0.6));
  vec3 col=pal(fract(z*0.04)+hue)*(wall*s*1.2+rings*0.25*(0.15+s))*clamp(r*3.0,0.0,1.0);
  col+=vec3(1.0,0.03,0.08)*kick*kick*0.7;                                           // strobe
  col+=vec3(0.9,0.0,0.2)*pow(max(0.0,1.0-r*4.0),2.0)*bass;
  return col+feedback(0.97, 0.0, 0.70, vec2(0.0));
}
vec3 bars(vec2 uv){ // mirrored spectrum bars with reflection
  float x=variant<0.5 ? abs(st.x*2.0-1.0) : st.x;                                   // mirrored, or left-to-right
  float bi=floor(x*N)/N; float s=spectrum(bi+0.5/N);
  float inBar=step(fract(x*N),0.78);
  float h=s*0.6+0.01, y=uv.y;
  float bar=inBar*step(abs(y),h*0.5);
  float glow=inBar*0.012/(abs(abs(y)-h*0.5)+0.012)*s;
  vec3 col=pal(bi*0.7+hue)*(bar*(0.5+s*1.2)+glow);
  col*=mix(0.35,1.0,step(0.0,y));                                                   // dim reflection below
  float wy=waveform(st.x)*0.2*(0.5+rms*3.0);
  col+=pal(0.9+hue)*0.003/(abs(y-wy)+0.003);
  col+=vec3(0.3,0.0,0.08)*kick*(1.0-abs(y)*2.0);
  return col+feedback(0.998, 0.0, 0.70, vec2(0.0,-0.002));
}
void main(){
  vec2 uv=(st-0.5)*vec2(res.x/res.y,1.0);
  vec3 col = scene==0 ? vamp(uv) : scene==1 ? slowed(uv) : scene==2 ? rage(uv) : bars(uv);
  frag=vec4(max(col,0.0),1.0);
}"""

BLOOM_DOWN = """#version 330
uniform sampler2D tex; in vec2 st; out vec4 frag;
void main(){ vec3 c=texture(tex,st).rgb; frag=vec4(max(c-0.85,0.0),1.0); }"""

BLUR = """#version 330
uniform sampler2D tex; uniform vec2 dir; in vec2 st; out vec4 frag;
void main(){ vec2 px=dir/vec2(textureSize(tex,0)); vec3 c=vec3(0.0);
  float w[5]=float[](0.227,0.194,0.121,0.054,0.016);
  c+=texture(tex,st).rgb*w[0];
  for(int i=1;i<5;i++){ c+=texture(tex,st+px*float(i)*1.5).rgb*w[i]; c+=texture(tex,st-px*float(i)*1.5).rgb*w[i]; }
  frag=vec4(c,1.0); }"""

COMPOSITE = """#version 330
uniform sampler2D tex, bloom; uniform float time, kick, hue; in vec2 st; out vec4 frag;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
vec3 hueShift(vec3 c, float h){ const vec3 k=vec3(0.57735); float cs=cos(h), sn=sin(h);
  return c*cs + cross(k,c)*sn + k*dot(k,c)*(1.0-cs); }
void main(){
  vec2 d=(st-0.5)*(0.0025+kick*0.012);                                   // chromatic aberration, pumps on kicks
  vec3 c=vec3(texture(tex,st+d).r, texture(tex,st).g, texture(tex,st-d).b);
  c+=texture(bloom,st).rgb*0.6;
  c=hueShift(c, hue*0.35);
  c=c/(1.0+c)*1.15;                                                       // tonemap
  c*=1.0-0.55*pow(length(st-0.5),2.2);                                    // vignette
  c+=(hash(st*vec2(1920.0,1080.0)+fract(time))-0.5)*0.02;               // grain
  frag=vec4(pow(max(c,0.0),vec3(1.0/2.2)),1.0);
}"""
