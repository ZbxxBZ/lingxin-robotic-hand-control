"use strict";

// L10 网页控制台前端：读取 /api/catalog 建界面，每 100 ms 轮询 /api/state 刷新，
// 操作通过 POST /api/command 发给 06_web_dashboard.py。不依赖任何外部库。

const $ = (id) => document.getElementById(id);
const FINGERS = ["thumb", "index", "middle", "ring", "pinky"];
const FINGER_COLOR = { thumb: "#e4572e", index: "#f3a712", middle: "#29a19c", ring: "#3d7ad6", pinky: "#8f5bd6" };
const POLL_MS = 100;
const HISTORY_S = 10;
const HOLD_AFTER_SEND_MS = 400; // 发出滑块指令后，这段时间内不用服务端的值覆盖滑块

let catalog = null;
let displayMax = 100;
let fingerLabel = {};
const dragging = new Set();
const pending = new Map();
const holdUntil = new Array(10).fill(0);
let flushTimer = null;
let speedTouched = false;
const history = [];
let lastLogKey = "";

// ---------------------------------------------------------------- 工具
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 2600);
}

async function command(kind, payload = {}) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, ...payload }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    toast(`指令发送失败：${err.message}`);
  }
}

// 触觉热力图配色（近似 inferno）
const HEAT_STOPS = [
  [0, [0, 0, 4]],
  [0.25, [87, 16, 110]],
  [0.5, [188, 55, 84]],
  [0.75, [249, 142, 9]],
  [1, [252, 255, 164]],
];

function ramp(stops, x) {
  const t = Math.min(1, Math.max(0, Number.isFinite(x) ? x : 0));
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i][0]) {
      const [x0, c0] = stops[i - 1];
      const [x1, c1] = stops[i];
      const k = (t - x0) / (x1 - x0);
      return c0.map((v, j) => Math.round(v + (c1[j] - v) * k));
    }
  }
  return stops[stops.length - 1][1];
}
const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;

// ---------------------------------------------------------------- 关节滑块
function buildJoints() {
  const box = $("joints");
  catalog.joints.forEach((joint, i) => {
    const row = el("div", "joint-row");
    const label = el("div", "j-label");
    label.title = joint.name;
    label.append(el("span", "j-idx", String(i + 1)), document.createTextNode(joint.label));

    const track = el("div", "j-track");
    const slider = document.createElement("input");
    Object.assign(slider, { type: "range", min: "0", max: "255", step: "1", id: `j${i}` });
    const meas = el("div", "j-meas");
    meas.id = `jm${i}`;
    track.append(slider, meas);

    const value = el("div", "j-val");
    const targetText = el("span", null, "--");
    targetText.id = `jt${i}`;
    const measText = el("small", null, "--");
    measText.id = `jmv${i}`;
    value.append(targetText, measText);

    row.append(label, track, value);
    box.appendChild(row);

    slider.addEventListener("pointerdown", () => dragging.add(i));
    const release = () => {
      if (dragging.delete(i)) queueJoint(i, Number(slider.value));
    };
    slider.addEventListener("pointerup", release);
    slider.addEventListener("pointercancel", release);
    slider.addEventListener("input", () => {
      targetText.textContent = slider.value;
      queueJoint(i, Number(slider.value));
    });
  });
}

function queueJoint(index, value) {
  pending.set(index, value);
  if (!flushTimer) flushTimer = setTimeout(flushJoints, 60);
}

function flushJoints() {
  flushTimer = null;
  const now = performance.now();
  for (const [index, value] of pending) {
    command("joint", { index, value });
    holdUntil[index] = now + HOLD_AFTER_SEND_MS;
  }
  pending.clear();
}

function updateJoints(s) {
  if (!Array.isArray(s.target)) return;
  const now = performance.now();
  s.target.forEach((target, i) => {
    if (!dragging.has(i) && !pending.has(i) && now > holdUntil[i]) {
      $(`j${i}`).value = target;
      $(`jt${i}`).textContent = target;
    }
    const measured = Array.isArray(s.measured) ? s.measured[i] : null;
    $(`jmv${i}`).textContent = measured == null ? "--" : `实测 ${measured}`;
    if (measured != null) {
      // 滑块圆钮约 16px 宽，竖线按圆钮中心的位置换算
      $(`jm${i}`).style.left = `calc(8px + (100% - 16px) * ${measured / 255})`;
    }
  });
}

// ---------------------------------------------------------------- 预设 / 动画
function describePreset(p) {
  const fingers = p.fingers ? `手指 [${p.fingers.join(", ")}]` : "手指保持不变";
  const palm = p.palm ? `掌部 [${p.palm.join(", ")}]` : "掌部保持不变";
  return `${fingers}\n${palm}`;
}

function buildPresets() {
  const box = $("presets");
  const groups = new Map();
  for (const p of catalog.presets) {
    if (!groups.has(p.group)) groups.set(p.group, []);
    const title = (p.custom ? "已按真机调整（l10_actions.USER_OVERRIDES）\n" : "") + describePreset(p);
    groups.get(p.group).push({ key: p.key, label: p.label, title });
  }
  groups.set(
    "官方 GUI 预设（齿轮版 L10，来自 l10_config.POSES）",
    catalog.gui_poses.map((g) => ({ key: g.key, label: g.label, title: `[${g.pose.join(", ")}]` })),
  );
  for (const [name, items] of groups) {
    box.appendChild(el("div", "group-title", name));
    const grid = el("div", "btn-grid");
    for (const item of items) {
      const button = el("button", "btn", item.label);
      button.title = item.title;
      button.addEventListener("click", () => command("preset", { key: item.key }));
      grid.appendChild(button);
    }
    box.appendChild(grid);
  }
}

function buildAnimations() {
  const box = $("animations");
  for (const a of catalog.animations) {
    const button = el("button", "btn");
    button.dataset.key = a.key;
    button.append(document.createTextNode(a.label), el("small", null, `${a.interval_ms}ms`));
    button.title = `来源：${a.source}\n${a.steps} 步，默认间隔 ${a.interval_ms} ms`;
    button.addEventListener("click", () => {
      const intervalRaw = $("anim-interval").value.trim();
      const cyclesRaw = $("anim-cycles").value.trim();
      const interval = intervalRaw === "" ? null : Math.max(catalog.min_anim_interval_ms, parseInt(intervalRaw, 10) || 0);
      const cycles = cyclesRaw === "" ? 1 : Math.max(0, parseInt(cyclesRaw, 10) || 0);
      command("animation", { key: a.key, interval_ms: interval, cycles });
    });
    box.appendChild(button);
  }
}

function updateAnimation(s) {
  const a = s.animation;
  const status = $("anim-status");
  document.querySelectorAll("#animations .btn").forEach((b) => {
    b.classList.toggle("active", !!a && b.dataset.key === a.key);
  });
  const tr =
    (s.transition ? ` · 分段过渡 ${s.transition.stage}/${s.transition.stages}` : "") + (s.relief ? " · 压力卸载中" : "");
  if (!a) {
    status.textContent = tr ? `预设${tr.replace(" · ", " ")}` : "未播放";
    status.classList.toggle("playing", !!tr);
    return;
  }
  status.classList.add("playing");
  const loops = a.cycles === 0 ? `第 ${a.cycle} 轮（无限）` : `第 ${a.cycle}/${a.cycles} 轮`;
  status.textContent = `播放中：${a.label} · ${loops} · ${a.step}/${a.steps} 步 · ${a.interval_ms} ms${tr}`;
}

