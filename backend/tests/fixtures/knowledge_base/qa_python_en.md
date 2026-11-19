# Python interview notes

These notes collect the questions that come back most often in Python backend interviews.

## Concurrency

### What is the GIL?

The Global Interpreter Lock lets only one thread run Python bytecode at a time. It protects the interpreter memory management but limits CPU-bound parallelism in threads.

### Q: When should you use asyncio instead of threads

Use asyncio for many concurrent I/O-bound operations, such as network calls, because coroutines are cheaper than threads and switch only at explicit await points.

```python
async def fetch_all(urls):
    return await asyncio.gather(*(fetch(u) for u in urls))
```

## Decorators

### Question: How does functools.wraps work?

It copies the name, docstring and other metadata of the wrapped function onto the wrapper, so introspection and debugging still show the original function.
