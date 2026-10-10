"use strict";
// HTTP pages may not expose the Clipboard API. Keep a selectable field visible
// when the browser also blocks legacy copying, so the code is still usable.
async function copyText(text, field) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* try the HTTP fallback */ }
  }
  const temporary = !field;
  const control = field || document.createElement("textarea");
  if (temporary) {
    control.value = text;
    control.setAttribute("readonly", "");
    control.className = "clipboard-fallback";
    document.body.append(control);
  }
  const previous = document.activeElement;
  control.focus(); control.select();
  control.setSelectionRange?.(0, text.length);
  let copied = false;
  try { copied = document.execCommand?.("copy") === true; } catch { /* keep the selection */ }
  if (temporary && copied) control.remove();
  if (copied) previous?.focus();
  return copied;
}
