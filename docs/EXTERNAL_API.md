# Dromac External API

A small local HTTP API exposed by Dromac's Mac server so other applications
on this Mac can read what's currently playing on the phone -- title/artist,
play state, the derived album artwork, and synced lyrics -- without needing
to know anything about how Dromac talks to the phone itself.

- **Base URL:** `http://127.0.0.1:8811`
- **Binding:** loopback only (`127.0.0.1`) -- this API is not reachable from
  other machines on the network, only from processes running on this Mac.
- **Auth:** none. Being local-only *is* the access control.
- **Stability:** this is a separate, deliberately stable contract from the
  internal `/api/state` endpoint the dashboard itself polls. `/api/state`
  carries a lot of dashboard-specific fields (camera state, BLE signal,
  mirror/keyboard flags, ...) and can change shape as the dashboard evolves.
  `/api/external/*` won't change shape without a good reason -- safe to
  build against.

## `GET /api/external`

Self-describing manifest of the available endpoints and their response
shapes. Useful as a live reference without needing to re-read this file or
the source.

## `GET /api/external/now-playing`

Returns the phone's current playback, derived artwork, and synced lyrics as
one snapshot.

### Response

```json
{
  "connected": true,
  "title": "Don't Wanna Know (feat. Kendrick Lamar)",
  "artist": "Maroon 5",
  "playing": true,
  "positionMs": 30000,
  "durationMs": 214000,
  "artworkUrl": "http://127.0.0.1:8811/api/artwork?t=1790466000",
  "lyrics": {
    "available": true,
    "current": "Wasted",
    "prev": "I don't wanna know",
    "next": "And the more I drink, the more I think about you",
    "lines": [
      { "t": 0, "text": "..." },
      { "t": 4200, "text": "..." }
    ]
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `connected` | bool | Whether the phone is currently reachable. Every other field is `null`/empty if false. |
| `title`, `artist` | string \| null | |
| `playing` | bool | |
| `positionMs` | int | Playback position **at the moment of this request** -- not live. If you need a live-updating display, extrapolate forward yourself using `playing` and your own clock, the same way Dromac's own dashboard does, rather than polling every second. |
| `durationMs` | int \| null | |
| `artworkUrl` | string \| null | Always an **absolute** URL -- either a `/api/artwork` passthrough of art embedded in the phone's notification, or an iTunes-derived cover lookup. Safe to fetch directly. |
| `lyrics.available` | bool | False if no synced lyrics could be matched. |
| `lyrics.current` / `.prev` / `.next` | string \| null | The line at/around `positionMs`, for a quick single-line display. |
| `lyrics.lines` | array \| null | The full synced timeline (`{t: ms, text}[]`), same data the dashboard's own scrolling ticker uses. Use this if you want to build your own live ticker instead of polling for `current`/`prev`/`next` repeatedly. For "slowed + reverb" fan-edit tracks, timestamps are already stretched to match the actual playing track's duration -- no extra handling needed on the consumer side. |

### Example

```bash
curl -s http://127.0.0.1:8811/api/external/now-playing | jq .
```

## Adding more

This currently covers now-playing + artwork + lyrics because that's what
was asked for. Volume, battery, and playback control (play/pause/skip) all
already exist internally (see `/api/state` and the `/api/media/*`,
`/api/volume/*` routes in `server.py`) and could be exposed the same way
under `/api/external/*` if another app needs them later.
