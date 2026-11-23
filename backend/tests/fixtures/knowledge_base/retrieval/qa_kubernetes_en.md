---
kind: qa_sheet
language: en
---

# Kubernetes interview notes

## Question: What is a pod and how is it different from a container?

A pod is the smallest deployable unit in Kubernetes. It wraps one or more containers that share a network namespace and storage volumes, so they are scheduled together on the same node and reach each other through localhost.

## Question: How does a Deployment roll out a new version of an application?

A Deployment creates a new ReplicaSet for the new image and gradually scales it up while scaling the old one down, keeping a minimum number of pods available. If the new pods never become ready, the rollout stalls and can be reverted with a rollback.

## Question: What are readiness and liveness probes for?

A liveness probe tells Kubernetes to restart a container that is stuck, while a readiness probe removes a pod from the service endpoints until it can accept traffic. Mixing them up causes restart loops during slow startups.

## Question: How do you keep configuration and secrets out of a container image?

Configuration goes into ConfigMaps and sensitive values into Secrets, both injected as environment variables or mounted files at runtime. The same image can then run in every environment with different settings and no credentials baked in.

## Question: What does a Service provide?

A Service gives a stable virtual address and DNS name to a changing set of pods selected by labels, and load balances requests between the ready ones. Clients keep using the same name while pods are created and destroyed.
