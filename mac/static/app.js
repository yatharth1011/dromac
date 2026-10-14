const $ = (id) => document.getElementById(id);

const connPill = $("connPill");
const connText = $("connText");
const artImg = $("artwork");
const artPlaceholder = $("artPlaceholder");
const trackTitle = $("trackTitle");
const trackArtist = $("trackArtist");
const iconPlay = $("iconPlay");
const iconPause = $("iconPause");
const volSlider = $("volSlider");
const deviceModel = $("deviceModel");
const batteryFill = $("batteryFill");
const batteryText = $("batteryText");
const connDetail = $("connDetail");
const bleSignalRow = $("bleSignalRow");
const pairForm = $("pairForm");
const pairStatus = $("pairStatus");
const notifList = $("notifList");
const toast = $("toast");
const likeIcon = $("likeIcon");
const rateRow = $("rateRow");
const btnLike = $("btnLike");
const btnDislike = $("btnDislike");
const seekRow = $("seekRow");
const seekSlider = $("seekSlider");
const seekPos = $("seekPos");
const seekDur = $("seekDur");
const btnFlashlight = $("btnFlashlight");
const btnLyrics = $("btnLyrics");
const lyricsOverlay = $("lyricsOverlay");
const lyricsBody = $("lyricsBody");
const lyricsTrackName = $("lyricsTrackName");
const btnDnd = $("btnDnd");
const btnKeepAwake = $("btnKeepAwake");
const lockRow = $("lockRow");
const recentTracks = $("recentTracks");
const recentTracksList = $("recentTracksList");
const nextEventRow = $("nextEventRow");
const nextEventText = $("nextEventText");
const screenTimeRow = $("screenTimeRow");
const screenTimeText = $("screenTimeText");
const callBanner = $("callBanner");
const callNumber = $("callNumber");
const appList = $("appList");
const appLaunchRow = $("appLaunchRow");

let toastTimer = null;
function showToast(message, kind) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = "toast" + (kind ? " " + kind : "");
  toastTimer = setTimeout(() => { toast.classList.add("hidden"); }, 3200);
}

let lastArtwork = null;
let userDraggingVolume = false;
let lastNotifKey = "";

// ---------- lyrics glow color, derived from the album art ----------
// The lyrics ticker's glow uses whatever color actually dominates the current
// track's artwork instead of a fixed color -- with a brightness floor, since
// a dominant color pulled straight from a dark/muted cover would glow too
// dimly to read against the dashboard's own dark background.
const GLOW_FALLBACK = { r: 255, g: 213, b: 74 }; // gold, used before first artwork loads or if extraction fails
const GLOW_MIN_LIGHTNESS = 0.72;
const GLOW_MIN_SATURATION = 0.6; // keeps a brightened near-gray from just becoming plain white
let lastGlowKey = "";

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0;
  const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break;
      case g: h = (b - r) / d + 2; break;
      default: h = (r - g) / d + 4; break;
    }
    h /= 6;
  }
  return [h, s, l];
}

function hslToRgb(h, s, l) {
  const hue2rgb = (p, q, t) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  let r, g, b;
  if (s === 0) {
    r = g = b = l;
  } else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1 / 3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1 / 3);
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}

function brightenToFloor(r, g, b) {
  const [h, s, l] = rgbToHsl(r, g, b);
  if (l >= GLOW_MIN_LIGHTNESS) return [r, g, b];
  return hslToRgb(h, Math.max(s, GLOW_MIN_SATURATION), GLOW_MIN_LIGHTNESS);
}

// A histogram over quantized color buckets, not a plain average -- averaging
// a busy cover tends to muddy toward gray, while the dominant color is really
// whichever hue actually covers the most area. Frequency is weighted by
// saturation too, so a large flat black/white background doesn't beat out a
// smaller but genuinely colorful region.
function extractDominantColor(img) {
  try {
    const size = 32;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, size, size);
    const data = ctx.getImageData(0, 0, size, size).data;
    const buckets = {};
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 128) continue;
      const r = data[i], g = data[i + 1], b = data[i + 2];
      const key = `${Math.round(r / 32)},${Math.round(g / 32)},${Math.round(b / 32)}`;
      const bucket = buckets[key] || (buckets[key] = { r: 0, g: 0, b: 0, count: 0 });
      bucket.r += r; bucket.g += g; bucket.b += b; bucket.count++;
    }
    let best = null;
    for (const key in buckets) {
      const bucket = buckets[key];
      const avgR = bucket.r / bucket.count, avgG = bucket.g / bucket.count, avgB = bucket.b / bucket.count;
      const max = Math.max(avgR, avgG, avgB), min = Math.min(avgR, avgG, avgB);
      const saturation = max === 0 ? 0 : (max - min) / max;
      const score = bucket.count * (0.4 + saturation);
      if (!best || score > best.score) best = { r: avgR, g: avgG, b: avgB, score };
    }
    return best ? { r: best.r, g: best.g, b: best.b } : null;
  } catch (e) {
    return null; // e.g. a cross-origin image the source server doesn't allow canvas reads on
  }
}

function applyGlowColor(r, g, b) {
  document.documentElement.style.setProperty("--glow-r", Math.round(r));
  document.documentElement.style.setProperty("--glow-g", Math.round(g));
  document.documentElement.style.setProperty("--glow-b", Math.round(b));
}

function resetGlowColor() {
  applyGlowColor(GLOW_FALLBACK.r, GLOW_FALLBACK.g, GLOW_FALLBACK.b);
}

// Sampled via a separate, hidden probe image (never the visible artwork
// <img>) so that if the artwork host doesn't support CORS canvas access, the
// failure just quietly keeps the previous/fallback glow color -- it can never
// break the actual visible album art.
function extractGlowColorFromUrl(url) {
  const probe = new Image();
  probe.crossOrigin = "anonymous";
  probe.onload = () => {
    const color = extractDominantColor(probe);
    if (!color) return;
    const [r, g, b] = brightenToFloor(color.r, color.g, color.b);
    applyGlowColor(r, g, b);
  };
  probe.onerror = () => {};
  probe.src = url;
}

const sliderAnims = new WeakMap();

function animateSliderTo(el, target, duration = 280) {
  const from = Number(el.value);
  if (from === target) return;
  const existing = sliderAnims.get(el);
  if (existing) cancelAnimationFrame(existing);

  const start = performance.now();
  const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

  function step(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = easeOutCubic(t);
    el.value = from + (target - from) * eased;
    if (el === volSlider) updateVolumeFillVisual();
    if (t < 1) {
      sliderAnims.set(el, requestAnimationFrame(step));
    } else {
      el.value = target;
      if (el === volSlider) updateVolumeFillVisual();
      sliderAnims.delete(el);
    }
  }
  sliderAnims.set(el, requestAnimationFrame(step));
}

function updateFillVisual(el) {
  const min = Number(el.min) || 0;
  const max = Number(el.max) || 100;
  const val = Number(el.value);
  const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
  el.style.setProperty("--fill-percent", pct + "%");
}
function updateVolumeFillVisual() { updateFillVisual(volSlider); }

// The phone's real volume range is often tiny (many devices only have ~15-16
// steps), which makes a slider bound directly to it feel chunky/stepped while
// dragging -- the browser can only snap to whole native steps. So the slider
// itself always moves at a fixed, fine-grained resolution for a smooth feel
// under the finger/cursor, and only the OUTGOING command sent to the phone
// gets quantized down to whichever device step is nearest.
const SLIDER_RESOLUTION = 1000;
let deviceVolMin = 0;
let deviceVolMax = 15;

function deviceLevelToSliderPos(level) {
  const span = deviceVolMax - deviceVolMin;
  if (span <= 0) return 0;
  return Math.round(((level - deviceVolMin) / span) * SLIDER_RESOLUTION);
}

