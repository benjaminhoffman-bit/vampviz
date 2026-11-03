"""VAMPVIZ shaders: one fullscreen-triangle vertex shader, the scene shader (five scenes, feedback), the bloom mip chain
(BLOOM_DOWN, BLOOM_UP) and the composite pass. Plain GLSL 330 strings; vampviz.pyw compiles them with moderngl.

Uniforms the app sets each frame on the scene program (any a scene does not read is dropped by the driver and skipped):
  res, time, bass, mid, high, kick, rms, hue, beats, variant, fdt, travel, scene; samplers spec, wave, prev.
Composite: tex, bloom, time, kick.
"""
N_BANDS = 96  # must match vampviz.N_BANDS

# ─── shaders ────────────────────────────────────────────────────────────────────────────────────────────────────────
VERT = """#version 330
out vec2 st;
void main(){ vec2 v = vec2((gl_VertexID<<1)&2, gl_VertexID&2); st = v; gl_Position = vec4(v*2.0-1.0, 0.0, 1.0); }"""

COMMON = """#version 330
uniform vec2 res; uniform float time, bass, mid, high, kick, rms, hue, beats, variant, fdt, travel; uniform int scene;
uniform sampler2D spec, wave, prev;
in vec2 st; out vec4 frag;
#define PI 3.14159265
const float N = __N__;
float spectrum(float x){ return texture(spec, vec2(clamp(x,0.0,1.0), 0.5)).r; }
float peakv(float x){ return texture(spec, vec2(clamp(x,0.0,1.0), 0.5)).g; }
float waveform(float x){ return texture(wave, vec2(fract(x), 0.5)).r; }
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
float noise(vec2 p){ vec2 i=floor(p), f=fract(p); f=f*f*(3.0-2.0*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),f.x), mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x), f.y); }
float fbm(vec2 p){ float v=0.0, a=0.5; mat2 m=mat2(1.6,1.2,-1.2,1.6); for(int i=0;i<5;i++){ v+=a*noise(p); p=m*p; a*=0.5; } return v; }
// vamp palette loop, stored linear (the composite's 1/2.2 gamma returns the designed display values): void -> dried blood -> neon crimson -> violet -> void, no seam while hue drifts
vec3 pal(float t){ t=fract(t)*4.0;
  vec3 a=vec3(0.0004,0.0,0.0014), b=vec3(0.268,0.0,0.0063), c=vec3(1.0,0.0063,0.061), d=vec3(0.173,0.0,1.0);
  return t<1.0 ? mix(a,b,t) : t<2.0 ? mix(b,c,t-1.0) : t<3.0 ? mix(c,d,t-2.0) : mix(d,a,t-3.0); }
mat2 rot(float a){ float c=cos(a), s=sin(a); return mat2(c,-s,s,c); }
vec3 dust(vec2 uv, float fly, float amt){   // motes flying out of the centre, 3 parallax layers (0 = nearest: biggest, fastest, brightest)
  float r=length(uv), a=atan(uv.y,uv.x), lr=log(r+0.05); vec3 col=vec3(0.0);
  for(int l=0;l<3;l++){ float fl=float(l);
    vec2 q=vec2(a/(2.0*PI)*(24.0+fl*16.0), lr*(6.0+fl*3.0)-fly*(1.6-fl*0.5));   // integer angle-cell counts: the atan seam is cell-aligned
    vec2 id=floor(q), f=fract(q)-0.5, o=(vec2(hash(id),hash(id+7.3))-0.5)*0.6;
    float on=step(0.6,hash(id+31.0));                                            // 40 % of cells carry a mote
    float e=length(f-o), d=e*(0.35+r);                                           // tighter away from the centre
    float g=exp(-d*d*120.0)*clamp(1.0-e/0.3,0.0,1.0);                            // windowed inside the cell: no tail, no cell edges, black stays black
    col+=pal(hash(id+3.0)*0.5+hue+0.4)*on*g*(1.0-fl*0.35)*smoothstep(0.0,0.25,r); }
  return col*amt; }
vec3 feedback(vec3 col, float zoom, float spin, float decay, vec2 warp){  // fdt = frames elapsed at 240 Hz, the rate these decays were tuned at: same trail length on every monitor
  vec2 c=(st-0.5)*pow(zoom,fdt); c=rot(spin*fdt)*c+warp*fdt;
  return col*(1.0-pow(decay,fdt))/(1.0-decay)                                  // direct light summed over the fdt frames this one stands for: same brightness on every monitor
       + max(texture(prev, c+0.5).rgb*pow(decay,fdt) - 0.012*fdt, 0.0); }  // floor subtract: trails die, black stays black
"""

