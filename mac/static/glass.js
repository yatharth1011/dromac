// Liquid glass theme: Rainy Desktop's rain-on-glass shader rendered behind
// the dashboard, with the topbar and every card drawn as liquid-glass lenses
// over that rain scene (a second render pass).
//
// LICENSE: unlike the rest of Dromac (MIT), this file contains shader code
// adapted from "Heartfelt" by Martijn Steinrucken (BigWings),
// https://www.shadertoy.com/view/ltffzl, and is licensed under Creative
// Commons Attribution-NonCommercial-ShareAlike 3.0 Unported (CC BY-NC-SA
// 3.0), https://creativecommons.org/licenses/by-nc-sa/3.0/
//
// The rain shader is a vendored snapshot of Rainy Desktop's WebGL port
// (github.com/yatharth1011/rainy-desktop, ChromeExtension/RainyTab/rain.js),
// itself a line-for-line GLSL port of Rainy's RenderShaders.metal. It's
// copied rather than loaded from Rainy so Dromac stays self-contained -- if
// the shader changes there, re-copy it here.
//
// Wallpaper, settings and the rain clock come from Rainy's loopback bridge,
// proxied by Dromac's server (/api/rainy/*) -- or, when Rainy isn't running,
// from Rainy's saved preferences and the macOS desktop picture directly.
(() => {
  const MAX_GLASS = 16;
  const STORE_KEY = "dromac.glassTheme";

  // Rainy's own RainSettings defaults (App/RainSettings.swift).
  const RAINY_DEFAULTS = {
    rainIntensity: 0.7, rainSpeed: 1, staticDropDensity: 1, layer1Density: 1, layer2Density: 1,
    fogMinBlur: 2, fogMaxBlurLow: 3, fogMaxBlurHigh: 6, refractionStrength: 1, dropZoomOut: 1,
    lightningBoost: 2.6, lightningSpeed: 1, lightningSharpness: 10,
    colorGradeStrength: 1, vignetteStrength: 1, brightness: 1, dimAmount: 0,
    zoomAmount: 1, zoomSpeed: 1,
  };

  // Same groups, labels and ranges as Rainy's SettingsView, so the two read
  // as the same controls. "Glass Tint" is Dromac-only (Rainy has no UI glass).
  const SLIDERS = [
    ["Rain", [
      ["rainIntensity", "Intensity", 0, 1],
      ["rainSpeed", "Fall Speed", 0.1, 3],
      ["staticDropDensity", "Static Droplets", 0, 3],
      ["layer1Density", "Drop Layer 1", 0, 3],
      ["layer2Density", "Drop Layer 2", 0, 3],
      ["refractionStrength", "Refraction", 0, 4],
      ["dropZoomOut", "Zoom Out (more drops)", 0.5, 4],
    ]],
    ["Glass / Fog", [
      ["fogMinBlur", "Min Blur (driest)", 0, 10],
      ["fogMaxBlurLow", "Max Blur (dry)", 0, 12],
      ["fogMaxBlurHigh", "Max Blur (wet)", 0, 14],
    ]],
    ["Lightning", [
      ["lightningBoost", "Brightness", 0, 10],
      ["lightningSpeed", "Flicker Speed", 0.1, 4],
      ["lightningSharpness", "Sharpness (rarity)", 1, 20],
    ]],
    ["Look", [
      ["colorGradeStrength", "Color Cycling", 0, 1],
      ["vignetteStrength", "Vignette", 0, 2],
      ["brightness", "Brightness", 0.2, 2],
      ["dimAmount", "Dim Wallpaper", 0, 1],
      ["zoomAmount", "Breathing Zoom Amount", 0, 1],
      ["zoomSpeed", "Breathing Zoom Speed", 0, 3],
    ]],
  ];

  // ------------------------------------------------------------ prefs ----

  let prefs = {
    enabled: false,
    useRainy: true,
    glassTint: 0.55,
    paused: false,
    local: { ...RAINY_DEFAULTS },
  };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (saved) prefs = { ...prefs, ...saved, local: { ...RAINY_DEFAULTS, ...(saved.local || {}) } };
  } catch (e) {}

  function savePrefs() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(prefs)); } catch (e) {}
  }

  // What Rainy reported last: source is "rainy" (live bridge), "prefs"
  // (Rainy not running, its saved settings), or "none" (never installed).
  let rainy = { source: "none", settings: null, paused: false, time: null, wallpaperVersion: null };

  function syncingWithRainy() {
    return prefs.useRainy && !!rainy.settings;
  }

  function effectiveSettings() {
    const base = syncingWithRainy() ? { ...RAINY_DEFAULTS, ...rainy.settings } : prefs.local;
    return { ...base, glassTint: prefs.glassTint };
  }

  function effectivePaused() {
    return syncingWithRainy() ? rainy.paused : prefs.paused;
  }

  // Rain clock. Starts at 10s so the shader's built-in 10s fade-in is
  // already done (same as Rainy Tab). While live-synced with Rainy it snaps
  // to Rainy's own clock, so drops line up with the real rainy desktop
  // visible around the window.
  let clock = { base: 10, at: performance.now(), paused: false };
  function currentTime(now = performance.now()) {
    return clock.paused ? clock.base : clock.base + (now - clock.at) / 1000;
  }
  function setPaused(paused) {
    if (paused === clock.paused) return;
    clock = { base: currentTime(), at: performance.now(), paused };
  }

  // ------------------------------------------------------------- panel ----

  const panel = document.getElementById("themePanel");
  const btnTheme = document.getElementById("btnTheme");
  const sliderEls = {};

  function buildPanel() {
    const fmt = (v) => Number(v).toFixed(2);
    let html = `
      <div class="theme-panel-head">
        <div class="card-label">$ liquid_glass</div>
        <button class="notif-dismiss" data-act="close" title="close">×</button>
      </div>
      <label class="theme-toggle"><span>Liquid glass theme</span><input type="checkbox" data-pref="enabled"></label>
      <label class="theme-toggle"><span>Use Rainy Desktop's settings</span><input type="checkbox" data-pref="useRainy"></label>
      <div class="theme-source" data-role="source"></div>
      <div class="theme-group">
        <div class="theme-group-title">Dromac</div>
        ${sliderRow("glassTint", "Glass Tint", 0, 1)}
      </div>
      <label class="theme-toggle theme-local-only"><span>Pause</span><input type="checkbox" data-pref="paused"></label>`;
    for (const [title, rows] of SLIDERS) {
      html += `<div class="theme-group"><div class="theme-group-title">${title}</div>`;
      for (const [key, label, min, max] of rows) html += sliderRow(key, label, min, max);
      html += `</div>`;
    }
    html += `<button class="btn btn--ghost theme-reset theme-local-only" data-act="reset"><span>reset to defaults</span></button>`;
    panel.innerHTML = html;

    function sliderRow(key, label, min, max) {
      return `<div class="theme-slider" data-key="${key}">
        <div class="theme-slider-top"><span>${label}</span><span class="theme-slider-val"></span></div>
        <input type="range" min="${min}" max="${max}" step="${(max - min) / 200}">
      </div>`;
    }

    panel.querySelectorAll(".theme-slider").forEach((row) => {
      const key = row.dataset.key;
      const input = row.querySelector("input");
      const val = row.querySelector(".theme-slider-val");
      sliderEls[key] = { row, input, val };
      input.addEventListener("input", () => {
        const v = parseFloat(input.value);
        if (key === "glassTint") prefs.glassTint = v;
        else prefs.local[key] = v;
        val.textContent = fmt(v);
        savePrefs();
      });
    });

    panel.querySelectorAll("[data-pref]").forEach((box) => {
      box.addEventListener("change", () => {
        const key = box.dataset.pref;
        if (key === "useRainy" && !box.checked && rainy.settings) {
          // Turning sync off starts local tweaking from where Rainy's
          // values were, rather than jumping back to old local ones.
          prefs.local = { ...RAINY_DEFAULTS, ...rainy.settings };
          prefs.paused = rainy.paused;
        }
        prefs[key] = box.checked;
        savePrefs();
        if (!syncingWithRainy()) setPaused(effectivePaused());
        applyEnabled();
        refreshPanel();
      });
    });

    panel.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "close") panel.classList.add("hidden");
      if (act === "reset") {
        prefs.local = { ...RAINY_DEFAULTS };
        prefs.glassTint = 0.55;
        prefs.paused = false;
        savePrefs();
        refreshPanel();
      }
    });
  }

  function refreshPanel() {
    const s = effectiveSettings();
    const syncing = syncingWithRainy();
    for (const [key, { row, input, val }] of Object.entries(sliderEls)) {
      const locked = syncing && key !== "glassTint";
      input.disabled = locked;
      row.classList.toggle("theme-slider--locked", locked);
      // Don't fight a slider the user is mid-drag on.
      if (document.activeElement !== input) input.value = s[key];
      val.textContent = Number(s[key]).toFixed(2);
    }
    panel.querySelectorAll("[data-pref]").forEach((box) => { box.checked = !!prefs[box.dataset.pref]; });
    panel.querySelector('[data-pref="paused"]').checked = effectivePaused();
    panel.querySelectorAll(".theme-local-only").forEach((el) => el.classList.toggle("theme-slider--locked", syncing));
    panel.querySelector('[data-pref="paused"]').disabled = syncing;

    const source = panel.querySelector('[data-role="source"]');
    source.textContent = !prefs.useRainy ? "Using Dromac's own settings below."
      : rainy.source === "rainy" ? "Live from Rainy Desktop — change them there."
      : rainy.source === "prefs" ? "Rainy isn't running — using its last saved settings."
      : "Rainy Desktop not found — using Dromac's own settings.";
  }

  btnTheme.addEventListener("click", () => {
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) refreshPanel();
  });

  // ------------------------------------------------------------ bridge ----

  async function poll() {
    let state;
    try {
      const res = await fetch("/api/rainy/state", { cache: "no-store" });
      state = await res.json();
    } catch (e) {
      return;
    }
    rainy = state;
    if (syncingWithRainy() && rainy.source === "rainy" && typeof rainy.time === "number") {
      const now = performance.now();
      // Only snap when drift is noticeable, same as Rainy Tab.
      if (rainy.paused !== clock.paused || Math.abs(currentTime(now) - rainy.time) > 0.08) {
        clock = { base: rainy.time, at: now, paused: rainy.paused };
      }
    } else {
      setPaused(effectivePaused());
    }
    if (rainy.wallpaperVersion !== wallpaperVersion) {
      wallpaperVersion = rainy.wallpaperVersion;
      loadWallpaper();
    }
    if (!panel.classList.contains("hidden")) refreshPanel();
  }

  let wallpaperVersion = null;
  let wallpaperUrl = null;

  async function loadWallpaper() {
    try {
      const res = await fetch("/api/rainy/wallpaper", { cache: "no-store" });
      if (!res.ok) throw new Error("no wallpaper");
      const blob = await res.blob();
      if (wallpaperUrl) URL.revokeObjectURL(wallpaperUrl);
      wallpaperUrl = URL.createObjectURL(blob);
      document.body.style.setProperty("--glass-wallpaper", `url("${wallpaperUrl}")`);
      if (gl) await setWallpaper(blob);
    } catch (e) {
      // Keep whatever was showing (or the dark placeholder).
    }
  }

  // -------------------------------------------------------------- WebGL ----

  const canvas = document.getElementById("glassCanvas");
  const gl = canvas.getContext("webgl2", { antialias: false, alpha: false, premultipliedAlpha: false });

  const VERT = `#version 300 es
void main() {
  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

  const FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_sharp;
uniform sampler2D u_blur;
uniform vec2 u_res;      // screen size, device px (the space Rainy renders in)
uniform vec2 u_offset;   // this viewport's bottom-left within the screen, device px
uniform float u_time, u_dpr;
uniform float u_rainIntensity, u_rainSpeed, u_staticDropDensity, u_layer1Density, u_layer2Density;
uniform float u_fogMinBlur, u_fogMaxBlurLow, u_fogMaxBlurHigh, u_refractionStrength;
uniform float u_lightningBoost, u_lightningSpeed, u_lightningSharpness;
uniform float u_colorGradeStrength, u_vignetteStrength, u_brightness;
uniform float u_zoomAmount, u_zoomSpeed, u_dimAmount, u_dropZoomOut;
out vec4 outColor;

#define S(a, b, t) smoothstep(a, b, t)

// ---- "Heartfelt" by Martijn Steinrucken (BigWings), CC BY-NC-SA 3.0 ----
// https://www.shadertoy.com/view/ltffzl -- same port as RenderShaders.metal.
vec3 N13(float p) {
  vec3 p3 = fract(vec3(p) * vec3(.1031, .11369, .13787));
  p3 += dot(p3, p3.yzx + 19.19);
  return fract(vec3((p3.x + p3.y) * p3.z, (p3.x + p3.z) * p3.y, (p3.y + p3.z) * p3.x));
}
float N(float t) { return fract(sin(t * 12345.564) * 7658.76); }
float Saw(float b, float t) { return S(0., b, t) * S(1., b, t); }

vec2 DropLayer2(vec2 uv, float t) {
  vec2 UV = uv;
  uv.y += t * 0.75;
  vec2 a = vec2(6., 1.);
  vec2 grid = a * 2.;
  vec2 id = floor(uv * grid);
  float colShift = N(id.x);
  uv.y += colShift;
  id = floor(uv * grid);
  vec3 n = N13(id.x * 35.2 + id.y * 2376.1);
  vec2 st = fract(uv * grid) - vec2(.5, 0);
  float x = n.x - .5;
  float y = UV.y * 20.;
  float wiggle = sin(y + sin(y));
  x += wiggle * (.5 - abs(x)) * (n.z - .5);
  x *= .7;
  float ti = fract(t + n.z);
  y = (Saw(.85, ti) - .5) * .9 + .5;
  vec2 p = vec2(x, y);
  float d = length((st - p) * a.yx);
  float mainDrop = S(.4, .0, d);
  float r = sqrt(S(1., y, st.y));
  float cd = abs(st.x - x);
  float trail = S(.23 * r, .15 * r * r, cd);
  float trailFront = S(-.02, .02, st.y - y);
  trail *= trailFront * r * r;
  y = UV.y;
  y = fract(y * 10.) + (st.y - .5);
  float dd = length(st - vec2(x, y));
  float droplets = S(.3, 0., dd);
  float m = mainDrop + droplets * r * trailFront;
  return vec2(m, trail);
}

float StaticDrops(vec2 uv, float t) {
  uv *= 40.;
  vec2 id = floor(uv);
  uv = fract(uv) - .5;
  vec3 n = N13(id.x * 107.45 + id.y * 3543.654);
  vec2 p = (n.xy - .5) * .7;
  float d = length(uv - p);
  float fade = Saw(.025, fract(t + n.z));
  return S(.3, 0., d) * fract(n.z * 10.) * fade;
}

vec2 Drops(vec2 uv, float t, float l0, float l1, float l2) {
  float s = StaticDrops(uv, t) * l0;
  vec2 m1 = DropLayer2(uv, t) * l1;
  vec2 m2 = DropLayer2(uv * 1.85, t) * l2;
  float c = S(.3, 1., s + m1.x + m2.x);
  return vec2(c, max(m1.y * l0, m2.y * l1));
}

vec3 encodeSRGB(vec3 c) {
  c = clamp(c, 0., 1.);
  return mix(c * 12.92, 1.055 * pow(c, vec3(1. / 2.4)) - .055, step(.0031308, c));
}

void main() {
  vec2 local = gl_FragCoord.xy;
  vec2 iResolution = u_res;
  vec2 fragCoord = local + u_offset;

  vec2 uv = (fragCoord - .5 * iResolution) / iResolution.y;
  vec2 UV = fragCoord / iResolution;
  float T = u_time;
  float t = T * .2 * u_rainSpeed;
  float rainAmount = clamp(u_rainIntensity, 0., 1.);
  float maxBlur = mix(u_fogMaxBlurLow, u_fogMaxBlurHigh, rainAmount);
  float minBlur = u_fogMinBlur;

  float zoom = -cos(T * .2 * u_zoomSpeed) * u_zoomAmount;
  uv *= .7 + zoom * .3;
  uv *= max(u_dropZoomOut, 0.1);
  float uvScale = .9 + zoom * .1;
  UV = (UV - .5) * uvScale + .5;

  float staticDropsAmt = S(-.5, 1., rainAmount) * 2. * u_staticDropDensity;
  float layer1 = S(.25, .75, rainAmount) * u_layer1Density;
  float layer2 = S(.0, .5, rainAmount) * u_layer2Density;
  vec2 c = Drops(uv, t, staticDropsAmt, layer1, layer2);
  vec2 e = vec2(.001, 0.);
  float cx = Drops(uv + e, t, staticDropsAmt, layer1, layer2).x;
  float cy = Drops(uv + e.yx, t, staticDropsAmt, layer1, layer2).x;
  vec2 n = vec2(cx - c.x, cy - c.x) * u_refractionStrength;

  float focus = mix(maxBlur - c.y, minBlur, S(.1, .2, c.x));
  float blurAmount = clamp(exp2(focus - maxBlur), 0., 1.);
  vec2 texUV = vec2(UV.x + n.x, 1. - (UV.y + n.y));
  vec3 col = mix(texture(u_sharp, texUV).rgb, texture(u_blur, texUV).rgb, blurAmount);

  float pt = (T + 3.) * .5 * u_lightningSpeed;
  float colFade = (sin(pt * .2) * .5 + .5) * clamp(u_colorGradeStrength, 0., 1.);
  vec3 grade = mix(vec3(1.), vec3(.8, .9, 1.3), colFade);
  col *= grade;
  float fade = S(0., 10., T);
  float lightning = sin(pt * sin(pt * 10.));
  lightning *= pow(max(0., sin(pt + sin(pt))), max(u_lightningSharpness, 0.01));
  float flash = 1. + lightning * fade * u_lightningBoost;
  col *= flash;
  vec2 vUV = (UV - .5) * u_vignetteStrength;
  col *= 1. - clamp(dot(vUV, vUV), 0., 1.);
  float exposure = fade * u_brightness * (1. - clamp(u_dimAmount, 0., 1.));
  col *= exposure;

  outColor = vec4(encodeSRGB(col), 1.);
}`;

  // Pass 2: each card is a lens over the rendered rain scene. No light is
  // ever added (no rims, highlights or sheen) -- like Apple's liquid glass,
  // a card's edge reads only because the scene bends through its bevel.
  const LENS_FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_scene;   // pass 1 output, sRGB-encoded, mipmapped
