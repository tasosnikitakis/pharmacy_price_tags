/*
 * Camera barcode scanning for the binding and audit pages (PLAN.md section 9).
 *
 * A "scan" button (templates/_scan.html) fills a text field from the camera and submits
 * its form, exactly as a USB scanner typing into the field and pressing Enter would.
 * The browser's own BarcodeDetector is used when it knows the formats; otherwise the
 * vendored ZXing bundle is loaded on first use (no CDN). Cameras need HTTPS: without a
 * secure context the button stays hidden and a hint says why. The field always works.
 *
 * No user-facing text lives here: messages come from data attributes rendered from
 * el.json (decision 0015).
 */
(() => {
  "use strict";

  const script = document.currentScript;
  const zxingSrc = script ? script.dataset.zxing : null;
  const SCAN_INTERVAL_MS = 150;
  let active = null;
  let zxingLoading = null;

  const canUseCamera = () =>
    window.isSecureContext && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

  function prepare(root) {
    const camera = canUseCamera();
    root.querySelectorAll("[data-scan]").forEach((button) => { button.hidden = !camera; });
    root.querySelectorAll("[data-scan-https]").forEach((hint) => { hint.hidden = camera; });
  }

  function loadZxing() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    if (!zxingSrc) return Promise.reject(new Error("no ZXing source"));
    if (!zxingLoading) {
      zxingLoading = new Promise((resolve, reject) => {
        const tag = document.createElement("script");
        tag.src = zxingSrc;
        tag.onload = () => resolve(window.ZXing);
        tag.onerror = () => { zxingLoading = null; reject(new Error("ZXing did not load")); };
        document.head.appendChild(tag);
      });
    }
    return zxingLoading;
  }

  function stop() {
    const state = active;
    if (!state) return;
    active = null;
    state.stopped = true;
    if (state.reader) { try { state.reader.reset(); } catch (e) { /* already stopped */ } }
    const stream = state.stream || state.video.srcObject;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    state.video.srcObject = null;
    state.view.hidden = true;
    state.button.hidden = false;
  }

  function fail(box, key) {
    const message = box.querySelector("[data-scan-error]");
    if (message) {
      message.textContent = message.dataset[key] || "";
      message.hidden = false;
    }
  }

  async function nativeDetector(formats) {
    if (!("BarcodeDetector" in window)) return null;
    try {
      const supported = await window.BarcodeDetector.getSupportedFormats();
      const usable = formats.filter((f) => supported.includes(f));
      return usable.length ? new window.BarcodeDetector({ formats: usable }) : null;
    } catch (e) {
      return null;
    }
  }

  async function start(button) {
    stop();
    const field = document.getElementById(button.dataset.scan);
    const box = button.closest("[data-scan-box]");
    if (!field || !box) return;
    const view = box.querySelector("[data-scan-view]");
    const video = view.querySelector("video");
    const error = box.querySelector("[data-scan-error]");
    if (error) error.hidden = true;
    const formats = (button.dataset.scanFormats || "ean_13,ean_8").split(",");
    const state = { button, view, video, stopped: false, stream: null, reader: null };
    active = state;
    button.hidden = true;
    view.hidden = false;

    const found = (value) => {
      if (state.stopped || !value) return;
      stop();
      field.value = String(value).trim();
      if (field.form) field.form.requestSubmit();
    };
    const constraints = { audio: false, video: { facingMode: { ideal: "environment" } } };

    try {
      const detector = await nativeDetector(formats);
      if (detector) {
        state.stream = await navigator.mediaDevices.getUserMedia(constraints);
        if (state.stopped) { state.stream.getTracks().forEach((t) => t.stop()); return; }
        video.srcObject = state.stream;
        await video.play();
        const tick = async () => {
          if (state.stopped) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length) { found(codes[0].rawValue); return; }
          } catch (e) { /* a frame that cannot be read yet */ }
          setTimeout(tick, SCAN_INTERVAL_MS);
        };
        tick();
      } else {
        const ZXing = await loadZxing();
        if (state.stopped) return;
        const hints = new Map();
        const wanted = formats.map((f) => ZXing.BarcodeFormat[f.toUpperCase()])
          .filter((f) => f !== undefined);
        hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, wanted);
        state.reader = new ZXing.BrowserMultiFormatReader(hints, SCAN_INTERVAL_MS);
        await state.reader.decodeFromConstraints(constraints, video, (result) => {
          if (result) found(result.getText());
        });
      }
    } catch (e) {
      const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
      stop();
      fail(box, denied ? "msgDenied" : "msgFailed");
    }
  }

  document.addEventListener("click", (event) => {
    const startButton = event.target.closest("[data-scan]");
    if (startButton) { event.preventDefault(); start(startButton); return; }
    if (event.target.closest("[data-scan-stop]")) { event.preventDefault(); stop(); }
  });
  // Never leave the camera on when the page changes (htmx-boosted navigation too).
  document.addEventListener("htmx:beforeRequest", stop);
  window.addEventListener("pagehide", stop);
  document.addEventListener("htmx:load", (event) => prepare(event.target));
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => prepare(document));
  } else {
    prepare(document);
  }
})();