// ---------------------------------------------------------------- 手部 3D 示意
// 纯 Canvas 2D 绘制，不依赖外部库。坐标单位约为毫米：手掌在 x–y 平面，手指沿 +y，掌心朝 +z；
// 掌心朝向观察者时，左手拇指在左（-x 侧）、右手拇指在右（+x 侧）。关节角到形状的换算是示意性的：
//   四指弯曲 255→0 对应掌指 / 近节 / 远节合计弯曲约 230°，侧摆约 ±12°；
//   拇指翻滚决定离开掌面的抬起角，拇指旋转决定朝食指方向的摆角，拇指弯曲决定近节 / 远节弯曲。
const DEG = Math.PI / 180;
const VIEWS = {
  default: { yaw: -30, pitch: -18 },
  palm: { yaw: 0, pitch: 0 },
  side: { yaw: -82, pitch: -6 },
  back: { yaw: 180, pitch: 0 },
};
const PALM_OUTLINE = [
  [-38, 84], [-14, 90], [10, 92], [34, 88], [44, 58], [42, 22], [28, 0], [-26, 0], [-40, 18],
];
const PALM_HALF_THICK = 11;
const FINGER_MODEL = {
  index: { base: [30, 87], lens: [44, 26, 20], r: 8.5, splay: 3, away: 1, flex: 2, abd: 6 },
  middle: { base: [10, 91], lens: [49, 30, 22], r: 9, splay: 0, away: 0, flex: 3, abd: null },
  ring: { base: [-11, 89], lens: [46, 28, 21], r: 8.5, splay: -2, away: -1, flex: 4, abd: 7 },
  pinky: { base: [-30, 82], lens: [37, 22, 18], r: 7.5, splay: -6, away: -1, flex: 5, abd: 8 },
};
const THUMB_MODEL = { base: [32, 14, 4], lens: [38, 31, 25], r: 10 };
const SKIN = [241, 205, 185];
const TIP3D_STOPS = [
  [0, SKIN],
  [0.4, [249, 140, 60]],
  [1, [220, 38, 38]],
];
const LIGHT = norm3([0.35, 0.55, 1]);
const view3d = { yaw: VIEWS.default.yaw * DEG, pitch: VIEWS.default.pitch * DEG, side: 1, drag: null, frame: 0, data: null };

function add3(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}
function sub3(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}
function mul3(a, k) {
  return [a[0] * k, a[1] * k, a[2] * k];
}
function dot3(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}
function cross3(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function norm3(a) {
  const len = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / len, a[1] / len, a[2] / len];
}

// 把方向 d 朝 toward 方向转 angle 弧度（在两者张成的平面内）
function rotateToward(d, toward, angle) {
  if (!angle) return d;
  const perp = sub3(toward, mul3(d, dot3(toward, d)));
  const len = Math.hypot(perp[0], perp[1], perp[2]);
  if (len < 1e-6) return d;
  const p = mul3(perp, 1 / len);
  return norm3(add3(mul3(d, Math.cos(angle)), mul3(p, Math.sin(angle))));
}

function fingerChain(name, pose, side) {
  const m = FINGER_MODEL[name];
  const t = 1 - pose[m.flex] / 255; // 0 伸直 → 1 完全弯曲
  const bends = [80 * t, 95 * t, 55 * t];
  const abdRaw = m.abd == null ? 128 : pose[m.abd];
  const spread = (m.splay + ((abdRaw - 128) / 128) * 12 * m.away) * DEG;
  const d0 = [side * Math.sin(spread), Math.cos(spread), 0];
  const pts = [[side * m.base[0], m.base[1], 0]];
  let theta = 0;
  m.lens.forEach((len, i) => {
    theta += bends[i] * DEG;
    const dir = add3(mul3(d0, Math.cos(theta)), [0, 0, Math.sin(theta)]); // 向掌心（+z）弯
    pts.push(add3(pts[i], mul3(dir, len)));
  });
  return pts;
}

function thumbChain(pose, side) {
  const elev = (1 - pose[1] / 255) * 65 * DEG; // 翻滚：255 贴近掌面，0 抬到垂直掌面
  const swing = (55 - (1 - pose[9] / 255) * 75) * DEG; // 旋转：255 外展，0 转向食指
  let dir = norm3([side * Math.sin(swing) * Math.cos(elev), Math.cos(swing) * Math.cos(elev), Math.sin(elev)]);
  const t = 1 - pose[0] / 255;
  const bends = [0, 45 * t, 65 * t]; // 掌骨不弯，近节 / 远节随拇指弯曲
  const toward = norm3([-side, 0.35, 0.9]); // 弯向掌心和食指方向
  const pts = [[side * THUMB_MODEL.base[0], THUMB_MODEL.base[1], THUMB_MODEL.base[2]]];
  THUMB_MODEL.lens.forEach((len, i) => {
    dir = rotateToward(dir, toward, bends[i] * DEG);
    pts.push(add3(pts[i], mul3(dir, len)));
  });
  return pts;
}

function handChains(pose, side) {
  const chains = {};
  for (const name of ["index", "middle", "ring", "pinky"]) {
    const m = FINGER_MODEL[name];
    chains[name] = { pts: fingerChain(name, pose, side), r: m.r };
  }
  chains.thumb = { pts: thumbChain(pose, side), r: THUMB_MODEL.r };
  return chains;
}

// 世界坐标 → 视图坐标：以手掌中部为中心，先绕 y 轴转 yaw，再绕 x 轴转 pitch
function toView(p) {
  const c = [p[0] - view3d.side * 18, p[1] - 85, p[2]];
  const cy = Math.cos(view3d.yaw);
  const sy = Math.sin(view3d.yaw);
  const x1 = c[0] * cy + c[2] * sy;
  const z1 = -c[0] * sy + c[2] * cy;
  const cp = Math.cos(view3d.pitch);
  const sp = Math.sin(view3d.pitch);
  return [x1, c[1] * cp - z1 * sp, c[1] * sp + z1 * cp];
}

function project(p, w, h) {
  const v = toView(p);
  const camera = 520;
  const depth = camera - v[2];
  const k = (Math.min(w * 1.9, h * 2.15) / camera) * (camera / depth);
  return { x: w / 2 + v[0] * k, y: h * 0.52 - v[1] * k, k, depth, v };
}

function shade(rgbArr, f) {
  return `rgb(${rgbArr.map((c) => Math.max(0, Math.min(255, Math.round(c * f)))).join(",")})`;
}

