# LinkedIn Agent

Use `mcp mac` → `http_fetch` with base URL `http://127.0.0.1:3190`. The agent is local-only and stores LinkedIn OAuth credentials in macOS Keychain.

## Connection and profile

- Health: `GET /health`
- OAuth status and granted scopes: `GET /oauth/status`
- Start or renew OAuth in the Mac browser: use `mcp mac` → `open_url` with `http://127.0.0.1:3190/oauth/start`
- Connected member summary: `GET /connection`
- Basic authenticated profile: `GET /profile`

Do not display the connected email or member identifier unless the user explicitly asks for it.

## Posts

- List the authenticated member's posts: `GET /posts?count=<1-100>`
- Publish a public text post: `POST /posts`

```json
{
  "commentary": "<final post text>",
  "confirmed": true
}
```

Use `confirmed: true` only after the user explicitly approves the exact post. Full post-history reads can require LinkedIn Community Management access.

## Comments

- Read comments: `GET /comments?post_urn=<URL-encoded post URN>`
- Publish a comment or reply: `POST /comments`

```json
{
  "post_urn": "<LinkedIn post URN>",
  "message": "<final comment text>",
  "confirmed": true
}
```

Comment reads or writes may require Community Management social-feed permissions.

## Creator analytics

- Post analytics: `GET /analytics/post?post_urn=<URL-encoded URN>&metric=<metric>`
  - Metrics: `IMPRESSION`, `MEMBERS_REACHED`, `REACTION`, `COMMENT`, `RESHARE`
  - Required permission: `r_member_postAnalytics`
- Profile analytics: `GET /analytics/profile?metric=<metric>`
  - Metrics: `PROFILE_VIEW`, `SEARCH_APPEARANCE`
  - Required permission: `r_member_profileAnalytics`

These permissions require approved LinkedIn Community Management access. A configured endpoint is not evidence that the permission has been granted; inspect `/oauth/status` and the live response.

## Advanced official API request

Use `POST /linkedin/request` only when no narrower endpoint covers the task.

```json
{
  "method": "GET",
  "path": "/rest/<official-endpoint>",
  "body": null,
  "confirmed": false
}
```

- Only `/v2/` and `/rest/` paths are accepted.
- Methods: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`.
- Any non-GET request requires `confirmed: true` and exact user authorization.
- Use only documented official LinkedIn endpoints and granted scopes.

## Response handling

- `200`/successful write: summarize the verified result and return the LinkedIn identifier when available.
- `400`: fix input validation; do not weaken the confirmation guard.
- `401`: OAuth is absent or expired; open `/oauth/start` on the Mac.
- `403`: report the restricted or missing scope exactly. Do not scrape, use cookies, or attempt a policy bypass.
- Network failure: report that the Mac or LinkedIn Agent is unavailable.
