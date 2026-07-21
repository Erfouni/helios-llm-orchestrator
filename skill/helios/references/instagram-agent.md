# Instagram Agent

Use `mcp mac` → `http_fetch` with base URL `http://127.0.0.1:3191`. The agent uses Meta's official Instagram API and stores credentials in macOS Keychain.

## Connection

- Health: `GET /health`
- OAuth status: `GET /oauth/status`
- Start OAuth: open `http://127.0.0.1:3191/oauth/start` on the Mac
- Public callback: `https://instagram.cumran.ir/oauth/callback`
- Public webhook: `https://instagram.cumran.ir/webhook`

The account must be Instagram Professional. Do not display account identifiers, tokens, App Secret, webhook token, or private profile fields.

## Profile and media

- Profile: `GET /profile`
- Recent media: `GET /media?limit=<1-100>`
- Continue pagination with the returned cursor: `GET /media?limit=<1-100>&after=<cursor>`

## Comments

- Read: `GET /comments?media_id=<URL-encoded media ID>`
- Reply: `POST /comments/reply`

```json
{"comment_id":"<id>","message":"<exact approved reply>","confirmed":true}
```

- Hide: `POST /comments/hide`
- Delete: `POST /comments/delete`

```json
{"comment_id":"<id>","confirmed":true}
```

Every write requires exact user approval and `confirmed:true`.

## Publishing

Publish: `POST /publish`

```json
{
  "media_type":"IMAGE",
  "media_url":"https://public.example/image.jpg",
  "caption":"<exact approved caption>",
  "confirmed":true
}
```

`media_type` can be `IMAGE`, `REELS`, or `STORIES`. The media must be available at a public HTTPS URL for Meta to fetch.

## Insights

- Account: `GET /insights/account?metric=reach,profile_views,total_interactions&period=day`
- Media: `GET /insights/media?media_id=<id>&metric=reach,views,likes,comments,saved,shares`

Report only metrics returned by the live API. Some metrics depend on account type, follower count, media type, retention window, and granted permissions.

## Token maintenance

- Refresh: `POST /refresh-token` with an empty JSON object.

Do not read or display the token. If OAuth is expired or absent, open the local OAuth start URL.

## Error handling

- `400`: fix input or obtain exact confirmation.
- `401`: reconnect OAuth.
- `403`: report the missing permission or API restriction exactly.
- Network failure: report that the Mac or Instagram Agent is unavailable.