function drawFace(ctx, pts) {
  const v = pts.map((p) => p.v);
  const n = norm3(cross3(sub3(v[1], v[0]), sub3(v[2], v[0])));
  const light = 0.8 + 0.26 * Math.abs(dot3(n, LIGHT));
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
  ctx.closePath();
  ctx.fillStyle = shade(SKIN, light);
  ctx.fill();
  ctx.strokeStyle = shade(SKIN, 0.84);
  ctx.lineWidth = 1;
  ctx.stroke();
}

function drawCapsule(ctx, a, b, r) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len;
  const ny = dx / len;
  const rs = (r * (a.k + b.k)) / 2;
  const fog = Math.max(0.82, Math.min(1.05, 520 / ((a.depth + b.depth) / 2)));
  const grad = ctx.createLinearGradient(a.x + nx * rs, a.y + ny * rs, a.x - nx * rs, a.y - ny * rs);
  grad.addColorStop(0, shade(SKIN, 0.66 * fog));
  grad.addColorStop(0.35, shade(SKIN, 1.06 * fog));
  grad.addColorStop(1, shade(SKIN, 0.72 * fog));
  ctx.strokeStyle = grad;
  ctx.lineWidth = rs * 2;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
}

function drawTip(ctx, p, r, force) {
  const rs = r * p.k * 1.02;
  const base = force == null ? SKIN : ramp(TIP3D_STOPS, force / displayMax);
  const grad = ctx.createRadialGradient(p.x - rs * 0.35, p.y - rs * 0.4, rs * 0.15, p.x, p.y, rs);
  grad.addColorStop(0, shade(base, 1.18));
  grad.addColorStop(1, shade(base, 0.72));
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(p.x, p.y, rs, 0, Math.PI * 2);
  ctx.fill();
}

function render3d() {
  view3d.frame = 0;
  const canvas = $("hand3d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const data = view3d.data;
  if (!data) {
    ctx.fillStyle = "#6b7280";
    ctx.font = "13px 'Microsoft YaHei', sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("等待实测数据…", w / 2, h / 2);
    return;
  }
  const side = catalog.side === "right" ? 1 : -1; // 模型按 +x 侧拇指建模，左手镜像到 -x
  view3d.side = side;
  const P = (p) => project(p, w, h);
  const prims = [];

  // 手掌：前后两个面 + 一圈侧面
  const front = PALM_OUTLINE.map(([x, y]) => P([side * x, y, PALM_HALF_THICK]));
  const back = PALM_OUTLINE.map(([x, y]) => P([side * x, y, -PALM_HALF_THICK]));
  const faceDepth = (pts) => pts.reduce((s, p) => s + p.depth, 0) / pts.length;
  prims.push({ depth: faceDepth(front), draw: () => drawFace(ctx, front) });
  prims.push({ depth: faceDepth(back), draw: () => drawFace(ctx, back) });
  for (let i = 0; i < front.length; i++) {
    const j = (i + 1) % front.length;
    const quad = [front[i], front[j], back[j], back[i]];
    prims.push({ depth: faceDepth(quad), draw: () => drawFace(ctx, quad) });
  }

  // 手指：每节一个胶囊体，指尖一个球（颜色表示压力）
  const chains = handChains(data.measured, side);
  const projected = {};
  for (const [name, chain] of Object.entries(chains)) {
    const pts = chain.pts.map(P);
    projected[name] = pts;
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i];
      const b = pts[i + 1];
      const r = chain.r * (1 - 0.07 * i);
      prims.push({ depth: (a.depth + b.depth) / 2, draw: () => drawCapsule(ctx, a, b, r) });
    }
    const tip = pts[pts.length - 1];
    const force = data.forces[name];
    prims.push({ depth: tip.depth - 0.5, draw: () => drawTip(ctx, tip, chain.r * 0.86, force) });
  }

  prims.sort((a, b) => b.depth - a.depth); // 先画远的
  for (const prim of prims) prim.draw();

  // 目标姿态：和实测差别明显时画蓝色虚线骨架
  const differs = data.target.some((t, i) => Math.abs(t - data.measured[i]) > 8);
  if (differs) {
    const targetChains = handChains(data.target, side);
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = "rgba(37, 99, 235, 0.9)";
    ctx.fillStyle = "rgba(37, 99, 235, 0.9)";
    ctx.lineWidth = 2;
    for (const chain of Object.values(targetChains)) {
      const pts = chain.pts.map(P);
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.stroke();
      const tip = pts[pts.length - 1];
      ctx.beginPath();
      ctx.arc(tip.x, tip.y, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // 指尖压力数值
  ctx.font = "11px Consolas, monospace";
  ctx.textAlign = "center";
  for (const name of FINGERS) {
    const f = data.forces[name];
    if (f == null || f < 1) continue;
    const tip = projected[name][projected[name].length - 1];
    ctx.fillStyle = "#111827";
    ctx.fillText(String(Math.round(f)), tip.x, tip.y - chains[name].r * tip.k - 4);
  }

  ctx.fillStyle = "#6b7280";
  ctx.font = "12px 'Microsoft YaHei', sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`${side === 1 ? "右手" : "左手"} · 拇指旋转 ${data.measured[9]}`, 10, 18);
}

// 有官方模型时用 WebGL 渲染（renderGl），否则用上面的简化示意（render3d）；模型加载中先不画，
// 以免画布被 2D 上下文占用后无法再创建 WebGL 上下文。
function requestRender3d() {
  if (glModel.state === "loading" || view3d.frame) return;
  view3d.frame = requestAnimationFrame(glModel.state === "ready" ? renderGl : render3d);
}

function setView(name) {
  const table = glModel.state === "ready" ? GL_VIEWS : VIEWS;
  const v = table[name] || table.default;
  view3d.yaw = v.yaw * DEG;
  view3d.pitch = v.pitch * DEG;
  requestRender3d();
}

function buildHand() {
  bindHandCanvas($("hand3d"));
  document.querySelectorAll(".view-btns .btn").forEach((b) => {
    b.addEventListener("click", () => setView(b.dataset.view));
  });
  window.addEventListener("resize", requestRender3d);
  const note = $("hand-model-note");
  glModel.state = "loading";
  initGlModel($("hand3d"))
    .then((manifest) => {
      glModel.state = "ready";
      note.textContent = `官方模型 L20Lite（L10），来自 ${manifest.source.replace(/^linker-bot\//, "")} · 拖动旋转 · 双击复位`;
      setView("default");
    })
    .catch((err) => {
      console.warn("官方 3D 模型不可用，改用简化示意：", err);
      glModel.state = "failed";
      glModel.gl = null;
      const canvas = $("hand3d");
      if (!canvas.getContext("2d")) {
        // 画布已被 WebGL 占用：换一块新画布给 2D 示意用
        const fresh = canvas.cloneNode(false);
        canvas.replaceWith(fresh);
        bindHandCanvas(fresh);
      }
      note.textContent = `未能加载官方模型（${err.message}），显示简化示意 · 拖动旋转 · 双击复位`;
      setView("default");
    });
}

function bindHandCanvas(canvas) {
  canvas.addEventListener("pointerdown", (e) => {
    view3d.drag = { x: e.clientX, y: e.clientY, yaw: view3d.yaw, pitch: view3d.pitch };
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", (e) => {
    const d = view3d.drag;
    if (!d) return;
    view3d.yaw = d.yaw + (e.clientX - d.x) * 0.01;
    view3d.pitch = Math.max(-1.45, Math.min(1.45, d.pitch + (e.clientY - d.y) * 0.01));
    requestRender3d();
  });
  const endDrag = () => {
    view3d.drag = null;
    canvas.classList.remove("dragging");
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("dblclick", () => setView("default"));
}

function updateHand(s) {
  if (!Array.isArray(s.measured) || !Array.isArray(s.target)) return;
  view3d.data = { measured: s.measured, target: s.target, forces: s.forces || {} };
  requestRender3d();
}

// ---------------------------------------------------------------- 官方 3D 模型（WebGL2）
// 模型：linker-bot/linker-sim-assets 的 hands/l20lite（L10，Apache-2.0），由 tools/convert_l10_model.py
// 转成 web/model/l20lite_{side}.json/.bin。关节角按官方约定换算：角度 = 上限 + (下限 - 上限) × raw / 255，
// 指节联动按 URDF 的 mimic。加载失败或浏览器不支持 WebGL2 时，退回上面的简化示意图。
// 模型坐标：z 沿手指方向，手指向 +x 弯曲（+x 为掌心一侧），y 为横向。
const GL_VIEWS = {
  default: { yaw: 38, pitch: 16 },
  palm: { yaw: 0, pitch: 0 },
  side: { yaw: 90, pitch: 4 },
  back: { yaw: 180, pitch: 0 },
};
const GL_SHELL = [0.64, 0.67, 0.72];
const GL_PALM = [0.5, 0.52, 0.57];
const GL_TIP_STOPS = [
  [0, [150, 156, 166]],
  [0.4, [249, 140, 60]],
  [1, [220, 38, 38]],
];
const GL_FOV = 30 * DEG;
const glModel = { state: "idle", gl: null, prog: null, loc: {}, meshes: [], manifest: null, order: [], jointByName: {}, center: [0, 0, 0.09], distance: 0.4, tipLinks: {} };

const GL_VERTEX = `#version 300 es
in vec3 aPos;
uniform mat4 uViewProj;
uniform mat4 uModel;
uniform vec3 uMin;
uniform vec3 uExtent;
out vec3 vWorld;
void main() {
  vec4 world = uModel * vec4(uMin + aPos * uExtent, 1.0);
  vWorld = world.xyz;
  gl_Position = uViewProj * world;
}`;

const GL_FRAGMENT = `#version 300 es
precision highp float;
in vec3 vWorld;
uniform vec4 uColor;
uniform vec3 uEye;
uniform vec3 uLight;
out vec4 outColor;
void main() {
  vec3 n = normalize(cross(dFdx(vWorld), dFdy(vWorld)));   // 平面着色：由屏幕导数求法线
  vec3 v = normalize(uEye - vWorld);
  if (dot(n, v) < 0.0) n = -n;
  float diff = max(dot(n, uLight), 0.0);
  float rim = pow(1.0 - max(dot(n, v), 0.0), 2.0) * 0.18;
  float spec = pow(max(dot(n, normalize(uLight + v)), 0.0), 48.0) * 0.28;
  vec3 col = uColor.rgb * (0.4 + 0.6 * diff) + spec + rim;
  outColor = vec4(col, uColor.a);
}`;

// ---- 4×4 矩阵（列主序，和 WebGL 一致）
function m4identity() {
  const m = new Float32Array(16);
  m[0] = m[5] = m[10] = m[15] = 1;
  return m;
}
function m4mul(a, b) {
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
  }
  return o;
}
function m4translate(x, y, z) {
  const m = m4identity();
  m[12] = x;
  m[13] = y;
  m[14] = z;
  return m;
}
function m4axisAngle(axis, angle) {
  const [x, y, z] = axis;
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  const t = 1 - c;
  const m = m4identity();
  m[0] = t * x * x + c;
  m[1] = t * x * y + s * z;
  m[2] = t * x * z - s * y;
  m[4] = t * x * y - s * z;
  m[5] = t * y * y + c;
  m[6] = t * y * z + s * x;
  m[8] = t * x * z + s * y;
  m[9] = t * y * z - s * x;
  m[10] = t * z * z + c;
  return m;
}
function m4rpy([roll, pitch, yaw]) {
  // URDF：R = Rz(yaw) · Ry(pitch) · Rx(roll)
  return m4mul(m4mul(m4axisAngle([0, 0, 1], yaw), m4axisAngle([0, 1, 0], pitch)), m4axisAngle([1, 0, 0], roll));
}
function m4point(m, p) {
  return [
    m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
    m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
    m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
  ];
}
function m4perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  const m = new Float32Array(16);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (far + near) / (near - far);
  m[11] = -1;
  m[14] = (2 * far * near) / (near - far);
  return m;
}
function m4lookAt(eye, center, up) {
  const z = norm3(sub3(eye, center));
  const x = norm3(cross3(up, z));
  const y = cross3(z, x);
  const m = m4identity();
  m[0] = x[0];
  m[4] = x[1];
  m[8] = x[2];
  m[1] = y[0];
  m[5] = y[1];
  m[9] = y[2];
  m[2] = z[0];
  m[6] = z[1];
  m[10] = z[2];
  m[12] = -dot3(x, eye);
  m[13] = -dot3(y, eye);
  m[14] = -dot3(z, eye);
  return m;
}

function glCompile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
  return shader;
}

