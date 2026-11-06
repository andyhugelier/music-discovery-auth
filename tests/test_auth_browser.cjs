// Exercise browser behavior without an Apple account or real credentials.
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const elements = {};
for (const id of ["status", "connect", "recent", "copy-token", "clear-clipboard", "results", "token"]) {
  elements[id] = { value: "", textContent: "", disabled: ["recent", "copy-token"].includes(id),
    addEventListener(event, handler) { this[event] = handler; } };
}
let clipboard;
let fail = false;
let failSetup = false;
let configureCalls = 0;
let authorizeCalls = 0;
function token(expires) {
  return "synthetic." + Buffer.from(JSON.stringify({ exp: expires })).toString("base64url") + ".signature";
}
const validDeveloper = token(Math.floor(Date.now() / 1000) + 900);
const music = {
  authorize: async () => {
    authorizeCalls++;
    if (fail) throw Object.assign(Error("synthetic-secret"), { code: "AUTHORIZATION_ERROR" });
    return "synthetic-user";
  },
  api: { music: async () => ({ data: { data: [{ attributes: { artistName: "Artist", name: "Song" } }] } }) }
};
vm.runInNewContext(fs.readFileSync("auth.js", "utf8"), {
  document: { getElementById: id => elements[id] },
  atob: value => Buffer.from(value, "base64").toString("binary"),
  navigator: { clipboard: { writeText: async value => { clipboard = value; } } },
  MusicKit: { configure: async () => {
    configureCalls++;
    if (failSetup) throw Error("synthetic-secret-setup");
  }, getInstance: () => music }
});
(async () => {
  elements.token.value = token(Math.floor(Date.now() / 1000) - 1);
  await elements.connect.click();
  assert.match(elements.status.textContent, /expired/);
  assert.equal(configureCalls, 0);
  elements.token.value = "not-a-jwt";
  await elements.connect.click();
  assert.match(elements.status.textContent, /format is invalid/);
  assert.equal(configureCalls, 0);
  elements.token.value = validDeveloper;
  await elements.connect.click();
  assert.equal(authorizeCalls, 0, "setup must not open sign-in after an async wait");
  assert.equal(elements["copy-token"].disabled, true);
  assert.match(elements.status.textContent, /MusicKit is ready/);
  await elements.connect.click();
  assert.equal(elements.token.value, "");
  assert.equal(elements["copy-token"].disabled, false);
  await elements["copy-token"].click();
  assert.equal(clipboard, "synthetic-user");
  await elements.recent.click();
  assert.equal(elements.results.textContent, "Artist — Song");
  await elements["clear-clipboard"].click();
  assert.equal(clipboard, "");
  fail = true;
  await elements.connect.click();
  assert.equal(elements["copy-token"].disabled, true);
  assert.equal(elements.recent.disabled, true);
  assert.equal(elements.token.value, "");
  assert.ok(!elements.status.textContent.includes("synthetic-secret"));
  assert.match(elements.status.textContent, /sign-in failed \(AUTHORIZATION_ERROR\)/);
  await elements["copy-token"].click();
  assert.equal(clipboard, "");
  failSetup = true;
  elements.token.value = validDeveloper;
  await elements.connect.click();
  assert.match(elements.status.textContent, /MusicKit setup failed/);
  assert.ok(!elements.status.textContent.includes("synthetic-secret"));
  console.log("Browser authorization tests passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
