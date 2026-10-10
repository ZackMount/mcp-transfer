"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync("src/mcp_transfer/web/clipboard.js", "utf8");

function setup(clipboard, legacy) {
  const field = { focused: false, selected: false, focus() { this.focused = true; }, select() { this.selected = true; }, setSelectionRange() {} };
  const context = vm.createContext({ navigator: { clipboard }, document: { activeElement: null, execCommand() { return legacy; } } });
  vm.runInContext(source, context);
  return { field, copy: text => context.copyText(text, field) };
}

test("HTTP without navigator.clipboard falls back to copying the selected field", async () => {
  const { copy, field } = setup(undefined, true);
  assert.equal(await copy("one-time-code"), true);
  assert.equal(field.selected, true);
});
test("permission rejection falls back instead of losing the enrollment code", async () => {
  const { copy } = setup({ writeText: async () => { throw new Error("denied"); } }, true);
  assert.equal(await copy("one-time-code"), true);
});
test("when all clipboard access is blocked the code stays selected for manual copying", async () => {
  const { copy, field } = setup(undefined, false);
  assert.equal(await copy("one-time-code"), false);
  assert.equal(field.focused, true);
  assert.equal(field.selected, true);
});
test("working secure clipboard receives the exact code", async () => {
  let value;
  const { copy, field } = setup({ writeText: async text => { value = text; } }, false);
  assert.equal(await copy("exact-code"), true);
  assert.equal(value, "exact-code");
  assert.equal(field.selected, false);
});
