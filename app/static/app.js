import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const $ = (id) => document.getElementById(id);
const state = {
  jobId: null,
  mode: "relief",
  customPalette: null, // [{src, hex, name}] once the user edited / snapped the palette:
                       // pixels map to `src`, the part is printed in filament colour `hex`
  names: null,         // custom names for the auto palette
  result: null,
  busy: false,
  pending: false,
};

// ---------- status ----------
function setStatus(msg, isErr = false) {
  const s = $("status");
  s.textContent = msg;
  s.classList.toggle("err", isErr);
}

// ---------- upload ----------
const drop = $("drop");
$("file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
window.addEventListener("paste", (e) => {
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
  if (item) upload(item.getAsFile());
});

async function upload(file) {
  if (!file.type.startsWith("image/")) return setStatus("That is not an image", true);
  setStatus(`Uploading ${file.name}…`);
  const fd = new FormData();
  fd.append("file", file, file.name || "pasted.png");
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  if (!res.ok) return setStatus((await res.json()).detail || "Upload failed", true);
  state.jobId = (await res.json()).job_id;
  state.customPalette = null;
  state.names = null;
  const url = URL.createObjectURL(file);
  $("thumb").src = url;
  $("srcImg").src = url;
  $("thumb").hidden = false;
  $("dropText").innerHTML = `<small>${file.name || "pasted image"} · click to replace</small>`;
  $("generate").disabled = false;
  $("snapBtn").disabled = false;
  generate();
}

// ---------- controls ----------
const ranges = ["n_colors", "smoothing", "min_region_mm2", "bg_tolerance"];
ranges.forEach((id) => {
  const el = $(id);
  const out = $(id + "_out");
  el.addEventListener("input", () => { out.textContent = el.value; if (id === "n_colors") amsHint(); });
});
function amsHint() {
  const n = +$("n_colors").value + ($("baseColor").value === "custom" && state.mode === "relief" ? 1 : 0);
  const h = $("amsHint");
  h.textContent = n <= 4 ? "Fits one 4-slot AMS" : n <= 8 ? `${n} filaments: needs 2 AMS units` :
    `${n} filaments: needs ${Math.ceil(n / 4)} AMS units or manual swaps`;
  h.classList.toggle("warn", n > 4);
}

$("n_colors").addEventListener("change", () => { state.customPalette = null; state.names = null; autoGen(); });
["smoothing", "min_region_mm2", "bg_tolerance", "width_mm", "pixel_mm", "base_thickness",
 "relief_height", "step_height", "border_mm", "edge_smoothing"].forEach((id) => $(id).addEventListener("change", autoGen));
$("remove_bg").addEventListener("change", () => { $("bgTolField").hidden = !$("remove_bg").checked; autoGen(); });
$("stepped").addEventListener("change", () => { $("stepField").hidden = !$("stepped").checked; autoGen(); });
$("baseColor").addEventListener("change", () => { $("baseCustom").hidden = $("baseColor").value !== "custom"; amsHint(); autoGen(); });
$("baseCustom").addEventListener("change", autoGen);
$("generate").addEventListener("click", generate);

const MODE_HINTS = {
  relief: "A backing plate in one colour with the other colours raised on top. Colour changes only happen in the raised layers, so it prints fast with little purge waste.",
  flat: "Every colour is a full-thickness tile of the same height: a flat, two-sided mosaic (coasters, signs). Colour changes on every layer.",
};
document.querySelectorAll("#modeSeg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#modeSeg button").forEach((x) => x.classList.toggle("on", x === b));
  state.mode = b.dataset.mode;
  applyMode();
  autoGen();
}));
function applyMode() {
  document.querySelectorAll(".relief-only").forEach((el) => (el.style.display = state.mode === "relief" ? "" : "none"));
  $("baseLabel").textContent = state.mode === "relief" ? "Base (mm)" : "Thickness (mm)";
  $("modeHint").textContent = MODE_HINTS[state.mode];
  amsHint();
}
applyMode();

function autoGen() { if ($("auto").checked && state.jobId) generate(); }

