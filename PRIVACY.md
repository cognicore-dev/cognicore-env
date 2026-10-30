# CogniCore Privacy Policy

**Effective date:** 21 October 2026

This policy explains what data the hosted CogniCore service collects, how it is used, and the choices you have. It covers the hosted MCP server at `https://mcp.cognicoree.tech`, which is what you use when you connect CogniCore to Claude, ChatGPT or another AI app.

It does **not** cover the open-source `cognicore-env` Python package when you run it yourself. In that case your data stays on your own machine and nothing is sent to us.

## In short

- You sign in with GitHub. We receive your **GitHub user ID and username** only, not your email, repositories or private data.
- We store **what you ask CogniCore to remember**, in a database that belongs to your account alone.
- We **do not sell** your data, show ads, use analytics trackers, or use your data to train AI models.
- You can delete individual memories at any time, and ask us to delete everything.

## What we collect

**1. Your GitHub identity.** When you connect, you sign in through GitHub. We request no GitHub permissions beyond your public profile, and we read two things: your numeric GitHub user ID and your username. Your account inside CogniCore is identified as `github:<your user ID>`. GitHub gives us a temporary access token during sign-in; we use it once to read your ID and username, and do not store it.

**2. What you store through CogniCore's tools.** This is the content the service exists to keep for you, for example:

- facts and notes you ask it to remember, and saved session summaries
- recorded experiences and outcomes of tasks
- design data synced from a Figma file you choose (styles, components, variables)
- voice preferences you give for ElevenLabs (voice ID and settings; we do not contact ElevenLabs)
- memories you choose to list on the CogniCore marketplace (see "Sharing" below)

**3. Long conversations, in one case.** Some tools accept the current conversation so they can keep it within the AI app's limits. If a conversation sent this way is longer than about 150,000 tokens, the older part is compressed into a summary and **saved to your memory**, so it can be recalled later. Shorter conversations are not stored.

**4. Technical data.** Our hosting provider records standard server logs: IP address, time, the address requested and the response status, plus error messages. We use one short-lived cookie (up to 10 minutes) during sign-in to protect it against forgery. We use no analytics, advertising or tracking cookies.

## How we use it

Only to run the service:

- to sign you in and keep your session active
- to store your memories and return them to you when your AI app asks
- to keep the service secure and to fix problems

We do not sell your data, use it for advertising, or use it to train AI models.

## Where it is stored, and who can see it

Your memories are kept in a separate database for your account, named with a one-way hash of your account ID. Other users cannot read it. The service runs on servers operated by our hosting provider (currently Microsoft Azure, in South Korea), so your data may be stored and processed outside your country.

## Sharing

We share data only in these cases:

- **Marketplace, when you choose.** If you use the tool that lists your memories for sale, other signed-in users can discover those listed memories and copy them into their own memory. Your account ID and marketplace reputation are visible to them, and the transfer is recorded. Nothing is listed unless you do it, and no real money is involved.
- **The AI app you use.** Results of the tools you call are returned to the app you connected (such as Claude or ChatGPT), and that app's own privacy policy applies to them.
- **Services that run CogniCore:**
  - GitHub, for sign-in
  - our hosting provider (currently Microsoft Azure), which stores the data and runs the server
  - Cloudflare, which provides the domain name service for `cognicoree.tech`
- **Figma, when you ask.** If you sync a Figma file, the Figma access token you provide is sent to Figma for that request only. We do not store it.
- **When required by law**, or to protect the service and its users from abuse.

## How long we keep it

- **Your memories:** until you delete them or ask us to delete your data.
- **Backups:** we copy the stored databases to our hosting provider's storage nightly, so the service can be restored after a failure, and each backup is deleted 30 days after it is made. A memory you delete is gone from the live service immediately, and leaves the backups within 30 days.
- **Sign-in tokens:** expire automatically; access tokens after 1 hour, refresh tokens after 30 days.
- **Server logs:** kept by our hosting provider for a limited period, then deleted.

If we shut the service down, we will delete the stored data.

## Your choices

- **Delete a memory:** ask your AI app to forget it (this uses the `cognicore_forget` tool).
- **Delete or export everything:** email us at the address below from, or mentioning, the GitHub account you signed in with. We will act on the request within 30 days.
- **Disconnect:** remove CogniCore in your AI app's connector settings, and revoke its access on GitHub under *Settings → Applications → Authorized OAuth Apps*. Disconnecting stops access but does not delete stored memories; ask us if you want them deleted.

Depending on where you live, you may have further rights over your data, such as access or correction. Contact us to use them.

## Security

Connections use HTTPS. Sign-in uses OAuth 2.1 with PKCE, and access is granted through short-lived signed tokens. Each account's memories are kept in its own database. The server's code is open source, so how it handles data can be checked: <https://github.com/cognicore-dev/cognicore-env>. No system is perfectly secure, but we work to protect your data and will tell affected users about any breach that puts their data at risk.

## Children

CogniCore is not intended for children under 13, and GitHub accounts require users to be at least 13.

## Changes

If we change this policy, we will update the effective date above. If a change significantly affects how your data is used, we will announce it in the project's repository before it takes effect.

## Contact

Email: [contact@cognicoree.tech](mailto:contact@cognicoree.tech)

For questions that are not private, you can also open an issue at <https://github.com/cognicore-dev/cognicore-env/issues>. Please do not post personal data in public issues.