SCENE = COMMON.replace("__N__", f"{N_BANDS}.0") + """
vec3 vamp(vec2 uv){ // kaleidoscope spectrum ring
  float r=length(uv), a=atan(uv.y,uv.x);
  float seg=floor(3.0+variant*6.0); float spin=(variant-0.5)*0.5;                   // 3..8 mirrors, either way
  float sa=mod(a+time*spin, 2.0*PI/seg); sa=abs(sa-PI/seg); float fx=sa/(PI/seg);
  float s=spectrum(fx*0.85+0.02);
  float ring=0.14+variant*0.12+bass*0.07+kick*0.02, d=r-ring, bar=s*(0.18+variant*0.16);
  float ad=fwidth(d); float inside=smoothstep(bar, bar-0.012, d)*smoothstep(-ad,ad,d);
  float line=0.0035/(abs(d)+0.0035);
  vec3 col=pal(fx*0.45+s*0.3+hue)*(inside*(0.35+0.6*s)+line*0.4*(0.4+bass));
  col+=vec3(1.0,0.0039,0.036)*pow(max(0.0,1.0-r/(ring*0.92)),3.0)*(0.25+kick*1.6);  // beating core
  float wr=0.52+waveform(abs(a)/PI)*0.07*(0.4+rms*4.0);                       // waveform halo
  col+=pal(0.85+hue)*0.0025/(abs(r-wr)+0.0025)*0.9;
  float smoke=fbm(uv*2.5+vec2(time*0.05,-time*0.03))*0.5;                           // void smoke
  col+=vec3(0.10,0.0,0.03)*smoke*(0.2+bass)*smoothstep(ring,ring+0.5,r);
  return feedback(col*0.8, 0.986, 0.004*(1.0+bass*2.0), 0.80, vec2(0.0));
}
vec3 slowed(vec2 uv){ // domain-warped fluid, heavy smear
  vec2 p=uv*(1.2+variant*1.4); float t=time*(0.06+variant*0.14);
  float n1=fbm(p+vec2(t,-t*0.7)), n2=fbm(p*1.7+vec2(n1*2.0,t*0.5));
  float n=fbm(p+3.0*vec2(n1,n2)*(0.5+bass*0.8));
  float s=spectrum(fract(n*1.4+bass*0.2));
  vec3 col=pal(n*0.9+hue+0.45)*(0.12+1.1*s);
  col+=vec3(0.268,0.0039,1.0)*mid*pow(n2,3.0)*1.2;
  float wy=waveform(st.x*0.5+time*0.02)*0.18*(0.5+rms*3.0)+sin(time*0.3)*0.1;    // drifting waveform
  col+=pal(0.9+hue)*0.004/(abs(uv.y-wy)+0.004)*0.6;
  vec2 warp=vec2(fbm(uv*3.0+time*0.2)-0.5, fbm(uv*3.0-time*0.17)-0.5)*0.004;
  return feedback(col*0.22, 0.993, 0.0015, 0.90, warp);
}
vec3 rage(vec2 uv){ // tunnel, strobing on kicks
  uv+=vec2(sin(time*53.0), cos(time*41.0))*kick*kick*0.012;  // decays with the kick: an impact, not jitter
  float r=length(uv), a=atan(uv.y,uv.x);
  float z=0.25/(r+0.02)+time*(0.6+variant*1.5+rms*8.0)+(beats-kick*kick)*0.5;  // kick^2 = exp(-t/0.08): the half-ring jump eases in over 80 ms
  float ang=a/(2.0*PI)+0.5+time*0.03;
  float s=spectrum(abs(fract(ang*2.0)*2.0-1.0)*0.9);
  float rings=smoothstep(0.08,0.0,abs(fract(z)-0.5)-0.42);
  float walls=floor(12.0+variant*40.0);
  float wall=smoothstep(0.2,0.0,abs(fract(ang*walls)-0.5)-0.45*(0.1+s*0.6));
  vec3 col=pal(fract(z*0.04)+hue)*(wall*s*1.2+rings*0.25*(0.15+s))*clamp(r*3.0,0.0,1.0);
  float flash=smoothstep(0.7,0.8,kick); col+=vec3(1.0,0.03,0.08)*flash*0.7*(0.25+0.75*wall)*smoothstep(0.0,0.5,r);  // 46 ms hard strobe on the walls, the core stays dark: no full-panel flash for the OLED's ABL
  col+=vec3(0.793,0.0,0.029)*pow(max(0.0,1.0-r*4.0),2.0)*bass;
  col+=dust(uv, time*(0.8+variant*0.6)+(beats-kick*kick)*0.5, 0.6)*(0.6+kick);  // same snap as the rings, so motes and rings pop together
  return feedback(col, 0.97, 0.0, 0.70, vec2(0.0));
}
vec3 bars(vec2 uv){ // mirrored spectrum bars, peak caps, floor reflection
  float x0=st.x; if(uv.y<0.0) x0=(x0-0.5)*(1.0+(-uv.y)*0.9)+0.5;                 // the floor recedes
  float x=variant<0.5 ? abs(x0*2.0-1.0) : x0;                                      // mirrored, or left-to-right
  float bi=floor(x*N)/N; float s=spectrum(bi+0.5/N);
  float inBar=step(fract(x*N),0.78)*step(abs(x0-0.5),0.5);                         // outside the strip the corners stay black
  float h=s*0.6+0.01, y=uv.y;
  float bar=inBar*step(abs(y),h*0.5);
  float glow=inBar*0.012/(abs(abs(y)-h*0.5)+0.012)*s;
  vec3 col=pal(bi*0.7+hue)*(bar*(0.5+s*1.2)+glow);
  float pk=peakv(bi+0.5/N)*0.6+0.02;                                               // gravity-fall peak cap (spec.g)
  col+=pal(bi*0.7+hue+0.35)*inBar*0.006/(abs(abs(y)-pk*0.5)+0.006)*1.6;
  col*=y<0.0 ? 0.35*exp(y*5.0) : 1.0;                                             // floor fades with depth
  float wy=waveform(st.x)*0.2*(0.5+rms*3.0);
  col+=pal(0.9+hue)*0.003/(abs(y-wy)+0.003);
  col+=vec3(0.071,0.0,0.0039)*kick*(1.0-abs(y)*2.0);
  return feedback(col, 0.998, 0.0, 0.70, vec2(0.0,-0.002));
}
float sdLattice(vec3 p){ vec3 q=abs(fract(p)-0.5);                         // beams through the cell centres, cell 1.0
  return min(min(max(q.y,q.z), max(q.x,q.z)), max(q.x,q.y))-0.06; }
vec3 lattice(vec2 uv){ // flight down the void between the beams; travel is integrated by the app (rms, variant, kick change speed, never position)
  vec3 ro=vec3(0.0, 0.0, travel);                                        // (0,0) is mid-void; (0.5,0.5) would be inside a beam
  vec3 rd=normalize(vec3(uv, 1.1-kick*0.25));                            // FOV pumps on the kick
  rd.xy=rot(time*0.1*(variant-0.5))*rd.xy;                               // slow roll, direction per section
  float t=0.0, d=1.0; vec3 col=vec3(0.0); int i=0;
  for(; i<128; i++){ d=sdLattice(ro+rd*t); if(d<0.002 || t>14.0) break; t+=d*0.9; }
  if(d<0.002){ vec3 p=ro+rd*t; float band=spectrum(fract(p.z*0.15));
    col=pal(fract(p.z*0.05)+hue+band*0.2)*(0.3+0.9*band);                  // emissive stripes down z
    col+=vec3(1.0,0.0039,0.036)*kick*0.5*step(0.9, fract(p.z*0.5)); }       // a 0.2-unit slab flashes every 2 units
  col*=exp(-t*0.18);                                                       // fog to void
  col+=vec3(0.6,0.0,0.15)*bass*0.08/(length(uv)+0.3);
  return feedback(col, 0.985, 0.0, 0.75, vec2(0.0)); }
void main(){
  vec2 uv=(st-0.5)*vec2(res.x/res.y,1.0)*(1.0 - kick*0.05);  // 5 % punch-in on the kick; feedback samples st, so the trails hold still behind it
  vec3 col = scene==0 ? vamp(uv) : scene==1 ? slowed(uv) : scene==2 ? rage(uv) : scene==3 ? bars(uv) : lattice(uv);
  frag=vec4(max(col,0.0),1.0);
}"""

