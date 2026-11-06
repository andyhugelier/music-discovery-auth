# Music Discovery authorization

Browser MusicKit authorization and a small Python CLI for proving persistent,
unattended Apple Music access on Synology. No collector or NAS deployment is
included in this milestone. The user verified token import and a separate NAS
access check on November 5, 2026; both returned 10 recent tracks. The user also
verified the saved authorization works after a container restart. Container
recreation and long-term unattended operation remain unverified.

## Credential model

- The MusicKit `.p8` key stays on the NAS, mounted read-only in `/run/secrets`.
- Set `APPLE_TEAM_ID`, `APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY_PATH` in the CLI's
  environment. IDs and paths are configuration; never put token values in
  environment variables, command arguments, shell history, source files or chat.
- Credentials live in `/app/config/private` (the existing persistent `Config`
  bind mount). The directory must be mode `700`; files must be `600` or `400`.
  The key and credential files must belong to the user running the CLI. Synology
  ACLs must also restrict access. Do not solve permission errors with `chmod 777`.
- Storage is plaintext with filesystem access restrictions, not application-level
  encryption. NAS administrators and a compromised runtime can access it. Protect
  backups with equivalent access controls or encryption.
- Developer JWTs are signed automatically with a one-hour lifetime for each
  access check. The short browser bootstrap token lasts 15 minutes. User tokens
  have no assumed lifetime or automatic refresh mechanism here: denied access
  requires investigation and, when necessary, interactive reauthorization.
- No public NAS endpoint is needed. The user explicitly copies the Music User
  Token into a hidden prompt over SSH. Clear clipboard/history copies afterward;
  disable clipboard sync for this operation where possible.

## Local verification (no NAS or real credentials)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
node tests/test_auth_browser.cjs
```

Python 3.12 is the NAS target. Local tests also support Python 3.9+. Tests use
synthetic keys and mocked HTTP/MusicKit; they do not establish live Apple access.

## Proposed NAS proof procedure

Review and approve staging before executing this procedure against the NAS.
Do not replace the existing Compose service or recreate its container yet.

1. Confirm SSH settings in DSM **Control Panel → Terminal & SNMP → Terminal**.
   Record the existing port. Connect from VS Code's terminal with
   `ssh -p PORT USER@NAS_HOST`. Enter passwords only into your own terminal.
   Verify a first-connection host fingerprint through a trusted source before
   accepting it. No router port forwarding is needed for local-network access.
2. After staging approval, transfer `music_auth.py` into `Config` under
   `/volume1/docker/Music-discovery`. It will appear at `/app/config/music_auth.py`
   in the existing container (the path used for the verified proof). Use a runtime user
   that owns the key and can write the private credential directory. Supply the
   three configuration variables above through Docker exec's environment
   options or the approved runtime configuration. The existing NAS already has
   matching PyJWT and cryptography versions; no installation was needed for the
   proof. On a new environment, install the pinned requirements only after
   reviewing that change to the test container. This remains a test
   environment; dependency installation will move into an image build later.
3. Run the following commands in that configured container environment. Prefix
   them with `docker exec -it ... music-discovery-test` when running from SSH;
   `...` represents the reviewed user/environment options, not literal syntax.

   ```sh
   python /app/config/music_auth.py browser-token
   python /app/config/music_auth.py import-user-token
   python /app/config/music_auth.py check
   ```

   `browser-token` writes a private bootstrap credential file named
   `/app/config/private/browser-developer-token`; it never prints its contents.
   Use your own SFTP client to retrieve that file privately and paste its contents
   into the browser developer-token field. Do not use the assistant to read it.
   Remove the local bootstrap copy when finished; it expires after 15 minutes.
4. Use the updated browser UI: paste the bootstrap token, click **Prepare Apple
   Music**, then click **Connect to Apple Music** to open sign-in directly from
   your click. The page checks token expiration locally and distinguishes setup
   failures from sign-in failures without displaying raw SDK errors.
   Authorize your account, then click **Copy NAS
   authorization token**. Paste into `import-user-token`'s hidden prompt and
   clear the clipboard. A new token is saved only after Apple accepts the recent
   tracks request; a failed import preserves any previously saved credential.
   An empty track list is a successful authorization result.
5. Close the browser and run `check` again. After a separately approved container
   restart, repeat it to establish persistence. Later verify survival of container
   recreation with the same `Config` mount. The standalone access and restart
   checks passed; the recreation check is still pending.

To test the updated UI before publishing, serve **only** `index.html` and
`auth.js` from a separate temporary directory on `127.0.0.1`, using
`python3 -m http.server 8000 --bind 127.0.0.1`. Open `http://localhost:8000`.
Do not serve the repository root or NAS credential directories. The current
GitHub Pages version will not have the copy button until publication is approved.
The bootstrap JWT has no origin restriction so it can support this local proof;
the eventual hosted bootstrap should use an origin-restricted token.

## Errors and recovery

- Developer authorization failure: check the team/key, key permissions and NAS
  clock. A new user token alone will not repair an invalid developer key.
- User access denied: reconnect/import and check the subscription or authorization
  status. A 401/403 is not definitive proof of token expiration.
- Rate limit, timeout or Apple outage: retry later. Existing credentials stay intact.
- `forget` removes only the saved NAS user token; it does not revoke the app's
  access at Apple. Revoke access through Apple's account controls when needed.
- Permission failure: inspect ownership and permissions without printing file
  contents. Run under the intended owner; don't loosen access to make it work.

Apple references: [developer tokens](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens),
[user authentication](https://developer.apple.com/documentation/applemusicapi/user-authentication-for-musickit),
[recent tracks](https://developer.apple.com/documentation/applemusicapi/get-v1-me-recent-played-tracks).

## Git checkpoint

The authorization milestone includes the migrated project context, browser
interface, NAS CLI, dependency pins and tests. Inspect staged changes before
committing; never use force-add for ignored credential paths. A push may publish
GitHub Pages, so treat it as a deployment and obtain authorization first.
