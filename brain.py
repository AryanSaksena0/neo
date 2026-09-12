"""
brain.py — Neo's memory, drawn as a living, thinking brain.

Reads memory.json and renders an interactive 3D neural cloud of Neo's memories:
muted glowing nodes grouped into clusters, connected by synapses, slowly
turning and firing. Drag to rotate, scroll to zoom in, click a memory to focus
it and light up everything connected, hover to read. On open it assembles
itself; every new thing Neo learns adds a node next time, so the brain grows.

Opens as its own native fullscreen window (falls back to the browser if
pywebview isn't installed). Say "show me your brain" / "open your memory", or
run `python brain.py`.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

from memory import load_memory

HERE = os.path.dirname(os.path.abspath(__file__))

STOP = set("""a an the and or but for with from into your you neo that this have has had
will would can could should its they them their her she they we us our of to in on at is are
was were be been being do does did not no yes about like just really very more most than
then over into onto only also after before while when where which who whom whose what how
the user wants built build building uses use using runs""".split())

# Generic buckets. Nothing here names a person, a company or a school: the
# map is drawn from whatever THIS person's memory holds.
CLUSTERS = [
    ("Goals",     r"\bgoal|deadline|target|plan to|wants? to|aim"),
    ("Projects",  r"project|building|\bapp\b|startup|company|product|launch|\brepo\b|\bcode\b"),
    ("People",    r"\bdad\b|\bmom\b|\bmum\b|mother|father|sister|brother|partner|wife|husband|friend|teammate|colleague|manager|boss|teacher|professor|coach"),
    ("Habits",    r"prefers?|likes?|hates?|always|never|usually|every (morning|day|week)|routine|wakes?|sleeps?|gym|runs?\b"),
    ("Money",     r"money|budget|pricing|price|revenue|salary|rent|invest|equity"),
    ("Work",      r"school|class|exam|course|homework|work\b|office|shift|meeting|client|customer"),
    ("Identity",  r""),
]


def _kw(text):
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOP}


def _cluster_of(text):
    low = text.lower()
    for i, (name, pat) in enumerate(CLUSTERS):
        if pat == "" or re.search(pat, low):
            return i, name
    return len(CLUSTERS) - 1, CLUSTERS[-1][0]


def build_graph(mem):
    facts = mem.get("facts", [])
    nodes = [{"id": "core", "label": "the user", "kind": "core", "group": -1, "size": 28}]
    links = []

    grouped, used, counts = [], {}, {}
    for f in facts:
        text = f["text"] if isinstance(f, dict) else str(f)
        gi, gname = _cluster_of(text)
        grouped.append((gi, gname, text))
        used.setdefault(gi, gname)
        counts[gi] = counts.get(gi, 0) + 1

    for gi, gname in sorted(used.items()):
        nodes.append({"id": f"hub{gi}", "label": gname, "kind": "hub",
                      "group": gi, "size": 12 + counts[gi] * 1.3})
        links.append({"source": "core", "target": f"hub{gi}", "strong": True})

    factkw = []
    for i, (gi, gname, text) in enumerate(grouped):
        nid = f"f{i}"
        nodes.append({"id": nid, "label": text, "kind": "fact", "group": gi, "size": 6.5})
        links.append({"source": f"hub{gi}", "target": nid, "strong": False})
        factkw.append((nid, _kw(text)))

    seen = set()
    for a in range(len(factkw)):
        scored = []
        for b in range(len(factkw)):
            if a == b:
                continue
            ov = len(factkw[a][1] & factkw[b][1])
            if ov >= 2:
                scored.append((ov, b))
        scored.sort(reverse=True)
        for _, b in scored[:2]:
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": factkw[a][0], "target": factkw[b][0],
                          "strong": False, "syn": True})

    return {"nodes": nodes, "links": links}


def render_html(graph):
    html = _TEMPLATE.replace("__DATA__", json.dumps(graph))
    path = os.path.join(tempfile.gettempdir(), "neo_brain.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def render_current(demo=False):
    mem = load_memory()
    if demo or not mem.get("facts"):
        mem = {"facts": [{"text": t} for t in _DEMO_FACTS]}
    return render_html(build_graph(mem))


def show(demo=False):
    """Open the brain in a native fullscreen window.
    Spawned as a separate process so its GUI loop never blocks Neo."""
    path = render_current(demo)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--window", path])
    return path


def _open_window(path):
    """Native WebKit window: activates to the front and enters real fullscreen."""
    import WebKit  # pyobjc-framework-WebKit
    from Cocoa import (
        NSApplication, NSWindow, NSObject, NSMakeRect, NSBackingStoreBuffered,
        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
        NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    )
    from Foundation import NSURL
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)  # Regular app -> can be frontmost (no beep)

    class _Delegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, s):
            return True
        def windowWillClose_(self, n):
            NSApplication.sharedApplication().terminate_(None)

    deleg = _Delegate.alloc().init()
    app.setDelegate_(deleg)

    mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
    rect = NSMakeRect(0, 0, 1280, 820)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, mask, NSBackingStoreBuffered, False)
    win.setTitle_("Neo — memory")
    win.setDelegate_(deleg)

    web = WebKit.WKWebView.alloc().initWithFrame_(rect)
    web.setAutoresizingMask_(18)  # width-sizable | height-sizable
    win.setContentView_(web)

    url = NSURL.fileURLWithPath_(path)
    # Allow loading local file:// images (e.g. generated carousel slides) from anywhere.
    web.loadFileURL_allowingReadAccessToURL_(url, NSURL.fileURLWithPath_("/"))

    win.center()
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    win.toggleFullScreen_(None)   # real macOS fullscreen
    AppHelper.runEventLoop()


_DEMO_FACTS = [
    "They prefer short, direct replies.",
    "They take the 8:15 train on weekdays.",
    "Their sister is called Ana.",
    "They are working on a thesis due in November.",
    "Neo runs free on Gemini plus local speech models.",
]


_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Neo — memory</title>
<style>
  html,body{margin:0;height:100%;overflow:hidden;background:#070809;
    font-family:-apple-system,BlinkMacSystemFont,sans-serif;}
  #wrap{position:fixed;inset:0;background:radial-gradient(ellipse at 50% 45%,#0d0f12 0%,#070809 72%);}
  #c{display:block;cursor:grab;}
  #c:active{cursor:grabbing;}
  #title{position:fixed;top:18px;left:22px;z-index:5;color:#c2cad2;font-weight:600;font-size:15px;}
  #sub{position:fixed;top:40px;left:22px;z-index:5;color:#565d66;font-size:12px;}
  #btn{position:fixed;top:16px;right:20px;z-index:5;background:#11151c;color:#cfd2da;
    border:1px solid #29333f;border-radius:8px;padding:8px 13px;font-size:13px;cursor:pointer;}
  #btn:hover{background:#1a212b;}
  #info{position:fixed;left:22px;bottom:22px;max-width:min(560px,46vw);z-index:5;
    color:#e2e8ef;font-size:15px;line-height:1.5;background:rgba(14,17,22,.85);
    border:1px solid #28323e;border-radius:12px;padding:14px 16px;opacity:0;
    transition:opacity .15s;pointer-events:none;}
  #info .tag{display:inline-block;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px;}
</style></head>
<body><div id="wrap">
  <div id="title">Neo · memory</div>
  <div id="sub"></div>
  <button id="btn">Fullscreen</button>
  <div id="info"></div>
  <canvas id="c"></canvas>
</div>
<script>
const G = __DATA__;
const COL=["#cfd5dc","#9fb0c1","#828f9d","#b3a994","#9fae9f","#a89fa8","#aeb6bf","#b9b0a2","#9aa6b2"];
const groupColor=g=> g<0 ? "#eef4f8" : COL[g%COL.length];

const cv=document.getElementById('c'),ctx=cv.getContext('2d'),info=document.getElementById('info');
const dpr=Math.min(2,window.devicePixelRatio||1);let W,H;
function size(){W=innerWidth;H=innerHeight;cv.width=W*dpr;cv.height=H*dpr;cv.style.width=W+'px';cv.style.height=H+'px';ctx.setTransform(dpr,0,0,dpr,0,0);}
size();addEventListener('resize',size);
document.getElementById('btn').onclick=()=>{const e=document.documentElement;(e.requestFullscreen||e.webkitRequestFullscreen||(()=>{})).call(e);};
document.getElementById('sub').textContent=G.nodes.filter(n=>n.kind==='fact').length+" memories — turning, firing, growing";

const idx={};G.nodes.forEach((n,i)=>idx[n.id]=i);
const groups=[...new Set(G.nodes.filter(n=>n.kind==='hub').map(n=>n.group))];
const dirs={};
groups.forEach((g,k)=>{const ng=groups.length;const y=1-(k+0.5)/ng*2;const rad=Math.sqrt(Math.max(0.0001,1-y*y));
  const th=k*2.39996;dirs[g]={x:Math.cos(th)*rad,y:y,z:Math.sin(th)*rad};});
const jit=a=>(Math.random()*2-1)*a;
G.nodes.forEach(n=>{
  if(n.kind==='core'){n.X3=0;n.Y3=0;n.Z3=0;n.r0=7;n.born=0;}
  else{const d=dirs[n.group]||{x:0,y:0.2,z:0};const R=n.kind==='hub'?98:165;
    n.X3=d.x*R+jit(n.kind==='hub'?12:60);
    n.Y3=d.y*R*0.82+jit(n.kind==='hub'?12:52);
    n.Z3=d.z*R+jit(n.kind==='hub'?12:60);
    n.r0=n.kind==='hub'?Math.max(4,n.size*0.42):2.4;
    n.born=(n.kind==='hub'?0.2:0.5)+Math.random()*1.2;}
  n.ph=Math.random()*6.28;n.sp=1+Math.random()*1.5;
});
const E=G.links.map(l=>({a:idx[l.source],b:idx[l.target],strong:l.strong,syn:l.syn}))
  .filter(e=>e.a!=null&&e.b!=null);
const adj={};E.forEach(e=>{(adj[e.a]=adj[e.a]||new Set()).add(e.b);(adj[e.b]=adj[e.b]||new Set()).add(e.a);});

let angY=0,tilt=0.42,zoom=1,auto=true,drag=false,lx=0,ly=0,moved=0;
let focus=-1,hover=-1,Pp=[];const START=performance.now()/1000;const FOV=360;
function proj(n){const ca=Math.cos(angY),sa=Math.sin(angY);
  let x=n.X3*ca-n.Z3*sa,z=n.X3*sa+n.Z3*ca,y=n.Y3;
  const ct=Math.cos(tilt),st=Math.sin(tilt);const y2=y*ct-z*st,z2=y*st+z*ct;
  const sc=FOV/(FOV+z2)*zoom;return{X:W/2+x*sc,Y:H/2+y2*sc,s:sc,z:z2};}
const appear=(n,t)=>Math.max(0,Math.min(1,(t-START-n.born)/0.7));

function rel(e){const r=cv.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};}
function pick(e){const m=rel(e);let best=-1,bd=1e9;
  for(let i=0;i<Pp.length;i++){const p=Pp[i];const dx=p.X-m.x,dy=p.Y-m.y,d=dx*dx+dy*dy;
    const rad=(G.nodes[i].r0*p.s)+9;if(d<rad*rad&&d<bd){bd=d;best=i;}}return best;}
function showInfo(i){const n=G.nodes[i];
  if(n.kind==='core'){info.innerHTML='<div class="tag" style="color:#9fb0c1">core</div>the user — everything Neo knows branches from here.';}
  else{const hub=G.nodes.find(m=>m.kind==='hub'&&m.group===n.group);const c=groupColor(n.group);
    const tag=hub?hub.label:'memory';
    info.innerHTML='<div class="tag" style="color:'+c+'">'+tag+'</div>'+
      (n.kind==='fact'?n.label:n.label+' — '+(adj[i]?adj[i].size:0)+' linked memories');}
  info.style.opacity=1;}

cv.addEventListener('mousedown',e=>{drag=true;auto=false;lx=e.clientX;ly=e.clientY;moved=0;});
addEventListener('mouseup',e=>{if(drag&&moved<5){const i=pick(e);if(i>=0){focus=(focus===i?-1:i);focus>=0?showInfo(focus):info.style.opacity=0;}else{focus=-1;info.style.opacity=0;}}
  drag=false;setTimeout(()=>{auto=true;},1500);});
addEventListener('mousemove',e=>{
  if(drag){const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;moved+=Math.abs(dx)+Math.abs(dy);
    angY+=dx*0.005;tilt=Math.max(-1.2,Math.min(1.2,tilt+dy*0.005));}
  else{hover=pick(e);cv.style.cursor=hover>=0?'pointer':'grab';if(hover>=0)showInfo(hover);else if(focus<0)info.style.opacity=0;else showInfo(focus);}});
cv.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(0.4,Math.min(5,zoom*(e.deltaY<0?1.12:0.89)));},{passive:false});

let pulses=[];
setInterval(()=>{if(pulses.length<20&&E.length){pulses.push({e:E[(Math.random()*E.length)|0],t:0,sp:0.4+Math.random()*0.6});}},170);

function frame(){
  const t=performance.now()/1000;
  if(auto&&!drag)angY+=0.0022;
  ctx.clearRect(0,0,W,H);
  Pp=G.nodes.map(proj);

  for(const e of E){const a=Pp[e.a],b=Pp[e.b];
    if(appear(G.nodes[e.a],t)<=0||appear(G.nodes[e.b],t)<=0)continue;
    let al=0.03+(a.s+b.s)/2*0.06;
    if(focus>=0)al=(e.a===focus||e.b===focus)?0.55:0.012;
    ctx.strokeStyle="rgba(150,165,185,"+al.toFixed(3)+")";
    ctx.lineWidth=e.strong?1.0:0.6;ctx.beginPath();ctx.moveTo(a.X,a.Y);ctx.lineTo(b.X,b.Y);ctx.stroke();}

  pulses.forEach(p=>p.t+=p.sp*0.02);pulses=pulses.filter(p=>p.t<1);
  for(const p of pulses){const a=Pp[p.e.a],b=Pp[p.e.b];if(!a||!b)continue;
    const x=a.X+(b.X-a.X)*p.t,y=a.Y+(b.Y-a.Y)*p.t;
    ctx.fillStyle="rgba(225,231,238,"+(Math.sin(p.t*Math.PI)*0.7).toFixed(3)+")";
    ctx.shadowColor="rgba(200,212,226,0.7)";ctx.shadowBlur=6;ctx.beginPath();ctx.arc(x,y,1.8,0,6.3);ctx.fill();ctx.shadowBlur=0;}

  const order=[...G.nodes.keys()].sort((i,j)=>Pp[i].z-Pp[j].z);
  for(const i of order){const n=G.nodes[i],p=Pp[i],ap=appear(n,t);if(ap<=0)continue;
    const br=0.55+0.45*Math.sin(t*n.sp+n.ph);
    let r=n.r0*p.s*(0.8+0.4*br)*ap,alpha=Math.max(0.12,Math.min(0.95,p.s*br));
    if(focus>=0){const near=i===focus||(adj[focus]&&adj[focus].has(i));alpha*=near?1:0.16;if(i===focus)r*=1.9;}
    if(i===hover)r*=1.5;
    const c=n.kind==='core'?"#eef4f8":groupColor(n.group);
    ctx.shadowColor=c;ctx.shadowBlur=(i===focus?14:5)*p.s;ctx.globalAlpha=alpha;ctx.fillStyle=c;
    ctx.beginPath();ctx.arc(p.X,p.Y,Math.max(0.6,r),0,6.3);ctx.fill();}
  ctx.globalAlpha=1;ctx.shadowBlur=0;

  ctx.textBaseline="middle";
  for(let i=0;i<G.nodes.length;i++){const n=G.nodes[i],p=Pp[i],ap=appear(n,t);if(ap<0.5)continue;
    let show=false,fs=13,c="#c2cad2";
    if(n.kind==='core'){show=true;fs=16;c="#eef4f8";}
    else if(n.kind==='hub'){show=true;fs=13;c=groupColor(n.group);}
    else if(zoom>1.6||i===hover||i===focus){show=true;fs=11;c="#aeb6c2";}
    if(!show)continue;
    if(focus>=0&&!(i===focus||(adj[focus]&&adj[focus].has(i))))continue;
    ctx.globalAlpha=Math.min(1,ap*Math.max(0.45,p.s));ctx.fillStyle=c;
    ctx.font=(n.kind==='fact'?"400 ":"600 ")+fs+"px -apple-system,sans-serif";
    const lbl=n.kind==='fact'?(n.label.length>42?n.label.slice(0,40)+'…':n.label):n.label;
    ctx.fillText(lbl,p.X+n.r0*p.s+6,p.Y);}
  ctx.globalAlpha=1;
  requestAnimationFrame(frame);
}
frame();
</script></body></html>
"""


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--window":
        try:
            _open_window(sys.argv[2])
        except Exception as e:
            print(f"[brain] native window failed ({e}); opening in browser.")
            subprocess.Popen(["open", sys.argv[2]])
    else:
        demo = "--demo" in sys.argv
        path = render_current(demo)
        try:
            _open_window(path)
        except Exception:
            subprocess.Popen(["open", path])
            print("Opening Neo's brain in your browser:", path)