// 由 10 个原始值算出全部 URDF 关节角（含联动关节）
function glJointAngles(pose) {
  const q = {};
  glModel.manifest.channels.forEach((name, i) => {
    const j = glModel.jointByName[name];
    q[name] = j.upper + (j.lower - j.upper) * (Math.min(255, Math.max(0, pose[i])) / 255);
  });
  for (const j of glModel.manifest.joints) {
    if (j.mimic) q[j.name] = j.mimic.multiplier * (q[j.mimic.joint] ?? 0) + j.mimic.offset;
  }
  return q;
}

// 正运动学：各连杆在手掌（根连杆）坐标系中的位姿
function glLinkMatrices(pose) {
  const q = glJointAngles(pose);
  const mats = { [glModel.manifest.root]: m4identity() };
  for (const j of glModel.order) {
    let m = m4mul(mats[j.parent], j.origin);
    if (j.type === "revolute" || j.type === "continuous") m = m4mul(m, m4axisAngle(j.axis, q[j.name] ?? 0));
    mats[j.child] = m;
  }
  return mats;
}

async function initGlModel(canvas) {
  const side = catalog.side === "right" ? "right" : "left";
  const [metaRes, binRes] = await Promise.all([
    fetch(`/model/l20lite_${side}.json`, { cache: "force-cache" }),
    fetch(`/model/l20lite_${side}.bin`, { cache: "force-cache" }),
  ]);
  if (!metaRes.ok || !binRes.ok) throw new Error("没有找到 web/model 下的模型文件（先运行 tools/convert_l10_model.py）");
  const manifest = await metaRes.json();
  const bin = await binRes.arrayBuffer();
  const gl = canvas.getContext("webgl2", { antialias: true, premultipliedAlpha: false });
  if (!gl) throw new Error("浏览器不支持 WebGL2");

  const prog = gl.createProgram();
  gl.attachShader(prog, glCompile(gl, gl.VERTEX_SHADER, GL_VERTEX));
  gl.attachShader(prog, glCompile(gl, gl.FRAGMENT_SHADER, GL_FRAGMENT));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
  const loc = {};
  for (const name of ["uViewProj", "uModel", "uMin", "uExtent", "uColor", "uEye", "uLight"]) {
    loc[name] = gl.getUniformLocation(prog, name);
  }
  const aPos = gl.getAttribLocation(prog, "aPos");

  const meshes = manifest.meshes.map((mesh) => {
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const pos = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, pos);
    gl.bufferData(gl.ARRAY_BUFFER, new Uint16Array(bin, mesh.posOffset, mesh.vertexCount * 3), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 3, gl.UNSIGNED_SHORT, true, 0, 0); // 归一化到 0~1，着色器里还原
    const idx = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idx);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(bin, mesh.idxOffset, mesh.indexCount), gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    return { ...mesh, vao };
  });

  // 关节按“父连杆先于子连杆”排序，并预先算好固定的原点变换
  const jointByName = Object.fromEntries(manifest.joints.map((j) => [j.name, j]));
  const byParent = {};
  for (const j of manifest.joints) (byParent[j.parent] ||= []).push(j);
  const order = [];
  const queue = [manifest.root];
  while (queue.length) {
    const link = queue.shift();
    for (const j of byParent[link] || []) {
      j.origin = m4mul(m4translate(...j.xyz), m4rpy(j.rpy));
      order.push(j);
      queue.push(j.child);
    }
  }

  Object.assign(glModel, { gl, prog, loc, meshes, manifest, order, jointByName });
  const prefix = side === "right" ? "rh" : "lh";
  glModel.tipLinks = Object.fromEntries(FINGERS.map((f) => [`${prefix}_${f}_distal`, f]));

  // 以张开姿态的包围盒确定观察中心和距离
  const mats = glLinkMatrices([255, 255, 255, 255, 255, 255, 128, 128, 128, 255]);
  let lo = [Infinity, Infinity, Infinity];
  let hi = [-Infinity, -Infinity, -Infinity];
  for (const mesh of meshes) {
    for (const corner of [0, 1, 2, 3, 4, 5, 6, 7]) {
      const p = mesh.min.map((v, k) => v + ((corner >> k) & 1) * mesh.extent[k]);
      const w = m4point(mats[mesh.link], p);
      lo = lo.map((v, k) => Math.min(v, w[k]));
      hi = hi.map((v, k) => Math.max(v, w[k]));
    }
  }
  glModel.center = lo.map((v, k) => (v + hi[k]) / 2);
  const radius = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2;
  glModel.distance = (radius / Math.sin(GL_FOV / 2)) * 0.92;
  return manifest;
}

