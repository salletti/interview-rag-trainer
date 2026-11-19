---
kind: qa_sheet
language: en
---

# System design interview notes

## Question: How does a cache-aside strategy work and what are its risks?

With cache-aside, the application reads the cache first, loads from the database on a miss, then stores the value in the cache. The risks are stale data after a write and a stampede when a popular key expires and many requests rebuild it at once. A time to live and a lock on the rebuild limit both problems.

## Question: What does idempotency mean for an API and why does it matter?

An operation is idempotent when repeating it has the same effect as doing it once. Clients retry after timeouts, so a payment endpoint should accept an idempotency key and return the stored result for a repeated key instead of charging the customer twice.

```http
POST /payments
Idempotency-Key: 7b1c2f9e-3a5d-4c60-9d1e-0a3b2c4d5e6f
```

## Question: When would you choose a message queue over a direct call?

A queue decouples producer and consumer in time and absorbs bursts: the producer returns immediately and the consumer processes work at its own pace, with retries when it fails. The price is eventual consistency and the need to make consumers tolerate duplicate deliveries.

## Question: What does the CAP theorem say?

When a network partition happens, a distributed system must choose between consistency, answering with the latest write or refusing to answer, and availability, always answering but possibly with stale data. Without partitions it can offer both, which is why the real trade-off is between latency and consistency.

## Question: How does a token bucket rate limiter work?

A bucket holds tokens that refill at a constant rate up to a maximum capacity. Each request consumes a token and is rejected or delayed when the bucket is empty. The capacity allows short bursts while the refill rate enforces the long-term average.

## Question: What is horizontal scaling and what does it require from the application?

Horizontal scaling adds more instances behind a load balancer instead of making one machine larger. It requires stateless instances: sessions and uploads must live in a shared store, so that any instance can serve any request and an instance can be replaced without losing user data.