BLOOM_DOWN = """#version 330
uniform sampler2D tex; uniform float thresh, knee; in vec2 st; out vec4 frag;
void main(){ vec2 px=1.0/vec2(textureSize(tex,0)); vec3 c=vec3(0.0);
  for(int i=0;i<4;i++){ vec2 o=vec2(float(i&1), float(i>>1))*2.0-1.0; c+=texture(tex, st+o*px).rgb; } c*=0.25;   // 4 bilinear taps = a full 4x4 box
  float br=max(c.r,max(c.g,c.b));
  float soft=clamp(br-thresh+knee, 0.0, 2.0*knee); soft=soft*soft/(4.0*knee+1e-4);                          // soft knee; thresh=knee=0 is a plain box
  frag=vec4(c*max(soft, br-thresh)/max(br,1e-4), 1.0); }"""

BLOOM_UP = """#version 330
uniform sampler2D tex; in vec2 st; out vec4 frag;   // 3x3 tent of the level below; the app adds it into this level (blend ONE,ONE)
void main(){ vec2 px=1.0/vec2(textureSize(tex,0)); vec3 c=vec3(0.0);
  float w[9]=float[](1.0,2.0,1.0,2.0,4.0,2.0,1.0,2.0,1.0);
  for(int i=0;i<9;i++) c+=texture(tex, st+(vec2(float(i%3), float(i/3))-1.0)*px).rgb*w[i];
  frag=vec4(c/16.0, 1.0); }"""

COMPOSITE = """#version 330
uniform sampler2D tex, bloom; uniform float time, kick; in vec2 st; out vec4 frag;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
  vec2 d=(st-0.5)*(0.0025+kick*0.012);                                   // chromatic aberration, pumps on kicks
  vec3 c=vec3(texture(tex,st+d).r, texture(tex,st).g, texture(tex,st-d).b);
  c+=texture(bloom,st).rgb*0.25;                                          // mip-chain bloom (see BLOOM_DOWN/UP)
  float m=max(c.r,max(c.g,c.b)); c*=min((1.0+m/16.0)/(1.0+m), 1.0/max(m,1e-4));  // tonemap the max channel, scale the rest with it: hue stays; above m=4 it clips to flat crimson instead of bleaching pink
  c*=1.0-0.55*pow(length(st-0.5),2.2);                                    // vignette
  vec3 o=pow(max(c,0.0),vec3(1.0/2.2));
  o+=(hash(gl_FragCoord.xy+fract(time)*vec2(37.0,91.0))-0.5)*(3.0/255.0);  // dither + faint grain, after gamma so black stays black
  frag=vec4(o,1.0);
}"""