function glDrawPose(pose, colorOf) {
  const { gl, loc, meshes } = glModel;
  const mats = glLinkMatrices(pose);
  for (const mesh of meshes) {
    gl.uniformMatrix4fv(loc.uModel, false, mats[mesh.link]);
    gl.uniform3fv(loc.uMin, mesh.min);
    gl.uniform3fv(loc.uExtent, mesh.extent);
    gl.uniform4fv(loc.uColor, colorOf(mesh.link));
    gl.bindVertexArray(mesh.vao);
    gl.drawElements(gl.TRIANGLES, mesh.indexCount, gl.UNSIGNED_SHORT, 0);
  }
  gl.bindVertexArray(null);
}

function renderGl() {
  view3d.frame = 0;
  const { gl, prog, loc } = glModel;
  const canvas = gl.canvas;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  const data = view3d.data;
  if (!data) return;

  // 相机：绕手部中心旋转。yaw=0 从掌心一侧（+x）看，屏幕上方为手指方向（+z）
  const c = glModel.center;
  const cp = Math.cos(view3d.pitch);
  const dir = [cp * Math.cos(view3d.yaw), cp * Math.sin(view3d.yaw), Math.sin(view3d.pitch)];
  const eye = add3(c, mul3(dir, glModel.distance));
  const viewProj = m4mul(m4perspective(GL_FOV, w / h, 0.01, 5), m4lookAt(eye, c, [0, 0, 1]));

  gl.useProgram(prog);
  gl.uniformMatrix4fv(loc.uViewProj, false, viewProj);
  gl.uniform3fv(loc.uEye, eye);
  gl.uniform3fv(loc.uLight, norm3(add3(dir, [0, 0, 0.6])));
  gl.enable(gl.DEPTH_TEST);
  gl.disable(gl.CULL_FACE);
  gl.disable(gl.BLEND);
  gl.depthMask(true);

  const forces = data.forces || {};
  glDrawPose(data.measured, (link) => {
    const finger = glModel.tipLinks[link];
    if (finger && forces[finger] != null) {
      const c3 = ramp(GL_TIP_STOPS, forces[finger] / displayMax);
      return [c3[0] / 255, c3[1] / 255, c3[2] / 255, 1];
    }
    const base = link.endsWith("hand_base_link") ? GL_PALM : GL_SHELL;
    return [...base, 1];
  });

  // 目标姿态：和实测明显不同时，叠加半透明蓝色
  if (data.target.some((t, i) => Math.abs(t - data.measured[i]) > 8)) {
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    glDrawPose(data.target, () => [0.2, 0.45, 1.0, 0.28]);
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }
}

// ---------------------------------------------------------------- 触觉点阵
function buildTactile() {
  const box = $("tactile");
  for (const name of FINGERS) {
    const cell = el("div", "tactile-cell");
    const canvas = document.createElement("canvas");
    canvas.width = 6;
    canvas.height = 12;
    canvas.id = `tc-${name}`;
    const max = el("div", "tactile-max", "--");
    max.id = `tm-${name}`;
    cell.append(el("div", "tactile-name", fingerLabel[name]), canvas, max);
    box.appendChild(cell);
  }
  const empty = el("div", "tactile-empty", "没有点阵数据（当前力数据来源不是 fn_max）");
  empty.id = "tactile-empty";
  empty.hidden = true;
  box.appendChild(empty);
}

function drawTactile(matrices) {
  const empty = $("tactile-empty");
  const cells = document.querySelectorAll(".tactile-cell");
  if (!matrices) {
    empty.hidden = false;
    cells.forEach((c) => (c.hidden = true));
    return;
  }
  empty.hidden = true;
  cells.forEach((c) => (c.hidden = false));
  for (const name of FINGERS) {
    const rows = matrices[name];
    const canvas = $(`tc-${name}`);
    if (!Array.isArray(rows) || rows.length === 0) continue;
    const h = rows.length;
    const w = rows[0].length;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      canvas.style.aspectRatio = `${w} / ${h}`;
    }
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(w, h);
    let max = 0;
    rows.forEach((row, r) => {
      row.forEach((v, c) => {
        if (v > max) max = v;
        const color = ramp(HEAT_STOPS, v / displayMax);
        const k = (r * w + c) * 4;
        img.data[k] = color[0];
        img.data[k + 1] = color[1];
        img.data[k + 2] = color[2];
        img.data[k + 3] = 255;
      });
    });
    ctx.putImageData(img, 0, 0);
    $(`tm-${name}`).textContent = `最大 ${max}`;
  }
}