uniform vec2 u_size;         // canvas size, device px
uniform float u_dpr, u_glassTint;
uniform int u_glassCount;
uniform vec4 u_glassRect[${MAX_GLASS}];  // x, y (bottom-left, canvas px), w, h
uniform float u_glassRadius[${MAX_GLASS}];
out vec4 outColor;

#define S(a, b, t) smoothstep(a, b, t)

float sdRoundRect(vec2 p, vec2 b, float r) {
  vec2 q = abs(p) - b + r;
  return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - r;
}

// Frosting from the scene's mip chain. Four taps offset by ~half a texel of
// the chosen level hide the blocky look a single mip sample has.
vec3 frosted(vec2 px, float lod) {
  vec2 uv = px / u_size;
  vec2 o = vec2(.5 * exp2(lod)) / u_size;
  return (textureLod(u_scene, uv + o, lod).rgb + textureLod(u_scene, uv - o, lod).rgb +
          textureLod(u_scene, uv + vec2(o.x, -o.y), lod).rgb + textureLod(u_scene, uv - vec2(o.x, -o.y), lod).rgb) * .25;
}

void main() {
  vec2 px = gl_FragCoord.xy;
  vec3 col = textureLod(u_scene, px / u_size, 0.).rgb;
  float tint = mix(.8, .35, clamp(u_glassTint, 0., 1.));

  for (int i = 0; i < ${MAX_GLASS}; i++) {
    if (i >= u_glassCount) break;
    vec4 R = u_glassRect[i];
    vec2 hb = R.zw * .5;
    vec2 p = px - (R.xy + hb);
    float r = min(u_glassRadius[i], min(hb.x, hb.y));
    float d = sdRoundRect(p, hb, r);

    // Soft contact shadow under the card (darkening only).
    float ds = sdRoundRect(p + vec2(0., 4. * u_dpr), hb, r);
    col *= 1. - .22 * (1. - S(-4. * u_dpr, 20. * u_dpr, ds)) * S(-1., 1., d);
    if (d > 1.) continue;

    float h = 1.5;
    vec2 g = vec2(sdRoundRect(p + vec2(h, 0.), hb, r) - sdRoundRect(p - vec2(h, 0.), hb, r),
                  sdRoundRect(p + vec2(0., h), hb, r) - sdRoundRect(p - vec2(0., h), hb, r));
    vec2 nrm = normalize(g + 1e-5);
    float bevel = min(22. * u_dpr, min(hb.x, hb.y));
    float edge = S(-bevel, 0., d);  // 0 across the flat middle -> 1 at the rim

    // Thick convex rim: displacement ramps up steeply toward the edge, so
    // the drops and wallpaper just inside the border visibly stretch and
    // bend. The middle stays frosted; the rim is sampled sharper so that
    // bending is actually visible, with slight per-channel dispersion.
    vec2 refr = -nrm * pow(edge, 2.2) * 46. * u_dpr;
    float lod = mix(4.2, 1., edge);
    vec3 glass;
    for (int k = 0; k < 3; k++) {
      vec2 pk = px + refr - nrm * edge * edge * float(k - 1) * 3. * u_dpr;
      glass[k] = frosted(pk, lod)[k];
    }
    glass *= tint;

    col = mix(col, glass, 1. - S(-1., 1., d));
  }
  outColor = vec4(col, 1.);
}`;

  let rainProgram = null, lensProgram = null;
  const rainU = {}, lensU = {};
  let sharpTex, blurTex, wallpaperBitmap = null, blurKey = "";
  let sceneTex = null, sceneFbo = null, sceneW = 0, sceneH = 0;

  function compile(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }

  function link(fragSrc, out) {
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fragSrc));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    const count = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < count; i++) {
      const name = gl.getActiveUniform(prog, i).name.replace(/\[0\]$/, "");
      out[name] = gl.getUniformLocation(prog, name);
    }
    return prog;
  }

  function makeTexture() {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    // Until a wallpaper arrives: the same dark blue-black Rainy falls back to.
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.SRGB8_ALPHA8, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([18, 22, 32, 255]));
    return t;
  }

  function upload(tex, source) {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    // sRGB internal format: sampling decodes to linear, like Rainy's pipeline.
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.SRGB8_ALPHA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
  }

  async function setWallpaper(blob) {
    wallpaperBitmap = await createImageBitmap(blob);
    upload(sharpTex, wallpaperBitmap);
    blurKey = "";
  }

  // Fog texture, mirroring RainRenderer's gaussian: 2^(maxBlur-1) px at
  // 1080p, built at the smallest size where that sigma still spans ~5
  // texels (smooth when upsampled, cheap to rebuild on slider changes).
  function rebuildBlurIfNeeded(s) {
    if (!wallpaperBitmap) return;
    const screenH = screen.height * devicePixelRatio, screenW = screen.width * devicePixelRatio;
    const maxBlur = s.fogMaxBlurLow + (s.fogMaxBlurHigh - s.fogMaxBlurLow) * Math.min(Math.max(s.rainIntensity, 0), 1);
    const sigma = Math.min(2 ** (maxBlur - 1) * screenH / 1080, screenH);
    const key = `${Math.round(sigma * 10)}|${screenW}x${screenH}`;
    if (key === blurKey) return;
    blurKey = key;
    const scale = Math.min(1, 5 / Math.max(sigma, 0.001));
    const w = Math.max(16, Math.round(screenW * scale)), h = Math.max(16, Math.round(screenH * scale));
    const c = new OffscreenCanvas(w, h);
    const ctx = c.getContext("2d");
    ctx.filter = `blur(${sigma * scale}px)`;
    ctx.drawImage(wallpaperBitmap, 0, 0, w, h);
    // The canvas blur fades to transparent at the borders; uploading it
    // unpremultiplied renormalizes those edges instead of darkening them.
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    upload(blurTex, c);
  }

  // The rain scene is rendered into this (mipmapped, so the lens pass can
  // frost it cheaply) and then composited to the screen by the lens pass.
  function ensureSceneTarget(w, h) {
    if (sceneTex && w === sceneW && h === sceneH) return;
    sceneW = w; sceneH = h;
    if (!sceneTex) {
      sceneTex = gl.createTexture();
      sceneFbo = gl.createFramebuffer();
    }
    gl.bindTexture(gl.TEXTURE_2D, sceneTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, sceneFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, sceneTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  function glassRects() {
    const dpr = devicePixelRatio;
    const out = [];
    for (const el of document.querySelectorAll(".topbar, .dashboard .card")) {
      const b = el.getBoundingClientRect();
      if (b.width === 0 || b.height === 0 || b.bottom < 0 || b.top > innerHeight) continue;
      out.push({
        x: b.left * dpr, y: (innerHeight - b.bottom) * dpr, w: b.width * dpr, h: b.height * dpr,
        r: (parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0) * dpr,
      });
      if (out.length === MAX_GLASS) break;
    }
    return out;
  }

  function initGL() {
    if (!gl) return false;
    try {
      rainProgram = link(FRAG, rainU);
      lensProgram = link(LENS_FRAG, lensU);
    } catch (err) {
      console.error("Liquid glass shader failed:", err);
      return false;
    }
    sharpTex = makeTexture();
    blurTex = makeTexture();
    gl.useProgram(rainProgram);
    gl.uniform1i(rainU.u_sharp, 0);
    gl.uniform1i(rainU.u_blur, 1);
    gl.useProgram(lensProgram);
    gl.uniform1i(lensU.u_scene, 0);
    gl.bindVertexArray(gl.createVertexArray());
    return true;
  }

  const rectData = new Float32Array(MAX_GLASS * 4);
  const radiusData = new Float32Array(MAX_GLASS);

  function frame(now) {
    rafId = requestAnimationFrame(frame);
    const dpr = devicePixelRatio;
    const w = Math.round(innerWidth * dpr), h = Math.round(innerHeight * dpr);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ensureSceneTarget(w, h);
    const s = effectiveSettings();
    rebuildBlurIfNeeded(s);

    // ---- Pass 1: the rain scene, into the offscreen target.
    gl.bindFramebuffer(gl.FRAMEBUFFER, sceneFbo);
    gl.viewport(0, 0, w, h);
    gl.useProgram(rainProgram);
    // Where this viewport sits on its screen (CSS px): window position minus
    // the screen's origin, plus the window chrome above the page -- so the
    // wallpaper lines up with the real desktop around the window.
    const screenLeft = "left" in screen ? screen.left : (screen.availLeft || 0);
    const screenTop = "top" in screen ? screen.top : 0;
    const vpLeft = screenX - screenLeft + (outerWidth - innerWidth) / 2;
    const vpTop = screenY - screenTop + (outerHeight - innerHeight);
    gl.uniform2f(rainU.u_res, screen.width * dpr, screen.height * dpr);
    gl.uniform2f(rainU.u_offset, vpLeft * dpr, (screen.height - vpTop - innerHeight) * dpr);
    gl.uniform1f(rainU.u_time, currentTime(now));
    gl.uniform1f(rainU.u_dpr, dpr);
    for (const [k, v] of Object.entries(s)) {
      const loc = rainU[`u_${k}`];
      if (loc) gl.uniform1f(loc, v);
    }
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, sharpTex);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, blurTex);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, sceneTex);
    gl.generateMipmap(gl.TEXTURE_2D);

    // ---- Pass 2: glass lenses over that scene, to the screen.
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.useProgram(lensProgram);
    gl.uniform2f(lensU.u_size, w, h);
    gl.uniform1f(lensU.u_dpr, dpr);
    gl.uniform1f(lensU.u_glassTint, s.glassTint);
    const rects = glassRects();
    rectData.fill(0);
    radiusData.fill(0);
    rects.forEach((g, i) => { rectData.set([g.x, g.y, g.w, g.h], i * 4); radiusData[i] = g.r; });
    gl.uniform1i(lensU.u_glassCount, rects.length);
    gl.uniform4fv(lensU.u_glassRect, rectData);
    gl.uniform1fv(lensU.u_glassRadius, radiusData);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // ------------------------------------------------------------ on/off ----

  const glOk = initGL();
  let rafId = null;
  let pollTimer = null;

  function applyEnabled() {
    const on = prefs.enabled;
    document.body.classList.toggle("theme-glass", on && glOk);
    document.body.classList.toggle("theme-glass-fallback", on && !glOk);
    btnTheme.classList.toggle("theme-btn--on", on);
    canvas.hidden = !(on && glOk);
    if (on) {
      if (!pollTimer) {
        poll();
        pollTimer = setInterval(() => { if (!document.hidden) poll(); }, 2000);
      }
      if (glOk && rafId === null) rafId = requestAnimationFrame(frame);
    } else {
      clearInterval(pollTimer);
      pollTimer = null;
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  buildPanel();
  refreshPanel();
  applyEnabled();
})();
