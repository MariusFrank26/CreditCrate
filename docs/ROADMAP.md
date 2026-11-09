# CreditCrate roadmap

## MCP server: use CreditCrate through Claude
Let users connect Claude to their CreditCrate account, so they can ask things like "find the producers on Iceman" or "send the October pack to my mixers".

- Expose the existing features as MCP tools: album search, credits, contacts, packs, campaigns, activity.
- Each user signs in with their own account. Tokens are per user and can be revoked.
- Sending stays behind an explicit confirmation step in the tool, so Claude can't mail anyone by accident.
- Keep the daily sending limit and the trial or Pro check in the same place as the web app.

## iOS app (on hold)
Not planned for now. Apple requires subscriptions sold in an app to go through its in-app purchase system, and the owner doesn't want that. Revisit if Pro can be sold outside the app, or if Apple's rules change.

## Desktop app (Windows and Mac)
A desktop version of CreditCrate, so producers can use it as an installed app.

- Start by wrapping the existing site in Tauri (or Electron), with Deal Desk as the first candidate because it already runs without the server.
- Windows first. Mac needs Apple Developer membership ($99 a year) and notarization, otherwise macOS blocks the app for most users.
- Needs auto-update and code signing before a public release. Unsigned Windows builds are fine for testers only.
- The login token needs secure storage in the app, and the backend must allow the app's origin (CORS).
- Pro payments: sold through Stripe on the website. Check Apple's rules before selling Pro inside a Mac app.