// ---------------------------------------------------------------- 指尖力
function buildForceBars() {
  const box = $("force-bars");
  for (const name of FINGERS) {
    const row = el("div", "force-row");
    const track = el("div", "force-track");
    const fill = el("div", "force-fill");
    fill.id = `fb-${name}`;
    fill.style.background = FINGER_COLOR[name];
    track.appendChild(fill);
    const num = el("div", "force-num", "--");
    num.id = `fn-${name}`;
    row.append(el("div", null, fingerLabel[name]), track, num);
    box.appendChild(row);
  }
}

function updateForces(forces) {
  const now = performance.now() / 1000;
  history.push({ t: now, v: { ...forces } });
  while (history.length && now - history[0].t > HISTORY_S) history.shift();
  for (const name of FINGERS) {
    const v = forces[name];
    $(`fb-${name}`).style.width = v == null ? "0%" : `${Math.min(100, (v / displayMax) * 100)}%`;
    $(`fn-${name}`).textContent = v == null ? "--" : Math.round(v);
  }
}

function drawChart() {
  const canvas = $("force-chart");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const left = 36;
  const right = 8;
  const top = 8;
  const bottom = 20;
  const plotW = w - left - right;
  const plotH = h - top - bottom;
  let ymax = displayMax;
  for (const p of history) for (const name of FINGERS) if (p.v[name] > ymax) ymax = p.v[name];

  ctx.strokeStyle = "#e5e7eb";
  ctx.fillStyle = "#6b7280";
  ctx.font = "11px Consolas, monospace";
  ctx.lineWidth = 1;
  for (const frac of [0, 0.5, 1]) {
    const y = top + plotH * (1 - frac);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotW, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(String(Math.round(ymax * frac)), left - 4, y + 4);
  }
  ctx.textAlign = "center";
  for (const sec of [10, 5, 0]) {
    ctx.fillText(sec === 0 ? "现在" : `-${sec}s`, left + plotW * (1 - sec / HISTORY_S), h - 5);
  }

  const now = performance.now() / 1000;
  ctx.lineWidth = 2;
  for (const name of FINGERS) {
    ctx.strokeStyle = FINGER_COLOR[name];
    ctx.beginPath();
    let started = false;
    for (const p of history) {
      const v = p.v[name];
      if (v == null) {
        started = false;
        continue;
      }
      const x = left + plotW * (1 - (now - p.t) / HISTORY_S);
      const y = top + plotH * (1 - v / ymax);
      if (started) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
      started = true;
    }
    ctx.stroke();
  }
}

// ---------------------------------------------------------------- 温度 / 故障 / 日志 / 状态
function buildTemps() {
  const box = $("temps");
  catalog.joints.forEach((joint, i) => {
    const cell = el("div", "temp");
    cell.id = `temp-${i}`;
    cell.title = joint.label;
    const value = el("div", "temp-val", "--");
    cell.append(el("div", "temp-name", joint.label), value);
    box.appendChild(cell);
  });
}

function updateTemps(temps) {
  if (!Array.isArray(temps)) return;
  temps.forEach((t, i) => {
    const cell = $(`temp-${i}`);
    if (!cell) return;
    cell.querySelector(".temp-val").textContent = t;
    cell.classList.toggle("hot", t >= 60);
  });
}

function updateFaults(faults) {
  const box = $("faults");
  const list = Array.isArray(faults) ? faults : [];
  box.classList.toggle("has", list.length > 0);
  box.textContent = list.length ? list.join("；") : "无";
}

function updateLog(entries) {
  if (!Array.isArray(entries)) return;
  const key = entries.length ? `${entries.length}|${entries[0].t}|${entries[0].text}` : "0";
  if (key === lastLogKey) return;
  lastLogKey = key;
  const box = $("log");
  box.replaceChildren(
    ...entries.map((e) => {
      const line = el("div", e.level);
      line.append(el("time", null, e.t), document.createTextNode(e.text));
      return line;
    }),
  );
}

function updateStatus(s, reachable) {
  const pill = $("status");
  if (!reachable) {
    pill.className = "pill pill-err";
    pill.textContent = "网页服务未响应";
    pill.title = "请确认 06_web_dashboard.py 仍在运行";
    return;
  }
  if (s.connected) {
    pill.className = "pill pill-ok";
    pill.textContent = `已连接 · ${s.loop_hz ?? "--"} Hz`;
    pill.title = "";
  } else {
    pill.className = s.error ? "pill pill-err" : "pill pill-wait";
    pill.textContent = s.error ? "通信异常" : "连接中…";
    pill.title = s.error || "";
  }
  const d = s.device || {};
  const side = catalog.side === "right" ? "右手" : "左手";
  $("device").textContent =
    `${d.model || "L20Lite"}（L10）· ${side} · SN ${d.serial_number || "--"} · 固件 ${d.firmware_version || "--"} · 力数据 ${s.force_source || "--"}`;
}

function updateSpeed(s) {
  if (speedTouched || !Array.isArray(s.speed)) return;
  $("speed").value = s.speed[0];
  $("speed-val").textContent = s.speed[0];
}

// ---------------------------------------------------------------- 选项卡
function setTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
  });
  // “镜像跟随”和“游戏”共用摄像头页（同一个画面），只切换右侧显示的内容
  const panel = name === "control" ? "control" : name === "handshake" ? "handshake" : "camera";
  document.querySelectorAll("main[data-panel]").forEach((p) => {
    p.hidden = p.dataset.panel !== panel;
  });
  if (panel === "camera") document.querySelector('main[data-panel="camera"]').dataset.modeActive = name;
  try {
    localStorage.setItem("l10-tab", name);
  } catch {
    /* 隐私模式等情况下不记住选项卡，不影响使用 */
  }
  if (name === "control") {
    requestRender3d(); // 隐藏时画布尺寸为 0，切回来立即按新尺寸重画
    drawChart();
  }
}

function buildTabs() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
  let saved = null;
  try {
    saved = localStorage.getItem("l10-tab");
  } catch {
    saved = null;
  }
  if (saved === "camera") saved = "follow"; // 旧版只有一个“摄像头镜像”选项卡
  if (["follow", "game", "handshake"].includes(saved)) setTab(saved);
}

// ---------------------------------------------------------------- 摄像头镜像 / 猜拳
// 画面由服务端以 MJPEG 推送（/api/camera.mjpg，已画好骨架、镜像显示）；状态随 /api/state 一起轮询。
const cam = { on: false, streaming: false, following: false, gameActive: false };

