"""ASGI middleware. Cross-cutting request policy that no route should have to
remember to opt into — the point being that a new endpoint is covered the day
it is written, without anyone adding a decorator."""