// ---------- palette ----------
$("resetPalette").addEventListener("click", () => { state.customPalette = null; state.names = null; generate(); });
$("snapBtn").addEventListener("click", () => generate({ snap: true }));

function renderPalette(palette) {
  const box = $("palette");
  box.innerHTML = "";
  const tpl = $("swatchTpl");
  palette.forEach((p, i) => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    const color = node.querySelector(".sw-color");
    const name = node.querySelector(".sw-name");
    color.value = p.hex.toLowerCase();
    name.value = p.name;
    node.querySelector(".sw-pct").textContent = `${p.area_pct}%`;
    color.addEventListener("change", () => editPalette((list) => { list[i].hex = color.value.toUpperCase(); }));
    name.addEventListener("change", () => {
      if (state.customPalette) state.customPalette[i].name = name.value;
      else state.names = currentPalette().map((x, j) => (j === i ? name.value : x.name));
      autoGen();
    });
    node.querySelector(".sw-del").addEventListener("click", () => {
      if (palette.length <= 1) return;
      editPalette((list) => list.splice(i, 1));
    });
    box.appendChild(node);
  });
  $("resetPalette").hidden = !state.customPalette;

  // base colour choices
  const sel = $("baseColor");
  const prev = sel.value;
  sel.innerHTML = `<option value="-1">Most used colour</option>` +
    palette.map((p, i) => `<option value="${i}">${p.name} (${p.hex})</option>`).join("") +
    `<option value="custom">Extra filament…</option>`;
  sel.value = [...sel.options].some((o) => o.value === prev) ? prev : "-1";
}

function currentPalette() {
  return state.result ? state.result.palette.map((p) => ({ src: p.source_hex, hex: p.hex, name: p.name })) : [];
}
function editPalette(fn) {
  const list = state.customPalette ? state.customPalette.map((x) => ({ ...x })) : currentPalette();
  fn(list);
  state.customPalette = list;
  generate();
}

// ---------- generation ----------
async function generate(opts = {}) {
  if (!state.jobId) return;
  if (state.busy) { state.pending = true; return; }
  state.busy = true;
  $("busy").hidden = false;
  $("generate").disabled = true;
  setStatus("Generating…");
  const fd = new FormData();
  const val = (id) => $(id).value;
  fd.append("n_colors", val("n_colors"));
  fd.append("width_mm", val("width_mm"));
  fd.append("pixel_mm", val("pixel_mm"));
  fd.append("mode", state.mode);
  fd.append("base_thickness", val("base_thickness"));
  fd.append("relief_height", val("relief_height"));
  fd.append("stepped", $("stepped").checked);
  fd.append("step_height", val("step_height"));
  const bc = val("baseColor");
  fd.append("base_color_index", bc === "custom" ? -1 : bc);
  fd.append("base_color_custom", bc === "custom" ? val("baseCustom") : "");
  fd.append("border_mm", val("border_mm"));
  fd.append("edge_smoothing", val("edge_smoothing"));
  fd.append("remove_bg", $("remove_bg").checked);
  fd.append("bg_tolerance", val("bg_tolerance"));
  fd.append("smoothing", val("smoothing"));
  fd.append("min_region_mm2", val("min_region_mm2"));
  if (state.customPalette) {
    fd.append("palette", JSON.stringify(state.customPalette.map((p) => p.src)));
    fd.append("colors", JSON.stringify(state.customPalette.map((p) => p.hex)));
    fd.append("names", JSON.stringify(state.customPalette.map((p) => p.name)));
  } else if (state.names) {
    fd.append("names", JSON.stringify(state.names));
  }
  if (opts.snap) fd.append("snap_filaments", "true");

  try {
    const res = await fetch(`/api/generate/${state.jobId}`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Generation failed");
    if (opts.snap) state.customPalette = data.palette.map((p) => ({ src: p.source_hex, hex: p.hex, name: p.name }));
    state.result = data;
    showResult(data);
    setStatus(`Done in ${data.seconds}s`);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    state.busy = false;
    $("busy").hidden = true;
    $("generate").disabled = false;
    if (state.pending) { state.pending = false; generate(); }
  }
}

function fileUrl(path) {
  return `/api/jobs/${state.jobId}/files/${path}?t=${Date.now()}`;
}

function showResult(r) {
  renderPalette(r.palette);
  $("mapImg").src = fileUrl(r.files.preview);
  $("results").hidden = false;
  const [w, h, z] = r.size_mm;
  $("stats").innerHTML = [
    `Size <b>${w} × ${h} × ${z} mm</b>`,
    `Filaments <b>${r.filaments.length}</b>`,
    `Parts <b>${r.parts.length}</b>`,
    `Grid <b>${r.grid_px[0]} × ${r.grid_px[1]} px</b>`,
  ].map((s) => `<span>${s}</span>`).join("");
  $("warnings").innerHTML = r.warnings.map((w) => `<div>⚠ ${w}</div>`).join("");
  $("dl3mf").href = `/api/jobs/${state.jobId}/files/${r.files["3mf"]}`;
  $("dlStl").href = `/api/jobs/${state.jobId}/files/${r.files.stl}`;
  $("dlZip").href = `/api/jobs/${state.jobId}/files/${r.files.zip}`;
  $("partsBody").innerHTML = r.parts.map((p) => `
    <tr><td><span class="dot" style="background:${p.color}"></span></td><td>${escapeHtml(p.name)}</td>
    <td>Slot ${p.filament} · ${p.color}</td><td>${p.z0} → ${p.z1} mm</td><td>${p.triangles.toLocaleString()}</td>
    <td><a href="/api/jobs/${state.jobId}/files/${p.stl}" download>STL</a></td></tr>`).join("");
  loadModel(r.parts);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
  const is3d = t.dataset.view === "3d";
  $("view3d").hidden = !is3d;
  $("view2d").hidden = is3d;
  $("viewTools").style.visibility = is3d ? "visible" : "hidden";
  if (is3d) resize();
}));