function buildCamera() {
  $("cam-index").value = catalog.camera_index ?? 0;
  const box = $("calib-btns");
  (catalog.calibrations || []).forEach((c) => {
    const b = el("button", "btn btn-small", c.label);
    b.dataset.kind = c.kind;
    b.disabled = true;
    b.addEventListener("click", () => command("calibrate", { pose: c.kind }));
    box.append(b);
  });
  const reset = el("button", "btn btn-small", "清除标定");
  reset.id = "btn-calib-reset";
  reset.addEventListener("click", () => {
    if (confirm("清除标定，恢复默认映射？")) command("calibration_reset");
  });
  box.append(reset);

  $("btn-cam").addEventListener("click", () => {
    if (cam.on) command("camera", { on: false });
    else command("camera", { on: true, index: Number($("cam-index").value) || 0 });
  });
  $("btn-follow").addEventListener("click", () => command("follow", { on: !cam.following }));
  const band = $("deadband");
  band.max = catalog.deadband_max ?? 20;
  band.value = catalog.deadband ?? 6;
  $("deadband-val").textContent = band.value;
  band.addEventListener("input", () => {
    $("deadband-val").textContent = band.value;
  });
  band.addEventListener("change", () => command("deadband", { value: Number(band.value) }));
  $("btn-game").addEventListener("click", () => command("game", { action: cam.gameActive ? "exit" : "enter" }));
  $("btn-round").addEventListener("click", () => command("game", { action: "start" }));
  $("cam-img").addEventListener("error", () => {
    cam.streaming = false; // 流断开：下次轮询时如果摄像头仍开着就重连
    $("cam-img").removeAttribute("src");
  });
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || !cam.gameActive) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    e.preventDefault(); // 避免空格同时“点击”当前聚焦的按钮
    command("game", { action: "start" });
  });
  if (!catalog.mirror_model) {
    $("cam-placeholder").textContent = "缺少手部模型：先运行 python 07_camera_mirror.py --download-model";
  }
}

function camLine(parts) {
  const line = el("div");
  parts.forEach(([text, cls]) => line.append(cls ? el("span", cls, text) : document.createTextNode(text)));
  return line;
}

function updateCamera(s) {
  const m = s.mirror;
  if (!m) return;
  const c = m.camera || {};
  const game = m.game;
  cam.on = !!c.on;
  cam.following = !!m.following;
  cam.gameActive = !!game;

  // 画面
  const img = $("cam-img");
  if (cam.on && !cam.streaming) {
    img.src = `/api/camera.mjpg?t=${Date.now()}`;
    cam.streaming = true;
  } else if (!cam.on && cam.streaming) {
    img.removeAttribute("src");
    cam.streaming = false;
  }
  const holder = $("cam-placeholder");
  holder.hidden = cam.on;
  if (!cam.on) {
    if (c.starting) holder.textContent = "正在开启摄像头…";
    else if (c.error) holder.textContent = `摄像头：${c.error}`;
    else if (catalog.mirror_model) holder.textContent = "摄像头未开启";
  }

  // 状态
  const status = $("cam-status");
  if (!cam.on) {
    status.replaceChildren(camLine([[c.error ? "摄像头没有开启（见日志）" : "摄像头未开启", c.error ? "err" : null]]));
  } else {
    const seen = c.seen
      ? [[`看到${c.label}`, "ok"], [` ${c.score}`, null]]
      : [["没看到手", "warn"]];
    const gesture = c.gesture_label ? [[` · 手势 ${c.gesture_label}`, null]] : [];
    if (c.pinch >= 0.05) {
      gesture.push([` · 捏取（${c.pinch_label}）${Math.round(c.pinch * 100)}%`, c.pinch >= 0.5 ? "ok" : null]);
    }
    let mode;
    if (game) mode = [["游戏中", "ok"], ["：L10 不跟随你的手", null]];
    else if (cam.following) mode = c.seen ? [["● 跟随中", "ok"]] : [["● 跟随中", "ok"], ["（没看到手，L10 保持当前姿态）", null]];
    else mode = [["未跟随", null]];
    status.replaceChildren(camLine([...seen, ...gesture, [` · ${c.fps} fps`, null]]), camLine(mode));
  }
  const floors = (m.floors || []).length ? ` · 卸力中：${m.floors.join("、")}` : "";
  const who = m.pinch_label || "手指";
  const pinch = m.pinch_mode ? `捏取辅助：拇指和${who}对准捏取姿态，可以同时动` : "";
  $("cam-phase").textContent = [m.phase_text, pinch].filter(Boolean).join(" · ") + floors;

  // 按钮
  const ready = cam.on && s.connected;
  $("btn-cam").textContent = cam.on ? "关闭摄像头" : c.starting ? "开启中…" : "开启摄像头";
  $("btn-cam").disabled = !!c.starting;
  $("cam-index").disabled = cam.on || !!c.starting;
  $("btn-follow").disabled = !ready;
  $("btn-follow").textContent = cam.following ? "暂停跟随" : "开始跟随";
  $("btn-follow").classList.toggle("on", cam.following);
  $("btn-game").disabled = !ready;
  $("btn-game").textContent = game ? "退出游戏" : "进入游戏";
  $("btn-round").disabled = !game || !["idle", "result"].includes(game.state);

  // 防抖：拖动时不覆盖；其余时候显示服务端当前值
  const band = $("deadband");
  if (typeof c.deadband === "number" && document.activeElement !== band) {
    band.value = c.deadband;
    $("deadband-val").textContent = c.deadband;
  }

  // 标定
  const done = new Set(c.calibrated || []);
  document.querySelectorAll("#calib-btns [data-kind]").forEach((b) => {
    b.disabled = !cam.on || !!c.calibrating;
    b.classList.toggle("done", done.has(b.dataset.kind));
  });
  const labels = Object.fromEntries((catalog.calibrations || []).map((x) => [x.kind, x.label]));
  $("calib-status").textContent = c.calibrating
    ? `标定中：${labels[c.calibrating]}，请保持…`
    : done.size
      ? `已标定：${[...done].map((k) => labels[k]).join("、")}${done.size < 3 ? "（其余用默认值）" : ""}`
      : "未标定（使用默认映射）";
  $("cam-calib").hidden = !c.calibrating;
  $("cam-calib-bar").style.width = `${Math.round((c.calib_progress || 0) * 100)}%`;

  // 选项卡上的状态提示：在控制页也能看到摄像头那边正在做什么
  const badges = { "tab-follow-badge": cam.following ? "跟随中" : "", "tab-game-badge": game ? "进行中" : "" };
  for (const [id, text] of Object.entries(badges)) {
    $(id).hidden = !text;
    $(id).textContent = text;
  }

  // 游戏字幕和比分
  $("cam-big").textContent = game ? game.big : "";
  $("cam-small").textContent = game ? game.small : "";
  const scoreText = game ? `你 ${game.score.human} : ${game.score.robot} L10　平局 ${game.score.draw}` : "";
  $("cam-score").hidden = !game;
  $("cam-score").textContent = scoreText;
  if (game) $("game-score").textContent = scoreText; // 退出后保留最后的比分，下次进入游戏重新计分
}

// ---------------------------------------------------------------- 握手（v2）
const HS_STATES_CAN_GRIP = ["waiting", "rearm", "armed"];
const HS_STATES_GRIPPING = ["armed", "closing", "holding"];
let hsActive = false;
let hsState = null;