function sliderPosToDeviceLevel(pos) {
  // Deliberately NOT rounded to an integer step -- the phone itself now
  // interpolates fractional levels (native volume step + a small loudness
  // boost) so the fine slider position translates to an actually audible
  // difference, not just a visual one.
  const span = deviceVolMax - deviceVolMin;
  return deviceVolMin + (pos / SLIDER_RESOLUTION) * span;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// How the Mac is reaching the phone, in priority order (the server moves up
// to a better one on its own the moment it's available).
const METHOD_LABELS = {
  usb: ["USB cable", "best path"],
  ble: ["Wi-Fi", "address from the Bluetooth beacon"],
  wifi: ["Wi-Fi", "the phone's remembered address"],
  gateway: ["Wi-Fi", "phone's hotspot"],
  mdns: ["Wi-Fi", "found by mDNS"],
  bluetooth: ["Bluetooth data link", "fallback · slower"],
  scan: ["Wi-Fi", "found by network search"],
  "ip-ring": ["Wi-Fi", "found by network search"],
  manual: ["Wi-Fi", "linked manually"],
};

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function renderState(state) {
  if (state.connected) {
    connPill.classList.add("pill-on");
    connPill.classList.remove("pill-off");
    connText.textContent = state.device_model ? `Connected · ${state.device_model}` : "Connected";
    const [via, note] = METHOD_LABELS[state.method] || [null, null];
    const local = !state.host || String(state.host).startsWith("127.");
    connDetail.innerHTML = via
      ? `Linked via <span class="tok-keyword">${esc(via)}</span>` +
        (local ? "" : ` <span class="tok-number">${esc(state.host)}</span>`) +
        (note ? ` <span class="tok-comment">· ${esc(note)}</span>` : "")
      : state.host
        ? `Linked to <span class="tok-number">${esc(state.host)}:${esc(String(state.port))}</span>`
        : "Connected.";
  } else {
    connPill.classList.remove("pill-on");
    connPill.classList.add("pill-off");
    connText.textContent = state.host ? "Unreachable" : "Not connected";
    connDetail.innerHTML = state.host
      ? `Can't reach <span class="tok-number">${esc(state.host)}:${esc(String(state.port))}</span>` +
        (state.error ? ` <span class="tok-comment">— ${esc(state.error)}</span>` : "")
      : "No phone linked yet.";
  }

  const ble = state.ble;
  if (ble) {
    if (!ble.helperRunning) {
      bleSignalRow.innerHTML = `<span class="tok-red">BLE: scanner not running</span>`;
    } else if (ble.lastSeenAgoSec == null) {
      bleSignalRow.innerHTML = `<span class="tok-comment">BLE: no packet seen yet</span>`;
    } else {
      const ago = ble.lastSeenAgoSec;
      const stale = ago > 15;
      const agoText = ago < 1 ? "just now" : `${Math.round(ago)}s ago`;
      bleSignalRow.innerHTML = stale
        ? `<span class="tok-red">BLE: last packet ${agoText}</span>`
        : `<span class="tok-comment">BLE: last packet ${agoText} (${esc(ble.lastAddr || "")})</span>`;
    }
  }

  // In "ai split" you hear the song ~9.5 s after the phone plays it. Show what
  // you're hearing: the now-playing report from that long ago (title, art,
  // lyrics, progress), so a new song only appears when it reaches your ears.
  const heardDelay = (state.audio && state.audio.on && state.audio.pos && state.audio.pos.delay_ms) || 0;
  const np = heardNowPlaying(state.now_playing, heardDelay);
  if (np) {
    trackTitle.textContent = np.title || "Unknown title";
    trackArtist.textContent = np.artist || "";
    iconPlay.style.display = np.playing ? "none" : "block";
    iconPause.style.display = np.playing ? "block" : "none";
    // with the audio running behind, refresh the cover only when the track you
    // hear changes (the phone's current cover would be the next song's)
    const artKey = heardDelay ? `${np.title || ""}::${np.artist || ""}` : np.artwork;
    if (np.artwork && artKey !== lastArtwork) {
      artImg.src = np.artwork;
      lastArtwork = artKey;
    }
    const glowKey = `${np.title || ""}::${np.artist || ""}`;
    if (np.artwork && glowKey !== lastGlowKey) {
      lastGlowKey = glowKey;
      extractGlowColorFromUrl(np.artwork);
    } else if (!np.artwork) {
      lastGlowKey = "";
      resetGlowColor();
    }
    if (np.artwork) {
      artImg.style.display = "block";
      artPlaceholder.style.display = "none";
    } else {
      artImg.style.display = "none";
      artPlaceholder.style.display = "flex";
    }

    likeIcon.classList.remove("hidden", "liked", "not-liked");
    if (np.liked === true) {
      likeIcon.classList.add("liked");
    } else if (np.liked === false) {
      likeIcon.classList.add("not-liked");
    } else {
      likeIcon.classList.add("hidden");
    }

    const hasLike = !!np.likeAction;
    const hasDislike = !!np.dislikeAction;
    rateRow.classList.toggle("hidden", !hasLike && !hasDislike);
    btnLike.style.visibility = hasLike ? "visible" : "hidden";
    btnDislike.style.visibility = hasDislike ? "visible" : "hidden";
    btnLike.classList.toggle("active", np.liked === true);
    btnDislike.classList.toggle("active", np.liked === false);
    btnLike.dataset.key = hasLike ? np.likeAction.key : "";
    btnLike.dataset.index = hasLike ? np.likeAction.index : "";
    btnDislike.dataset.key = hasDislike ? np.dislikeAction.key : "";
    btnDislike.dataset.index = hasDislike ? np.dislikeAction.index : "";

    if (np.duration && np.duration > 0) {
      seekRow.classList.remove("hidden");
      seekState = { position: np.heardPosition ?? (np.position || 0), duration: np.duration, playing: !!np.playing, syncedAt: Date.now() };
      if (!userDraggingSeek) applySeekDisplay(seekState.position);
    } else {
      seekRow.classList.add("hidden");
      seekState.duration = -1;
    }

    onTrackChanged(np.title, np.artist, np.duration);
  } else {
    trackTitle.textContent = state.connected ? "Nothing playing" : "Not connected";
    trackArtist.textContent = state.connected ? "" : "Link your phone to get started";
    artImg.style.display = "none";
    artPlaceholder.style.display = "flex";
    iconPlay.style.display = "block";
    iconPause.style.display = "none";
    likeIcon.classList.add("hidden");
    rateRow.classList.add("hidden");
    seekRow.classList.add("hidden");
    seekState.duration = -1;
    btnLyrics.classList.add("hidden");
  }

  if (state.volume && !userDraggingVolume) {
    deviceVolMin = state.volume.min;
    deviceVolMax = state.volume.max;
    animateSliderTo(volSlider, deviceLevelToSliderPos(state.volume.level));
  }

  btnFlashlight.classList.toggle("btn--active", !!state.flashlight);
  btnDnd.classList.toggle("btn--active", !!state.dnd);

  deviceModel.textContent = state.device_model || (state.connected ? "—" : "Not connected");

  if (typeof state.battery === "number") {
    batteryFill.style.width = state.battery + "%";
    batteryText.textContent = state.battery + "%";
    checkBatteryAlert(state.battery);
  } else {
    batteryFill.style.width = "0%";
    batteryText.textContent = "—";
  }

  if (state.nextEvent && state.nextEvent.title) {
    const minsAway = Math.round((state.nextEvent.startAt - Date.now()) / 60000);
    const whenText = minsAway <= 0 ? "now" : minsAway < 60 ? `${minsAway}m` : `${Math.round(minsAway / 60)}h`;
    nextEventText.textContent = `${state.nextEvent.title} in ${whenText}`;
    nextEventRow.classList.remove("hidden");
  } else {
    nextEventRow.classList.add("hidden");
  }

  if (typeof state.screenTimeTodayMin === "number" && state.screenTimeTodayMin >= 0) {
    const h = Math.floor(state.screenTimeTodayMin / 60);
    const m = state.screenTimeTodayMin % 60;
    screenTimeText.textContent = h > 0 ? `${h}h ${m}m screen time` : `${m}m screen time`;
    screenTimeRow.classList.remove("hidden");
  } else {
    screenTimeRow.classList.add("hidden");
  }

  if (state.recentTracks && state.recentTracks.length) {
    recentTracksList.innerHTML = "";
    for (const track of state.recentTracks) {
      const row = document.createElement("div");
      row.className = "recent-track-item";
      const t = document.createElement("span");
      t.className = "t";
      t.textContent = track.title;
      const a = document.createElement("span");
      a.className = "a";
      a.textContent = track.artist || "";
      row.appendChild(t);
      row.appendChild(a);
      row.onclick = () => {
        if (state.locked) {
          showToast("Unlock the phone first", "err");
          return;
        }
        playRecentTrack(track);
      };
      recentTracksList.appendChild(row);
    }
    recentTracks.classList.remove("hidden");
  } else {
    recentTracks.classList.add("hidden");
  }

  // Opening an app only makes sense once the phone is actually unlocked --
  // launching it behind the lock screen is confusing at best.
  appLaunchRow.classList.toggle("hidden", !!state.locked);
  lastKnownOverlayGranted = state.overlayGranted !== false;

  btnKeepAwake.classList.toggle("btn--active", !!state.keepAwake);
  renderSpeaker(state.audio);
  lockRow.classList.toggle("hidden", !state.lockAvailable);

  if (state.incomingCall) {
    callNumber.textContent = state.incomingCall.number || "unknown number";
    callBanner.classList.remove("hidden");
  } else {
    callBanner.classList.add("hidden");
  }

  checkOtp(state.otp);
  checkCameraSync(state);
}

let lastBatteryAlertLevel = null;
function checkBatteryAlert(level) {
  if (level <= 20) {
    if (lastBatteryAlertLevel === null || lastBatteryAlertLevel > 20) {
      showToast(`Phone battery low: ${level}%`, "err");
    }
  }
  lastBatteryAlertLevel = level;
}

async function playRecentTrack(track) {
  if (!lastKnownOverlayGranted) {
    showToast("Grant \"Display Over Apps\" in Dromac on the phone first", "err");
    return;
  }
  const query = `${track.title} ${track.artist || ""}`.trim();
  showToast(`Opening ${track.title}…`, "ok");
  await post("/api/media/play_search", { query });
  setTimeout(poll, 500);
}

let lastKnownOverlayGranted = true;

let lastOtpSeen = null;
function checkOtp(otp) {
  if (otp && otp !== lastOtpSeen) {
    showToast(`OTP ${otp} copied to Mac clipboard`, "ok");
  }
  lastOtpSeen = otp;
}

async function poll() {
  try {
    const state = await api("/api/state");
    renderState(state);
  } catch (e) {
    connText.textContent = "Server unreachable";
  }
}

function renderNotifications(data) {
  const items = (data && data.notifications) || [];
  const key = items.map((n) => n.app + n.title + n.when).join("|");
  if (key === lastNotifKey) return;
  lastNotifKey = key;

  if (items.length === 0) {
    notifList.innerHTML = '<p class="hint">No notifications yet.</p>';
    return;
  }
  notifList.innerHTML = "";
  for (const n of items) {
    notifList.appendChild(buildNotifItem(n));
  }
}

function buildNotifItem(n) {
  const row = document.createElement("div");
  row.className = "notif-item";

  const headRow = document.createElement("div");
  headRow.className = "notif-head-row";
  const app = document.createElement("div");
  app.className = "notif-app";
  app.textContent = n.app || "";
  const dismissBtn = document.createElement("button");
  dismissBtn.className = "notif-dismiss";
  dismissBtn.textContent = "×";
  dismissBtn.title = "dismiss";
  dismissBtn.addEventListener("click", async () => {
    row.style.opacity = "0.4";
    const res = await post("/api/notifications/dismiss", { key: n.key });
    if (res.ok) {
      row.remove();
    } else {
      row.style.opacity = "1";
      showToast(res.error || "Couldn't dismiss", "err");
    }
  });
  headRow.appendChild(app);
  headRow.appendChild(dismissBtn);

  const title = document.createElement("div");
  title.className = "notif-title";
  title.textContent = n.title || "";
  const text = document.createElement("div");
  text.className = "notif-text";
  text.textContent = n.text || "";

  row.appendChild(headRow);
  row.appendChild(title);
  if (n.text) row.appendChild(text);

  const actions = n.actions || [];
  if (actions.length > 0) {
    const actionRow = document.createElement("div");
    actionRow.className = "notif-actions";
    for (const a of actions) {
      const btn = document.createElement("button");
      btn.className = "notif-action-btn";
      btn.textContent = a.title || "action";
      btn.addEventListener("click", () => {
        if (a.hasReply) {
          showReplyBox(row, n.key, a.index);
        } else {
          fireNotifAction(n.key, a.index, null, btn);
        }
      });
      actionRow.appendChild(btn);
    }
    row.appendChild(actionRow);
  }

  return row;
}

async function fireNotifAction(key, actionIndex, text, btn) {
  if (btn) btn.disabled = true;
  const payload = { key, actionIndex };
  if (text !== null) payload.text = text;
  const res = await post("/api/notifications/action", payload);
  if (res.ok) {
    showToast("Sent", "ok");
  } else {
    showToast(res.error || "Action failed", "err");
  }
  if (btn) btn.disabled = false;
}

function showReplyBox(row, key, actionIndex) {
  const existing = row.querySelector(".notif-reply-row");
  if (existing) { existing.remove(); return; }
  const replyRow = document.createElement("div");
  replyRow.className = "notif-reply-row";
  const input = document.createElement("input");
  input.placeholder = "reply…";
  const send = document.createElement("button");
  send.className = "notif-action-btn";
  send.textContent = "send";
  const submit = () => {
    if (!input.value.trim()) return;
    fireNotifAction(key, actionIndex, input.value, send);
    replyRow.remove();
  };
  send.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  replyRow.appendChild(input);
  replyRow.appendChild(send);
  row.appendChild(replyRow);
  input.focus();
}

async function pollNotifications() {
  try {
    const data = await api("/api/notifications");
    renderNotifications(data);
  } catch (e) {
    // ignore while disconnected
  }
}

$("btnPlay").onclick = () => post("/api/media/play_pause");
$("btnNext").onclick = () => post("/api/media/next");
$("btnPrev").onclick = () => post("/api/media/prev");

btnLike.onclick = async () => {
  if (!btnLike.dataset.key) return;
  await fireNotifAction(btnLike.dataset.key, Number(btnLike.dataset.index), null, btnLike);
  setTimeout(poll, 400);
};
btnDislike.onclick = async () => {
  if (!btnDislike.dataset.key) return;
  await fireNotifAction(btnDislike.dataset.key, Number(btnDislike.dataset.index), null, btnDislike);
  setTimeout(poll, 400);
};

let volumeThrottle = null;
let volumePendingLevel = null;

function sendVolumeLevel(level) {
  post("/api/volume/set", { level });
}

volSlider.addEventListener("input", () => {
  userDraggingVolume = true;
  updateVolumeFillVisual();
  const level = sliderPosToDeviceLevel(Number(volSlider.value));
  volumePendingLevel = level;
  if (!volumeThrottle) {
    sendVolumeLevel(level);
    volumeThrottle = setTimeout(() => {
      volumeThrottle = null;
      if (volumePendingLevel !== null && volumePendingLevel !== level) {
        sendVolumeLevel(volumePendingLevel);
      }
    }, 120);
  }
});
volSlider.addEventListener("change", () => {
  const level = sliderPosToDeviceLevel(Number(volSlider.value));
  sendVolumeLevel(level);
  userDraggingVolume = false;
});
$("volUp").onclick = () => post("/api/volume/up");
$("volDown").onclick = () => post("/api/volume/down");

// ---------- seek / scrub ----------

function formatTime(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "0:00";
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

let userDraggingSeek = false;
let seekPendingPos = null;
let seekState = { position: 0, duration: -1, playing: false, syncedAt: 0 };

function applySeekDisplay(posMs) {
  if (seekState.duration > 0) {
    seekSlider.value = Math.max(0, Math.min(1000, (posMs / seekState.duration) * 1000));
  }
  updateFillVisual(seekSlider);
  seekPos.textContent = formatTime(posMs);
  seekDur.textContent = formatTime(seekState.duration > 0 ? seekState.duration : 0);
}

// Recent now-playing reports, for showing what you're hearing when the audio
// runs behind the phone (see renderState).
const npHistory = [];
function heardNowPlaying(np, delayMs) {
  const now = Date.now();
  npHistory.push({ t: now, np });
  while (npHistory.length && npHistory[0].t < now - delayMs - 30000) npHistory.shift();
  if (!delayMs) return np;
  // the newest report that's at least delayMs old (else the oldest we have)
  let pick = npHistory[0];
  for (const h of npHistory) if (h.t <= now - delayMs) pick = h;
  if (!pick.np) return null;
  // its position then, carried forward to the moment you're hearing now
  const ahead = pick.np.playing ? Math.max(0, now - delayMs - pick.t) : 0;
  return { ...pick.np, heardPosition: Math.min(pick.np.duration || Infinity, (pick.np.position || 0) + ahead) };
}

// Ticks locally between polls so the bar visibly advances in real time
// instead of only jumping every ~2s when a fresh poll lands — same idea as
// the volume slider's smoothing, just continuous rather than a single ease.
setInterval(() => {
  if (userDraggingSeek || !seekState.playing || seekState.duration <= 0) return;
  const elapsed = Date.now() - seekState.syncedAt;
  const posMs = Math.min(seekState.duration, seekState.position + elapsed);
  applySeekDisplay(posMs);
  highlightLyricsAt(posMs);
}, 250);

// ---------- lyrics ----------
// Fetched eagerly on every track change (not gated behind the modal being
// open) so the glowy ticker on the main dashboard always has something to
// show -- the modal, when opened, just renders the same already-fetched data
// as a full scrolling list instead of doing its own separate fetch.

const lyricsTicker = $("lyricsTicker");
const lyricsTickerLine = $("lyricsTickerLine");
const lyricsTickerPrev = $("lyricsTickerPrev");
const lyricsTickerNext = $("lyricsTickerNext");

let currentLyrics = null; // [{t, text}] | null (null = none found), for the CURRENT track
let currentLyricsKey = "";
let lyricsOpen = false;
let activeLyricLine = -1;
let lyricsTransitionTimer = null;
let lyricsBlurTimer = null;
// The media session's reported duration can lag a poll or two behind a fresh
// track change (metadata arriving before duration is finalized) -- fetching
// lyrics exactly once, right at track-change, can permanently miss the real
// (slowed) duration and leave a "slowed + reverb" track un-stretched for its
// whole playback. This tracks whether we still owe a re-fetch once a real
// duration shows up.
let lyricsDurationPending = false;

function onTrackChanged(title, artist, durationMs) {
  const key = `${title || ""}::${artist || ""}`;
  const haveDuration = durationMs && durationMs > 0;
  if (key === currentLyricsKey) {
    if (lyricsDurationPending && haveDuration) {
      lyricsDurationPending = false;
      fetchLyrics(title, artist, durationMs);
    }
    return;
  }
  currentLyricsKey = key;
  currentLyrics = null;
  activeLyricLine = -1;
  lyricsDurationPending = !haveDuration;
  lyricsTicker.classList.add("hidden");
  btnLyrics.classList.add("hidden");
  fetchLyrics(title, artist, durationMs);
}

async function fetchLyrics(title, artist, durationMs) {
  if (lyricsOpen) {
    lyricsTrackName.textContent = title || "";
    lyricsBody.innerHTML = '<p class="hint">Loading…</p>';
  }
  try {
    // durationMs lets the backend detect + time-stretch "slowed + reverb"
    // fan edits, whose synced lyrics (matched against the ORIGINAL, shorter
    // song) would otherwise run fast and drift out of sync as the track plays.
    let url = "/api/lyrics?title=" + encodeURIComponent(title || "") + "&artist=" + encodeURIComponent(artist || "");
    if (durationMs && durationMs > 0) url += "&duration=" + Math.round(durationMs);
    const res = await api(url);
    currentLyrics = res && res.lines && res.lines.length ? res.lines : null;
  } catch (e) {
    currentLyrics = null;
  }
  btnLyrics.classList.toggle("hidden", !currentLyrics);
  if (lyricsOpen) renderLyricsModal();
  activeLyricLine = -1;
  highlightLyricsAt(seekState.position);
}

function renderLyricsModal() {
  if (!currentLyrics) {
    lyricsBody.innerHTML = '<p class="hint">No synced lyrics found for this track.</p>';
    return;
  }
  lyricsBody.innerHTML = "";
  currentLyrics.forEach((line, i) => {
    const el = document.createElement("div");
    el.className = "lyrics-line";
    el.textContent = line.text;
    el.dataset.index = i;
    lyricsBody.appendChild(el);
  });
}

function highlightLyricsAt(posMs) {
  if (!currentLyrics || !currentLyrics.length) return;
  let idx = -1;
  for (let i = 0; i < currentLyrics.length; i++) {
    if (currentLyrics[i].t <= posMs) idx = i;
    else break;
  }
  if (idx === activeLyricLine) return;
  activeLyricLine = idx;

  if (idx >= 0) {
    lyricsTicker.classList.remove("hidden");
    const lines = [lyricsTickerLine, lyricsTickerPrev, lyricsTickerNext];

    // Swapping text the instant the new content appears reads as a "blip"
    // (an instant cut, then a little settle-wiggle) since the old text is
    // never actually seen leaving. Play a brief exit fade on the OLD text
    // first, then swap and play the entrance -- so it reads as one line
    // receding before the next arrives, not a jump-cut.
    if (lyricsTransitionTimer) clearTimeout(lyricsTransitionTimer);
    if (lyricsBlurTimer) clearTimeout(lyricsBlurTimer);
    lines.forEach((el) => {
      el.classList.remove("lyrics-line-enter");
      el.classList.add("lyrics-line-exit");
    });
    // The main line has no blur at rest (it's the sharp, glowing focal
    // point) -- only add it for the moving part of the transition, same as
    // the prev/next lines already have permanently.
    lyricsTickerLine.classList.add("lyrics-line-motion-blur");

    lyricsTransitionTimer = setTimeout(() => {
      lyricsTransitionTimer = null;
      const prev = currentLyrics[idx - 1];
      const next = currentLyrics[idx + 1];
      lyricsTickerPrev.textContent = prev ? prev.text : "";
      lyricsTickerPrev.classList.toggle("lyrics-ticker-empty", !prev);
      lyricsTickerNext.textContent = next ? next.text : "";
      lyricsTickerNext.classList.toggle("lyrics-ticker-empty", !next);
      lyricsTickerLine.textContent = currentLyrics[idx].text;

      lines.forEach((el) => {
        el.classList.remove("lyrics-line-exit");
        void el.offsetWidth; // force reflow so the animation restarts
        el.classList.add("lyrics-line-enter");
      });

      lyricsBlurTimer = setTimeout(() => {
        lyricsBlurTimer = null;
        lyricsTickerLine.classList.remove("lyrics-line-motion-blur");
      }, 220); // matches lyrics-line-in's duration
    }, 90); // matches lyrics-line-out's duration
  } else {
    lyricsTicker.classList.add("hidden");
  }

  if (!lyricsOpen) return;
  const children = lyricsBody.children;
  for (let i = 0; i < children.length; i++) {
    children[i].classList.toggle("active", i === idx);
  }
  if (idx >= 0 && children[idx]) {
    children[idx].scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

btnLyrics.onclick = () => {
  lyricsOpen = true;
  lyricsOverlay.classList.remove("hidden");
  lyricsTrackName.textContent = trackTitle.textContent;
  renderLyricsModal();
};
$("btnLyricsClose").onclick = () => {
  lyricsOpen = false;
  lyricsOverlay.classList.add("hidden");
};

seekSlider.addEventListener("input", () => {
  userDraggingSeek = true;
  updateFillVisual(seekSlider);
  if (seekState.duration > 0) {
    seekPendingPos = (Number(seekSlider.value) / 1000) * seekState.duration;
    seekPos.textContent = formatTime(seekPendingPos);
  }
});
seekSlider.addEventListener("change", () => {
  if (seekState.duration > 0 && seekPendingPos != null) {
    post("/api/media/seek", { positionMs: Math.round(seekPendingPos) });
    seekState.position = seekPendingPos;
    seekState.syncedAt = Date.now();
  }
  userDraggingSeek = false;
  seekPendingPos = null;
});

// ---------- flashlight / DND ----------

btnFlashlight.onclick = async () => {
  const res = await post("/api/flashlight/toggle");
  btnFlashlight.classList.toggle("btn--active", !!res.on);
};

btnDnd.onclick = async () => {
  const enabling = !btnDnd.classList.contains("btn--active");
  const res = await post("/api/dnd/toggle", { enabled: enabling });
  if (res.ok) {
    btnDnd.classList.toggle("btn--active", enabling);
  } else {
    showToast("Grant Do Not Disturb access on the phone first", "err");
  }
};

$("btnClearNotifs").onclick = async () => {
  await post("/api/notifications/clear_all");
  setTimeout(pollNotifications, 300);
};

// ---------- screen / lock / locate ----------

$("btnWake").onclick = () => post("/api/screen/wake");

btnKeepAwake.onclick = async () => {
  const res = await post("/api/screen/keep_awake/toggle");
  btnKeepAwake.classList.toggle("btn--active", !!res.on);
};

$("btnLock").onclick = async () => {
  const res = await post("/api/screen/lock");
  showToast(res.ok ? "Phone locked" : "Couldn't lock the phone", res.ok ? "ok" : "err");
};

$("btnLocate").onclick = async () => {
  const res = await api("/api/location");
  if (res && typeof res.lat === "number") {
    window.open(`https://maps.google.com/?q=${res.lat},${res.lng}`, "_blank");
  } else {
    showToast("No recent location fix available", "err");
  }
};

// ---------- camera ----------
// A received capture (from either the Mac's own capture button or the
// phone's dedicated camera screen) is never written into Documents/Dromac
// automatically -- it's auto-copied to the clipboard and held as a single
// "pending" shot until Save is explicitly clicked, so casual/test captures
// don't quietly pile up the folder.

const cameraThumbWrap = $("cameraThumbWrap");
const cameraThumbLabel = $("cameraThumbLabel");
let lastSeenPendingAt = null;

function checkCameraSync(state) {
  if (state.cameraPendingAt && state.cameraPendingAt !== lastSeenPendingAt) {
    const isFirstLoad = lastSeenPendingAt === null;
    lastSeenPendingAt = state.cameraPendingAt;
    cameraThumbLabel.textContent = "unsaved — copied to clipboard";
    cameraThumbWrap.classList.remove("hidden");
    btnCameraSave.classList.remove("hidden");
    btnCameraCopy.classList.remove("hidden");
    if (!isFirstLoad) showToast("Copied to clipboard — tap save to keep it", "ok");
  }
}

// The dashboard never preloads the actual image (no point spending bandwidth
// on a thumbnail nobody's looking at yet) -- the gallery icon fetches it only
// when clicked, into a simple full-size preview overlay.
const photoPreviewOverlay = $("photoPreviewOverlay");
const photoPreviewImg = $("photoPreviewImg");
$("btnCameraGallery").onclick = () => {
  photoPreviewImg.src = `/api/camera/last?t=${Date.now()}`;
  photoPreviewOverlay.classList.remove("hidden");
};
$("btnPhotoPreviewClose").onclick = () => {
  photoPreviewOverlay.classList.add("hidden");
  photoPreviewImg.src = "";
};

async function copyLastPhoto() {
  const res = await post("/api/camera/copy");
  showToast(res.ok ? "Copied to clipboard" : (res.error || "Couldn't copy"), res.ok ? "ok" : "err");
}
$("btnCameraThumbCopy").onclick = copyLastPhoto;

async function saveLastPhoto() {
  const res = await post("/api/camera/save");
  if (res.ok) {
    showToast(`Saved ${res.filename} to Documents/Dromac`, "ok");
    cameraThumbLabel.textContent = `saved as ${res.filename}`;
  } else {
    showToast(res.error || "Couldn't save", "err");
  }
}
$("btnCameraThumbSave").onclick = saveLastPhoto;

$("btnCameraClearFolder").onclick = async () => {
  if (!confirm("Delete every photo in Documents/Dromac? This can't be undone.")) return;
  const res = await post("/api/camera/folder/clear");
  showToast(res.ok ? `Deleted ${res.count} photo(s)` : (res.error || "Couldn't clear folder"), res.ok ? "ok" : "err");
};

// Mac-triggered camera: a real live preview (MJPEG, same mechanism as remote
// screen below) instead of a blind shutter -- "start" opens the camera and
// begins streaming, "capture" takes a still on top of that already-running,
// already-focused session.
const cameraOverlay = $("cameraOverlay");
const cameraStreamImg = $("cameraStreamImg");
const cameraPlaceholder = $("cameraPlaceholder");
const btnCameraSwitch = $("btnCameraSwitch");
const btnCameraFlash = $("btnCameraFlash");
const btnCameraCapture = $("btnCameraCapture");
const btnCameraSave = $("btnCameraSave");
const btnCameraCopy = $("btnCameraCopy");
let cameraFacing = "back";
let cameraFlashOn = false;

// A streamed <img> shows the browser's own "broken image" glyph the instant
// its src is empty or a request fails -- these two helpers keep the element
// invisible (via a class, not display:none, so layout doesn't jump) until a
// frame has actually loaded, and hide it again the moment loading stops.
function resetStreamImg(img) {
  img.classList.remove("loaded");
  img.onerror = null;
  img.onload = null;
  img.src = "";
}
function armStreamImg(img, src, onReady) {
  img.onerror = () => { img.classList.remove("loaded"); };
  img.onload = () => { img.classList.add("loaded"); if (onReady) onReady(); };
  img.src = src;
}

async function startCamera() {
  cameraOverlay.classList.remove("hidden");
  cameraPlaceholder.textContent = "Starting…";
  cameraPlaceholder.classList.remove("hidden");
  resetStreamImg(cameraStreamImg);
  const res = await post("/api/camera/start", { facing: cameraFacing, flash: cameraFlashOn });
  if (!res.ok) {
    cameraPlaceholder.textContent = "Couldn't start camera." + (res.error ? ` (${res.error})` : "");
    return;
  }
  armStreamImg(cameraStreamImg, "/api/camera/stream?t=" + Date.now(), () => cameraPlaceholder.classList.add("hidden"));
  startBrightnessWatch();
}
$("btnCameraOpen").onclick = startCamera;

function hideCamera() {
  resetStreamImg(cameraStreamImg);
  cameraOverlay.classList.add("hidden");
  stopBrightnessWatch();
  post("/api/camera/stop");
}
$("btnCameraClose").onclick = hideCamera;

btnCameraSwitch.onclick = async () => {
  cameraFacing = cameraFacing === "back" ? "front" : "back";
  cameraPlaceholder.textContent = "Switching…";
  cameraPlaceholder.classList.remove("hidden");
  resetStreamImg(cameraStreamImg);
  const res = await post("/api/camera/switch", { facing: cameraFacing });
  if (!res.ok) {
    cameraPlaceholder.textContent = "Couldn't switch camera." + (res.error ? ` (${res.error})` : "");
    return;
  }
  armStreamImg(cameraStreamImg, "/api/camera/stream?t=" + Date.now(), () => cameraPlaceholder.classList.add("hidden"));
};

btnCameraFlash.onclick = async () => {
  cameraFlashOn = !cameraFlashOn;
  btnCameraFlash.classList.toggle("icon-btn--active", cameraFlashOn);
  await post("/api/camera/flash", { on: cameraFlashOn });
};

btnCameraCapture.onclick = async () => {
  btnCameraCapture.disabled = true;
  const label = btnCameraCapture.querySelector("span");
  const prevText = label.textContent;
  label.textContent = "capturing…";
  try {
    const res = await post("/api/camera/photo");
    if (res.ok) {
      showToast("Copied to clipboard — tap save to keep it", "ok");
      btnCameraSave.classList.remove("hidden");
      btnCameraCopy.classList.remove("hidden");
    } else {
      showToast(res.error || "Capture failed", "err");
    }
  } catch (e) {
    showToast("Capture failed", "err");
  } finally {
    btnCameraCapture.disabled = false;
    label.textContent = prevText;
  }
};

btnCameraSave.onclick = saveLastPhoto;
btnCameraCopy.onclick = copyLastPhoto;

// A very bright/washed-out preview (pointed at a whiteboard, a bright window,
// a sheet of paper) is hard to read on screen -- sampling the frame's average
// brightness and inverting it once it's mostly white makes it read like a
// photographic negative, which is much easier to actually look at.
const cameraSampleCanvas = document.createElement("canvas");
cameraSampleCanvas.width = 24;
cameraSampleCanvas.height = 24;
const cameraSampleCtx = cameraSampleCanvas.getContext("2d", { willReadFrequently: true });
let cameraBrightnessTimer = null;

function startBrightnessWatch() {
  stopBrightnessWatch();
  cameraBrightnessTimer = setInterval(() => {
    if (!cameraStreamImg.classList.contains("loaded")) return;
    try {
      cameraSampleCtx.drawImage(cameraStreamImg, 0, 0, 24, 24);
      const data = cameraSampleCtx.getImageData(0, 0, 24, 24).data;
      let total = 0;
      for (let i = 0; i < data.length; i += 4) total += (data[i] + data[i + 1] + data[i + 2]) / 3;
      const avg = total / (data.length / 4);
      cameraStreamImg.classList.toggle("camera-inverted", avg > 210);
    } catch (e) {
      // frame not ready this tick -- try again next interval
    }
  }, 400);
}
function stopBrightnessWatch() {
  clearInterval(cameraBrightnessTimer);
  cameraBrightnessTimer = null;
  cameraStreamImg.classList.remove("camera-inverted");
}

// ---------- remote screen ----------

const mirrorOverlay = $("mirrorOverlay");
const mirrorImg = $("mirrorImg");
const mirrorPlaceholder = $("mirrorPlaceholder");
const btnMirrorBlackout = $("btnMirrorBlackout");

async function startMirror() {
  mirrorOverlay.classList.remove("hidden");
  mirrorPlaceholder.textContent = "Starting…";
  mirrorPlaceholder.classList.remove("hidden");
  resetStreamImg(mirrorImg);
  const res = await post("/api/screen/mirror/start");
  if (!res.ok) {
    // needsConsent used to hide res.error behind a generic hint -- but a
    // wiped-consent case (needsConsent=true) almost always has real
    // diagnostic detail in res.error explaining WHY it got wiped, which is
    // exactly what's needed to fix it instead of just re-granting on repeat.
    const hint = res.needsConsent
      ? "Open Dromac on the phone and tap \"Allow Screen Mirroring\" once, then try again."
      : "Couldn't start mirroring.";
    mirrorPlaceholder.textContent = hint + (res.error ? ` (${res.error})` : "");
    return;
  }
  armStreamImg(mirrorImg, "/api/screen/stream?t=" + Date.now(), () => {
    mirrorPlaceholder.classList.add("hidden");
    refreshMirrorRects();
  });
}

$("btnMirror").onclick = startMirror;

// ---------- phone audio on the Mac speakers ----------
// Lossless over USB / Wi-Fi, compressed over the Bluetooth link; the server
// follows whichever link is up. The tile lights up while it's on.
const btnSpeaker = $("btnSpeaker");
let speakerOn = false;
let lastSpeakerError = null;
const speakerLabel = btnSpeaker.querySelector(".grid-tile-label");
function renderSpeaker(audio) {
  if (!audio) return;
  speakerOn = !!audio.on;
  btnSpeaker.classList.toggle("btn--active", speakerOn);
  // The tile names the path audio is taking right now (USB, else Wi-Fi).
  speakerLabel.textContent = speakerOn ? (audio.route ? `audio link · ${audio.route}` : audio.waiting ? "audio link · waiting" : "audio link · …") : "audio link";
  btnSpeaker.title = speakerOn
    ? (audio.route
        ? `phone audio playing here over ${audio.route} (${audio.codec === "raw" ? "lossless PCM" : "lossless FLAC"}) — click to stop`
        : "phone audio: waiting for USB or Wi-Fi — click to stop")
    : "audio link is off: everything plays normally. Click to play the phone's audio on this Mac";
  if (audio.error && audio.error !== lastSpeakerError && !audio.error.startsWith("waiting")) showToast(audio.error, "error");
  lastSpeakerError = audio.error;
  if (audio.fx) renderFx(audio.fx);
  if (audio.pos) syncFxPosition(audio.pos);
  renderLock(audio);
}
// ---------- audio effects (8D, slowed + reverb) ----------
// Two switches (8D, slowed + reverb), 8D's speed and path, and a live 3D view.
// The path formulas mirror dromac_fx.py exactly; the view re-syncs to the
// effects process's reported position, so the dot is where you hear it.
const fxPanel = document.querySelector(".fx-panel");
const fx8dButtons = [...document.querySelectorAll("#fx8d .fx-preset")];
const btn8d = $("btn8d");
const btnSlowed = $("btnSlowed");
const fxSpeedEl = $("fxSpeed");
const SPEED_STEPS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
let fxState = { "8d_on": false, "8d": "orbit", speed: 1, slowed: false, mode: "8d", vocals: true, accel: 50 };
// ---------- the mixer: which instruments you hear ----------
const HEAR_PARTS = ["vocals", "drums", "bass", "other"];
const hearRow = $("hearRow");
const hearChips = [...hearRow.querySelectorAll(".hear-chip")];
const hearAll = $("hearAll");
function hearing() {
  const h = { vocals: fxState.vocals ?? true, ...(fxState.hear || {}) };
  return Object.fromEntries(HEAR_PARTS.map((p) => [p, h[p] ?? true]));
}
function setHear(h) {
  fxState.hear = h; fxState.vocals = h.vocals;
  renderHear();
  sendFx({ hear: h });
}
// click: that one on/off (never all off -- that's just silence); double-click:
// only that one (again: everything back)
let hearClickTimer = null;
hearChips.forEach((chip) => {
  const part = chip.dataset.part;
  chip.onclick = () => {
    clearTimeout(hearClickTimer);
    hearClickTimer = setTimeout(() => {
      const h = hearing();
      h[part] = !h[part];
      if (!Object.values(h).some(Boolean)) return;
      setHear(h);
    }, 230);
  };
  chip.ondblclick = () => {
    clearTimeout(hearClickTimer);
    const h = hearing();
    const solo = HEAR_PARTS.every((p) => h[p] === (p === part));
    setHear(Object.fromEntries(HEAR_PARTS.map((p) => [p, solo || p === part])));
  };
});
hearAll.onclick = () => setHear(Object.fromEntries(HEAR_PARTS.map((p) => [p, true])));
function renderHear() {
  const h = hearing();
  hearChips.forEach((chip) => chip.classList.toggle("on", h[chip.dataset.part]));
  hearAll.classList.toggle("show", !Object.values(h).every(Boolean));
  const splitOn = fxState.mode === "dsp" || fxState.mode === "ai";
  hearRow.classList.toggle("nosplit", !splitOn);
  hearRow.title = splitOn ? "" : "no split: you hear the whole song -- leaving an instrument out picks ai split";
}
// ---------- head tracking (the Mac's camera) ----------
const btnHead = $("btnHead");
const btnRecenter = $("btnRecenter");
const headInfo = $("headInfo");
btnHead.onclick = () => sendFx({ head: !fxState.head });
btnRecenter.onclick = () => sendFx({ recenter: true });
function renderHead() {
  btnHead.classList.toggle("on", !!fxState.head);
  btnRecenter.classList.toggle("show", !!fxState.head);
  const h = fxSync && fxSync.head;
  headInfo.classList.toggle("waiting", !!(fxState.head && h && (h.error || !h.face)));
  headInfo.textContent = !fxState.head ? "" : !h || !h.on ? (h && h.error ? h.error : "starting the camera…") :
    h.face ? `· ${Math.round(h.yaw * 180 / Math.PI)}°` : "· can't see your face";
}
const btnKitTight = $("btnKitTight");
const btnKitBoost = $("btnKitBoost");
btnKitTight.onclick = () => sendFx({ kit_tight: !(fxState.kit_tight ?? true) });
btnKitBoost.onclick = () => sendFx({ kit_boost: !(fxState.kit_boost ?? true) });
const btnSelfMix = $("btnSelfMix");
btnSelfMix.onclick = () => sendFx({ self_mix: !(fxState.self_mix ?? true) });
const btnKit = $("btnKit");
btnKit.onclick = () => sendFx({ kit: !fxState.kit });
const btnRoom = $("btnRoom");
btnRoom.onclick = () => sendFx({ room: !(fxState.room ?? true) });
const fxModeButtons = [...document.querySelectorAll("#fxMode .fx-mode-btn")];
fxModeButtons.forEach((b) => { b.onclick = () => sendFx({ mode: b.dataset.mode }); });
const MODE_INFO = {
  "8d": "the whole song travels the path",
  none: "",
  dsp: "vocals · drums · bass · the rest, each in its own place — live",
  ai: "AI-separated instruments, studio quality — plays a few seconds behind",
};
const AI_STATE = { loading: "loading the AI model…", separating: "separating — music starts in a few seconds", playing: "playing behind the phone" };
let heardDelayMs = 0;
let aiState = null;

function renderFx(fx) {
  if (!fx) return;
  fxState = { ...fxState, ...fx };
  btn8d.classList.toggle("on", !!fxState["8d_on"]);
  btnSlowed.classList.toggle("on", !!fxState.slowed);
  fxPanel.classList.toggle("off", !fxState["8d_on"]);
  fx8dButtons.forEach((b) => b.classList.toggle("on", b.dataset.fx8d === fxState["8d"]));
  fxModeButtons.forEach((b) => b.classList.toggle("on", b.dataset.mode === (fxState.mode || "8d")));
  // vocals and the split work with or without 8D (vocals off picks ai split)
  const splitOn = fxState.mode === "dsp" || fxState.mode === "ai";
  renderHear();
  renderHead();
  renderRain();
  btnRoom.classList.toggle("on", fxState.room ?? true);
  btnKit.classList.toggle("on", !!fxState.kit);
  btnSelfMix.classList.toggle("on", fxState.self_mix ?? true);
  btnSelfMix.style.display = fxState["8d"] === "auto" && fxState["8d_on"] ? "" : "none";
  $("kitOpts").classList.toggle("hidden", !(fxState.kit && fxState.mode === "ai"));
  btnKitTight.classList.toggle("on", fxState.kit_tight ?? true);
  btnKitBoost.classList.toggle("on", fxState.kit_boost ?? true);
  btnKit.classList.toggle("disabled", fxState.mode !== "ai");
  btnRoom.classList.toggle("disabled", !fxState["8d_on"] || fxState["8d"] === "still");
  renderAccel();
  if (document.activeElement !== bassSlider) bassSlider.value = fxState.bass ?? 0;
  showBass(Number(bassSlider.value));
  const mode = fxState.mode || "8d";
  $("fxModeInfo").textContent = !(fxState["8d_on"] || splitOn) ? "" : (mode === "ai" && aiState
    ? (aiState === "playing" && heardDelayMs ? `playing ${(heardDelayMs / 1000).toFixed(1)} s behind the phone` : AI_STATE[aiState])
    : MODE_INFO[mode]);
  fxSpeedEl.textContent = `${fxState.speed}×`;
  // the live view has its own pane; dimmed while 8D is off
  $("fxViewWrap").classList.toggle("off", !fxState["8d_on"]);
  if (fxState["8d"] === "auto" && fxState["8d_on"]) pollAutoPositions();
  const label = (fx8dButtons.find((b) => b.dataset.fx8d === fxState["8d"]) || {}).title || "";
  setCaption(`8d · ${fxState["8d"]} · ${fxState.speed}×${label ? " — " + label : ""}`);
}
async function sendFx(change) {
  renderFx({ ...fxState, ...change, ...(change["8d"] ? { "8d_on": true } : {}) }); // instant; server confirms
  const res = await post("/api/audio/fx", change);
  if (res && res.audio) { renderFx(res.audio.fx); renderSpeaker(res.audio); }
}
fx8dButtons.forEach((b) => { b.onclick = () => sendFx({ "8d": b.dataset.fx8d }); });
btn8d.onclick = () => sendFx({ "8d_on": !fxState["8d_on"] });
btnSlowed.onclick = () => sendFx({ slowed: !fxState.slowed });
function stepSpeed(dir) {
  const i = SPEED_STEPS.findIndex((v) => v >= fxState.speed - 1e-6);
  const next = SPEED_STEPS[Math.min(SPEED_STEPS.length - 1, Math.max(0, (i < 0 ? 3 : i) + dir))];
  sendFx({ speed: next });
}
$("fxSlower").onclick = () => stepSpeed(-1);
$("fxFaster").onclick = () => stepSpeed(1);

// ---------- ai acceleration (GPU share -> how far behind ai split plays) ----------
const accelRow = $("accelRow");
const accelSlider = $("accelSlider");
const accelInfo = $("accelInfo");
let accelSending = null;
let aiTargetMs = null;
function renderAccel() {
  accelRow.classList.toggle("hidden", fxState.mode !== "ai");
  const v = Number(fxState.accel ?? 50);
  if (document.activeElement !== accelSlider) accelSlider.value = Math.min(80, v);
  showAccelInfo(Number(accelSlider.value));
}
function showAccelInfo(v) {
  const ultra = v >= 80;
  accelRow.classList.toggle("ultra", ultra);
  // the delay the AI is actually aiming for, once it reports one; else an estimate
  const est = aiTargetMs != null && Number(fxState.accel ?? 50) === v ? aiTargetMs / 1000 : (0.5 / (v / 100)) + 1 + 0.85;
  const behind = `~${est.toFixed(1)} s behind`;
  if (ultra) {
    accelInfo.innerHTML = '<span class="ultramuse">' + [..."ultramuse"].map((ch, i) =>
      `<span style="animation-delay:${-i * 0.12}s,${-i * 0.1}s">${ch}</span>`).join("") + "</span>";
    accelInfo.title = `80% GPU · ${behind}`;
  } else {
    accelInfo.textContent = `${v}% gpu · ${behind}`;
    accelInfo.title = "";
  }
}
accelSlider.oninput = () => {
  const v = Number(accelSlider.value);
  showAccelInfo(v);
  clearTimeout(accelSending);
  accelSending = setTimeout(() => sendFx({ accel: v }), 150);
};
// The 3D view's caption: one line (the full text on hover), and only touched
// when it actually changes -- not 40 times a second
function setCaption(text) {
  const el = $("fxViewCaption");
  if (el.textContent !== text) { el.textContent = text; el.title = text; }
}

// ---------- ambience: rain and thunder ----------
// lightning: while rain's on, a light check 4x a second for a new strike; the
// 3D view flashes the moment one's seen (its thunder is still seconds away)
let flashStart = null, lastStrike = 0;
setInterval(async () => {
  const r = fxState.rain || {};
  if (!r.on || r.paused || document.hidden) return;
  try {
    const pos = await (await fetch("/api/audio/pos")).json();
    if (pos.flash_ago == null) return;
    const strike = pos.t - pos.flash_ago;
    if (strike > lastStrike + 1 && Date.now() / 1000 - strike < 3) flashStart = performance.now();
    lastStrike = Math.max(lastStrike, strike);
  } catch (e) {}
}, 250);
const btnRain = $("btnRain");
const rainVolume = $("rainVolume");
const rainVolumeInfo = $("rainVolumeInfo");
const rainPreset = $("rainPreset");
let rainSending = null;
const thunderLevel = $("thunderLevel");
const thunderLevelInfo = $("thunderLevelInfo");
let thunderSending = null;
const dbText = (v) => (v > 0 ? `+${v}` : `${v}`) + " dB";
function rainState() { return { on: false, preset: "storm cycle", volume: 35, thunder: 0, ...(fxState.rain || {}) }; }
function renderRain() {
  const r = rainState();
  btnRain.classList.toggle("on", !!r.on && !r.paused);
  btnRain.classList.toggle("paused", !!r.on && !!r.paused);
  btnRain.title = r.on && r.paused ? "rain paused (play/pause brings it back, then the music)" : "rain and thunder, made live -- it never repeats";
  document.querySelector(".ambience-card").classList.toggle("off", !r.on);
  if (document.activeElement !== rainVolume) rainVolume.value = r.volume;
  rainVolumeInfo.textContent = `${rainVolume.value}%`;
  if (document.activeElement !== thunderLevel) thunderLevel.value = r.thunder;
  thunderLevelInfo.textContent = dbText(Number(thunderLevel.value));
  if (document.activeElement !== rainPreset) rainPreset.value = r.preset;
}
function sendRain(change) {
  fxState.rain = { ...rainState(), ...change };
  renderRain();
  sendFx({ rain: change });
}
btnRain.onclick = () => sendRain({ on: !rainState().on });
rainPreset.onchange = () => sendRain({ preset: rainPreset.value, on: true });
thunderLevel.oninput = () => {
  thunderLevelInfo.textContent = dbText(Number(thunderLevel.value));
  clearTimeout(thunderSending);
  thunderSending = setTimeout(() => sendRain({ thunder: Number(thunderLevel.value) }), 120);
};
rainVolume.oninput = () => {
  rainVolumeInfo.textContent = `${rainVolume.value}%`;
  clearTimeout(rainSending);
  rainSending = setTimeout(() => sendRain({ volume: Number(rainVolume.value) }), 120);
};

// ---------- bass boost (0 to +12 dB under 100 Hz) ----------
const bassSlider = $("bassSlider");
const bassInfo = $("bassInfo");
let bassSending = null;
function showBass(v) { bassInfo.textContent = v ? `+${v} dB` : "off"; }
bassSlider.oninput = () => {
  const v = Number(bassSlider.value);
  showBass(v);
  clearTimeout(bassSending);
  bassSending = setTimeout(() => sendFx({ bass: v }), 120);
};

// x = right, y = in front, z = up (same as dromac_fx.py)
const LAP_SECONDS = 8;
const FX_PATHS = {
  orbit: (u) => [Math.sin(u), Math.cos(u), 0],
  helix: (u) => [Math.sin(u), Math.cos(u), 0.85 * Math.sin(u / 4)],
  loop: (u) => [Math.sin(u), 0.2, Math.cos(u)],
  infinity: (u) => [1.1 * Math.sin(u), 0.8 * Math.sin(2 * u), 0.75 * Math.cos(u) ** 2],
  saddle: (u) => [Math.sin(u), Math.cos(u), 0.65 * Math.cos(2 * u)],
  knot: (u) => [0.45 * (Math.sin(u) + 2 * Math.sin(2 * u)), 0.45 * (Math.cos(u) - 2 * Math.cos(2 * u)), -0.55 * Math.sin(3 * u)],
  lissajous: (u) => { const v = u / 3; return [1.2 * Math.cos(3 * v + 0.3), 1.2 * Math.cos(2 * v + 1.1), 0.7 * Math.cos(5 * v + 2.0)]; },
  breathe: (u) => { const r = 0.45 + 1.6 * (1 - Math.cos(u / 3)) / 2; return [r * Math.sin(u), r * Math.cos(u), 0.25 * Math.sin(u / 2)]; },
  gyro: (u) => {
    const t = 55 * Math.PI / 180, x0 = Math.cos(u), y0 = Math.sin(u) * Math.cos(t), z0 = Math.sin(u) * Math.sin(t), p = u * 0.17;
    return [x0 * Math.cos(p) - y0 * Math.sin(p), x0 * Math.sin(p) + y0 * Math.cos(p), z0];
  },
  swing: (u) => { const a = (Math.PI / 2) * Math.sin(u); return [Math.sin(a), Math.cos(a), 0.1 * Math.sin(2 * u)]; },
  // near & far
  comet: (u) => {
    const e = 0.9, a = 3.2, M = ((u % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
    let E = M + e * Math.sin(M);
    for (let k = 0; k < 8; k++) E -= (E - e * Math.sin(E) - M) / (1 - e * Math.cos(E));
    const px = a * (Math.cos(E) - e), py = a * Math.sqrt(1 - e * e) * Math.sin(E), t = 25 * Math.PI / 180;
    return [py, -px * Math.cos(t), px * Math.sin(t)];
  },
  golden: (u) => {
    const PHI = (1 + Math.sqrt(5)) / 2, lo = Math.log(0.2), hi = Math.log(6);
    const lr = lo + (hi - lo) * (1 - Math.cos(u / 4)) / 2, th = (lr - lo) / (Math.log(PHI) / (Math.PI / 2)), r = Math.exp(lr);
    return [r * Math.sin(th), r * Math.cos(th), 0.15 * r * Math.sin(u / 3)];
  },
  rose: (u) => { const th = u / 2, r = 0.22 + 3.4 * Math.abs(Math.cos(2.5 * th)); return [r * Math.sin(th), r * Math.cos(th), 0.5 * Math.sin(3 * th)]; },
  torus: (u) => { const ph = u / 3, w = 2.3 + 2.0 * Math.cos(7 * ph); return [w * Math.cos(2 * ph), w * Math.sin(2 * ph), 0.9 * Math.sin(7 * ph)]; },
  spiro: (u) => {
    const R = 5, r = 3, d = 2.2, k = 0.85, th = u * 1.5;
    return [k * ((R - r) * Math.cos(th) + d * Math.cos((R - r) / r * th)), k * ((R - r) * Math.sin(th) - d * Math.sin((R - r) / r * th)), 0.3 * Math.sin(u / 2)];
  },
  pendulum: (u) => { const s = 3 * Math.cos(u), p = 0.12 * u; return [s * Math.cos(p), s * Math.sin(p), 0.35 - 0.05 * s * s]; },
  zoom: (u) => {
    const r = 0.15 + 5.5 * (1 + Math.cos(u)) / 2;
    const az = Math.PI / 2 * Math.sign(Math.sin(u / 2) + 1e-9) * (0.85 + 0.15 * Math.cos(u / 3));
    return [r * Math.sin(az), r * Math.cos(az) * 0.4, 0.2 * Math.sin(u / 5)];
  },
  tide: (u) => { const PHI = (1 + Math.sqrt(5)) / 2, r = 0.3 + 4.7 * (1 - Math.cos(u / (PHI * PHI))) / 2; return [r * Math.sin(u), r * Math.cos(u), 0.6 * Math.sin(u / PHI)]; },
  loxodrome: (u) => {
    const lat = 78 * Math.PI / 180 * Math.cos(u / 6), lon = 3 * Math.log(Math.tan(Math.PI / 4 + lat / 2)), r = 1.6 + 1.2 * Math.sin(u / 5);
    return [r * Math.cos(lat) * Math.sin(lon), r * Math.cos(lat) * Math.cos(lon), r * Math.sin(lat)];
  },
  stage: () => [0, 1.5, 0],
  still: () => [0, 1.0, 0],
  wander: (u) => [
    Math.sin(u * 0.83 + 1.3) * 0.7 + Math.sin(u * 0.31 + 0.2) * 0.6,
    Math.sin(u * 0.57 + 2.1) * 0.8 + Math.cos(u * 0.23) * 0.5,
    Math.sin(u * 0.41 + 0.7) * 0.6,
  ],
};
// phase span of one full figure (for drawing the whole curve faintly)
const FX_PERIOD = { helix: 8 * Math.PI, lissajous: 6 * Math.PI, breathe: 12 * Math.PI, golden: 8 * Math.PI,
  rose: 4 * Math.PI, torus: 6 * Math.PI, spiro: 4 * Math.PI, pendulum: 2 * Math.PI / 0.12, zoom: 12 * Math.PI, loxodrome: 60 * Math.PI };
const FX_OPEN = new Set(["gyro", "wander", "tide", "loxodrome"]);

// "auto": the conductor decides the positions, so the view draws the real ones
// (fetched ~6x a second) and glides between them
let autoPos = null, autoShown = null, autoInfo = null, autoPolling = false;
async function pollAutoPositions() {
  if (autoPolling) return;
  autoPolling = true;
  while (fxState["8d"] === "auto" && fxState["8d_on"]) {
    try {
      const r = await fetch("/api/audio/pos");
      const pos = await r.json();
      if (pos.positions && pos.positions.length) autoPos = pos.positions;
      if (pos.auto) autoInfo = pos.auto;
    } catch (e) {}
    await new Promise((res) => setTimeout(res, 160));
  }
  autoPolling = false;
} // never close: no "whole figure" to draw

const fxView = $("fxView");
const fxCtx = fxView.getContext("2d");
let shownHeadYaw = 0;
let fxPhase = 0, fxLastFrame = performance.now(), fxSync = null, fxPhaseRef = null;
let lorenzState = [1, 1, 20], rosslerState = [1, 1, 0];
const lorenzTrail = [];
function rosslerStep(dt) {
  const d = (s) => [-s[1] - s[2], s[0] + 0.2 * s[1], 0.2 + s[2] * (s[0] - 5.7)];
  const h = dt / 4;
  for (let k = 0; k < 4; k++) {
    const s = rosslerState, k1 = d(s);
    const k2 = d(s.map((v, i) => v + h / 2 * k1[i]));
    const k3 = d(s.map((v, i) => v + h / 2 * k2[i]));
    const k4 = d(s.map((v, i) => v + h * k3[i]));
    rosslerState = s.map((v, i) => v + h / 6 * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]));
  }
  const [x, y, z] = rosslerState;
  return [x / 2.6, y / 2.6, z / 7 - 0.6];
}
function lorenzStep(dt) {
  const d = (s) => [10 * (s[1] - s[0]), s[0] * (28 - s[2]) - s[1], s[0] * s[1] - (8 / 3) * s[2]];
  const h = dt / 4;
  for (let k = 0; k < 4; k++) {
    const s = lorenzState, k1 = d(s);
    const k2 = d(s.map((v, i) => v + h / 2 * k1[i]));
    const k3 = d(s.map((v, i) => v + h / 2 * k2[i]));
    const k4 = d(s.map((v, i) => v + h * k3[i]));
    lorenzState = s.map((v, i) => v + h / 6 * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]));
  }
  const [x, y, z] = lorenzState;
  return [x / 15, (z - 25) / 13, y / 20];
}
// Each instrument's own way along the path (same as STEM_SPEED/STEM_PHASE in
// dromac_fx.py): its own speed and starting point; for the attractors, its own
// chaotic system, synced from the effects process's report.
// (then, with the drum kit split: snare, toms, hihat, cymbals)
const STEM_SPEED = [1.0, 1.17, 0.83, 1.29, 1.29, 1.41, 0.71, 1.53, 0.91];
const STEM_PHASE = [0, Math.PI / 2, Math.PI, 3 * Math.PI / 2, 3 * Math.PI / 2 + Math.PI,
  Math.PI / 4, 5 * Math.PI / 4, 3 * Math.PI / 4, 7 * Math.PI / 4];
// every instrument's dot colour; the kit in warm shades of the drums' orange
const STEM_COLORS = { drums: "#ffb347", bass: "#5ab0ff", other: "#c58bff", "other ": "#c58bff",
  kick: "#ff8c42", snare: "#ffd166", toms: "#f4a261", hihat: "#fff3b0", cymbals: "#ffe08a" };
// The split kit stays one kit (same as kit_around in dromac_fx.py): its pieces
// around the drums' spot, offsets (right, away, up) in metres at 1.5 m.
const KIT_OFFSETS = { kick: [0, 0, -0.3], snare: [-0.3, -0.2, 0], toms: [0.35, 0.05, 0.1], hihat: [-0.5, -0.05, 0.25], cymbals: [0.2, 0.15, 0.5] };
function kitAround([cx, cy, cz], piece) {
  const h = Math.hypot(cx, cy);
  const [fx, fy] = h > 0.05 ? [cx / h, cy / h] : [0, 1];
  const [rx, ry] = [fy, -fx];
  const scale = Math.min(2, Math.max(0.6, Math.hypot(cx, cy, cz) / 1.5));
  const [dr, dd, du] = KIT_OFFSETS[piece];
  return [cx + (rx * dr + fx * dd) * scale, cy + (ry * dr + fy * dd) * scale, cz + du * scale];
}
// the names the effects process reports ("other_l"...) -> the labels shown
const stemLabel = (n) => n === "other_l" ? "other" : n === "other_r" ? "other " : n;
let stemStates = { lorenz: [], rossler: [] };      // integrated here, every frame
let stemReported = { lorenz: null, rossler: null, t: 0 };
function attractorStep(kind, s, dt) {
  const d = kind === "lorenz"
    ? (v) => [10 * (v[1] - v[0]), v[0] * (28 - v[2]) - v[1], v[0] * v[1] - (8 / 3) * v[2]]
    : (v) => [-v[1] - v[2], v[0] + 0.2 * v[1], 0.2 + v[2] * (v[0] - 5.7)];
  const h = dt / 4;
  for (let i = 0; i < 4; i++) {
    const k1 = d(s), k2 = d(s.map((v, j) => v + h / 2 * k1[j])), k3 = d(s.map((v, j) => v + h / 2 * k2[j])), k4 = d(s.map((v, j) => v + h * k3[j]));
    s = s.map((v, j) => v + h / 6 * (k1[j] + 2 * k2[j] + 2 * k3[j] + k4[j]));
  }
  return s;
}
// each frame: advance every instrument's system, and pull it gently toward the
// reported one (carried forward to now) -- smooth, and it stays with the sound
function advanceStemAttractors(kind, dt, speed) {
  const rate = kind === "lorenz" ? 0.18 : 0.9;
  const rep = stemReported[kind];
  if (!stemStates[kind].length && rep) stemStates[kind] = rep.map((v) => v.slice());
  stemStates[kind] = stemStates[kind].map((st, k) => {
    let next = attractorStep(kind, st, rate * speed * STEM_SPEED[k] * dt);
    if (rep && rep[k]) {
      const age = Math.min(3, Math.max(0, Date.now() / 1000 - stemReported.t));
      const there = attractorStep(kind, rep[k], rate * speed * STEM_SPEED[k] * age);
      next = next.map((v, j) => v + (there[j] - v) * Math.min(1, dt * 1.5));
    }
    return next;
  });
}
function stemPosition(preset, k, fallback) {
  if (preset === "lorenz" || preset === "rossler") {
    const st = stemStates[preset][k];
    if (!st) return fallback;
    return preset === "lorenz" ? [st[0] / 15, (st[2] - 25) / 13, st[1] / 20] : [st[0] / 2.6, st[1] / 2.6, st[2] / 7 - 0.6];
  }
  const f = FX_PATHS[preset];
  return f ? f(fxPhase * STEM_SPEED[k] + STEM_PHASE[k]) : fallback;
}

function syncFxPosition(pos) {
  if (pos.stems_lorenz && pos.stems_lorenz.length) { stemReported.lorenz = pos.stems_lorenz; stemReported.t = pos.t; }
  if (pos.stems_rossler && pos.stems_rossler.length) { stemReported.rossler = pos.stems_rossler; stemReported.t = pos.t; }
  if (!pos) return;
  fxSync = pos;
  renderHead();
  const newDelay = Math.round((pos.delay_ms || 0) / 100) * 100;
  if (pos.ai !== aiState || newDelay !== heardDelayMs) { aiState = pos.ai; heardDelayMs = newDelay; renderFx(fxState); }
  if (pos.ai_target_ms != null && pos.ai_target_ms !== aiTargetMs) { aiTargetMs = pos.ai_target_ms; renderAccel(); }
  // Where the sound really is (the reported phase, carried forward to now).
  // Not snapped to: drawFxView eases toward it, or a 2-second correction
  // shows as a lurch -- multiplied by each instrument's own speed.
  fxPhaseRef = { u: pos.u, t: pos.t, speed: pos.speed || 1 };
  const ageSec = Math.max(0, Date.now() / 1000 - pos.t);
  const target = pos.u + (2 * Math.PI / LAP_SECONDS) * (pos.speed || 1) * ageSec;
  if (Math.abs(target - fxPhase) > Math.PI) fxPhase = target;   // a big jump (new preset): go there
  if (pos.lorenz && fxState["8d"] === "lorenz") lorenzState = pos.lorenz.slice();
  if (pos.rossler && fxState["8d"] === "rossler") rosslerState = pos.rossler.slice();
}
// The view redraws at most FX_FPS times a second: the motion is eased, so 40
// looks as smooth as the display's 120 -- at a third of the drawing (it was
// the biggest heat source of all of Dromac, ahead of the audio and the AI).
const FX_FPS = 40;
// colours and fonts from the stylesheet, re-read once a second, not every frame
let fxStyleCache = { t: -1e9 };
function fxStyles(now) {
  if (now - fxStyleCache.t > 1000) {
    fxStyleCache = {
      t: now,
      accent: getComputedStyle(document.documentElement).getPropertyValue("--accent1").trim() || "#33ff99",
      mono: getComputedStyle(document.body).getPropertyValue("--font-mono") || "monospace",
    };
  }
  return fxStyleCache;
}
function drawFxView(now) {
  if (now - fxLastFrame < 1000 / FX_FPS - 2) { requestAnimationFrame(drawFxView); return; }
  const dt = Math.min(0.1, (now - fxLastFrame) / 1000);
  fxLastFrame = now;
  const dpr = window.devicePixelRatio || 1;
  const W = fxView.clientWidth, H = fxView.clientHeight;
  if (!W || !H) { requestAnimationFrame(drawFxView); return; } // hidden: nothing to draw
  if (fxView.width !== Math.round(W * dpr) || fxView.height !== Math.round(H * dpr)) { fxView.width = Math.round(W * dpr); fxView.height = Math.round(H * dpr); }
  const c = fxCtx;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const accent = fxStyles(now).accent;
  const preset = fxState["8d"];
  // lightning (rain on): the view lights up -- a flash and a flicker -- and the
  // thunder follows a few seconds later, as far off as the strike was
  if (flashStart != null) {
    const ago = (now - flashStart) / 1000;
    if (ago >= 0.45) flashStart = null;
    else {
      const flick = ago < 0.08 ? 1 : ago < 0.14 ? 0.25 : ago < 0.22 ? 0.8 : Math.max(0, 1 - (ago - 0.22) / 0.23) * 0.5;
      c.fillStyle = `rgba(220, 230, 255, ${0.35 * flick})`;
      c.fillRect(0, 0, W, H);
    }
  }
  const speed = fxState.speed || 1;
  const running = fxState["8d_on"] && speakerOn;
  if (running) {
    fxPhase += (2 * Math.PI / LAP_SECONDS) * speed * dt;
    // ease toward the true position (~0.5 s), so corrections never jerk
    if (fxPhaseRef) {
      const target = fxPhaseRef.u + (2 * Math.PI / LAP_SECONDS) * fxPhaseRef.speed * Math.max(0, Date.now() / 1000 - fxPhaseRef.t);
      fxPhase += (target - fxPhase) * Math.min(1, dt * 2);
    }
  }

  // camera: fixed, just behind and above you, facing the way you face -- so
  // your head stays still (nose pointing into the screen) and only the sound moves
  const yaw = 0, pitch = 0.62;
  // fit the figure: the near & far paths reach ~6 m, the others ~1.5 m
  const reach = { auto: 2.4, stage: 3.6, comet: 6.2, golden: 6.2, rose: 3.8, torus: 4.4, spiro: 3.8, pendulum: 3.1, zoom: 5.6, tide: 5.2, loxodrome: 2.9, rossler: 4.2, lorenz: 2.2 }[preset] || 1.6;
  // perspective (the camera never moves): far things smaller and dimmer, near
  // things bigger; every sound stands on a stem over its shadow on the
  // ear-level floor, so its height and where it is on the floor both read
  const cam = reach * 3;
  const scale = Math.min(W / 2, H * 0.62) / (reach * 1.2);
  const cx = W / 2, cy = H / 2 + 2;
  const project = ([x, y, z]) => {
    const X = x * Math.cos(yaw) - y * Math.sin(yaw);
    const D = x * Math.sin(yaw) + y * Math.cos(yaw);
    const depth = D * Math.cos(pitch) - z * Math.sin(pitch);
    const k = cam / Math.max(cam * 0.4, cam + depth);
    return [cx + scale * k * X, cy - scale * k * (z * Math.cos(pitch) + D * Math.sin(pitch)), depth, k];
  };
  const fade = (depth) => Math.max(0.4, Math.min(1, 1 - depth / (reach * 2.4)));
  // the floor at ear level: distance rings (1 m, then out to the path's
  // reach) and the front/back, left/right lines through you
  c.lineWidth = 1;
  const rings = [1];
  for (let r = 2; r <= reach * 1.1; r *= 2) rings.push(r);
  if (reach > 1.6 && rings[rings.length - 1] < reach * 0.8) rings.push(Math.round(reach));
  rings.forEach((r) => {
    c.strokeStyle = r === 1 ? "rgba(255,255,255,0.13)" : "rgba(255,255,255,0.06)";
    c.beginPath();
    for (let a = 0; a <= 96; a++) {
      const [px, py] = project([r * Math.cos(a / 96 * 2 * Math.PI), r * Math.sin(a / 96 * 2 * Math.PI), 0]);
      a ? c.lineTo(px, py) : c.moveTo(px, py);
    }
    c.stroke();
  });
  const edge = rings[rings.length - 1];
  c.strokeStyle = "rgba(255,255,255,0.06)";
  [[[-edge, 0, 0], [edge, 0, 0]], [[0, -edge, 0], [0, edge, 0]]].forEach(([a, b]) => {
    const [ax, ay] = project(a), [bx, by] = project(b);
    c.beginPath(); c.moveTo(ax, ay); c.lineTo(bx, by); c.stroke();
  });
  const monoFont = fxStyles(now).mono;
  c.font = "9px " + monoFont;
  c.fillStyle = "rgba(255,255,255,0.28)";
  c.textAlign = "center";
  [["front", [0, edge, 0], -4], ["back", [0, -edge, 0], 11], ["L", [-edge, 0, 0], -4], ["R", [edge, 0, 0], -4]].forEach(([t, p, dy]) => {
    const [px, py] = project(p);
    c.fillText(t, px + (t === "L" ? -8 : t === "R" ? 8 : 0), py + dy);
  });
  c.textAlign = "left";
  // the listener: a head (with ears) and a nose pointing to the front
  const [hx, hy, , hk] = project([0, 0, 0]);
  // the head turns with yours (head tracking)
  const headYaw = (fxSync && fxSync.head && fxSync.head.on) ? fxSync.head.yaw : 0;
  shownHeadYaw += (headYaw - shownHeadYaw) * Math.min(1, dt * 10);
  const turned = ([x, y, z]) => [x * Math.cos(shownHeadYaw) - y * Math.sin(shownHeadYaw), x * Math.sin(shownHeadYaw) + y * Math.cos(shownHeadYaw), z];
  const [nx, ny] = project(turned([0, 0.3, 0]));
  c.fillStyle = "rgba(255,255,255,0.08)";
  c.beginPath(); c.ellipse(hx, hy, 9 * hk, 9 * hk * Math.sin(pitch), 0, 0, 2 * Math.PI); c.fill();
  c.fillStyle = "rgba(255,255,255,0.75)";
  [-0.13, 0.13].forEach((ex) => { const [ax, ay] = project(turned([ex, 0, 0])); c.beginPath(); c.arc(ax, ay, 1.8, 0, 2 * Math.PI); c.fill(); });
  c.beginPath(); c.arc(hx, hy, 4.5, 0, 2 * Math.PI); c.fill();
  c.strokeStyle = "rgba(255,255,255,0.75)";
  c.beginPath(); c.moveTo(hx, hy); c.lineTo(nx, ny); c.stroke();
  // a sound in 3D: the stem down (or, below ear level, dashed up) to its
  // shadow on the floor, then the dot -- farther ones first, sized by depth
  const drawSources = (list) => {
    // what you're not hearing stays on the map, greyed out: the singer with
    // vocals off; everything but the drums with drums only
    const PART_OF = { vocals: "vocals", drums: "drums", kick: "drums", snare: "drums", toms: "drums",
      hihat: "drums", cymbals: "drums", bass: "bass", other: "other" };
    const h = hearing();
    const silent = (label) => list.length > 1 && label && PART_OF[label] && !h[PART_OF[label]];
    // auto: the part in the spotlight gets a ring
    const spotPart = fxState["8d"] === "auto" && autoInfo ? autoInfo.spotlight : null;
    // (one ring per part: the kit's or the band's first dot, not all of them)
    let ringed = false;
    const spotlit = (label) => {
      if (ringed || !spotPart || !label || PART_OF[label] !== spotPart) return false;
      return (ringed = true);
    };
    const items = list.map((s) => ({ ...s, ...(silent(s.label) ? { color: "#6b6b6b", dim: 0.45 } : {}),
      q: project(s.p), g: project([s.p[0], s.p[1], 0]) }))
      .sort((a, b) => b.q[2] - a.q[2]);
    for (const s of items) {
      const f = fade(s.q[2]) * (s.dim || 1);
      c.globalAlpha = 0.45 * f;
      c.strokeStyle = s.color;
      c.setLineDash(s.p[2] < 0 ? [2, 3] : []);
      c.beginPath(); c.moveTo(s.g[0], s.g[1]); c.lineTo(s.q[0], s.q[1]); c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 0.3 * f;
      c.fillStyle = s.color;
      c.beginPath(); c.ellipse(s.g[0], s.g[1], 5 * s.g[3], 5 * s.g[3] * Math.sin(pitch), 0, 0, 2 * Math.PI); c.fill();
    }
    for (const s of items) {
      const f = fade(s.q[2]) * (s.dim || 1);
      // the glow: a soft halo behind the dot (a canvas blur on every dot,
      // every frame, cost more than everything else in the view)
      c.fillStyle = s.color;
      c.globalAlpha = 0.22 * f;
      c.beginPath(); c.arc(s.q[0], s.q[1], s.r * s.q[3] * 1.45, 0, 2 * Math.PI); c.fill();
      c.globalAlpha = f;
      c.beginPath(); c.arc(s.q[0], s.q[1], s.r * s.q[3], 0, 2 * Math.PI); c.fill();
      if (spotlit(s.label) && !s.dim) {
        c.strokeStyle = s.color; c.lineWidth = 1.2;
        c.beginPath(); c.arc(s.q[0], s.q[1], s.r * s.q[3] + 4, 0, 2 * Math.PI); c.stroke();
        c.lineWidth = 1;
      }
      if (s.label) {
        c.font = "10px " + monoFont;
        c.fillStyle = s.dim ? "rgba(255,255,255,0.4)" : "rgba(255,255,255,0.75)";
        c.fillText(s.dim ? s.label + " (off)" : s.label, s.q[0] + s.r * s.q[3] + 3, s.q[1] + 3);
      }
    }
    c.globalAlpha = 1;
  };

  // the path: the whole figure faintly, a fading trail, and the sound itself
  let trail = [], here;
  if (preset === "lorenz" || preset === "rossler") {
    const isL = preset === "lorenz";
    if (running) here = isL ? lorenzStep(0.18 * speed * dt) : rosslerStep(0.9 * speed * dt);
    else if (isL) { const [x, y, z] = lorenzState; here = [x / 15, (z - 25) / 13, y / 20]; }
    else { const [x, y, z] = rosslerState; here = [x / 2.6, y / 2.6, z / 7 - 0.6]; }
    if (lorenzTrail.preset !== preset) { lorenzTrail.length = 0; lorenzTrail.preset = preset; }
    lorenzTrail.push(here);
    if (lorenzTrail.length > 900) lorenzTrail.shift();
    trail = lorenzTrail;
  } else if (FX_PATHS[preset]) {
    const f = FX_PATHS[preset];
    const span = FX_PERIOD[preset] || 2 * Math.PI;
    if (!FX_OPEN.has(preset)) {
      c.strokeStyle = "rgba(255,255,255,0.10)";
      c.beginPath();
      for (let k = 0; k <= 400; k++) {
        const [px, py] = project(f(fxPhase + (k / 400) * span));
        k ? c.lineTo(px, py) : c.moveTo(px, py);
      }
      c.stroke();
    }
    for (let k = 160; k >= 0; k--) trail.push(f(fxPhase - (k / 160) * 1.5 * Math.PI));
    here = f(fxPhase);
  }
  if (preset === "auto") {
    if (autoPos) {
      if (!autoShown || autoShown.length !== autoPos.length) autoShown = autoPos.map((p) => p.slice());
      autoShown = autoShown.map((p, k) => p.map((v, j) => v + (autoPos[k][j] - v) * Math.min(1, dt * 6)));
      const reported = (fxSync && fxSync.names || []).map(stemLabel);
      const names = reported.length === autoShown.length ? reported :
        autoShown.length === 5 ? ["vocals", "drums", "bass", "other", "other "] : ["song"];
      const colors = { ...STEM_COLORS, vocals: accent, song: accent };
      drawSources(autoShown.map((p, k) => ({ p, color: colors[names[k]], r: 4.8, label: names.length > 1 ? names[k].trim() : "" })));
    }
    if (autoInfo) {
      const learned = autoInfo.states ? " · " + Array.from({length: autoInfo.states}, (_, i) => i + 1 === autoInfo.state ? "●" : "○").join("") : (autoInfo.listening ? " · learning the song…" : "");
      // where it is in the bar, what's in the spotlight, and what's coming
      const beatDots = autoInfo.beat ? " · " + [1, 2, 3, 4].map((b) => b === autoInfo.beat ? "▮" : "▯").join("") : "";
      const coming = autoInfo.drop_in != null ? ` · drop in ${autoInfo.drop_in.toFixed(1)} s` : "";
      const spot = autoInfo.spotlight ? ` · spotlight: ${autoInfo.spotlight}` : "";
      // what the engineer (self-mix) is doing to the sound right now
      const eng = autoInfo.engineer && autoInfo.engineer.on && autoInfo.engineer.doing ? ` · ${autoInfo.engineer.doing}` : "";
      setCaption(`auto · ${autoInfo.bpm} bpm · ${autoInfo.chord}${learned}${beatDots}${spot}${coming}${eng}`);
    }
    requestAnimationFrame(drawFxView);
    return;
  }
  // split modes: one dot per instrument -- on the stage, or chasing each other
  // around the path a quarter turn apart (same layout as dromac_fx.py)
  const split = fxState["8d_on"] && (fxState.mode === "dsp" || fxState.mode === "ai");
  if (split && running && (preset === "lorenz" || preset === "rossler")) advanceStemAttractors(preset, dt, speed);
  if (split && here) {
    const STAGE = { vocals: [0, 1.5, 0.15], drums: [0, 3.2, 0.6], bass: [0.9, 1.1, -0.35], "other": [-2.2, 1.6, 0.2], "other ": [2.2, 1.6, 0.2],
      kick: [0, 2.6, -0.5], snare: [-0.7, 2.2, 0.3], toms: [1.3, 2.6, 0.2], hihat: [-1.4, 1.6, 0.5], cymbals: [0.4, 3.4, 1.5] };
    const COLORS = { ...STEM_COLORS, vocals: accent };
    // which instruments are playing (the kit, once it's split), as reported
    const reported = (fxSync && fxSync.names || []).map(stemLabel);
    const names = reported.length === 9 ? reported : ["vocals", "drums", "bass", "other", "other "];
    const kitCenter = preset === "stage" ? STAGE.drums : stemPosition(preset, 1, here);
    const tight = fxState.kit_tight ?? true;
    drawSources(names.map((name, k) => ({
      p: tight && KIT_OFFSETS[name] ? kitAround(kitCenter, name) : preset === "stage" ? STAGE[name] : stemPosition(preset, k, here),
      color: COLORS[name], r: 4.5, label: name.trim(),
    })));
    requestAnimationFrame(drawFxView);
    return;
  }
  for (let k = 1; k < trail.length; k++) {
    const [ax, ay, ad, ak] = project(trail[k - 1]), [bx, by] = project(trail[k]);
    c.globalAlpha = (k / trail.length) ** 1.6 * 0.9 * fade(ad);
    c.strokeStyle = accent;
    c.lineWidth = 1.6 * ak;
    c.beginPath(); c.moveTo(ax, ay); c.lineTo(bx, by); c.stroke();
  }
  c.globalAlpha = 1;
  c.lineWidth = 1;
  if (here) drawSources([{ p: here, color: accent, r: 5.5 }]);
  requestAnimationFrame(drawFxView);
}
requestAnimationFrame(drawFxView);

// ---------- headphones only ----------
const btnHeadLock = $("btnHeadLock");
const lockInfo = $("lockInfo");
let lockState = { on: false, device: null };
function renderLock(audio) {
  if (!audio || !audio.lock) return;
  lockState = audio.lock;
  btnHeadLock.classList.toggle("on", !!lockState.on);
  const waiting = lockState.on && audio.on && audio.waiting;
  lockInfo.classList.toggle("waiting", !!waiting);
  lockInfo.textContent = !lockState.on ? "" : waiting ? audio.waiting : `· ${lockState.device}`;
}
btnHeadLock.onclick = async () => {
  const res = await post("/api/audio/lock", { on: !lockState.on });
  if (res && res.error) showToast(res.error, "error");
  if (res && res.audio) renderLock({ ...res.audio, on: speakerOn });
};

btnSpeaker.onclick = async () => {
  // the audio link: off = back to normal on both devices (see server set_link)
  const res = await post("/api/audio/link", { on: !speakerOn });
  if (res && res.audio) renderSpeaker(res.audio);
};
// Closing/stopping from the Mac only detaches the VIEW -- it deliberately does
// NOT tell the phone to end the capture session. Android only allows a screen-
// capture grant to be used to create ONE VirtualDisplay, ever, even on the same
// still-alive MediaProjection instance (confirmed by the exact SecurityException
// Android throws on reuse) -- so actually ending the session would force a brand
// new consent dialog on the phone next time. Leaving capture running in the
// background costs a little battery but means "remote screen" just works
// instantly every time after the first grant.
function hideMirror() {
  resetStreamImg(mirrorImg);
  mirrorOverlay.classList.add("hidden");
  keyboardWarned = false;
}
$("btnMirrorStop").onclick = hideMirror;
$("btnMirrorClose").onclick = hideMirror;

btnMirrorBlackout.onclick = async () => {
  const res = await post("/api/screen/blackout/toggle");
  btnMirrorBlackout.classList.toggle("btn--active", !!res.on);
  if (!res.ok) showToast(res.error || "Couldn't toggle blackout", "err");
};

// Trackpad-style control, like using the Mac's own trackpad on the phone:
// one-finger movement (plain mousemove) just points -- it repositions a
// reticle showing where a tap would land, with no effect on the phone yet.
// Two-finger movement (the "wheel" event a trackpad scroll gesture fires in
// a browser) is the actual touch -- it drags the real virtual finger on the
// phone starting from wherever the reticle is. A click/tap commits a tap at
// the reticle's current position, exactly like tapping a trackpad.
const mirrorCursor = $("mirrorCursor");
let cursorPos = { x: 0.5, y: 0.5 };
let lastMouseClient = null; // raw viewport coords of the real (now-hidden) pointer

function clamp01(v) { return Math.min(1, Math.max(0, v)); }

// object-fit: contain means the <img> ELEMENT's box and the actual visible
// PIXELS of the image usually aren't the same rectangle -- whenever the
// phone's aspect ratio doesn't exactly match the fixed 9:19.5 box, the real
// image is letterboxed inside it (blank margins on two sides). Normalizing
// against the element's full box instead of the letterboxed content area is
// exactly what was causing clicks to land somewhere other than the reticle.
function computeImageContentRect() {
  const rect = mirrorImg.getBoundingClientRect();
  const naturalW = mirrorImg.naturalWidth || rect.width;
  const naturalH = mirrorImg.naturalHeight || rect.height;
  const elementAspect = rect.width / rect.height;
  const imageAspect = naturalW / naturalH;
  let contentW, contentH, offsetX, offsetY;
  if (imageAspect > elementAspect) {
    contentW = rect.width;
    contentH = rect.width / imageAspect;
    offsetX = 0;
    offsetY = (rect.height - contentH) / 2;
  } else {
    contentH = rect.height;
    contentW = rect.height * imageAspect;
    offsetY = 0;
    offsetX = (rect.width - contentW) / 2;
  }
  return { left: rect.left + offsetX, top: rect.top + offsetY, width: contentW, height: contentH };
}

// getBoundingClientRect() forces a synchronous layout read -- calling it on
// every single mousemove event (which can fire dozens of times a second) was
// the actual source of the reticle feeling laggy, not any CSS animation.
// The rect only changes on resize/image-load, so it's cached and reused for
// the (much higher-frequency) pointer/wheel handlers instead.
let cachedContentRect = null;
let cachedWrapRect = null;
function refreshMirrorRects() {
  cachedContentRect = computeImageContentRect();
  cachedWrapRect = mirrorImg.parentElement.getBoundingClientRect();
}
window.addEventListener("resize", refreshMirrorRects);

function setCursorVisual() {
  const contentRect = cachedContentRect || computeImageContentRect();
  const wrapRect = cachedWrapRect || mirrorImg.parentElement.getBoundingClientRect();
  const px = contentRect.left - wrapRect.left + cursorPos.x * contentRect.width;
  const py = contentRect.top - wrapRect.top + cursorPos.y * contentRect.height;
  mirrorCursor.style.left = px + "px";
  mirrorCursor.style.top = py + "px";
}

mirrorImg.addEventListener("mousemove", (evt) => {
  lastMouseClient = { x: evt.clientX, y: evt.clientY };
  const rect = cachedContentRect || computeImageContentRect();
  cursorPos = {
    x: clamp01((evt.clientX - rect.left) / rect.width),
    y: clamp01((evt.clientY - rect.top) / rect.height),
  };
  setCursorVisual();
});

mirrorImg.addEventListener("click", () => {
  post("/api/screen/tap", { x: cursorPos.x, y: cursorPos.y });
});

// Wheel deltas arrive in a rapid burst during one continuous two-finger
// gesture -- accumulate them and dispatch a single swipe once the burst
// pauses, instead of flooding the phone with a request per tiny tick.
let wheelAccum = { dx: 0, dy: 0 };
let wheelStart = null;
let wheelTimer = null;
const MIRROR_WHEEL_SCALE = 0.0009; // trackpad pixels -> normalized screen fraction
const MIRROR_WHEEL_MAX_STEP = 60; // clamp each tick's raw pixels -- trackpad momentum/inertia can
                                   // spike a single wheel event into the hundreds, which is what was
                                   // sending the cursor flying; capping it keeps motion proportional.

mirrorImg.addEventListener("wheel", (evt) => {
  evt.preventDefault();
  if (!wheelStart) wheelStart = { x: cursorPos.x, y: cursorPos.y };
  const dx = Math.max(-MIRROR_WHEEL_MAX_STEP, Math.min(MIRROR_WHEEL_MAX_STEP, evt.deltaX));
  const dy = Math.max(-MIRROR_WHEEL_MAX_STEP, Math.min(MIRROR_WHEEL_MAX_STEP, evt.deltaY));
  // Inverted: macOS "natural scrolling" reports wheel deltas in the direction
  // content would move, which is the opposite of raw finger travel -- we want
  // the cursor to follow the fingers themselves.
  wheelAccum.dx -= dx * MIRROR_WHEEL_SCALE;
  wheelAccum.dy -= dy * MIRROR_WHEEL_SCALE;
  cursorPos = { x: clamp01(wheelStart.x + wheelAccum.dx), y: clamp01(wheelStart.y + wheelAccum.dy) };
  setCursorVisual();

  clearTimeout(wheelTimer);
  wheelTimer = setTimeout(() => {
    if (wheelStart && (Math.abs(wheelAccum.dx) > 0.002 || Math.abs(wheelAccum.dy) > 0.002)) {
      post("/api/screen/swipe", {
        x1: wheelStart.x, y1: wheelStart.y, x2: cursorPos.x, y2: cursorPos.y, durationMs: 180,
      });
    }
    wheelStart = null;
    wheelAccum = { dx: 0, dy: 0 };
    // Two-finger scrolling never moves the real (now-hidden) mouse pointer,
    // so once the gesture ends, snap the reticle back to wherever that real
    // pointer actually is -- instead of stranding it at the swipe's
    // destination, which is what made it look like it "didn't return".
    if (lastMouseClient) {
      const rect = cachedContentRect || computeImageContentRect();
      cursorPos = {
        x: clamp01((lastMouseClient.x - rect.left) / rect.width),
        y: clamp01((lastMouseClient.y - rect.top) / rect.height),
      };
      setCursorVisual();
    }
  }, 120);
}, { passive: false });

async function navAction(action) {
  const res = await post("/api/screen/nav", { action });
  if (!res.ok) showToast(res.error || "Nav action failed", "err");
}
$("btnNavBack").onclick = () => navAction("back");
$("btnNavHome").onclick = () => navAction("home");
$("btnNavRecents").onclick = () => navAction("recents");

// ---------- remote keyboard ----------
// No visible text box -- while the remote-screen overlay is open, every
// keystroke is forwarded straight to the phone's custom IME as it's typed,
// exactly like the trackpad's tap/swipe (no field to click into first).

let keyboardWarned = false;

async function keyboardCall(path, body) {
  const res = await post(path, body);
  if (!res.ok && !keyboardWarned) {
    keyboardWarned = true;
    showToast(res.error || "Enable Dromac as the active keyboard on the phone first", "err");
  }
  return res;
}

const MIRROR_SPECIAL_KEYS = { Enter: "enter", Backspace: "backspace" };

document.addEventListener("keydown", (evt) => {
  if (mirrorOverlay.classList.contains("hidden")) return;
  // Let real Mac shortcuts (Cmd/Ctrl/Alt combos) through untouched -- only
  // plain typing (Shift included, for capitals/symbols) gets forwarded.
  if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
  const special = MIRROR_SPECIAL_KEYS[evt.key];
  if (special) {
    evt.preventDefault();
    keyboardCall("/api/keyboard/key", { key: special });
    return;
  }
  if (evt.key.length === 1) {
    evt.preventDefault();
    keyboardCall("/api/keyboard/text", { text: evt.key });
  }
});

// ---------- calls ----------

$("btnAnswerCall").onclick = async () => {
  await post("/api/call/answer");
  callBanner.classList.add("hidden");
};

// ---------- app launcher ----------

let appsLoaded = false;
async function loadApps() {
  if (appsLoaded) return;
  const res = await api("/api/apps");
  if (!Array.isArray(res)) return;
  appsLoaded = true;
  appList.innerHTML = "";
  for (const app of res) {
    const opt = document.createElement("option");
    opt.value = app.label;
    opt.dataset.package = app.package;
    appList.appendChild(opt);
  }
}
loadApps();

$("btnLaunchApp").onclick = async () => {
  const input = $("appLaunchInput");
  const label = input.value.trim();
  if (!label) return;
  const match = Array.from(appList.options).find((o) => o.value === label);
  if (!match) {
    showToast("Pick an app from the list", "err");
    return;
  }
  if (!lastKnownOverlayGranted) {
    showToast("Grant \"Display Over Apps\" in Dromac on the phone first", "err");
    return;
  }
  const res = await post("/api/apps/launch", { package: match.dataset.package });
  showToast(res.ok ? `Opening ${label}` : "Couldn't launch that app", res.ok ? "ok" : "err");
  input.value = "";
};

connPill.onclick = () => post("/api/connect/reconnect").then(poll);
$("btnReconnect").onclick = () => post("/api/connect/reconnect").then(poll);

$("btnTogglePair").onclick = () => {
  pairForm.classList.toggle("hidden");
};

$("btnConnect").onclick = async () => {
  const host = $("phoneHost").value.trim();
  const port = $("phonePort").value.trim();
  if (!host) {
    pairStatus.textContent = "Enter the phone's IP address.";
    pairStatus.className = "pair-status err";
    return;
  }
  pairStatus.textContent = "Connecting…";
  pairStatus.className = "pair-status";
  const res = await post("/api/connect/set", { host, port });
  if (res.ok) {
    pairStatus.textContent = "Connected.";
    pairStatus.className = "pair-status ok";
    pairForm.classList.add("hidden");
    poll();
  } else {
    pairStatus.textContent = res.message || "Saved, but couldn't reach it yet.";
    pairStatus.className = "pair-status err";
  }
};

$("btnRing").onclick = () => post("/api/ring");

$("btnClipboard").onclick = () => {
  const input = $("clipboardInput");
  const text = input.value;
  if (!text) return;
  post("/api/clipboard", { text }).then(() => {
    input.value = "";
  });
};

const filedropStatusEl = $("filedropStatus");
const filedropUrlRow = $("filedropUrlRow");
const filedropUrlText = $("filedropUrlText");

function renderFiledrop(status) {
  if (status && status.running && status.url) {
    filedropStatusEl.textContent = status.url;
    filedropUrlText.value = status.url;
    filedropUrlRow.classList.remove("hidden");
  } else {
    filedropStatusEl.textContent = (status && status.error) || "not running";
    filedropUrlRow.classList.add("hidden");
  }
}

async function checkFiledropStatus() {
  try {
    renderFiledrop(await api("/api/filedrop/status"));
  } catch (e) {}
}

$("btnFiledropStart").onclick = async () => {
  filedropStatusEl.textContent = "starting…";
  renderFiledrop(await post("/api/filedrop/start"));
};

$("btnFiledropCopy").onclick = () => {
  if (filedropUrlText.value) navigator.clipboard.writeText(filedropUrlText.value).catch(() => {});
};

$("btnFiledropOpen").onclick = () => post("/api/filedrop/open_window");

checkFiledropStatus();
setInterval(checkFiledropStatus, 15000);

// ---- CodeGate: rooms of per-member workspaces (served by FileDrop) ----

const CG_KINDS = { code: "VS Code", desktop: "Ubuntu desktop" };
const cgOverlay = $("cgOverlay");
const cgBody = $("cgBody");
const cgSummary = $("cgSummary");
const cgUrlRow = $("cgUrlRow");
const cgUrlText = $("cgUrlText");
let cgState = null;
let cgBusy = false;
let cgNotice = "";

function cgTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function cgFmtPin(pin) {
  return pin ? pin.slice(0, 4) + "-" + pin.slice(4) : "";
}

function cgRenderSummary(st) {
  if (!st || st.codegateRunning === false || st.error) {
    cgSummary.textContent = (st && st.error) || (st && st.installed === false
      ? "not installed (github.com/yatharth1011/codegate)"
      : "not running (it starts when you open a room)");
    cgUrlRow.classList.add("hidden");
    document.querySelectorAll(".cg-room-btn").forEach((b) => b.classList.remove("cg-open"));
    return;
  }
  const parts = [];
  for (const [kind, room] of Object.entries(st.rooms)) {
    const btn = document.querySelector(`.cg-room-btn[data-kind="${kind}"]`);
    if (btn) btn.classList.toggle("cg-open", room.open);
    if (room.open) parts.push(`${room.label}: PIN ${cgFmtPin(room.pin)}`);
  }
  const running = st.members.filter((m) => m.running).length;
  cgSummary.textContent = parts.length
    ? `${parts.join(" · ")} · ${running} active`
    : (running ? `${running} active, no room open` : "no room open");
  const showUrl = st.gate && st.gate.running;
  cgUrlRow.classList.toggle("hidden", !showUrl);
  if (showUrl) cgUrlText.value = st.gate.url;
}

function cgRenderManager(st) {
  if (!st || st.codegateRunning === false) {
    cgBody.innerHTML = '<p class="hint">CodeGate isn\'t running. Opening a room from the card starts it.</p>';
    return;
  }
  const rt = st.runtime;
  let html = "";
  if (cgNotice) html += `<div class="cg-sec"><div class="cg-note">${esc(cgNotice)}</div></div>`;

  // Rooms
  html += '<div class="cg-sec"><div class="cg-sec-title">ROOMS</div>';
  for (const [kind, room] of Object.entries(st.rooms)) {
    const built = st.images[kind];
    const live = st.members.some((m) => m.kind === kind && m.running);
    html += `<div class="cg-row"><div><span class="cg-dot ${room.open ? "on" : ""}"></span>${esc(room.label)}
        <div class="dim">${room.members} member${room.members === 1 ? "" : "s"} · up to
        <input class="cg-num" type="number" min="1" max="30" value="${room.maxRunning}" data-act="limit" data-kind="${kind}"> at once</div></div>
      <div class="cg-acts">${
        room.open
          ? `<span class="cg-pin">${esc(cgFmtPin(room.pin))}</span>
             <button class="cg-mini" data-act="copy-pin" data-pin="${esc(room.pin)}">copy</button>
             <button class="cg-mini" data-act="close" data-kind="${kind}" title="No new joins; current members stay">close</button>
             <button class="cg-mini warn" data-act="stop-room" data-kind="${kind}" title="Close and shut down every running ${esc(room.label)} workspace">stop</button>`
          : (built === false
              ? `<button class="cg-mini" data-act="build" data-kind="${kind}">${st.building.includes(kind) ? "building…" : "build image"}</button>`
              : `${live ? `<button class="cg-mini warn" data-act="stop-room" data-kind="${kind}">stop</button>` : ""}
                 <button class="cg-mini" data-act="open" data-kind="${kind}">open room</button>`)
      }</div></div>`;
  }
  html += '<div class="cg-note">A PIN lets a new person join with a name of their choice; closing a room stops new joins but keeps current members. PINs expire after 12 hours.</div></div>';

  // Starters
  html += '<div class="cg-sec"><div class="cg-sec-title">STARTER FILES</div>';
  if (!st.starters.length) html += '<div class="cg-note">None yet. Each member gets their own copy of these when they first open their workspace.</div>';
  for (const a of st.starters) {
    html += `<div class="cg-row"><div>${esc(a.title)} <span class="dim">${esc(a.slug)} · ${a.files} files</span></div>
      <div class="cg-acts"><button class="cg-mini" data-act="collect" data-starter="${esc(a.slug)}" data-id="*">save everyone's</button>
      <button class="cg-mini danger" data-act="del-starter" data-slug="${esc(a.slug)}">remove</button></div></div>`;
  }
  html += '<div class="cg-row"><span></span><button class="cg-mini" data-act="add-starter">+ add from folder…</button></div></div>';

  // Members
  html += '<div class="cg-sec"><div class="cg-sec-title">MEMBERS</div>';
  if (!st.members.length) html += '<div class="cg-note">Nobody has joined yet.</div>';
  for (const m of st.members) {
    html += `<div class="cg-row"><div><span class="cg-dot ${m.running ? "on" : ""}"></span>${esc(m.name)}
        <span class="cg-badge">${esc(CG_KINDS[m.kind] || m.kind)}</span>
        <div class="dim">${m.running ? "active" : "idle"}${m.ip ? " · " + esc(m.ip) : ""}${m.lastSeen ? " · seen " + cgTime(m.lastSeen) : ""}</div></div>
      <div class="cg-acts">
        ${m.running ? `<button class="cg-mini" data-act="stop-member" data-id="${esc(m.id)}">stop</button>` : ""}
        <button class="cg-mini" data-act="new-code" data-id="${esc(m.id)}" title="They lost their resume code">new code</button>
        <button class="cg-mini" data-act="collect" data-id="${esc(m.id)}">save zip</button>
        <button class="cg-mini danger" data-act="remove-member" data-id="${esc(m.id)}" data-name="${esc(m.name)}">remove</button></div></div>`;
  }
  if (st.members.length) html += '<div class="cg-row"><span></span><button class="cg-mini" data-act="collect" data-id="*">save everyone\'s work</button></div>';
  html += '<div class="cg-note">Saved zips go to ~/Documents/CodeGate Collected.</div></div>';

  // Network + runtime
  html += `<div class="cg-sec"><div class="cg-sec-title">SAFETY</div>
    <div class="cg-row"><div>Internet access for members
      <div class="dim">Members can never reach this Mac, your network, or each other.</div></div>
      <div class="cg-acts"><button class="cg-mini ${st.internet ? "" : "warn"}" data-act="internet" data-on="${st.internet ? 0 : 1}">${st.internet ? "on · turn off" : "off · turn on"}</button></div></div>
    <div class="cg-row"><div>Container runtime <div class="dim">${!rt.installed ? "not installed (brew install colima docker)" : rt.up ? "running" : rt.starting ? "starting…" : "stopped"}</div></div>
      <div class="cg-acts">${rt.installed && !rt.up && !rt.starting ? '<button class="cg-mini" data-act="runtime">start</button>' : ""}
      <button class="cg-mini danger" data-act="stop-all">stop everything</button></div></div></div>`;
  cgBody.innerHTML = html;
}

async function cgRefresh() {
  try {
    cgState = await api("/api/codegate/status");
  } catch (e) {
    cgState = { codegateRunning: false };
  }
  cgRenderSummary(cgState);
  if (!cgOverlay.classList.contains("hidden") && document.activeElement?.tagName !== "INPUT") cgRenderManager(cgState);
}

async function cgAct(action, payload) {
  if (cgBusy) return;
  cgBusy = true;
  try {
    const res = await post("/api/codegate/" + action, payload || {});
    cgNotice = res.error ? res.error
      : res.code ? `New resume code for ${res.name}: ${res.code} (copied). The old one no longer works.`
      : (res.exported ? `Saved ${res.exported} zip${res.exported === 1 ? "" : "s"} to ${res.folder}` : "");
    if (res.code) navigator.clipboard.writeText(res.code).catch(() => {});
    if (!res.error) cgState = res;
  } catch (e) {
    cgNotice = "Couldn't reach FileDrop.";
  } finally {
    cgBusy = false;
  }
  cgRenderSummary(cgState);
  cgRenderManager(cgState);
}

const CG_OPEN_WARNING = (kind) =>
  `⚠️ Open the ${CG_KINDS[kind]} room?\n\n` +
  "Anyone on the network who gets the PIN can join and run code in their own container on this Mac.\n\n" +
  "Each container is limited (CPU, memory, processes, disk), can't see your files, this Mac, your local network or other members, and is removed when it goes idle. " +
  "Their work is kept until you remove them.\n\n" +
  "Share the PIN only with people you intend to let in. It expires after 12 hours.";

document.querySelectorAll(".cg-room-btn").forEach((btn) => {
  btn.onclick = () => {
    const kind = btn.dataset.kind;
    const open = cgState && cgState.rooms && cgState.rooms[kind] && cgState.rooms[kind].open;
    const live = cgState && cgState.members && cgState.members.some((m) => m.kind === kind && m.running);
    if (open || live) return cgAct("stop_room", { kind });
    if (window.confirm(CG_OPEN_WARNING(kind))) cgAct("open", { kind });
  };
});

$("btnCgManage").onclick = () => {
  cgOverlay.classList.remove("hidden");
  cgRenderManager(cgState);
  cgRefresh();
};
$("btnCgClose").onclick = () => cgOverlay.classList.add("hidden");
$("btnCgCopy").onclick = () => {
  if (cgUrlText.value) navigator.clipboard.writeText(cgUrlText.value).catch(() => {});
};

cgBody.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const d = el.dataset;
  switch (d.act) {
    case "open":
      if (window.confirm(CG_OPEN_WARNING(d.kind))) cgAct("open", { kind: d.kind });
      break;
    case "close": cgAct("close", { kind: d.kind }); break;
    case "stop-room": cgAct("stop_room", { kind: d.kind }); break;
    case "copy-pin": navigator.clipboard.writeText(d.pin).catch(() => {}); el.textContent = "copied"; break;
    case "build": cgAct("build_image", { kind: d.kind }); break;
    case "internet": cgAct("set_internet", { on: d.on === "1" }); break;
    case "runtime": cgAct("start_runtime"); break;
    case "stop-all":
      if (window.confirm("Stop every workspace and close all rooms? Members keep their saved work.")) cgAct("stop_all");
      break;
    case "stop-member": cgAct("stop_member", { id: d.id }); break;
    case "new-code":
      if (window.confirm(`Generate a new resume code for ${d.name}? Their old code stops working. Give the new one only to them.`)) cgAct("reset_code", { id: d.id });
      break;
    case "remove-member":
      if (window.confirm(`Remove ${d.name} and permanently delete their workspace? Save their zip first if you need it.`)) cgAct("remove_member", { id: d.id });
      break;
    case "collect": cgAct("export", { id: d.id, starter: d.starter || "" }); break;
    case "del-starter":
      if (window.confirm(`Remove starter "${d.slug}"? Copies members already have are kept.`)) cgAct("delete_starter", { slug: d.slug });
      break;
    case "add-starter": {
      const picked = await post("/api/codegate/pick_folder");
      if (!picked.folder) return;
      const folder = picked.folder.replace(/\/$/, "");
      const base = folder.split("/").pop();
      const slug = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "starter";
      const title = window.prompt("Name for this starter:", base);
      if (title === null) return;
      cgAct("add_starter", { slug, title: title || base, folder });
      break;
    }
  }
});

cgBody.addEventListener("change", (e) => {
  const el = e.target.closest('[data-act="limit"]');
  if (el) cgAct("set_limit", { kind: el.dataset.kind, n: parseInt(el.value, 10) });
});

cgRefresh();
setInterval(() => { if (!document.hidden) cgRefresh(); }, 5000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

const clockText = $("clockText");
function tickClock() {
  clockText.textContent = new Date().toLocaleTimeString([], {
    hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
  });
}
tickClock();
setInterval(tickClock, 1000);

updateVolumeFillVisual();
updateFillVisual(seekSlider);
resetGlowColor();
poll();
setInterval(poll, 2000);
pollNotifications();
setInterval(pollNotifications, 4000);