// ---------- 3D viewer ----------
const canvas = $("canvas");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14171d);
const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 5000);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x50545c, 2.2));
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
sun.position.set(-120, -160, 260);
scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.5);
fill.position.set(160, 120, 80);
scene.add(fill);

// P2S build plate 256 x 256
const plate = new THREE.Group();
const plateMesh = new THREE.Mesh(new THREE.PlaneGeometry(256, 256),
  new THREE.MeshStandardMaterial({ color: 0x23272f, roughness: 0.9 }));
plateMesh.position.z = -0.05;
plate.add(plateMesh);
const grid = new THREE.GridHelper(256, 16, 0x3a404c, 0x2c313b);
grid.rotation.x = Math.PI / 2;
plate.add(grid);
scene.add(plate);

const modelGroup = new THREE.Group();
scene.add(modelGroup);
const loader = new STLLoader();
let loadToken = 0;

async function loadModel(parts) {
  const token = ++loadToken;
  const geoms = await Promise.all(parts.map((p) => loader.loadAsync(fileUrl(p.stl))));
  if (token !== loadToken) return;
  modelGroup.children.forEach((m) => { m.geometry.dispose(); m.material.dispose(); });
  modelGroup.clear();
  geoms.forEach((g, i) => {
    g.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ color: parts[i].color, roughness: 0.55, metalness: 0.0, flatShading: true });
    const mesh = new THREE.Mesh(g, mat);
    mesh.userData.index = i;
    modelGroup.add(mesh);
  });
  $("empty3d").hidden = true;
  applyExplode();
  if (!state.framed) { frame(); state.framed = true; }
}

function applyExplode() {
  const e = +$("explode").value;
  modelGroup.children.forEach((m) => (m.position.z = m.userData.index * e));
}
$("explode").addEventListener("input", applyExplode);

function frame() {
  const box = new THREE.Box3().setFromObject(modelGroup);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const r = Math.max(size.x, size.y, 40);
  camera.position.set(center.x + r * 0.15, center.y - r * 1.1, center.z + r * 1.0);
  controls.target.copy(center);
  controls.update();
}
$("resetCam").addEventListener("click", frame);

function resize() {
  const el = $("view3d");
  const w = el.clientWidth, h = el.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe($("view3d"));
renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