function buildHandshake() {
  const box = $("hs-fingers");
  (catalog.fingers || []).forEach((f) => {
    const row = el("div", "hs-row");
    row.dataset.finger = f.name;
    const closure = el("div", "hs-bar hs-closure");
    closure.append(el("div", "hs-fill"), el("span", "hs-num"));
    const force = el("div", "hs-bar hs-force");
    force.append(el("div", "hs-fill"), el("div", "hs-mark"), el("span", "hs-num"));
    row.append(el("span", null, f.label), closure, force, el("span", "hs-chip", "—"));
    box.append(row);
  });
  const grip = $("hs-grip");
  const defaults = catalog.handshake || {};
  grip.max = Math.max(10, (defaults.force_limit ?? 60) - 5);
  grip.value = defaults.grip_force ?? 25;
  $("hs-grip-val").textContent = grip.value;
  grip.addEventListener("input", () => {
    $("hs-grip-val").textContent = grip.value;
  });
  grip.addEventListener("change", () => command("handshake", { action: "config", grip_force: Number(grip.value) }));
  $("hs-auto-start").addEventListener("change", (e) =>
    command("handshake", { action: "config", auto_start: e.target.checked }),
  );
  $("hs-auto-release").addEventListener("change", (e) =>
    command("handshake", { action: "config", auto_release: e.target.checked }),
  );
  $("hs-use-thumb").addEventListener("change", (e) =>
    command("handshake", { action: "config", use_thumb: e.target.checked }),
  );
  $("btn-hs").addEventListener("click", () => command("handshake", { action: hsActive ? "exit" : "enter" }));
  $("btn-hs-grip").addEventListener("click", () => command("handshake", { action: "grip" }));
  $("btn-hs-release").addEventListener("click", () => command("handshake", { action: "release" }));
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || !hsActive || !HS_STATES_CAN_GRIP.includes(hsState)) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    e.preventDefault();
    command("handshake", { action: "grip" });
  });
}

function updateHandshake(s) {
  const hs = s.handshake;
  hsActive = !!hs;
  hsState = hs ? hs.state : null;
  const badge = $("tab-hs-badge");
  const badgeText = !hs ? "" : hs.state === "holding" ? "握住了" : "进行中";
  badge.hidden = !badgeText;
  badge.textContent = badgeText;

  $("btn-hs").textContent = hs ? "退出握手" : "开始握手";
  $("btn-hs").disabled = !s.connected;
  $("btn-hs-grip").disabled = !hs || !HS_STATES_CAN_GRIP.includes(hs.state);
  $("btn-hs-release").disabled = !hs || !HS_STATES_GRIPPING.includes(hs.state);
  const state = $("hs-state");
  state.className = `hs-state ${hs ? hs.state : ""}`;
  state.textContent = hs ? hs.text : "还没开始：点“开始握手”";
  $("hs-sub").textContent = hs
    ? `已握手 ${hs.count} 次${hs.hold_force != null ? ` · 握稳时的接触力 ${hs.hold_force}` : ""}`
    : "";

  const grip = hs ? hs.grip_force : Number($("hs-grip").value);
  if (hs && document.activeElement !== $("hs-grip")) {
    $("hs-grip").value = hs.grip_force;
    $("hs-grip-val").textContent = hs.grip_force;
  }
  if (hs) {
    $("hs-auto-start").checked = hs.auto_start;
    $("hs-auto-release").checked = hs.auto_release;
    $("hs-use-thumb").checked = hs.use_thumb;
  }
  const limit = hs ? hs.force_limit : (catalog.handshake || {}).force_limit ?? 60;
  const scale = Math.max(limit * 1.2, 10);
  const fingers = hs ? Object.fromEntries(hs.fingers.map((f) => [f.name, f])) : {};
  document.querySelectorAll("#hs-fingers .hs-row").forEach((row) => {
    const f = fingers[row.dataset.finger];
    const closure = f ? f.closure : 0;
    const force = f ? f.force : null;
    const [cFill, cNum] = row.querySelector(".hs-closure").children;
    cFill.style.width = `${Math.round(closure * 100)}%`;
    cNum.textContent = f ? `${Math.round(closure * 100)}%` : "";
    const forceBar = row.querySelector(".hs-force");
    const [fFill, mark, fNum] = forceBar.children;
    const value = force ?? 0;
    fFill.style.width = `${Math.min(100, (value / scale) * 100)}%`;
    const ratio = row.dataset.finger === "thumb" && hs ? hs.thumb_ratio : 1; // 拇指的握力和上限都小一些
    fFill.classList.toggle("over", value > limit * ratio);
    mark.style.left = `${Math.min(100, (((f && f.grip) || grip * ratio) / scale) * 100)}%`;
    fNum.textContent = force == null ? "" : force.toFixed(0);
    const chip = row.querySelector(".hs-chip");
    const why = f ? f.stopped : "";
    chip.textContent = why || "—";
    chip.className = `hs-chip ${{ 碰到: "contact", 挡住: "blocked", 到头: "limit" }[why] || ""}`;
  });
}

// ---------------------------------------------------------------- 主循环
function render(s) {
  updateJoints(s);
  updateHand(s);
  if (s.forces) updateForces(s.forces);
  drawChart();
  if (s.connected) drawTactile(s.matrices);
  $("force-stale").hidden = !(s.connected && s.force_stale);
  updateTemps(s.temps);
  updateFaults(s.faults);
  updateLog(s.log);
  updateAnimation(s);
  updateSpeed(s);
  updateCamera(s);
  updateHandshake(s);
}

async function poll() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const s = await res.json();
    updateStatus(s, true);
    render(s);
  } catch {
    updateStatus(null, false);
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

function bindControls() {
  $("btn-stop").addEventListener("click", () => command("stop"));
  $("btn-anim-stop").addEventListener("click", () => command("stop"));
  $("btn-reset").addEventListener("click", () => command("preset", { key: "reset" }));
  $("btn-tare").addEventListener("click", () => {
    toast("去皮中：请不要触碰指尖");
    command("tare");
  });
  $("speed").addEventListener("input", () => {
    speedTouched = true;
    $("speed-val").textContent = $("speed").value;
  });
  $("btn-speed").addEventListener("click", () => command("speed", { value: Number($("speed").value) }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") command("stop");
  });
  window.addEventListener("resize", drawChart);
}

async function init() {
  try {
    const res = await fetch("/api/catalog", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    catalog = await res.json();
  } catch {
    updateStatus(null, false);
    setTimeout(init, 1500);
    return;
  }
  displayMax = catalog.display_max || 100;
  if (catalog.version) {
    document.querySelector(".brand").textContent = `L10 灵巧手控制台 v${catalog.version}`;
    document.title = `L10 控制台 v${catalog.version}`;
  }
  fingerLabel = Object.fromEntries(catalog.fingers.map((f) => [f.name, f.label]));
  $("cbar-max").textContent = displayMax;
  buildJoints();
  buildPresets();
  buildAnimations();
  buildHand();
  buildTactile();
  buildForceBars();
  buildTemps();
  buildCamera();
  buildHandshake();
  buildTabs();
  bindControls();
  poll();
}

init();
