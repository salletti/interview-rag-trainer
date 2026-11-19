# HTTP and REST course

## Methods and safety

GET reads a resource and must not change server state, so it is safe and idempotent. PUT replaces a resource and DELETE removes it; both are idempotent because repeating them leaves the same final state. POST creates a resource or triggers an action and is neither safe nor idempotent by default.

## Status codes

The first digit gives the class of a response: 2xx means success, 3xx a redirection, 4xx an error caused by the client and 5xx an error on the server. Use 201 with a Location header after a creation, 204 when there is nothing to return, 400 or 422 for invalid input, 401 when authentication is missing and 403 when it is present but not allowed.

## Caching headers

The ETag header carries a validator for a representation. A client repeats it in If-None-Match, and the server answers 304 Not Modified with no body when nothing changed. Cache-Control sets how long a response may be reused, for example max-age=3600 for one hour.

## Pagination

Offset pagination, with limit and offset parameters, is simple but slow and unstable on large tables because inserted rows shift the pages. Cursor pagination returns an opaque token that points after the last item seen, which stays fast and consistent while the data changes.

```http
GET /articles?limit=20&cursor=eyJpZCI6MTAwfQ
```

## Versioning an API

Breaking changes need a new version so existing clients keep working. The version can live in the path, such as /v2/articles, or in a header. Additive changes, like a new optional field, do not require a new version if clients ignore unknown fields.
