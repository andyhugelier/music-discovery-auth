/* Credentials stay in MusicKit/browser memory and are copied only on request. */
(() => {
  const status = document.getElementById("status");
  const connect = document.getElementById("connect");
  const recent = document.getElementById("recent");
  const copyToken = document.getElementById("copy-token");
  const results = document.getElementById("results");
  const input = document.getElementById("token");
  let music;
  let userToken;
  let developerExpires;

  function tokenExpiration(token) {
    // Local expiry check only; Apple still verifies the signature and permissions.
    try {
      const parts = token.split(".");
      if (parts.length !== 3) return null;
      const payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
      const claims = JSON.parse(atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, "=")));
      return typeof claims.exp === "number" && Number.isFinite(claims.exp) ? claims.exp : null;
    } catch {
      return null;
    }
  }

  function safeFailure(error, phase) {
    // Allowlist diagnostics; arbitrary SDK messages/URLs may contain credentials.
    const code = error && error.code;
    const knownCodes = ["AUTHORIZATION_ERROR", "AUTHENTICATION_ERROR", "TOKEN_EXPIRED",
      "INVALID_TOKEN", "NOT_AUTHORIZED", "NETWORK_ERROR"];
    const suffix = knownCodes.includes(code) ? ` (${code})` : "";
    if (phase === "setup") {
      return `MusicKit setup failed${suffix}. Verify the Team ID and key, and try a freshly generated developer token.`;
    }
    return `Apple Music sign-in failed${suffix}. Allow popups for this page and retry. If no popup appears, tell us that.`;
  }

  connect.addEventListener("click", async () => {
    connect.disabled = true;
    recent.disabled = true;
    copyToken.disabled = true;
    userToken = undefined;
    results.textContent = "";
    let phase = "setup";
    try {
      const token = input.value.trim();
      input.value = "";
      if (!music && !token) {
        status.textContent = "Enter a developer token first.";
        return;
      }
      if (token) {
        music = undefined;
        developerExpires = tokenExpiration(token);
        if (developerExpires === null) {
          status.textContent = "Developer token format is invalid. Copy the bootstrap token again without extra text.";
          return;
        }
        if (developerExpires <= Date.now() / 1000) {
          status.textContent = "Developer token has expired according to this computer's clock. Generate and copy a new browser token from the NAS.";
          return;
        }
        if (typeof MusicKit === "undefined") {
          status.textContent = "Apple's MusicKit script did not load. Check connectivity or content blockers, then reload this page.";
          return;
        }
        status.textContent = "Preparing Apple Music...";
        await MusicKit.configure({
          developerToken: token,
          app: { name: "Music Discovery", build: "1.2" }
        });
        music = MusicKit.getInstance();
        status.textContent = "MusicKit is ready. Click Connect to Apple Music to open sign-in.";
        return;
      }
      if (developerExpires <= Date.now() / 1000) {
        music = undefined;
        status.textContent = "Developer token has expired. Generate and copy a new browser token from the NAS.";
        return;
      }
      // No async setup before authorize(): preserve the direct click for the popup.
      phase = "sign-in";
      status.textContent = "Requesting authorization...";
      const authorized = await music.authorize();
      userToken = typeof authorized === "string" ? authorized : music.musicUserToken;
      if (!userToken) {
        status.textContent = "Apple sign-in returned no user token. Reconnect before copying NAS authorization.";
        return;
      }
      recent.disabled = false;
      copyToken.disabled = false;
      status.textContent = "Connected. You can test recent plays or copy the NAS token.";
    } catch (error) {
      // SDK errors may contain request details; never put raw errors in the page.
      userToken = undefined;
      if (phase === "setup") music = undefined;
      status.textContent = safeFailure(error, phase);
    } finally {
      input.value = "";
      connect.textContent = music ? "Connect to Apple Music" : "Prepare Apple Music";
      connect.disabled = false;
    }
  });

  copyToken.addEventListener("click", async () => {
    try {
      if (!userToken) throw new Error("Reconnect required");
      await navigator.clipboard.writeText(userToken);
      status.textContent = "Token copied. Paste into the hidden NAS prompt, then clear your clipboard.";
    } catch {
      status.textContent = "Could not copy token. Use HTTPS or localhost, allow clipboard access, and reconnect if needed.";
    }
  });

  document.getElementById("clear-clipboard").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText("");
      status.textContent = "Clipboard cleared. Remove any retained clipboard-history copies too.";
    } catch {
      status.textContent = "Could not clear clipboard. Clear it manually before continuing.";
    }
  });

  recent.addEventListener("click", async () => {
    recent.disabled = true;
    results.textContent = "";
    status.textContent = "Fetching recently played tracks...";
    try {
      const response = await music.api.music("/v1/me/recent/played/tracks", { limit: 10 });
      const tracks = response.data.data || [];
      results.textContent = tracks.length ? tracks.map(track => {
        const a = track.attributes || {};
        return `${a.artistName || "Unknown artist"} — ${a.name || "Unknown track"}`;
      }).join("\n") : "No recent tracks returned.";
      status.textContent = "Recent plays request completed.";
    } catch {
      status.textContent = "Recent plays failed. Check connectivity and reconnect if access was revoked.";
    } finally {
      recent.disabled = false;
    }
  });
})();
